# app/routes/chat.py
from fastapi import APIRouter, HTTPException, Request
from app.models import ChatRequest, ChatResponse, SourceDoc
from app.services.embeddings import EmbeddingClient
from app.services.vectorstore import ChromaClient
from app.services.es_retriever import es_search_text, rrf_fusion
from app.core.config import settings
from app.services.reranker import CohereReranker
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
reranker = CohereReranker()
MIN_RELEVANCE_SCORE = float(os.getenv("MIN_RELEVANCE_SCORE", "0.30"))
RERANK_TIMEOUT_SECONDS = 3.5


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

    persona_terms = (
        "你自己", "你是谁", "你叫什么", "你能做什么", "你的功能", "你是什么模型",
        "介绍一下你", "介绍下你", "你是干嘛的", "你是做什么的",
    )
    casual_terms = (
        "你好", "您好", "早上好", "晚上好", "嗨", "hello", "hi", "今天吃什么",
        "吃什么好", "吃什么", "天气怎么样", "今天天气", "讲个笑话", "谢谢", "再见",
        "在吗", "晚安",
    )
    return any(term.casefold() in normalized for term in persona_terms + casual_terms)


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
    baseline_scores = (0.98, 0.95, 0.92, 0.90, 0.88)
    scored = []
    for index, doc in enumerate(docs):
        item = dict(doc)
        score = item.get("rerank_score")
        is_overview_chunk = overview_ids is None or item.get("id") in overview_ids
        if is_overview_chunk and (score is None or float(score or 0) <= 0):
            item["rerank_score"] = (
                0.50 if _is_author_header_chunk(item)
                else baseline_scores[min(index, len(baseline_scores) - 1)]
            )
        scored.append(item)
    return scored


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
    ranked_candidates = sorted(
        candidates,
        key=lambda doc: float(doc.get("rerank_score", doc.get("score", 0)) or 0),
        reverse=True,
    )
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

def build_prompt(query: str, docs: List[Dict]) -> str:
    """构建 LLM Prompt - 不再包含 Source ID"""
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
    
    prompt = f"""你是一个专业的文档问答助手，请用中文回答。

内部状态标记仅供你判断回答方式，严禁在回答中复述内部标记、系统状态或内部上下文提示。
当状态为 [SYSTEM_STATUS: CHITCHAT_OR_PERSONA_QUERY]：直接自然友好地回答问候或关于你自身身份、能力的问题，不要引用或提及文档，也不要附加文档引导。
有参考文档片段时，严格依据片段回答；概述类问题应综合这些片段概括文档核心内容，不得编造片段未支持的事实。
主观评价、价值判断、贡献、局限、适用人群等文档级分析，应基于片段中的背景、问题、方法、结论等进行有依据的综合评估；不得因文档没有直接写“有价值”而拒答。区分片段明确陈述与基于片段作出的分析，说明依据和局限，不要虚构实验数据、参数或结论。
当状态为 [SYSTEM_STATUS: NO_RELEVANT_CHUNKS_MATCHED]：若问题是日常问候、生活建议或闲聊，用1至2句自然友好的话简短回应，并在句末提醒“如果有关于当前文档的问题，随时发给我来帮你分析。”若是纯外部专业事实问题，只礼貌说明“当前文档中未提及与该问题相关的信息”，不要使用外部知识补充。
当状态为 [SYSTEM_STATUS: NO_DOCUMENT_UPLOADED]：简洁提示用户先上传或选择需要分析的文档。

参考文档：
{context}

问题：{query}

要求：
1. 直接回答问题，不要在回答中添加"Source ID"或引用标记
2. 用清晰、专业的语言组织答案
3. 遵守上述无匹配时的闲聊与文档问答分流规则

回答："""
    return prompt


def filter_relevant_docs(reranked: List[Dict]) -> List[Dict]:
    """Keep only documents meeting the configured reranker score threshold."""
    return [
        doc for doc in reranked
        if float(doc.get("rerank_score", 0) or 0) >= MIN_RELEVANCE_SCORE
    ]


