# ✅ 兼容旧版 Cohere SDK（无 responses 模块）
import logging
from typing import List, Dict, Any
import cohere
from app.core.config import settings

logger = logging.getLogger(__name__)

class CohereReranker:
    def __init__(self):
        if not settings.COHERE_API_KEY:
            raise ValueError("❌ COHERE_API_KEY 未配置")
        self.client = cohere.Client(settings.COHERE_API_KEY)
        self.model = settings.COHERE_RERANK_MODEL or "rerank-multilingual-v3.0"
        logger.info(f"✅ CohereReranker 初始化成功，使用模型: {self.model}")

    def rerank(self, query: str, candidates: List[Dict[str, Any]], top_n: int = 5) -> List[Dict[str, Any]]:
        docs = [c["text"] for c in candidates]

        try:
            response = self.client.rerank(
                model=self.model,
                query=query,
                documents=docs,
                top_n=top_n
            )
        except Exception as e:
            logger.error(f"❌ Cohere rerank 请求失败: {e}")
            return candidates[:top_n]

        # 旧版 SDK 直接返回 results 列表
        results = response.results
        reranked = []
        for r in results:
            idx = r.index
            score = getattr(r, "relevance_score", 0)
            if idx < len(candidates):
                c2 = candidates[idx].copy()
                c2["rerank_score"] = score
                reranked.append(c2)

        reranked.sort(key=lambda x: x["rerank_score"], reverse=True)
        return reranked[:top_n]
