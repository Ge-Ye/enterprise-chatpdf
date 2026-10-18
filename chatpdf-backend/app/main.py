# app/main.py
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#测试错误日志
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import logging

import uvicorn
from fastapi import FastAPI
from app.routes import upload, chat, files   # ✅ 导入模块
from app.core.config import settings
from fastapi.middleware.cors import CORSMiddleware

#这个要保留
app = FastAPI(title="ChatPDF Backend")

logger = logging.getLogger(__name__)
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


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地开发可放开，生产请限制前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(files.router, prefix="/api")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
