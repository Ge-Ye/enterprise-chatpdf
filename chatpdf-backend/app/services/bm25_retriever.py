"""Small in-memory BM25 retrieval with English and CJK tokenization."""
from collections import Counter
import math
import re
from typing import Dict, List, Optional

_TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+", re.IGNORECASE)


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    for match in _TOKEN.findall(text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", match):
            tokens.extend(match)
            tokens.extend(match[i:i + 2] for i in range(len(match) - 1))
        else:
            tokens.append(match)
    return tokens


def bm25_search(
    query: str, documents: List[Dict], top_k: int = 5,
    *, k1: float = 1.5, b: float = 0.75,
) -> List[Dict]:
    """Return positive-score documents with scores normalized against the best hit."""
    query_terms = tokenize(query)
    if not query_terms or not documents or top_k <= 0:
        return []
    tokenized = [tokenize(str(doc.get("text", "") or "")) for doc in documents]
    valid = [i for i, tokens in enumerate(tokenized) if tokens]
    if not valid:
        return []
    n_docs = len(valid)
    avg_len = sum(len(tokenized[i]) for i in valid) / n_docs
    doc_freq = Counter(term for i in valid for term in set(tokenized[i]))
    scored = []
    for index in valid:
        frequencies = Counter(tokenized[index])
        length = len(tokenized[index])
        score = 0.0
        for term in query_terms:
            tf = frequencies.get(term, 0)
            if not tf:
                continue
            df = doc_freq[term]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1.0 - b + b * length / max(avg_len, 1.0))
            score += idf * tf * (k1 + 1.0) / denominator
        if score > 0:
            scored.append((score, index))
    if not scored:
        return []
    scored.sort(key=lambda row: row[0], reverse=True)
    best = scored[0][0]
    results = []
    for raw_score, index in scored[:top_k]:
        item = dict(documents[index])
        item["bm25_score"] = raw_score / best
        item["score"] = item["bm25_score"]
        results.append(item)
    return results
