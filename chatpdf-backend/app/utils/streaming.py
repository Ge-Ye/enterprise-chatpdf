# app/utils/streaming.py
import asyncio
import json
import logging
from typing import AsyncGenerator, Iterable

from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def _event(event: str, data) -> str:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(jsonable_encoder(data), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def sse_response(generator: AsyncGenerator, sources: Iterable = ()):
    async def event_generator():
        done_sent = False
        try:
            if sources:
                yield _event("sources", {"source_docs": list(sources)})
            async for item in generator:
                if isinstance(item, dict) and "event" in item:
                    event_name = item["event"]
                    yield _event(event_name, item.get("data", ""))
                    done_sent = done_sent or event_name == "done"
                else:
                    # Preserve compatibility for the legacy GET stream endpoint.
                    yield _event("chunk", {"text": item})
            if not done_sent:
                yield _event("done", {"status": "done"})
        except asyncio.CancelledError:
            logger.info("SSE 客户端断开，已取消生成流")
            raise
        except Exception as exc:
            logger.exception("SSE 流式生成失败")
            yield _event("error", {"message": str(exc)})
        finally:
            close = getattr(generator, "aclose", None)
            if close:
                try:
                    await close()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("关闭生成器时发生错误", exc_info=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
