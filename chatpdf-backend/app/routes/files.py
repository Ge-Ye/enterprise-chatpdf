import json
from pathlib import Path

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()
META_FILE = settings.upload_path / "files.json"


@router.get("/files")
def get_files():
    if not META_FILE.exists():
        return []
    with META_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)