def format_source_docs(
    reranked: List[Dict], top_n: int = 2, *, apply_threshold: bool = True
) -> List[SourceDoc]:
    """
    格式化引用来源，只返回前 top_n 个
    
    Args:
        reranked: Rerank 后的文档列表
        top_n: 返回的数量（默认2个）
        
    Returns:
        结构化的引用列表
    """
    source_docs = []
    
    docs = filter_relevant_docs(reranked) if apply_threshold else reranked
    for i, doc in enumerate(docs[:top_n]):
        # 提取相关度分数
        raw_score = doc.get("rerank_score")
        if raw_score is None:
            raw_score = doc.get("score")
        relevance_score = float(raw_score or 0.0)
        
        # 提取文本内容（截取前200字符作为摘要）
        text = str(doc.get('text', '') or '')
        abstract_match = re.search(r"\babstract\b|摘要", text, flags=re.IGNORECASE)
        if abstract_match and (
            abstract_match.start() > 400
            or _is_author_header_chunk(doc)
        ):
            text = text[abstract_match.start():]
        text_preview = text[:200] + '...' if len(text) > 200 else text
        
        # 提取页码信息（如果有）
        meta = doc.get('meta', {})
        page = meta.get('page')
        
        # 提取文档ID
        doc_id = doc.get('id', '')
        
        source_docs.append(SourceDoc(
            rank=i + 1,
            relevance_score=round(relevance_score, 4),  # 保留4位小数
            text=text_preview,
            page=page,
            doc_id=doc_id
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
    if not req.use_es:
        hits = []
    elif req.file_id:
        hits = es_search_text(
            settings.ES_INDEX, search_query, top_k=req.top_k,
            filter_query={"term": {"file_id": req.file_id}},
        )
    else:
        hits = es_search_text(settings.ES_INDEX, search_query, top_k=req.top_k)
    print(f"ES 检索耗时: {time.perf_counter() - started:.3f}s", flush=True)
    return hits


def _merge_candidates(vector_hits: List[Dict], es_hits: List[Dict], top_k: int) -> List[Dict]:
    ranked_lists = []
    if vector_hits:
        ranked_lists.append([hit["id"] for hit in vector_hits])
    if es_hits:
        ranked_lists.append([hit["id"] for hit in es_hits])
    fused_scores = rrf_fusion(ranked_lists) if ranked_lists else {}

    candidates = {}
    for hit in vector_hits:
        candidates[hit["id"]] = {
            "id": hit["id"], "text": hit["meta"].get("text", ""),
            "meta": hit["meta"], "score": fused_scores.get(hit["id"], 0),
        }
    for hit in es_hits:
        candidates[hit["id"]] = {
            "id": hit["id"], "text": hit["source"].get("text", ""),
            "meta": hit["source"].get("meta", {}),
            "score": fused_scores.get(hit["id"], 0),
        }
    return sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:top_k]


def _finalize_ranking(query: str, candidates: List[Dict], top_k: int):
    rerank_started = time.perf_counter()
    try:
        reranked = reranker.rerank(query, candidates, top_n=top_k) if candidates else []
        rerank_succeeded = True
    except Exception as exc:
        logger.warning("Cohere rerank 失败，回退到 RRF 排序: %s", exc)
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
        es_hits = _es_search(req)
        candidates = _merge_candidates([], es_hits, req.top_k)
        selected = _merge_overview_and_candidates(
            first_chunks, candidates, limit=8, query=req.query
        )
        return selected, format_source_docs(selected, top_n=2, apply_threshold=False)

    search_query = _evaluation_search_query(req.query)
    embedding_started = time.perf_counter()
    try:
        query_emb = emb_client.embed_texts([search_query])[0]
        vector_hits = _vector_search(req, query_emb)
        print(f"Embedding 耗时: {time.perf_counter() - embedding_started:.3f}s", flush=True)
    except Exception as exc:
        logger.warning("Embedding/向量检索失败，降级为 ES 与文档首部召回: %s", exc)
        vector_hits = []
    es_hits = _es_search(req, search_query) if search_query != req.query else _es_search(req)
    candidates = _merge_candidates(vector_hits, es_hits, req.top_k)
    selected, sources, reranked = _finalize_ranking(req.query, candidates, req.top_k)
    if is_summary_query(req.query):
        first_chunks = chroma.first_chunks(req.file_id or None, limit=8)
        if first_chunks:
            overview_candidates = selected
            if _is_document_evaluation_query(req.query) and not overview_candidates:
                overview_candidates = reranked[:3] or candidates[:3]
            selected = _merge_overview_and_candidates(
                first_chunks, overview_candidates, limit=8, query=req.query
            )
            sources = format_source_docs(selected, top_n=2, apply_threshold=False)
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
        es_hits = await asyncio.to_thread(_es_search, req)
        first_chunks = await first_chunks_task
        candidates = _merge_candidates([], es_hits, req.top_k)
        selected = _merge_overview_and_candidates(
            first_chunks, candidates, limit=8, query=req.query
        )
        return selected, format_source_docs(selected, top_n=2, apply_threshold=False)

    search_query = _evaluation_search_query(req.query)
    embedding_started = time.perf_counter()
    if search_query == req.query:
        es_search_task = asyncio.create_task(asyncio.to_thread(_es_search, req))
    else:
        es_search_task = asyncio.create_task(asyncio.to_thread(_es_search, req, search_query))
    try:
        query_embs = await asyncio.to_thread(emb_client.embed_texts, [search_query])
        query_emb = query_embs[0]
        vector_hits = await asyncio.to_thread(_vector_search, req, query_emb)
        print(f"Embedding 耗时: {time.perf_counter() - embedding_started:.3f}s", flush=True)
    except Exception as exc:
        logger.warning("Embedding/向量检索失败，降级为 ES 与文档首部召回: %s", exc)
        vector_hits = []
    es_hits = await es_search_task
    candidates = _merge_candidates(vector_hits, es_hits, req.top_k)

    rerank_started = time.perf_counter()
    rerank_succeeded = False
    try:
        reranked = await asyncio.wait_for(
            asyncio.to_thread(reranker.rerank, req.query, candidates, req.top_k),
            timeout=RERANK_TIMEOUT_SECONDS,
        ) if candidates else []
        rerank_succeeded = True
    except asyncio.TimeoutError:
        logger.warning("Cohere rerank 超过 %.1f 秒，回退到 RRF 排序", RERANK_TIMEOUT_SECONDS)
        reranked = candidates[:req.top_k]
    except Exception as exc:
        logger.warning("Cohere rerank 失败，回退到 RRF 排序: %s", exc)
        reranked = candidates[:req.top_k]
    print(f"Rerank 耗时: {time.perf_counter() - rerank_started:.3f}s", flush=True)

    selected = filter_relevant_docs(reranked) if rerank_succeeded else reranked
    if is_summary_query(req.query):
        first_chunks = await asyncio.to_thread(chroma.first_chunks, req.file_id or None, 8)
        if first_chunks:
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
        selected, top_n=2, apply_threshold=not is_summary_query(req.query)
    )


