# app/routes/chat.py
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import ValidationError
from app.models import (
    ChatRequest,
    ChatResponse,
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    SourceDoc,
)
from app.services.embeddings import EmbeddingClient
from app.services.vectorstore import ChromaClient
from app.services.es_retriever import es_search_text, rrf_fusion
from app.services.bm25_retriever import bm25_search
from app.core.config import settings
from app.services.reranker import SiliconFlowReranker
from app.services.generator import stream_from_deepseek
from app.utils.streaming import sse_response
from typing import List, Dict, Optional
import asyncio
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

router = APIRouter()
chroma = ChromaClient()
emb_client = EmbeddingClient()
reranker = SiliconFlowReranker()
MIN_RELEVANCE_SCORE = settings.MIN_RELEVANCE_SCORE
RERANK_TIMEOUT_SECONDS = settings.RERANK_TIMEOUT_SECONDS
RRF_K = 60

# Prompt-side bounds mirror app.models so a direct build_prompt() call cannot
# grow the LLM context without limit.
MAX_PROMPT_HISTORY_MESSAGES = 8
MAX_PROMPT_HISTORY_CHARS = 6000
MAX_CONTEXT_QUERY_CHARS = 400

_ORDINAL_REFERENCE_RE = re.compile(
    r"第[一二三四五六七八九十百零两0-9]+(?:条|点|项|个|句|段|章|节|部分|方面)"
)
# Unambiguous reference/continuation cues for an elliptical follow-up. Latin
# terms are matched as whole words; Chinese cues are substring markers.
_FOLLOW_UP_CJK_TERMS = (
    "它", "它们", "上述", "前面提到", "前面说", "刚才", "上一条", "上一轮",
    "上一次", "前文", "这条", "那条", "这些", "那些", "那么", "继续",
    "接着", "还有", "同样", "也是", "为什么", "为啥", "什么意思",
    "展开说", "详细说", "具体点", "举个",
)
_FOLLOW_UP_LATIN_TERMS = (
    "it", "its", "they", "them", "their", "these", "those", "above",
    "previous", "former", "latter", "what about", "how about",
    "the second", "the third", "the last", "last one", "first one",
    "more details", "elaborate", "continue", "go on",
)


def is_summary_query(query: str) -> bool:
    """Recognize document overview and document-level evaluation requests."""
    if is_chitchat_or_persona_query(query):
        return False
    summary_terms = (
        "主要内容", "讲了什么", "讲了些啥", "总结", "概括", "概述", "核心内容",
        "大意", "什么文档", "这篇文章", "这份文档", "本文", "全文", "summary", "overview",
    )
    normalized = query.casefold()
    if any(term.casefold() in normalized for term in summary_terms):
        return True
    document_refs = (
        "这个文档", "这份文档", "这篇文档", "本文", "这篇论文", "这本书", "该文档", "它",
    )
    evaluation_terms = (
        "有价值", "价值", "意义", "贡献", "作用", "用处", "有什么用", "亮点", "创新点",
        "优缺点", "局限", "不足", "评价", "评估", "看点", "怎么样", "好不好", "值得看", "适合谁",
    )
    has_document_ref = any(term in query for term in document_refs)
    has_evaluation = any(term in query for term in evaluation_terms)
    return has_evaluation and (has_document_ref or len(query.strip()) <= 12)


_PERSONA_TERMS = (
    "你自己", "你是谁", "你叫什么", "你能做什么", "你的功能", "你是什么模型",
    "介绍一下你", "介绍下你", "你是干嘛的", "你是做什么的",
)
_CASUAL_TERMS = (
    "你好", "您好", "早上好", "晚上好", "嗨", "今天吃什么",
    "吃什么好", "吃什么", "天气怎么样", "今天天气", "讲个笑话", "谢谢", "再见",
    "在吗", "晚安",
)
# English chat must match whole words: a substring rule makes "hi" fire inside
# "this", "which", "shipping" and silently disables retrieval for English questions.
_LATIN_CHITCHAT_TERMS = (
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "how are you", "how are you doing", "who are you", "what can you do",
    "what model are you", "thanks", "thanks a lot", "thank you", "bye",
    "goodbye", "see you",
)
# Words that carry no document question on their own; used to decide whether a
# greeting prefix still left a real question behind.
_CHITCHAT_FILLER_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "for", "in", "on", "at",
    "by", "with", "from", "is", "are", "was", "were", "am", "be", "been", "do",
    "does", "did", "can", "could", "would", "should", "will", "i", "me", "my",
    "we", "us", "you", "your", "yours", "it", "its", "this", "that", "these",
    "those", "what", "which", "who", "whom", "how", "when", "where", "why", "so",
    "well", "um", "uh", "hi", "hello", "hey", "thanks", "thank", "please", "good",
    "morning", "afternoon", "evening", "night", "today", "there", "again",
    "everyone", "all", "bot", "assistant", "friend", "team", "guys", "dear",
    "going", "doing", "up", "see", "bye", "goodbye", "s", "t",
})
_CHITCHAT_PUNCTUATION = " \t\r\n.,!?;:~-—–，。！？、；：…\"'()（）"


