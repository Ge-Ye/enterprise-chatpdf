# app/routes/upload.py
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException

from app.core.config import settings
from app.models import UploadResponse
from app.services.pdf_parser import extract_document_text
from app.services.chunker import chunk_text
from app.services.embeddings import EmbeddingClient
from app.services.vectorstore import ChromaClient
from app.services.es_retriever import es_delete_by_file_id
from app.services import file_registry

logger = logging.getLogger(__name__)
router = APIRouter()

UPLOAD_DIR = str(settings.upload_path)
# Status files are atomic too; files.json itself is owned by file_registry.
# (kept as plain module state so tests can point it at a temporary directory)

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_PDF_EXTENSION = ".pdf"
_PDF_MAGIC_WINDOW_BYTES = 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: str, payload) -> None:
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _status_path(file_id: str) -> str:
    return os.path.join(UPLOAD_DIR, f"{file_id}.status.json")


def write_index_status(file_id: str, status: str, **extra) -> None:
    """Persist the index status read by GET /api/files and /api/files/{id}/status."""
    # A delete may run while background indexing is still in flight; never let
    # that task recreate a status file for a document that no longer exists.
    if file_registry.is_deleting(file_id):
        return
    payload = {"file_id": file_id, "status": status, "updated_at": _now()}
    payload.update(extra)
    try:
        _atomic_write_json(_status_path(file_id), payload)
    except OSError:
        logger.exception("Failed to persist index status for %s", file_id)
        return
    if file_registry.is_deleting(file_id):
        try:
            os.remove(_status_path(file_id))
        except OSError:
            pass