@router.post("/chat", response_model=ChatResponse)
async def chat_sync(req: ChatRequest):
    """同步聊天接口"""
    try:
        logger.info(f"📥 收到聊天请求: query={req.query}, file_id={req.file_id}")
        
        reranked, formatted_sources = retrieve_and_rank(req)
        
        # ✅ 6. 构建 Prompt（只使用前2个文档）
        prompt = build_prompt(req.query, reranked[:5])
        
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
        # First event is emitted before any retrieval work begins.
        yield {"event": "status", "data": {"message": "正在检索相关文档..."}}
        try:
            relevant_docs, formatted_sources = await retrieve_and_rank_async(req)
            prompt = build_prompt(req.query, relevant_docs[:5])
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
    file_id: Optional[str] = None,
    query: str = "", 
    top_k: int = 5
):
    """
    SSE 流式接口
    Query params: file_id (可选), query, top_k
    """
    try:
        logger.info(f"📥 收到流式请求: query={query}, file_id={file_id}")
        req = ChatRequest(file_id=file_id or "", query=query, top_k=top_k)
        relevant_docs, formatted_sources = await retrieve_and_rank_async(req)
        prompt = build_prompt(query, relevant_docs[:5])
        generator = stream_from_deepseek(prompt)
        return await sse_response(generator, formatted_sources)
        
    except Exception as e:
        logger.error(f"❌ 流式接口错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")
