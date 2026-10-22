# app/services/embeddings.py

import httpcore
import httpx
import time
from typing import List
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class EmbeddingClient:
    """统一的 Embedding 服务客户端"""
    
    def __init__(self):
        self.provider = "siliconflow"
        self._api_key = settings.SILICONFLOW_API_KEY.strip()
        self.base_url = settings.SILICONFLOW_BASE_URL.strip().rstrip("/")
        self.model = settings.EMBEDDING_MODEL.strip()
        self._http_client = self._create_http_client()
        logger.info("SiliconFlow Embedding 初始化成功，模型: %s", self.model)
    
    @staticmethod
    def _create_http_client() -> httpx.Client:
        return httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=60.0,
            ),
            timeout=httpx.Timeout(connect=15.0, read=15.0, write=15.0, pool=15.0),
        )

    @staticmethod
    def _is_transient_connection_error(exc: Exception) -> bool:
        transient_types = (
            httpcore.RemoteProtocolError,
            httpx.RemoteProtocolError,
            httpx.TransportError,
            ConnectionError,
            TimeoutError,
        )
        current = exc
        while current is not None:
            if isinstance(current, transient_types):
                return True
            current = current.__cause__ or current.__context__
        message = str(exc).casefold()
        return any(marker in message for marker in (
            "server disconnected without sending a response",
            "remoteprotocolerror",
            "connection reset",
            "connection aborted",
            "connection refused",
            "timed out",
        ))

    def _embed(self, texts: List[str], input_type: str) -> List[List[float]]:
        if not texts:
            return []

        for attempt in range(3):
            try:
                if not self._api_key:
                    raise ValueError("SILICONFLOW_API_KEY 未配置")
                response = self._http_client.post(
                    f"{self.base_url}/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                return [item["embedding"] for item in response.json()["data"]]
            except Exception as exc:
                if not self._is_transient_connection_error(exc) or attempt >= 2:
                    logger.error("❌ Embedding 生成失败: %s", exc)
                    raise
                logger.warning(
                    "Embedding 连接异常，第 %d/2 次重试: %s",
                    attempt + 1,
                    exc,
                )
                time.sleep(0.2)

        raise RuntimeError("Embedding 重试意外结束")

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """将文本列表转换为向量，瞬态连接错误最多重试一次。"""
        return self._embed(texts, "search_document")

    def embed_query(self, query: str) -> List[float]:
        """将查询文本转换为向量，瞬态连接错误最多重试一次。"""
        return self._embed([query], "search_query")[0]


# 全局单例
embedding_client = EmbeddingClient()
