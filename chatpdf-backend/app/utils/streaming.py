# app/utils/streaming.py
from sse_starlette.sse import EventSourceResponse
from fastapi import Request
import asyncio
from typing import AsyncGenerator

async def sse_response(generator: AsyncGenerator[str, None]):
    async def event_generator():
        try:
            async for piece in generator:
                yield {"event": "message", "data": piece}
            # 发送结束事件
            yield {"event": "done", "data": "[DONE]"}
        except Exception as e:
            yield {"event": "error", "data": str(e)}
    return EventSourceResponse(event_generator())
