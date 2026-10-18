# app/services/embeddings.py

import cohere
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
            self.client = cohere.Client(settings.COHERE_API_KEY)
            self.model = getattr(settings, 'COHERE_EMBED_MODEL', 'embed-multilingual-v3.0')
            logger.info(f"✅ Cohere Embedding 初始化成功，模型: {self.model}")
        else:
            raise NotImplementedError(f"不支持的 Embedding 提供商: {self.provider}")
    
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """将文本列表转换为向量"""
        if not texts:
            return []
        
        try:
            if self.provider == "cohere":
                response = self.client.embed(
                    texts=texts,
                    model=self.model,
                    input_type="search_document"
                )
                return response.embeddings
            else:
                raise NotImplementedError(f"未实现的提供商: {self.provider}")
        except Exception as e:
            logger.error(f"❌ Embedding 生成失败: {e}")
            raise
    
    def embed_query(self, query: str) -> List[float]:
        """将查询文本转换为向量"""
        try:
            if self.provider == "cohere":
                response = self.client.embed(
                    texts=[query],
                    model=self.model,
                    input_type="search_query"
                )
                return response.embeddings[0]
            else:
                raise NotImplementedError(f"未实现的提供商: {self.provider}")
        except Exception as e:
            logger.error(f"❌ Query Embedding 生成失败: {e}")
            raise


# 全局单例
embedding_client = EmbeddingClient()