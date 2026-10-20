# Cohere SDK 5.x compatible client and response handling.
import logging
from typing import List, Dict, Any
import cohere
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

class CohereReranker:
    def __init__(self):
        if not settings.COHERE_API_KEY:
            raise ValueError("❌ COHERE_API_KEY 未配置")
        # Cohere SDK 5.x accepts timeout on Client, but max_retries belongs to
        # the per-request options passed to rerank().
        timeout = httpx.Timeout(connect=3.0, read=3.5, write=5.0, pool=3.0)
        self.http_client = httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=0,
                max_connections=20,
            ),
            timeout=timeout,
        )
        self.client = cohere.Client(
            settings.COHERE_API_KEY,
            timeout=timeout,
            httpx_client=self.http_client,
        )
        self.model = settings.COHERE_RERANK_MODEL or "rerank-multilingual-v3.0"
        logger.info(f"✅ CohereReranker 初始化成功，使用模型: {self.model}")

    def rerank(self, query: str, candidates: List[Dict[str, Any]], top_n: int = 5) -> List[Dict[str, Any]]:
        docs = [c["text"] for c in candidates]

        response = self.client.rerank(
            model=self.model,
            query=query,
            documents=docs,
            top_n=top_n,
            request_options={"max_retries": 0},
        )

        # SDK 5.19 returns RerankResponse.results items with index and
        # relevance_score attributes.
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
