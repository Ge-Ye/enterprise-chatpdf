# app/routes/upload.py
import os
import uuid
import json
import logging
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, HTTPException
from app.core.config import settings
from app.services.pdf_parser import extract_text_by_page
from app.services.chunker import chunk_text
from app.services.embeddings import EmbeddingClient
from app.services.vectorstore import ChromaClient
from app.models import UploadResponse
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)
router = APIRouter()
es = Elasticsearch(settings.ES_HOST)

UPLOAD_DIR = settings.UPLOAD_DIR
META_FILE = os.path.join(UPLOAD_DIR, "files.json")

def persist_file(file: UploadFile, dest_path: str):
    with open(dest_path, "wb") as f:
        f.write(file.file.read())

def load_files():
    if os.path.exists(META_FILE):
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_files(data):
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def index_to_elasticsearch(docs: list, index: str):
    for d in docs:
        doc_body = {
            "text": d["text"],
            "file_id": d.get("meta", {}).get("file_id"),
            "page": d.get("meta", {}).get("page"),
            "meta": d.get("meta", {}),
        }
        es.index(index=index, id=d["id"], document=doc_body)
    return True

def preprocess_and_index(file_id: str, filepath: str):
    """后台任务：解析、分块、向量化、索引"""
    try:
        logger.info(f"📄 开始处理文件: {file_id}")
        pages = extract_text_by_page(filepath)
        all_chunks = []
        for pidx, page_text in enumerate(pages):
            chunks = chunk_text(page_text, chunk_size=1200, overlap=200)
            for c in chunks:
                c["meta"].update({"page": pidx, "file_id": file_id})
            all_chunks.extend(chunks)

        emb_client = EmbeddingClient()
        texts = [c["text"] for c in all_chunks]
        embeddings = emb_client.embed_texts(texts)
        for c, e in zip(all_chunks, embeddings):
            c["embedding"] = e

        chroma = ChromaClient()
        chroma.add_documents(all_chunks)
        index_to_elasticsearch(all_chunks, settings.ES_INDEX)

        # ✅ 保存状态
        status_path = os.path.join(UPLOAD_DIR, f"{file_id}.status.json")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"file_id": file_id, "status": "indexed"}, f, ensure_ascii=False)

        logger.info(f"🎉 文件处理完成: {file_id}")

    except Exception as e:
        logger.error(f"❌ 文件处理失败: {e}", exc_info=True)
        status_path = os.path.join(UPLOAD_DIR, f"{file_id}.status.json")
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump({"file_id": file_id, "status": "failed", "error": str(e)}, f, ensure_ascii=False)

@router.post("/upload", response_model=UploadResponse)
async def upload_pdf(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """上传 PDF 文件"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    file_id = str(uuid.uuid4())
    dest = os.path.join(UPLOAD_DIR, f"{file_id}_{file.filename}")

    try:
        persist_file(file, dest)
        logger.info(f"✅ 文件保存成功: {dest}")
    except Exception as e:
        logger.error(f"❌ 文件保存失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # ✅ 记录文件信息
    files = load_files()
    files.append({
        "id": file_id,
        "name": file.filename,
        "alias": os.path.splitext(file.filename)[0],
        "path": dest,
    })
    save_files(files)

    if background_tasks is not None:
        background_tasks.add_task(preprocess_and_index, file_id, dest)

    return {"file_id": file_id, "filename": file.filename, "status": "processing"}