def _latin_term_pattern(term: str) -> str:
    """Whole-word regex for a Latin term so it cannot match inside another word."""
    parts = [re.escape(part) for part in term.casefold().split()]
    return r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])"


def _contains_latin_term(normalized: str, term: str) -> bool:
    return re.search(_latin_term_pattern(term), normalized) is not None


def _strip_leading_latin_chitchat(normalized: str) -> str:
    """Drop leading greetings and fillers so "Hello, <question>" keeps its question."""
    phrases = sorted(
        set(_LATIN_CHITCHAT_TERMS) | _CHITCHAT_FILLER_WORDS, key=len, reverse=True
    )
    remainder = normalized
    while True:
        remainder = remainder.lstrip(_CHITCHAT_PUNCTUATION)
        for phrase in phrases:
            match = re.match(_latin_term_pattern(phrase), remainder)
            if match:
                remainder = remainder[match.end():]
                break
        else:
            return remainder


def _has_substantive_remainder(remainder: str) -> bool:
    """True when the text left after a greeting still asks about something concrete."""
    latin_words = [
        word for word in re.findall(r"[a-z0-9]+", remainder)
        if word not in _CHITCHAT_FILLER_WORDS
    ]
    if len(latin_words) >= 2:
        return True
    cjk_only = remainder
    for term in _CASUAL_TERMS + _PERSONA_TERMS:
        cjk_only = cjk_only.replace(term, "")
    return len(re.findall(r"[\u4e00-\u9fff]", cjk_only)) >= 2


def is_chitchat_or_persona_query(query: str) -> bool:
    """Identify assistant persona and everyday chat before any document retrieval."""
    normalized = query.strip().casefold()
    document_refs = (
        "这个文档", "这份文档", "这篇文档", "本文", "文中", "这篇论文", "这本书", "该文档",
        "文档", "论文", "章节", "摘要", "结论", "实验", "作者",
    )
    if any(term in normalized for term in document_refs):
        return False
    # Numeric Latin tokens commonly name a model or other concrete technical entity.
    if re.search(r"(?<![a-z])[a-z][a-z0-9_-]*\d[a-z0-9_-]*(?![a-z])", normalized):
        return False

    if any(_contains_latin_term(normalized, term) for term in _LATIN_CHITCHAT_TERMS):
        remainder = _strip_leading_latin_chitchat(normalized)
        return not _has_substantive_remainder(remainder)

    return any(term.casefold() in normalized for term in _PERSONA_TERMS + _CASUAL_TERMS)


def _history_field(message, field: str) -> str:
    """Read one field from a ChatMessage or from a plain mapping."""
    value = message.get(field) if isinstance(message, dict) else getattr(message, field, None)
    return value.strip() if isinstance(value, str) else ""


def _last_history_user_query(history) -> str:
    """Return the most recent non-empty user turn, oldest-first history assumed."""
    for message in reversed(list(history or [])):
        if _history_field(message, "role") != "user":
            continue
        content = _history_field(message, "content")
        if content:
            return content
    return ""


def _is_follow_up_query(query: str) -> bool:
    """Detect elliptical follow-ups that only make sense with prior turns."""
    text = (query or "").strip()
    if not text:
        return False
    if text.startswith("那"):
        return True
    if _ORDINAL_REFERENCE_RE.search(text):
        return True
    if text.rstrip(" \t\r\n.。！!?？").endswith("呢"):
        return True
    normalized = text.casefold()
    if any(term in normalized for term in _FOLLOW_UP_CJK_TERMS):
        return True
    return any(_contains_latin_term(normalized, term) for term in _FOLLOW_UP_LATIN_TERMS)


def contextualize_retrieval_query(query: str, history=None) -> str:
    """Prefix a follow-up with the previous user question for retrieval.

    Retrieval runs before generation, so without this the query "那第二条呢？"
    would be embedded on its own and match nothing useful. The assistant answer
    is not mixed in: it stays in the prompt as conversational context, while the
    retrieval query keeps the user's own vocabulary and stays bounded.
    """
    previous = _last_history_user_query(history)
    if not previous:
        return query
    if not _is_follow_up_query(query):
        return query
    logger.info("检测到省略式追问，检索查询结合上一轮用户问题")
    previous = previous[:MAX_CONTEXT_QUERY_CHARS].strip()
    if not previous:
        return query
    return f"{previous}\n{query}"


