"""Small in-memory BM25 retrieval with English and CJK tokenization.

Tokenization, term frequencies and document frequencies are cached per corpus
so repeated questions do not rescan every stored chunk. The cache key includes
the chunk-id fingerprint, which changes whenever the corpus changes.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass
import math
import re
import threading
from typing import Dict, List, Optional, Tuple

_TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+", re.IGNORECASE)

_CORPUS_CACHE_MAX_ENTRIES = 8
_CORPUS_CACHE: "OrderedDict[Tuple[str, int, int], _BM25Corpus]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_STATS = {"hits": 0, "misses": 0}


@dataclass(frozen=True)
class _BM25Corpus:
    frequencies: List[Counter]
    lengths: List[int]
    doc_freq: Counter
    avg_len: float
    valid_indices: List[int]


def tokenize(text: str) -> List[str]:
    tokens: List[str] = []
    for match in _TOKEN.findall(text.casefold()):
        if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]+", match):
            tokens.extend(match)
            tokens.extend(match[i:i + 2] for i in range(len(match) - 1))
        else:
            tokens.append(match)
    return tokens


def cache_stats() -> Dict[str, int]:
    """Expose corpus-cache counters for manual verification and diagnostics."""
    with _CACHE_LOCK:
        return {
            "entries": len(_CORPUS_CACHE),
            "hits": _CACHE_STATS["hits"],
            "misses": _CACHE_STATS["misses"],
        }


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CORPUS_CACHE.clear()
        _CACHE_STATS["hits"] = 0
        _CACHE_STATS["misses"] = 0


def _corpus_key(cache_key: str, documents: List[Dict]) -> Tuple[str, int, int]:
    """Identify a corpus without hashing every chunk's full text."""
    ids = tuple(str(doc.get("id", "")) for doc in documents)
    return cache_key, len(documents), hash(ids)


def _build_corpus(documents: List[Dict]) -> _BM25Corpus:
    tokenized = [tokenize(str(doc.get("text", "") or "")) for doc in documents]
    valid_indices = [i for i, tokens in enumerate(tokenized) if tokens]
    frequencies = [Counter(tokens) for tokens in tokenized]
    lengths = [len(tokens) for tokens in tokenized]
    if not valid_indices:
        return _BM25Corpus(frequencies, lengths, Counter(), 0.0, [])
    n_docs = len(valid_indices)
    avg_len = sum(lengths[i] for i in valid_indices) / n_docs
    doc_freq = Counter(
        term for i in valid_indices for term in frequencies[i]
    )
    return _BM25Corpus(frequencies, lengths, doc_freq, avg_len, valid_indices)


def _cached_corpus(cache_key: Optional[str], documents: List[Dict]) -> _BM25Corpus:
    if not cache_key:
        return _build_corpus(documents)

    key = _corpus_key(cache_key, documents)
    with _CACHE_LOCK:
        cached = _CORPUS_CACHE.get(key)
        if cached is not None:
            _CORPUS_CACHE.move_to_end(key)
            _CACHE_STATS["hits"] += 1
            return cached
        _CACHE_STATS["misses"] += 1

    # Building runs outside the lock; a duplicate miss under a race is harmless
    # and preferable to blocking every other request.
    corpus = _build_corpus(documents)
    with _CACHE_LOCK:
        _CORPUS_CACHE[key] = corpus
        _CORPUS_CACHE.move_to_end(key)
        while len(_CORPUS_CACHE) > _CORPUS_CACHE_MAX_ENTRIES:
            _CORPUS_CACHE.popitem(last=False)
    return corpus


def bm25_search(
    query: str, documents: List[Dict], top_k: int = 5,
    *, k1: float = 1.5, b: float = 0.75,
    cache_key: Optional[str] = None,
) -> List[Dict]:
    """Return positive-score documents with scores normalized against the best hit."""
    query_terms = tokenize(query)
    if not query_terms or not documents or top_k <= 0:
        return []
    corpus = _cached_corpus(cache_key, documents)
    if not corpus.valid_indices:
        return []
    n_docs = len(corpus.valid_indices)
    scored = []
    for index in corpus.valid_indices:
        frequencies = corpus.frequencies[index]
        length = corpus.lengths[index]
        score = 0.0
        for term in query_terms:
            tf = frequencies.get(term, 0)
            if not tf:
                continue
            df = corpus.doc_freq[term]
            idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (
                1.0 - b + b * length / max(corpus.avg_len, 1.0)
            )
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
