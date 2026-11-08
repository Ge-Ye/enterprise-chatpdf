from pathlib import Path
from typing import List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """The single configuration loader for the backend.

    Precedence is fixed by pydantic-settings: process environment > backend/.env
    > field defaults. No module may call ``load_dotenv`` itself, otherwise the
    effective value can differ from ``settings`` (notably under Docker, where
    .env is not copied into the image but is injected as environment variables).
    """

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = Field(default=8000, validation_alias=AliasChoices("BACKEND_PORT", "APP_PORT"))
    UPLOAD_DIR: str = "./app/data/uploads"
    CHROMA_DB_DIR: str = "./app/data/chroma"
    ES_HOST: str = "http://localhost:9200"
    ES_INDEX: str = "chatpdf_docs"
    # Single Elasticsearch switch; retrieval and indexing both read this field.
    USE_ES: bool = False
    SILICONFLOW_API_KEY: str = ""
    SILICONFLOW_BASE_URL: str = "https://api.siliconflow.cn/v1"
    # Only a fallback for SILICONFLOW_API_KEY; the embedding/rerank/extraction
    # implementation is fixed to SiliconFlow, so no provider switch exists.
    OPENAI_API_KEY: str = ""
    TEXT_EXTRACT_MODEL: str = "Qwen/Qwen2.5-32B-Instruct"
    VISION_EXTRACT_MODEL: str = "Qwen/Qwen3-VL-8B-Instruct"
    VISION_EXTRACT_FALLBACK_MODEL: str = "Qwen/Qwen3-VL-8B-Instruct"
    VISION_MAX_IMAGE_SIDE: int = Field(default=1280, ge=128)
    CUSTOMS_LLM_READ_TIMEOUT: float = Field(default=120.0, gt=0)
    LLM_BYPASS_SYSTEM_PROXY: bool = True
    FEISHU_WEBHOOK_URL: str = ""
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    DEEPSEEK_API_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    DEBUG: bool = True
    MIN_RELEVANCE_SCORE: float = 0.30
    RERANK_TIMEOUT_SECONDS: float = 3.5

    # --- API authentication ---
    # Empty API_KEY keeps local development usable; set AUTH_REQUIRED=true to
    # refuse startup instead of silently serving an unauthenticated instance.
    API_KEY: str = ""
    API_KEY_HEADER: str = "X-API-Key"
    AUTH_REQUIRED: bool = False
    TRUST_PROXY_HEADERS: bool = False

    # --- Request rate limiting (in-memory, per process) ---
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)
    RATE_LIMIT_DEFAULT_REQUESTS: int = Field(default=120, ge=1)
    RATE_LIMIT_UPLOAD_REQUESTS: int = Field(default=10, ge=1)
    RATE_LIMIT_CHAT_REQUESTS: int = Field(default=20, ge=1)
    RATE_LIMIT_MAX_TRACKED_CLIENTS: int = Field(default=10000, ge=100)

    # --- Upload restrictions ---
    MAX_UPLOAD_MB: int = Field(default=20, ge=1, le=200)
    MAX_UPLOAD_FILENAME_LENGTH: int = Field(default=200, ge=32, le=512)
    ALLOWED_UPLOAD_MIME_TYPES: str = (
        "application/pdf,application/x-pdf,application/octet-stream"
    )

    @field_validator("*", mode="before")
    @classmethod
    def strip_string_settings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @property
    def allowed_origins(self) -> List[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def allowed_upload_mime_types(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.ALLOWED_UPLOAD_MIME_TYPES.split(",")
            if item.strip()
        }

    @property
    def upload_path(self) -> Path:
        path = Path(self.UPLOAD_DIR)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def chroma_path(self) -> Path:
        path = Path(self.CHROMA_DB_DIR)
        return path if path.is_absolute() else BACKEND_DIR / path


settings = Settings()
