import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Path as ApiPath

from app.core.config import settings
from app.services import file_registry
from app.services.es_retriever import es_delete_by_file_id
from app.services.vectorstore import ChromaClient

logger = logging.getLogger(__name__)

router = APIRouter()

_FALLBACK_STATUS = "unknown"
_VALID_STATUSES = {"processing", "indexed", "failed"}


def _status_path(file_id: str) -> Optional[Path]:
    """Resolve <file_id>.status.json only when it stays under the upload directory."""
    root = settings.upload_path.resolve()
    path = (root / f"{file_id}.status.json").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _read_status(file_id: str) -> Dict[str, Any]:
    """Read <file_id>.status.json defensively; missing/corrupt files fall back to unknown."""
    path = _status_path(file_id)
    if path is None or not path.exists():
        return {"file_id": file_id, "status": _FALLBACK_STATUS}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"file_id": file_id, "status": _FALLBACK_STATUS, "error": "status file unreadable"}
    if not isinstance(data, dict):
        return {"file_id": file_id, "status": _FALLBACK_STATUS}
    data.setdefault("file_id", file_id)
    if data.get("status") not in _VALID_STATUSES:
        data["status"] = _FALLBACK_STATUS
    return data


def _record_file_path(record: Dict[str, Any]) -> Optional[Path]:
    """Only allow deleting a PDF that is inside the configured upload directory."""
    raw_path = record.get("path")
    if not raw_path:
        return None
    root = settings.upload_path.resolve()
    candidate = Path(str(raw_path)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        logger.warning("拒绝删除上传目录之外的文件: %s", candidate)
        return None
    if candidate.suffix.lower() != ".pdf":
        return None
    return candidate


def _load_records() -> List[Dict[str, Any]]:
    try:
        return file_registry.list_records()
    except (OSError, ValueError, file_registry.FileRegistryError) as exc:
        logger.exception("读取文件元数据失败")
        raise HTTPException(status_code=500, detail="无法读取文件元数据") from exc


@router.get("/files")
def get_files():
    records = _load_records()
    enriched: List[Dict[str, Any]] = []
    for record in records:
        item = dict(record)
        status = _read_status(str(item.get("id", "")))
        item["status"] = status.get("status", _FALLBACK_STATUS)
        for key in ("stage", "error", "chunk_count", "updated_at"):
            if key in status:
                item[key] = status[key]
        enriched.append(item)
    return enriched


@router.get("/files/{file_id}/status")
def get_file_status(file_id: str = ApiPath(..., min_length=1, max_length=64)):
    try:
        record = file_registry.get_record(file_id)
    except (OSError, ValueError, file_registry.FileRegistryError) as exc:
        logger.exception("读取文件元数据失败")
        raise HTTPException(status_code=500, detail="无法读取文件元数据") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="file not found")
    return _read_status(file_id)


@router.delete("/files/{file_id}")
def delete_file(file_id: str = ApiPath(..., min_length=1, max_length=64)):
    """Delete a document's metadata, status, PDF and retrieval indexes."""
    try:
        record = file_registry.get_record(file_id)
    except (OSError, ValueError, file_registry.FileRegistryError) as exc:
        logger.exception("读取文件元数据失败")
        raise HTTPException(status_code=500, detail="无法读取文件元数据") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="file not found")

    # Stop a still-running indexing task from resurrecting the document.
    file_registry.mark_deleting(file_id)
    warnings: List[str] = []

    try:
        ChromaClient().delete_by_file_id(file_id)
    except Exception as exc:
        logger.exception("删除向量索引失败: %s", file_id)
        raise HTTPException(
            status_code=500,
            detail="删除向量索引失败，文件记录未删除，请重试",
        ) from exc

    if settings.USE_ES:
        try:
            if not es_delete_by_file_id(settings.ES_INDEX.strip(), file_id):
                warnings.append("ES 索引删除失败")
        except Exception:
            logger.exception("删除 ES 索引异常: %s", file_id)
            warnings.append("ES 索引删除失败")

    status_path = _status_path(file_id)
    if status_path is None:
        warnings.append("状态文件路径无效，未删除")
    else:
        try:
            status_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("删除状态文件失败: %s", status_path)
            warnings.append("状态文件删除失败")

    pdf_path = _record_file_path(record)
    if pdf_path is None:
        warnings.append("记录中的文件路径不在上传目录或不是 PDF，未删除源文件")
    else:
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.exception("删除 PDF 失败: %s", pdf_path)
            # Keep the record so the operator can retry the delete.
            raise HTTPException(
                status_code=500,
                detail="删除 PDF 文件失败，文件记录未删除，请重试",
            ) from exc

    try:
        file_registry.remove_record(file_id)
    except (OSError, ValueError, file_registry.FileRegistryError) as exc:
        logger.exception("删除文件元数据失败: %s", file_id)
        raise HTTPException(
            status_code=500,
            detail="删除文件元数据失败，请重试",
        ) from exc

    return {"file_id": file_id, "deleted": True, "warnings": warnings}
