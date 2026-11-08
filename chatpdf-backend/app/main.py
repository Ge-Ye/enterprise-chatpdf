# app/main.py
import os
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")
try:
    import posthog
    posthog.disabled = True
    posthog.capture = lambda *args, **kwargs: None
except Exception:
    pass

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#测试错误日志
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import logging

import uvicorn
from fastapi import FastAPI
from app.routes import upload, chat, files, customs_router   # ✅ 导入模块
# Configuration is loaded exactly once by app.core.config.Settings: process env
# overrides backend/.env. This module must not call load_dotenv(), otherwise the
# routing layer could observe different values from the ones used here.
from app.core.config import settings
from app.core.security import (
    ApiKeyAuthMiddleware,
    RateLimitMiddleware,
    UploadSizeLimitMiddleware,
)
from fastapi.middleware.cors import CORSMiddleware

#这个要保留
app = FastAPI(title="ChatPDF Backend")

logger = logging.getLogger(__name__)


@app.on_event("startup")
async def validate_security_settings():
    """Fail closed when authentication is demanded but not configured."""
    if settings.AUTH_REQUIRED and not settings.API_KEY:
        raise RuntimeError("AUTH_REQUIRED=true 但 API_KEY 为空，拒绝启动")
    if settings.API_KEY:
        if len(settings.API_KEY) < 16:
            logger.warning("API_KEY 长度不足 16 个字符，建议使用至少 32 字节的随机值")
        logger.info("API Key 认证已启用，请求头: %s", settings.API_KEY_HEADER)
    else:
        logger.warning("API_KEY 为空，API Key 认证未启用；仅建议本机开发使用")
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
        if settings.APP_HOST not in loopback_hosts or not settings.DEBUG:
            logger.warning(
                "服务配置为对外监听（APP_HOST=%s, DEBUG=%s）但没有 API_KEY，"
                "请勿在没有认证的情况下暴露到公网",
                settings.APP_HOST,
                settings.DEBUG,
            )
    logger.info(
        "请求限流已启用=%s（默认 %s/窗口，上传 %s/窗口，问答 %s/窗口，窗口 %ss）",
        settings.RATE_LIMIT_ENABLED,
        settings.RATE_LIMIT_DEFAULT_REQUESTS,
        settings.RATE_LIMIT_UPLOAD_REQUESTS,
        settings.RATE_LIMIT_CHAT_REQUESTS,
        settings.RATE_LIMIT_WINDOW_SECONDS,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """捕获 422 错误并打印详细信息"""
    logger.error(f"❌ 422 验证错误:")
    logger.error(f"  URL: {request.url}")
    logger.error(f"  Body: {await request.body()}")
    logger.error(f"  Errors: {exc.errors()}")
    
    return JSONResponse(
        status_code=422,
        content={
            "detail": exc.errors(),
            "body": exc.body
        }
    )


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


# add_middleware inserts at the front, so the last one added runs outermost.
# CORS must stay outermost to attach headers to 401/413/429 responses.
app.add_middleware(UploadSizeLimitMiddleware)
app.add_middleware(ApiKeyAuthMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(customs_router.router)

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
        reload_dirs=["app"] if settings.DEBUG else None,
    )