def _format_history(history) -> str:
    """Render the most recent turns within the prompt budget."""
    selected = []
    total = 0
    for message in reversed(list(history or [])):
        role = _history_field(message, "role")
        content = _history_field(message, "content")
        if role not in ("user", "assistant") or not content:
            continue
        if len(selected) >= MAX_PROMPT_HISTORY_MESSAGES:
            break
        if total + len(content) > MAX_PROMPT_HISTORY_CHARS:
            break
        selected.append((role, content))
        total += len(content)
    selected.reverse()
    return "\n".join(
        f"{'用户' if role == 'user' else '助手'}：{content}"
        for role, content in selected
    )


def _is_document_evaluation_query(query: str) -> bool:
    evaluation_terms = (
        "价值", "评价", "意义", "优缺点", "贡献", "创新", "局限", "你觉得",
        "怎么看", "你认为", "适不适合", "好不好",
    )
    return any(term in query for term in evaluation_terms)


def _is_pure_summary_query(query: str) -> bool:
    return is_summary_query(query) and not _is_document_evaluation_query(query)


def _evaluation_search_query(query: str) -> str:
    """Add document-structure terms for broad document-level assessments."""
    if not _is_document_evaluation_query(query):
        return query
    return f"{query} abstract introduction conclusion contribution value 摘要 引言 结论 贡献 价值"


def _is_toc_chunk(doc: Dict) -> bool:
    text = str(doc.get("text", "") or "")
    normalized = text.casefold()
    has_toc_label = "contents" in normalized or "目录" in text
    dot_leaders = len(re.findall(r"(?:\.\s*){3,}|…{2,}", text))
    numbered_entries = len(re.findall(r"\b\d+(?:\.\d+)*\s+[A-Za-z][^\n]{0,100}?\s+\d+\b", text))
    return has_toc_label and (dot_leaders >= 1 or numbered_entries >= 2)


