<#
Rebuild chatpdf-backend/.venv at its final path.

Why the environment is rebuilt at ".venv" instead of a temporary ".venv.new":
Windows console launchers (uvicorn.exe, pip.exe, ...) embed the absolute
environment path when they are installed. Creating the environment at
".venv.new" and renaming it afterwards would leave those launchers pointing at
".venv.new" - the same portability defect this script is meant to remove.

Safety rules:
  * no file or directory is deleted;
  * the current environment is renamed to ".venv.bak" and kept;
  * on any failure the partial environment is renamed to ".venv.failed-<time>"
    and the previous environment is renamed back to ".venv".

Usage:
  pwsh -NoProfile -File .\scripts\rebuild-venv.ps1
  pwsh -NoProfile -File .\scripts\rebuild-venv.ps1 -ConstraintsPath C:\temp\constraints.txt
#>
[CmdletBinding()]
param(
    [string]$BackendDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe,
    [string]$ConstraintsPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Stop-WithError {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

$venv = Join-Path $BackendDir '.venv'
$backup = Join-Path $BackendDir '.venv.bak'
$requirements = Join-Path $BackendDir 'requirements.txt'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $env:TEMP "chatpdf-venv-rebuild-$timestamp.log"

Write-Host "Backend:      $BackendDir"
Write-Host "Environment:  $venv"
Write-Host "Backup:       $backup"
Write-Host "Install log:  $log"

if (-not (Test-Path -LiteralPath $requirements)) {
    Stop-WithError "requirements.txt not found: $requirements"
}
if (-not (Test-Path -LiteralPath $venv)) {
    Stop-WithError "No existing .venv at $venv. Create one with: python -m venv .venv"
}
if (Test-Path -LiteralPath $backup) {
    Stop-WithError "Backup already exists: $backup. Inspect it and move it away manually before retrying."
}

# Refuse to touch an environment that a running process may still be using.
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uvicorn.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$venv\*" }
if ($running) {
    $ids = ($running | ForEach-Object { $_.ProcessId }) -join ', '
    Stop-WithError "Processes still use $venv (PID: $ids). Stop the backend first."
}

# Resolve the base interpreter from the recorded pyvenv.cfg, never from the
# broken launchers inside the environment.
if (-not $PythonExe) {
    $cfg = Join-Path $venv 'pyvenv.cfg'
    if (Test-Path -LiteralPath $cfg) {
        $homeLine = Select-String -LiteralPath $cfg -Pattern '^home\s*=\s*(.+)$' | Select-Object -First 1
        if ($homeLine) {
            $pythonHome = $homeLine.Matches[0].Groups[1].Value.Trim()
            $candidate = Join-Path $pythonHome 'python.exe'
            if (Test-Path -LiteralPath $candidate) {
                $PythonExe = $candidate
            }
        }
    }
}
if (-not $PythonExe -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $candidate = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path -LiteralPath $candidate)) {
        $PythonExe = $candidate
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) {
    Stop-WithError "Python 3.11 interpreter not found. Pass it explicitly with -PythonExe."
}

$pyVersion = (& $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $pyVersion -ne '3.11') {
    Stop-WithError "Expected Python 3.11, got '$pyVersion' from $PythonExe"
}
Write-Host "Interpreter:  $PythonExe (Python $pyVersion)"

# 1. Keep the current environment. No deletion.
Rename-Item -LiteralPath $venv -NewName (Split-Path -Leaf $backup)
Write-Host "MOVED   $venv -> $backup" -ForegroundColor Yellow

try {
    # 2. Create the new environment at the final path so every generated
    #    launcher embeds the final absolute path.
    & $PythonExe -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "python -m venv failed with exit code $LASTEXITCODE"
    }
    $newPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $newPython)) {
        throw "New environment has no python.exe: $newPython"
    }

    # 3. Install the declared requirements.
    $installArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements)
    if ($ConstraintsPath) {
        if (-not (Test-Path -LiteralPath $ConstraintsPath)) {
            throw "Constraints file not found: $ConstraintsPath"
        }
        $installArgs += @('-c', (Resolve-Path -LiteralPath $ConstraintsPath).Path)
    }
    Push-Location $BackendDir
    try {
        & $newPython @installArgs *>&1 | Tee-Object -FilePath $log
        if ($LASTEXITCODE -ne 0) {
            throw "pip install failed with exit code $LASTEXITCODE (log: $log)"
        }
    }
    finally {
        Pop-Location
    }

    # 4. Verify dependency consistency and the modules the backend imports.
    & $newPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "pip check reported inconsistent dependencies (log: $log)"
    }

    $imports = 'fastapi, uvicorn, chromadb, llama_index, fitz, pdfplumber, pydantic_settings, elasticsearch, sse_starlette, aiofiles, httpx'
    & $newPython -c "import $imports; print('core imports OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Core import smoke test failed"
    }

    Push-Location $BackendDir
    try {
        $paths = & $newPython -c "from app.core.config import settings; print('chroma_path=' + str(settings.chroma_path)); print('upload_path=' + str(settings.upload_path))"
        if ($LASTEXITCODE -ne 0) {
            throw "Configuration smoke test failed"
        }
    }
    finally {
        Pop-Location
    }
    $paths | ForEach-Object { Write-Host "CHECK   $_" }

    # 5. The launcher must work and therefore point at the final path.
    & (Join-Path $venv 'Scripts\uvicorn.exe') --version
    if ($LASTEXITCODE -ne 0) {
        throw "uvicorn.exe launcher is still broken"
    }

    Write-Host ""
    Write-Host "SUCCESS: $venv rebuilt and verified." -ForegroundColor Green
    Write-Host "The previous environment is kept at $backup (not deleted)."
    Write-Host "Run the backend, then remove $backup manually after you confirm everything works."
    exit 0
}
catch {
    $reason = $_.Exception.Message
    Write-Host ""
    Write-Host "FAILED: $reason" -ForegroundColor Red

    if (Test-Path -LiteralPath $venv) {
        $failed = Join-Path $BackendDir ".venv.failed-$timestamp"
        try {
            Rename-Item -LiteralPath $venv -NewName (Split-Path -Leaf $failed)
            Write-Host "Partial environment kept at $failed for inspection."
        }
        catch {
            Write-Host "WARNING: could not move the partial environment: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    if (Test-Path -LiteralPath $backup) {
        try {
            Rename-Item -LiteralPath $backup -NewName '.venv'
            Write-Host "Previous environment restored at $venv."
        }
        catch {
            Write-Host "WARNING: could not restore the backup automatically." -ForegroundColor Yellow
            Write-Host "Restore it manually by renaming $backup back to $venv."
        }
    }

    Write-Host "Install log: $log"
    exit 1
}
