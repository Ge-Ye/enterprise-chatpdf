# app/models.py
from pydantic import BaseModel
from typing import List, Optional

class UploadResponse(BaseModel):
    file_id: str
    filename: str
    status: str

class ChatRequest(BaseModel):
    file_id: str
    query: str
    top_k: int = 5
    use_es: bool = True
    use_vector: bool = True

class SourceDoc(BaseModel):
    """单个引用来源"""
    rank: int  # 排名（1, 2, 3...）
    relevance_score: float  # 相关度分数（0-1）
    text: str  # 文档片段内容
    page: Optional[int] = None  # 页码
    doc_id: str  # 文档ID

class ChatResponse(BaseModel):
    answer: str
    source_docs: List[SourceDoc] = []  # 修改为结构化的引用列表