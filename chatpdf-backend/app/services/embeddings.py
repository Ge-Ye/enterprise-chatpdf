# app/services/embeddings.py

import cohere
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
        self.provider = settings.EMBEDDING_PROVIDER
        
        if self.provider == "cohere":
            if not settings.COHERE_API_KEY:
                raise ValueError("❌ COHERE_API_KEY 未配置")
            self._api_key = settings.COHERE_API_KEY
            self.model = settings.COHERE_EMBEDDING_MODEL
            self._http_client = self._create_http_client()
            self.client = cohere.Client(
                self._api_key,
                timeout=self._http_client.timeout,
                httpx_client=self._http_client,
            )
            logger.info(f"✅ Cohere Embedding 初始化成功，模型: {self.model}")
        else:
            raise NotImplementedError(f"不支持的 Embedding 提供商: {self.provider}")
    
    @staticmethod
    def _create_http_client() -> httpx.Client:
        return httpx.Client(
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=60.0,
            ),
            timeout=httpx.Timeout(connect=6.0, read=6.0, write=6.0, pool=5.0),
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

        for attempt in range(2):
            try:
                if self.provider == "cohere":
                    request_options = {"max_retries": 0}
                    if len(texts) > 1:
                        request_options["timeout_in_seconds"] = 15.0
                    response = self.client.embed(
                        texts=texts,
                        model=self.model,
                        input_type=input_type,
                        request_options=request_options,
                    )
                    return response.embeddings
                raise NotImplementedError(f"未实现的提供商: {self.provider}")
            except Exception as exc:
                if not self._is_transient_connection_error(exc) or attempt >= 1:
                    logger.error("❌ Embedding 生成失败: %s", exc)
                    raise
                logger.warning(
                    "Embedding 连接异常，第 %d/1 次重试: %s",
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
