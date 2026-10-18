# app/services/chunker.py
from typing import List
import uuid

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[dict]:
    """
    返回 [{id, text, meta}]
    chunk_size: 每块字符数近似
    overlap: 重叠字符数
    """
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = start + chunk_size
        piece = text[start:end]
        chunks.append({
            "id": str(uuid.uuid4()),
            "text": piece,
            "meta": {"start": start, "end": min(end, n)}
        })
        start = end - overlap
        if start < 0:
            start = 0
    return chunks
