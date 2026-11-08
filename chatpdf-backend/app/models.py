# app/models.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Literal, Optional

MAX_QUERY_LENGTH = 2000
MAX_TOP_K = 20

# Conversation history is optional context, so it is bounded instead of rejected:
# a long chat must never turn a valid question into a 422 response.
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_MESSAGE_CHARS = 3000
MAX_HISTORY_TOTAL_CHARS = 6000


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    status: str

class ChatMessage(BaseModel):
    """One prior conversation turn supplied by the client."""
    role: Literal["user", "assistant"]
    content: str


def _truncate_history_content(content: str, limit: int) -> str:
    """Keep both ends of an over-long turn so a follow-up reference survives."""
    content = content.strip()
    if len(content) <= limit:
        return content
    marker = "\n…\n"
    kept = max(limit - len(marker), 1)
    head = kept * 2 // 3
    tail = kept - head
    return f"{content[:head]}{marker}{content[-tail:]}"

class ChatRequest(BaseModel):
    file_id: str = Field(max_length=64)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    use_es: bool = True
    use_vector: bool = True
    # Recent turns, oldest first. Defaults to empty so existing single-turn
    # clients (including the legacy GET stream endpoint) keep working.
    history: List[ChatMessage] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def query_must_have_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value

    @field_validator("history")
    @classmethod
    def history_must_be_bounded(cls, value: List[ChatMessage]) -> List[ChatMessage]:
        """Keep the most recent turns within the message and character budgets."""
        bounded: List[ChatMessage] = []
        total = 0
        for message in reversed(value):
            if len(bounded) >= MAX_HISTORY_MESSAGES:
                break
            content = _truncate_history_content(
                message.content, MAX_HISTORY_MESSAGE_CHARS
            )
            if not content:
                continue
            if total + len(content) > MAX_HISTORY_TOTAL_CHARS:
                if bounded:
                    break
                content = content[:MAX_HISTORY_TOTAL_CHARS]
            bounded.append(message.model_copy(update={"content": content}))
            total += len(content)
        bounded.reverse()
        return bounded

class SourceDoc(BaseModel):
    """单个引用来源"""
    rank: int  # 排名（1, 2, 3...）
    # None for overview sources: document-head chunks were never reranked, so a
    # numeric score would be fabricated.
    relevance_score: Optional[float] = None
    text: str  # 文档片段内容
    page: Optional[int] = None  # 页码
    doc_id: str  # 文档ID
    section_title: Optional[str] = None
    source_kind: Literal["rerank", "overview"] = "rerank"

class ChatResponse(BaseModel):
    answer: str
    source_docs: List[SourceDoc] = []  # 修改为结构化的引用列表