def sanitize_upload_filename(filename: str) -> str:
    """Return a single safe basename, never a client-supplied path."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL_CHARS.sub("_", name).strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少文件名")
    if len(name) > settings.MAX_UPLOAD_FILENAME_LENGTH:
        raise HTTPException(status_code=400, detail="文件名过长")
    if not name.lower().endswith(_PDF_EXTENSION):
        raise HTTPException(status_code=415, detail="仅支持上传 PDF 文件")
    return name


def validate_upload_content_type(content_type: str) -> None:
    """Check the declared media type before reading the body."""
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in settings.allowed_upload_mime_types:
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {media_type or 'unknown'}",
        )


async def persist_file(file: UploadFile, dest_path: str) -> int:
    """Stream the upload to disk with a hard byte cap and a PDF signature check.

    The response is only written to its final path after the whole file passed
    validation, so an interrupted or rejected upload never becomes a document.
    """
    max_bytes = settings.max_upload_bytes
    temp_path = f"{dest_path}.{uuid.uuid4().hex}.part"
    total = 0
    try:
        with open(temp_path, "wb") as f:
            first_chunk = True
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                if first_chunk:
                    if b"%PDF-" not in chunk[:_PDF_MAGIC_WINDOW_BYTES]:
                        raise HTTPException(
                            status_code=415,
                            detail="文件内容不是有效的 PDF",
                        )
                    first_chunk = False
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件大小超过 {settings.MAX_UPLOAD_MB} MB 限制",
                    )
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        os.replace(temp_path, dest_path)
        return total
    except Exception:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def index_to_elasticsearch(docs: list, index: str):
    from elasticsearch import Elasticsearch
    es = Elasticsearch(settings.ES_HOST.strip())
    for d in docs:
        doc_body = {
            "text": d["text"],
            "file_id": d.get("meta", {}).get("file_id"),
            "page": d.get("meta", {}).get("page"),
            "meta": d.get("meta", {}),
        }
        es.index(index=index, id=d["id"], document=doc_body)
    return True


def discard_vectors(file_id: str, chroma=None) -> None:
    """Best-effort cleanup for a document deleted while indexing was in flight."""
    try:
        (chroma if chroma is not None else ChromaClient()).delete_by_file_id(file_id)
    except Exception:
        logger.exception("删除期间清理向量失败: %s", file_id)
    if settings.USE_ES:
        try:
            if not es_delete_by_file_id(settings.ES_INDEX.strip(), file_id):
                logger.warning("删除期间清理 ES 索引失败: %s", file_id)
        except Exception:
            logger.exception("删除期间清理 ES 索引异常: %s", file_id)


def preprocess_and_index(file_id: str, filepath: str):
    """后台任务：解析、分块、向量化、索引"""
    if file_registry.is_deleting(file_id):
        logger.info("文件已在索引开始前删除，跳过: %s", file_id)
        return
    write_index_status(file_id, "processing", stage="parsing")
    try:
        logger.info(f"📄 开始处理文件: {file_id}")
        document_text, page_ranges = extract_document_text(filepath)
        all_chunks = chunk_text(
            document_text,
            chunk_size=900,
            overlap=180,
            page_ranges=page_ranges,
            doc_id=file_id,
        )
        for chunk in all_chunks:
            chunk["meta"].update({"file_id": file_id, "doc_id": file_id})

        if file_registry.is_deleting(file_id):
            logger.info("文件在解析后被删除，跳过后续索引: %s", file_id)
            return

        write_index_status(file_id, "processing", stage="embedding")
        emb_client = EmbeddingClient()
        texts = [c["text"] for c in all_chunks]
        embeddings = emb_client.embed_texts(texts)
        for c, e in zip(all_chunks, embeddings):
            c["embedding"] = e

        if file_registry.is_deleting(file_id):
            logger.info("文件在向量化后被删除，跳过后续索引: %s", file_id)
            return

        write_index_status(file_id, "processing", stage="indexing")
        chroma = ChromaClient()
        chroma.add_documents(all_chunks)

        # The delete request may have run while add_documents was in flight;
        # remove anything this task managed to write after that point.
        if file_registry.is_deleting(file_id):
            logger.info("索引写入期间文件被删除，清理刚写入的数据: %s", file_id)
            discard_vectors(file_id, chroma)
            return

        if settings.USE_ES:
            index_to_elasticsearch(all_chunks, settings.ES_INDEX.strip())

        if file_registry.is_deleting(file_id):
            logger.info("ES 写入期间文件被删除，清理刚写入的数据: %s", file_id)
            discard_vectors(file_id, chroma)
            return

        write_index_status(file_id, "indexed", chunk_count=len(all_chunks))
        logger.info(f"🎉 文件处理完成: {file_id}")

    except Exception as e:
        if file_registry.is_deleting(file_id):
            logger.info("文件在索引期间被删除，忽略失败并清理: %s", file_id)
            discard_vectors(file_id)
            return
        logger.error(f"❌ 文件处理失败: {e}", exc_info=True)
        write_index_status(file_id, "failed", error=str(e))


@router.post("/upload", response_model=UploadResponse)
async def upload_pdf(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """上传 PDF 文件（校验扩展名、MIME、PDF 签名与大小）"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    filename = sanitize_upload_filename(file.filename)
    validate_upload_content_type(file.content_type)
    declared_size = getattr(file, "size", None)
    if declared_size is not None and declared_size > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过 {settings.MAX_UPLOAD_MB} MB 限制",
        )

    file_id = str(uuid.uuid4())
    dest = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")

    try:
        size = await persist_file(file, dest)
        logger.info(f"✅ 文件保存成功: {dest} ({size} bytes)")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 文件保存失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="文件保存失败")

    # ✅ 记录文件信息
    try:
        file_registry.append_record({
            "id": file_id,
            "name": filename,
            "alias": os.path.splitext(filename)[0],
            "path": dest,
            "size_bytes": size,
        })
    except (OSError, ValueError, file_registry.FileRegistryError) as e:
        logger.error(f"❌ 文件元数据写入失败: {e}")
        try:
            os.remove(dest)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Failed to persist file metadata")

    # ✅ 立即写入 processing，前端轮询状态时不会遇到 404/空状态
    write_index_status(file_id, "processing", stage="queued")

    if background_tasks is not None:
        background_tasks.add_task(preprocess_and_index, file_id, dest)

    return {"file_id": file_id, "filename": filename, "status": "processing"}
