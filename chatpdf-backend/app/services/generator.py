# app/services/generator.py
import httpx
from app.core.config import settings
from typing import AsyncGenerator, Dict, List
import json
import logging

logger = logging.getLogger(__name__)

async def stream_from_deepseek(prompt: str, metadata: Dict = None) -> AsyncGenerator[str, None]:
    """
    异步生成器：对接 DeepSeek Chat Completion API (兼容 OpenAI 格式)
    将模型流式输出逐片 yield 出来，用于前端 SSE 或 WebSocket。
    """
    url = f"{settings.DEEPSEEK_API_URL.rstrip('/')}/chat/completions"
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

    # 打印调试日志（可选）
    logger.info(f"🚀 DeepSeek 请求地址: {url}")
    logger.info(f"🧠 请求体: {json.dumps(payload, ensure_ascii=False)}")

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            try:
                resp.raise_for_status()
            except Exception as e:
                # 打印详细错误
                text = await resp.aread()
                logger.error(f"❌ DeepSeek 请求失败: {e}\n响应内容: {text.decode('utf-8', errors='ignore')}")
                raise

            # ✅ 处理流式输出
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
                        # 返回非标准输出
                        yield line