def _is_author_header_chunk(doc: Dict) -> bool:
    text = str(doc.get("text", "") or "")
    header = text[:250]
    academic_markers = sum(header.count(marker) for marker in ("∗", "†", "‡", "§"))
    email_count = len(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", header))
    glued_names = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", header)
    normalized = header.casefold()
    institution_terms = (
        "university", "institute", "department", "laboratory", "college", "大学", "研究院", "学院", "实验室",
    )
    institution_count = sum(normalized.count(term) for term in institution_terms)
    return academic_markers >= 2 or email_count >= 2 or institution_count >= 2 or len(glued_names) >= 2


def _with_overview_scores(
    docs: List[Dict], overview_ids: Optional[set] = None
) -> List[Dict]:
    """Use reranker scores when available; otherwise mark ordered overview chunks."""
    scored = []
    for index, doc in enumerate(docs):
        item = dict(doc)
        score = item.get("rerank_score")
        is_overview_chunk = overview_ids is None or item.get("id") in overview_ids
        if is_overview_chunk and score is None and item.get("score") is not None:
            item["rerank_score"] = item["score"]
        scored.append(item)
    return scored


def _ranking_score(doc: Dict) -> float:
    """Prefer rerank, then RRF, then the legacy normalized retrieval score."""
    for key in ("rerank_score", "rrf_score", "score"):
        value = doc.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _merge_overview_and_candidates(
    overview_chunks: List[Dict],
    candidates: List[Dict],
    limit: int = 5,
    *,
    use_overview_scores: bool = True,
    query: str = "",
) -> List[Dict]:
    original_overview = list(overview_chunks)
    overview_chunks = [doc for doc in overview_chunks if not _is_toc_chunk(doc)]
    candidates = [doc for doc in candidates if not _is_toc_chunk(doc)]
    if not overview_chunks and not candidates and original_overview:
        overview_chunks = original_overview
    substantive_chunks = [doc for doc in overview_chunks if not _is_author_header_chunk(doc)]
    author_chunks = [doc for doc in overview_chunks if _is_author_header_chunk(doc)]
    ordered_overview = substantive_chunks + author_chunks
    ranked_candidates = sorted(candidates, key=_ranking_score, reverse=True)
    if _is_document_evaluation_query(query):
        ordered_chunks = ranked_candidates[:1] + ordered_overview + ranked_candidates[1:]
    else:
        ordered_chunks = ordered_overview + ranked_candidates

    candidates_by_id = {doc.get("id"): doc for doc in candidates}
    merged = []
    seen = set()
    for doc in ordered_chunks:
        doc_id = doc.get("id")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        item = dict(doc)
        ranked_match = candidates_by_id.get(doc_id, {})
        for score_key in ("rerank_score", "score"):
            if item.get(score_key) is None and ranked_match.get(score_key) is not None:
                item[score_key] = ranked_match[score_key]
        merged.append(item)
    merged = merged[:limit]
    overview_ids = {doc.get("id") for doc in overview_chunks}
    return _with_overview_scores(merged, overview_ids) if use_overview_scores else merged

def build_prompt(query: str, docs: List[Dict], history=None) -> str:
    """构建 LLM Prompt - 不再包含 Source ID

    ``history`` carries prior turns so elliptical follow-ups can resolve their
    referents without the user restating the whole question. It is conversational
    context only; the retrieved chunks remain the document evidence.
    """
    if not docs:
        if is_chitchat_or_persona_query(query):
            status = "[SYSTEM_STATUS: CHITCHAT_OR_PERSONA_QUERY]"
        else:
            status = (
                "[SYSTEM_STATUS: NO_DOCUMENT_UPLOADED]"
                if chroma.count() == 0
                else "[SYSTEM_STATUS: NO_RELEVANT_CHUNKS_MATCHED]"
            )
        context = status
    else:
        # 只传递文本内容，不传递 ID
        context = "\n\n---\n".join([
            f"文档片段 {i+1}:\n{d.get('text', '')}" 
            for i, d in enumerate(docs[:5])
        ])
    
    history_text = _format_history(history)
    history_section = ""
    if history_text:
        history_section = (
            "对话历史（按时间顺序；仅用于理解当前问题的指代，历史回答不是文档证据）：\n"
            f"{history_text}\n\n"
        )

    rules = [
        '1. 直接回答问题，不要在回答中添加"Source ID"或引用标记',
        "2. 用清晰、专业的语言组织答案",
        "3. 遵守上述无匹配时的闲聊与文档问答分流规则",
    ]
    if history_text:
        rules.append(
            "4. 当前问题若是省略式追问（如“它有什么限制？”“那第二条呢？”），"
            "结合对话历史确定指代对象，不要要求用户重复问题；历史回答中的既有结论可以延续，"
            "但涉及文档事实时应以本轮参考片段为准，片段未支持的内容不得编造；"
            "只回答当前追问，不要复述历史回答全文。"
        )
    if _is_pure_summary_query(query):
        rules.append(
            f"{len(rules) + 1}. 请用简明扼要的 3~5 个核心要点总结文档主旨与关键规定，"
            "总字数控制在 250~350 字以内，条理清晰，不要长篇大论。"
        )
    requirements = "\n".join(rules)

    prompt = f"""你是一个专业的文档问答助手，请用中文回答。

内部状态标记仅供你判断回答方式，严禁在回答中复述内部标记、系统状态或内部上下文提示。
当状态为 [SYSTEM_STATUS: CHITCHAT_OR_PERSONA_QUERY]：直接自然友好地回答问候或关于你自身身份、能力的问题，不要引用或提及文档，也不要附加文档引导。
有参考文档片段时，严格依据片段回答；概述类问题应综合这些片段概括文档核心内容，不得编造片段未支持的事实。
主观评价、价值判断、贡献、局限、适用人群等文档级分析，应基于片段中的背景、问题、方法、结论等进行有依据的综合评估；不得因文档没有直接写“有价值”而拒答。区分片段明确陈述与基于片段作出的分析，说明依据和局限，不要虚构实验数据、参数或结论。
当状态为 [SYSTEM_STATUS: NO_RELEVANT_CHUNKS_MATCHED]：若问题是日常问候、生活建议或闲聊，用1至2句自然友好的话简短回应，并在句末提醒“如果有关于当前文档的问题，随时发给我来帮你分析。”若是纯外部专业事实问题，只礼貌说明“当前文档中未提及与该问题相关的信息”，不要使用外部知识补充。
当状态为 [SYSTEM_STATUS: NO_DOCUMENT_UPLOADED]：简洁提示用户先上传或选择需要分析的文档。

{history_section}参考文档：
{context}

问题：{query}

要求：
{requirements}

回答："""
    return prompt


def _join_neighbor_text(left: str, right: str) -> str:
    """Join adjacent chunks while removing an exact character overlap when present."""
    left = str(left or "").rstrip()
    right = str(right or "").lstrip()
    limit = min(len(left), len(right), 500)
    for overlap in range(limit, 0, -1):
        if left.endswith(right[:overlap]):
            return left + right[overlap:]
    return f"{left}\n\n{right}" if left and right else left or right


def expand_neighbor_chunks(docs: List[Dict], file_id: str = "", query: str = "") -> List[Dict]:
    """Expand the two leading retrieval hits with their next stored chunk for LLM context only."""
    if not docs or not file_id or _is_pure_summary_query(query):
        return list(docs)
    indexed = {}
    for doc in docs:
        meta = doc.get("meta") or {}
        try:
            indexed[int(meta["chunk_index"])] = doc
        except (KeyError, TypeError, ValueError):
            continue

    expanded: List[Dict] = []
    consumed = set()
    expanded_cores = 0
    for doc in docs:
        doc_id = doc.get("id")
        if doc_id in consumed:
            continue
        item = dict(doc)
        meta = item.get("meta") or {}
        try:
            chunk_index = int(meta["chunk_index"])
        except (KeyError, TypeError, ValueError):
            expanded.append(item)
            continue

        if expanded_cores < 3:
            expanded_cores += 1
            neighbor = indexed.get(chunk_index + 1)
            if neighbor is None:
                neighbor = chroma.get_neighbor_chunk(file_id, chunk_index + 1)
            if neighbor and neighbor.get("id") not in consumed and neighbor.get("id") != doc_id:
                item["text"] = _join_neighbor_text(item.get("text", ""), neighbor.get("text", ""))
                consumed.add(neighbor.get("id"))
        consumed.add(doc_id)
        expanded.append(item)
    return expanded


def _sync_source_text(sources, context_docs: List[Dict]) -> None:
    """Reflect expanded hit text in existing source cards without adding neighbor-only cards."""
    text_by_id = {doc.get("id"): str(doc.get("text", "") or "") for doc in context_docs}
    for source in sources or []:
        source_id = source.get("doc_id") if isinstance(source, dict) else getattr(source, "doc_id", None)
        if source_id not in text_by_id:
            continue
        if isinstance(source, dict):
            source["text"] = text_by_id[source_id]
        else:
            source.text = text_by_id[source_id]


def filter_relevant_docs(reranked: List[Dict]) -> List[Dict]:
    """Keep only documents meeting the configured reranker score threshold."""
    return [
        doc for doc in reranked
        if float(doc.get("rerank_score", doc.get("score", 0)) or 0) > 0.01
        and float(doc.get("rerank_score", doc.get("score", 0)) or 0) >= MIN_RELEVANCE_SCORE
    ]


def clean_citation_text(raw_text: str) -> Dict[str, str]:
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    title = ""
    match = re.match(r"^\s*#{1,6}\s*([^\n#]+)", text)
    if match:
        title = re.sub(r"\s*[（(]续[)）]\s*$", "", match.group(1)).strip()
    text = re.sub(r"#{1,6}\s*[^\n#]+?\s*[（(]续[)）]\s*", "\n", text)
    if title:
        text = re.sub(rf"^\s*#{{1,6}}\s*{re.escape(title)}\s*(?:[（(]续[)）])?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[（(]续[)）]", "", text)
    text = re.sub(r"[\s\n]+-\s+(?=[a-z])", " ", text)
    text = re.sub(r"[ \t]+-\s+(?=[A-Z\u4e00-\u9fa5])", "\n- ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"section_title": title, "text": text}


def _doc_score(doc: Dict) -> Optional[float]:
    raw_score = doc.get("rerank_score")
    if raw_score is None:
        raw_score = doc.get("score")
    return None if raw_score is None else float(raw_score)


def _source_page(meta: Dict) -> Optional[int]:
    """Return a 1-based page, normalizing chunks indexed before page_base existed."""
    raw_page = meta.get("page")
    if raw_page is None:
        return None
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    try:
        page_base = int(meta.get("page_base", 0) or 0)
    except (TypeError, ValueError):
        page_base = 0
    if page_base != 1:
        page += 1
    return max(1, page)


def format_source_docs(
    reranked: List[Dict],
    top_n: int = 2,
    *,
    apply_threshold: bool = True,
    overview_ids: Optional[set] = None,
) -> List[SourceDoc]:
    """
    格式化引用来源，只返回前 top_n 个

    Args:
        reranked: Rerank 后的文档列表
        top_n: 返回的数量（默认2个）
        apply_threshold: 是否套用 MIN_RELEVANCE_SCORE 阈值
        overview_ids: 文档首部概览片段的 id 集合；这些卡片标记为 overview 且不带分数

    Returns:
        结构化的引用列表
    """
    overview_ids = overview_ids or set()
    source_docs = []

    docs = filter_relevant_docs(reranked) if apply_threshold else list(reranked)
    docs = [
        doc for doc in docs
        if doc.get("id") in overview_ids or (_doc_score(doc) or 0) > 0.01
    ]
    for i, doc in enumerate(docs[:top_n]):
        doc_id = doc.get("id", "")
        is_overview = doc_id in overview_ids
        # Overview chunks never went through rerank; keeping the raw vector/BM25
        # score here would present similarity as a rerank confidence.
        relevance_score = None if is_overview else _doc_score(doc)

        text = str(doc.get('text', '') or '')
        abstract_match = re.search(r"\babstract\b|摘要", text, flags=re.IGNORECASE)
        if abstract_match and (
            abstract_match.start() > 400
            or _is_author_header_chunk(doc)
        ):
            text = text[abstract_match.start():]
        citation = clean_citation_text(text)
        text_preview = citation["text"]

        # 提取页码信息（如果有）；旧索引没有 page_base，按 0-based 归一化。
        meta = doc.get('meta', {})
        page = _source_page(meta)

        source_docs.append(SourceDoc(
            rank=i + 1,
            relevance_score=None if relevance_score is None else round(relevance_score, 4),
            text=text_preview,
            page=page,
            doc_id=doc_id,
            section_title=citation["section_title"] or None,
            source_kind="overview" if is_overview else "rerank",
        ))

    return source_docs


def _vector_search(req: ChatRequest, query_emb: List[float]) -> List[Dict]:
    started = time.perf_counter()
    if not req.use_vector:
        hits = []
    elif req.file_id:
        hits = chroma.query(query_emb, top_k=req.top_k, where={"file_id": req.file_id})
    else:
        hits = chroma.query(query_emb, top_k=req.top_k)
    print(f"向量检索耗时: {time.perf_counter() - started:.3f}s", flush=True)
    return hits


def _es_search(req: ChatRequest, query: Optional[str] = None) -> List[Dict]:
    started = time.perf_counter()
    search_query = query if query is not None else req.query
    es_enabled = settings.USE_ES
    if not req.use_es or not es_enabled:
        hits = []
    elif req.file_id:
        hits = es_search_text(
            settings.ES_INDEX, search_query, top_k=req.top_k,
            filter_query={"term": {"file_id": req.file_id}},
        )
    else:
        hits = es_search_text(settings.ES_INDEX, search_query, top_k=req.top_k)
    if es_enabled and req.use_es:
        print(f"ES 检索耗时: {time.perf_counter() - started:.3f}s", flush=True)
    return hits


def _keyword_search(req: ChatRequest, query: Optional[str] = None) -> List[Dict]:
    search_query = query if query is not None else req.query
    es_enabled = settings.USE_ES
    if es_enabled:
        es_hits = _es_search(req, search_query)
        max_score = max((float(hit.get("score", 0) or 0) for hit in es_hits), default=0.0)
        return [
            {
                "id": hit["id"],
                "text": hit.get("source", {}).get("text", ""),
                "meta": hit.get("source", {}).get("meta", {}),
                "score": float(hit.get("score", 0) or 0) / max_score if max_score else 0.0,
                "bm25_score": float(hit.get("score", 0) or 0) / max_score if max_score else 0.0,
            }
            for hit in es_hits
        ]
    started = time.perf_counter()
    documents = chroma.all_chunks(req.file_id or None)
    hits = bm25_search(
        search_query,
        documents,
        req.top_k,
        cache_key=req.file_id or "__all__",
    )
    print(f"关键字检索(BM25)耗时: {time.perf_counter() - started:.3f}s", flush=True)
    return hits


def _ranked_ids(hits: List[Dict]) -> List[str]:
    """Keep first occurrence so one retriever cannot double-count a duplicate id."""
    ranked: List[str] = []
    seen = set()
    for hit in hits:
        doc_id = hit.get("id")
        if doc_id and doc_id not in seen:
            seen.add(doc_id)
            ranked.append(doc_id)
    return ranked


def _merge_candidates(vector_hits: List[Dict], keyword_hits: List[Dict], top_k: int) -> List[Dict]:
    """Fuse vector and keyword rankings with RRF; keep raw scores for display."""
    candidates = {}
    for hit in vector_hits:
        candidates[hit["id"]] = {
            "id": hit["id"], "text": hit["meta"].get("text", ""),
            "meta": hit["meta"], "score": hit.get("score", 0),
            "vector_score": hit.get("score", 0),
        }
    for hit in keyword_hits:
        item = candidates.setdefault(hit["id"], {
            "id": hit["id"], "text": hit.get("text", ""), "meta": hit.get("meta", {}),
            "score": 0.0, "vector_score": 0.0,
        })
        item["bm25_score"] = hit.get("bm25_score", hit.get("score", 0.0))
        item["score"] = max(float(item.get("score", 0) or 0), float(item["bm25_score"] or 0))

    rrf_scores = rrf_fusion(
        [_ranked_ids(vector_hits), _ranked_ids(keyword_hits)],
        k=RRF_K,
    )
    for doc_id, item in candidates.items():
        item["rrf_score"] = rrf_scores.get(doc_id, 0.0)

    # RRF decides the order. `score` intentionally remains the larger raw
    # retrieval score so existing source-card confidence values do not become
    # tiny reciprocal-rank numbers.
    return sorted(
        candidates.values(),
        key=lambda item: (
            float(item.get("rrf_score", 0.0) or 0.0),
            float(item.get("vector_score", 0.0) or 0.0),
            float(item.get("bm25_score", 0.0) or 0.0),
        ),
        reverse=True,
    )[:max(top_k * 2, top_k)]


def _finalize_ranking(query: str, candidates: List[Dict], top_k: int):
    rerank_started = time.perf_counter()
    try:
        reranked = reranker.rerank(query, candidates, top_n=top_k) if candidates else []
        rerank_succeeded = True
    except Exception as exc:
        logger.warning("SiliconFlow rerank 失败，回退到 RRF 融合排序: %s", exc)
        reranked = candidates[:top_k]
        rerank_succeeded = False
    print(f"Rerank 耗时: {time.perf_counter() - rerank_started:.3f}s", flush=True)

    selected = filter_relevant_docs(reranked) if rerank_succeeded else reranked
    return selected, format_source_docs(selected, top_n=2, apply_threshold=False), reranked


def retrieve_and_rank(req: ChatRequest):
    """Synchronous retrieval path shared with the async streaming pipeline."""
    if is_chitchat_or_persona_query(req.query):
        return [], []
    if _is_pure_summary_query(req.query):
        first_chunks = chroma.first_chunks(req.file_id or None, limit=8)
        selected = _merge_overview_and_candidates(
            first_chunks, [], limit=8, query=req.query
        )
        overview_ids = {doc.get("id") for doc in first_chunks}
        return selected, format_source_docs(
            selected, top_n=2, apply_threshold=False, overview_ids=overview_ids
        )

    retrieval_query = contextualize_retrieval_query(
        req.query, getattr(req, "history", None)
    )
    search_query = _evaluation_search_query(retrieval_query)
    embedding_started = time.perf_counter()
    try:
        query_emb = emb_client.embed_texts([search_query])[0]
        vector_hits = _vector_search(req, query_emb)
        print(f"Embedding 耗时: {time.perf_counter() - embedding_started:.3f}s", flush=True)
    except Exception as exc:
        logger.warning("Embedding/向量检索失败，降级为 ES 与文档首部召回: %s", exc)
        vector_hits = []
    keyword_hits = _keyword_search(req, search_query)
    candidates = _merge_candidates(vector_hits, keyword_hits, req.top_k)
    selected, sources, reranked = _finalize_ranking(retrieval_query, candidates, req.top_k)
    if is_summary_query(req.query):
        first_chunks = chroma.first_chunks(req.file_id or None, limit=8)
        if first_chunks:
            overview_candidates = selected
            if _is_document_evaluation_query(req.query) and not overview_candidates:
                overview_candidates = reranked[:3] or candidates[:3]
            selected = _merge_overview_and_candidates(
                first_chunks, overview_candidates, limit=8, query=req.query
            )
            overview_ids = {doc.get("id") for doc in first_chunks}
            sources = format_source_docs(
                selected, top_n=2, apply_threshold=False, overview_ids=overview_ids
            )
    elif not selected:
        first_chunks = chroma.first_chunks(req.file_id or None, limit=2)
        selected = _merge_overview_and_candidates(
            first_chunks, candidates[:2], limit=4, use_overview_scores=False
        )
        sources = format_source_docs(selected, top_n=2, apply_threshold=True)
    return selected, sources


async def retrieve_and_rank_async(req: ChatRequest):
    """Async pipeline with parallel retrieval and a hard Rerank deadline."""
    if is_chitchat_or_persona_query(req.query):
        return [], []
    if _is_pure_summary_query(req.query):
        first_chunks_task = asyncio.create_task(asyncio.to_thread(
            chroma.first_chunks, req.file_id or None, 8
        ))
        first_chunks = await first_chunks_task
        selected = _merge_overview_and_candidates(
            first_chunks, [], limit=8, query=req.query
        )
        overview_ids = {doc.get("id") for doc in first_chunks}
        return selected, format_source_docs(
            selected, top_n=2, apply_threshold=False, overview_ids=overview_ids
        )

    retrieval_query = contextualize_retrieval_query(
        req.query, getattr(req, "history", None)
    )
    search_query = _evaluation_search_query(retrieval_query)
    embedding_started = time.perf_counter()
    if search_query == req.query:
        keyword_search_task = asyncio.create_task(asyncio.to_thread(_keyword_search, req))
    else:
        keyword_search_task = asyncio.create_task(asyncio.to_thread(_keyword_search, req, search_query))
    try:
        query_embs = await asyncio.to_thread(emb_client.embed_texts, [search_query])
        query_emb = query_embs[0]
        vector_hits = await asyncio.to_thread(_vector_search, req, query_emb)
        print(f"Embedding 耗时: {time.perf_counter() - embedding_started:.3f}s", flush=True)
    except Exception as exc:
        logger.warning("Embedding/向量检索失败，降级为 ES 与文档首部召回: %s", exc)
        vector_hits = []
    keyword_hits = await keyword_search_task
    candidates = _merge_candidates(vector_hits, keyword_hits, req.top_k)

    rerank_started = time.perf_counter()
    rerank_succeeded = False
    try:
        reranked = await asyncio.wait_for(
            asyncio.to_thread(reranker.rerank, retrieval_query, candidates, req.top_k),
            timeout=RERANK_TIMEOUT_SECONDS,
        ) if candidates else []
        rerank_succeeded = True
    except asyncio.TimeoutError:
        logger.warning("SiliconFlow rerank 超过 %.1f 秒，回退到 RRF 融合排序", RERANK_TIMEOUT_SECONDS)
        reranked = candidates[:req.top_k]
    except Exception as exc:
        logger.warning("SiliconFlow rerank 失败，回退到 RRF 融合排序: %s", exc)
        reranked = candidates[:req.top_k]
    print(f"Rerank 耗时: {time.perf_counter() - rerank_started:.3f}s", flush=True)

    selected = filter_relevant_docs(reranked) if rerank_succeeded else reranked
    overview_ids = None
    if is_summary_query(req.query):
        first_chunks = await asyncio.to_thread(chroma.first_chunks, req.file_id or None, 8)
        if first_chunks:
            overview_ids = {doc.get("id") for doc in first_chunks}
            overview_candidates = selected
            if _is_document_evaluation_query(req.query) and not overview_candidates:
                overview_candidates = reranked[:3] or candidates[:3]
            selected = _merge_overview_and_candidates(
                first_chunks, overview_candidates, limit=8, query=req.query
            )
    elif not selected:
        first_chunks = await asyncio.to_thread(chroma.first_chunks, req.file_id or None, 2)
        selected = _merge_overview_and_candidates(
            first_chunks, candidates[:2], limit=4, use_overview_scores=False
        )
    return selected, format_source_docs(
        selected,
        top_n=2,
        apply_threshold=not is_summary_query(req.query),
        overview_ids=overview_ids,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat_sync(req: ChatRequest):
    """同步聊天接口"""
    try:
        logger.info(f"📥 收到聊天请求: query={req.query}, file_id={req.file_id}")
        
        reranked, formatted_sources = retrieve_and_rank(req)
        
        # ✅ 6. 构建 Prompt（只使用前2个文档）
        prompt_docs = expand_neighbor_chunks(reranked[:5], req.file_id, req.query)
        _sync_source_text(formatted_sources, prompt_docs)
        prompt = build_prompt(req.query, prompt_docs, getattr(req, "history", None))
        
        # ✅ 7. 调用 LLM 生成答案
        answer_parts = []
        async for piece in stream_from_deepseek(prompt):
            answer_parts.append(piece)
        final_answer = "".join(answer_parts)
        
        logger.info(f"✅ 生成答案完成，长度: {len(final_answer)}")
        
        return {
            "answer": final_answer, 
            "source_docs": formatted_sources
        }
        
    except Exception as e:
        logger.error(f"❌ 聊天接口错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


@router.post("/chat/stream")
async def chat_stream_post(req: ChatRequest):
    """POST SSE endpoint matching the synchronous chat request body."""
    async def event_generator():
        logger.info(
            "📥 收到流式聊天请求: query=%s, file_id=%s, history_messages=%d",
            req.query, req.file_id, len(req.history),
        )
        # First event is emitted before any retrieval work begins.
        yield {"event": "status", "data": {"message": "正在检索相关文档..."}}
        try:
            relevant_docs, formatted_sources = await retrieve_and_rank_async(req)
            prompt_docs = expand_neighbor_chunks(relevant_docs[:5], req.file_id, req.query)
            _sync_source_text(formatted_sources, prompt_docs)
            prompt = build_prompt(req.query, prompt_docs, getattr(req, "history", None))
            yield {"event": "status", "data": {"message": "正在思考并生成回答..."}}

            deepseek_started = time.perf_counter()
            first_piece = True
            async for piece in stream_from_deepseek(prompt):
                if first_piece:
                    print(
                        f"DeepSeek 首包耗时: {time.perf_counter() - deepseek_started:.3f}s",
                        flush=True,
                    )
                    first_piece = False
                yield {"event": "chunk", "data": {"text": piece}}

            if formatted_sources:
                yield {"event": "sources", "data": {"source_docs": formatted_sources}}
            yield {"event": "done", "data": {"status": "done"}}
        except asyncio.CancelledError:
            logger.info("SSE 客户端断开，已取消聊天生成")
            raise
        except Exception as exc:
            logger.exception("流式聊天处理失败")
            yield {"event": "error", "data": {"message": str(exc)}}

    return await sse_response(event_generator())


@router.get("/chat/stream")
async def chat_stream(
    request: Request, 
    file_id: Optional[str] = Query(default=None, max_length=64),
    query: str = Query(default="", max_length=MAX_QUERY_LENGTH),
    top_k: int = Query(default=5, ge=1, le=MAX_TOP_K),
):
    """
    SSE 流式接口
    Query params: file_id (可选), query, top_k (1-20)
    """
    try:
        req = ChatRequest(file_id=file_id or "", query=query, top_k=top_k)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="query 不能为空") from exc

    try:
        logger.info(f"📥 收到流式请求: query={query}, file_id={file_id}")
        relevant_docs, formatted_sources = await retrieve_and_rank_async(req)
        prompt_docs = expand_neighbor_chunks(relevant_docs[:5], req.file_id, req.query)
        _sync_source_text(formatted_sources, prompt_docs)
        prompt = build_prompt(req.query, prompt_docs, getattr(req, "history", None))
        generator = stream_from_deepseek(prompt)
        return await sse_response(generator, formatted_sources)
        
    except Exception as e:
        logger.error(f"❌ 流式接口错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
