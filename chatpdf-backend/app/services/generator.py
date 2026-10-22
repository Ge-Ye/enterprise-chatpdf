# app/services/generator.py
import httpx
from app.core.config import settings
from typing import AsyncGenerator, Dict, List
import json
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

async def stream_from_deepseek(prompt: str, metadata: Dict = None) -> AsyncGenerator[str, None]:
    """
    异步生成器：对接 DeepSeek Chat Completion API (兼容 OpenAI 格式)
    将模型流式输出逐片 yield 出来，用于前端 SSE 或 WebSocket。
    """
    base_url = settings.DEEPSEEK_API_URL.strip().rstrip("/")
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    # ✅ 正确格式：messages 而不是 prompt
    payload = {
        "model": getattr(settings, "DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": "You are a helpful AI assistant."},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
    }

    # 附加元数据（如文件ID等）
    if metadata:
        payload["metadata"] = metadata

    target_host = urlparse(url).hostname or "unknown"
    logger.info("DeepSeek 请求目标域名: %s", target_host)

    timeout = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    try:
                        resp.raise_for_status()
                    except Exception as e:
                        text = await resp.aread()
                        logger.error("DeepSeek 请求失败: %s; 响应内容: %s", e, text.decode("utf-8", errors="ignore"))
                        raise

                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data = line[len("data: "):]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                                delta = obj.get("choices", [{}])[0].get("delta", {}).get("content")
                                if delta:
                                    yield delta
                            except json.JSONDecodeError:
                                yield line
            return
        except (httpx.ConnectError, httpx.TimeoutException, socket.gaierror):
            if attempt == 2:
                logger.exception("DeepSeek 连接失败，目标域名=%s", target_host)
                raise
            logger.warning("DeepSeek 连接失败，目标域名=%s，将重试", target_host)
