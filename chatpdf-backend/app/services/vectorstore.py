# app/services/vectorstore.py

import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")
try:
    import posthog
    posthog.disabled = True
    posthog.capture = lambda *args, **kwargs: None
except Exception:
    pass

from chromadb import PersistentClient
from chromadb.config import Settings
from app.core.config import settings
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

class ChromaClient:
    def __init__(self, persist_directory=None):
        # 优先使用传入路径，否则从配置文件读取
        persist_directory = persist_directory or settings.CHROMA_DB_DIR
        persist_directory = "./app/data/chroma"
        
        # ✅ 确保目录存在
        os.makedirs(persist_directory, exist_ok=True)
        
        self.client = PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )

        # 创建（或获取）全局集合
        self.collection = self.client.get_or_create_collection(name="chatpdf_collection")
        logger.info(f"✅ ChromaDB 初始化成功，文档数量: {self.collection.count()}")

    def add_documents(self, docs: List[Dict]):
        """
        docs: [{'id': str, 'text': str, 'embedding': [...], 'meta': {...}}, ...]
        """
        ids = [d['id'] for d in docs]
        embeddings = [d['embedding'] for d in docs]
        metadatas = [d.get('meta', {}) | {'text': d['text']} for d in docs]

        self.collection.add(
            documents=[d['text'] for d in docs],
            embeddings=embeddings,
            ids=ids,
            metadatas=metadatas
        )
        logger.info(f"✅ 成功添加 {len(docs)} 个文档到 ChromaDB")

    def query(
        self, 
        query_embedding: List[float], 
        top_k: int = 5,
        where: Optional[Dict] = None  # ✅ 添加 where 参数
    ):
        """
        查询向量数据库
        
        Args:
            query_embedding: 查询向量
            top_k: 返回结果数量
            where: 过滤条件，例如 {"file_id": "xxx"}
            
        Returns:
            [{"id": "xxx", "score": 0.95, "meta": {...}}, ...]
        """
        try:
            # ✅ 构建查询参数
            query_params = {
                "query_embeddings": [query_embedding],
                "n_results": top_k,
                "include": ["metadatas", "distances", "documents"]
            }
            
            # ✅ 如果有过滤条件，添加 where 参数
            if where:
                query_params["where"] = where
                logger.info(f"🔍 使用过滤条件: {where}")
            
            results = self.collection.query(**query_params)

            hits = []
            if results and len(results["ids"]) > 0:
                ids = results["ids"][0]
                distances = results["distances"][0]
                metas = results["metadatas"][0]
                docs = results.get("documents", [[]])[0]
                
                for i, (_id, dist, meta) in enumerate(zip(ids, distances, metas)):
                    hit = {
                        "id": _id, 
                        "score": 1 - dist,  # ✅ 转换为相似度分数
                        "meta": meta
                    }
                    # ✅ 确保 text 字段存在
                    if 'text' not in meta and i < len(docs):
                        hit['meta']['text'] = docs[i]
                    
                    hits.append(hit)
            
            logger.info(f"✅ 向量检索完成，返回 {len(hits)} 个结果")
            return hits
            
        except Exception as e:
            logger.error(f"❌ 向量查询失败: {e}", exc_info=True)
            return []
    
    def delete_by_file_id(self, file_id: str):
        """删除指定文件的所有文档"""
        try:
            self.collection.delete(where={"file_id": file_id})
            logger.info(f"✅ 已删除 file_id={file_id} 的所有文档")
        except Exception as e:
            logger.error(f"❌ 删除文档失败: {e}", exc_info=True)
            raise
    
    def count(self) -> int:
        """返回集合中的文档总数"""
        return self.collection.count()

    def first_chunks(self, file_id: Optional[str] = None, limit: int = 5) -> List[Dict]:
        """Return the earliest stored chunks for a document, in source order."""
        try:
            where = {"file_id": file_id} if file_id else None
            results = self.collection.get(
                where=where,
                include=["metadatas", "documents"],
            )
            ids = results.get("ids", [])
            metadatas = results.get("metadatas", [])
            documents = results.get("documents", [])
            chunks = []
            for index, (chunk_id, metadata) in enumerate(zip(ids, metadatas)):
                meta = metadata or {}
                chunks.append({
                    "id": chunk_id,
                    "text": documents[index] if index < len(documents) else meta.get("text", ""),
                    "meta": meta,
                })
            def source_order(chunk: Dict) -> tuple:
                meta = chunk["meta"]
                try:
                    page = float(meta.get("page", 0) or 0)
                except (TypeError, ValueError):
                    page = 0.0
                try:
                    start = float(meta.get("start", 0) or 0)
                except (TypeError, ValueError):
                    start = 0.0
                return page, start, str(chunk["id"])

            chunks.sort(key=source_order)
            return chunks[:limit]
        except Exception as e:
            logger.error("❌ 获取文档首批切片失败: %s", e, exc_info=True)
            return []
