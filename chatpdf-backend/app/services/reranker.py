# SiliconFlow rerank API client and response handling.
import logging
from typing import List, Dict, Any
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

class SiliconFlowReranker:
    def __init__(self):
        self.api_key = settings.SILICONFLOW_API_KEY.strip()
        self.base_url = settings.SILICONFLOW_BASE_URL.strip().rstrip("/")
        self.model = settings.RERANK_MODEL.strip()
        timeout = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=2.0)
        self.http_client = httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=0,
                max_connections=20,
            ),
            timeout=timeout,
        )
        logger.info("SiliconFlow Reranker 初始化成功，模型: %s", self.model)

    def rerank(self, query: str, candidates: List[Dict[str, Any]], top_n: int = 5) -> List[Dict[str, Any]]:
        docs = [c["text"] for c in candidates]

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置")
        response = self.http_client.post(
            f"{self.base_url}/rerank",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "query": query, "documents": docs,
                  "top_n": top_n, "return_documents": False},
        )
        response.raise_for_status()
        results = response.json()["results"]
        reranked = []
        for r in results:
            idx = r["index"]
            score = r["relevance_score"]
            if idx < len(candidates):
                c2 = candidates[idx].copy()
                c2["rerank_score"] = score
                reranked.append(c2)

        reranked.sort(key=lambda x: x["rerank_score"], reverse=True)
        return reranked[:top_n]


# Kept as an import alias for callers that still use the former class name.
CohereReranker = SiliconFlowReranker
