# app/core/config.py

from pydantic_settings import BaseSettings
from pydantic import Field
import os

class Settings(BaseSettings):
    """
    全局配置类，用于统一管理 ChatPDF 后端的所有配置。
    自动支持从 .env 文件加载环境变量，也可通过系统环境变量覆盖。
    """

    # === FastAPI Server ===
    APP_HOST: str = Field("127.0.0.1", description="后端服务运行地址")
    APP_PORT: int = Field(8000, description="后端服务端口号")

    # === 文件存储 ===
    UPLOAD_DIR: str = Field("./app/data/uploads", description="文件上传存储路径")

    # === 向量数据库 (Chroma) ===
    CHROMA_DB_DIR: str = Field("./app/data/chroma", description="Chroma 数据库存储路径")
    CHROMA_PATH: str = Field("./chroma_db", description="兼容旧配置的路径别名")

    # === Elasticsearch ===
    ES_HOST: str = Field("http://localhost:9200", description="Elasticsearch 服务地址")
    ES_INDEX: str = Field("chatpdf_docs", description="Elasticsearch 索引名称")

    # === Embedding 模型 ===
    EMBEDDING_PROVIDER: str = Field("cohere", description="Embedding 模型提供方")
    RERANK_PROVIDER: str = Field("cohere", description="Rerank 模型提供方")
    COHERE_API_KEY: str = Field("", description="Cohere API Key")
    COHERE_EMBEDDING_MODEL: str = Field("embed-multilingual-v3.0", description="Cohere Embedding 模型名称")
    COHERE_RERANK_MODEL: str = Field("rerank-multilingual-v3.0", description="Cohere Rerank 模型名称")

    # === DeepSeek 大模型 ===
    DEEPSEEK_API_URL: str = Field("", description="DeepSeek 接口地址")
    DEEPSEEK_API_KEY: str = Field("", description="DeepSeek API Key")
    DEEPSEEK_MODEL: str = Field("deepseek-chat", description="DeepSeek 模型名称")  # ✅ 新增这一行

    # === 调试选项 ===
    DEBUG: bool = Field(True, description="是否启用调试模式")

    class Config:
        env_file = ".env"           # ✅ 自动从 .env 文件读取
        env_file_encoding = "utf-8"
        case_sensitive = False      # ✅ 环境变量名不区分大小写

# ✅ 全局单例配置对象
settings = Settings()
