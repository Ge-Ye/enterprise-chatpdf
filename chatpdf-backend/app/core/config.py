from pathlib import Path
from typing import List

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Central application settings loaded from backend/.env and process env."""

    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = Field(default=8000, validation_alias=AliasChoices("BACKEND_PORT", "APP_PORT"))
    UPLOAD_DIR: str = "./app/data/uploads"
    CHROMA_DB_DIR: str = "./app/data/chroma"
    ES_HOST: str = "http://localhost:9200"
    ES_INDEX: str = "chatpdf_docs"
    EMBEDDING_PROVIDER: str = "cohere"
    RERANK_PROVIDER: str = "cohere"
    COHERE_API_KEY: str = ""
    COHERE_EMBEDDING_MODEL: str = "embed-multilingual-v3.0"
    COHERE_RERANK_MODEL: str = "rerank-multilingual-v3.0"
    DEEPSEEK_API_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    DEBUG: bool = True
    MIN_RELEVANCE_SCORE: float = 0.30
    RERANK_TIMEOUT_SECONDS: float = 3.5

    @property
    def allowed_origins(self) -> List[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def upload_path(self) -> Path:
        path = Path(self.UPLOAD_DIR)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def chroma_path(self) -> Path:
        path = Path(self.CHROMA_DB_DIR)
        return path if path.is_absolute() else BACKEND_DIR / path


settings = Settings()
