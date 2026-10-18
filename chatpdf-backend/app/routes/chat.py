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

logger = logging.getLogger(__name__)

router = APIRouter()
chroma = ChromaClient()
emb_client = EmbeddingClient()
reranker = CohereReranker()

def build_prompt(query: str, docs: List[Dict]) -> str:
    """构建 LLM Prompt - 不再包含 Source ID"""
    if not docs:
        context = "没有找到相关文档。"
    else:
        # 只传递文本内容，不传递 ID
        context = "\n\n---\n".join([
            f"文档片段 {i+1}:\n{d.get('text', '')}" 
            for i, d in enumerate(docs[:2])  # 只使用前2个
        ])
    
    prompt = f"""你是一个专业的AI助手。请基于以下文档内容回答问题，用中文回答。

参考文档：
{context}

问题：{query}

要求：
1. 直接回答问题，不要在回答中添加"Source ID"或引用标记
2. 用清晰、专业的语言组织答案
3. 如果文档中没有相关信息，请诚实说明

回答："""
    return prompt


def format_source_docs(reranked: List[Dict], top_n: int = 2) -> List[SourceDoc]:
    """
    格式化引用来源，只返回前 top_n 个
    
    Args:
        reranked: Rerank 后的文档列表
        top_n: 返回的数量（默认2个）
        
    Returns:
        结构化的引用列表
    """
    source_docs = []
    
    for i, doc in enumerate(reranked[:top_n]):
        # 提取相关度分数
        relevance_score = doc.get('rerank_score', 0)
        
        # 提取文本内容（截取前200字符作为摘要）
        text = doc.get('text', '')
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


@router.post("/chat", response_model=ChatResponse)
async def chat_sync(req: ChatRequest):
    """同步聊天接口"""
    try:
        logger.info(f"📥 收到聊天请求: query={req.query}, file_id={req.file_id}")
        
        # ✅ 1. 生成查询向量
        query_emb = emb_client.embed_texts([req.query])[0]
        
        # ✅ 2. 使用 where 参数过滤
        if req.file_id:
            logger.info(f"🔍 检索文件: {req.file_id}")
            
            vector_hits = chroma.query(
                query_emb, 
                top_k=req.top_k,
                where={"file_id": req.file_id}
            ) if req.use_vector else []
            
            es_hits = es_search_text(
                settings.ES_INDEX, 
                req.query, 
                top_k=req.top_k,
                filter_query={"term": {"file_id": req.file_id}}
            ) if req.use_es else []
        else:
            logger.info("🔍 检索所有文档")
            
            vector_hits = chroma.query(
                query_emb, 
                top_k=req.top_k
            ) if req.use_vector else []
            
            es_hits = es_search_text(
                settings.ES_INDEX, 
                req.query, 
                top_k=req.top_k
            ) if req.use_es else []
        
        logger.info(f"📊 检索结果: vector={len(vector_hits)}, es={len(es_hits)}")
        
        # ✅ 3. RRF 融合排序
        ranked_lists = []
        if vector_hits:
            ranked_lists.append([h['id'] for h in vector_hits])
        if es_hits:
            ranked_lists.append([h['id'] for h in es_hits])
        
        fused_scores = rrf_fusion(ranked_lists) if ranked_lists else {}
        
        # ✅ 4. 收集候选文档
        candidates = {}
        for h in vector_hits:
            candidates[h['id']] = {
                "id": h['id'], 
                "text": h['meta'].get('text', ''),
                "meta": h['meta'],  # 保留完整的 meta 信息
                "score": fused_scores.get(h['id'], 0)
            }
        for h in es_hits:
            candidates[h['id']] = {
                "id": h['id'], 
                "text": h['source'].get('text', ''),
                "meta": h['source'].get('meta', {}),
                "score": fused_scores.get(h['id'], 0)
            }
        
        cand_list = sorted(
            list(candidates.values()), 
            key=lambda x: x['score'], 
            reverse=True
        )[:req.top_k]
        
        logger.info(f"📑 候选文档数量: {len(cand_list)}")
        
        # ✅ 5. Cohere Rerank
        if cand_list:
            reranked = reranker.rerank(req.query, cand_list, top_n=req.top_k)
            logger.info(f"🔄 Rerank 后文档数量: {len(reranked)}")
        else:
            reranked = []
            logger.warning("⚠️ 没有找到相关文档")
        
        # ✅ 6. 构建 Prompt（只使用前2个文档）
        prompt = build_prompt(req.query, reranked[:2])
        
        # ✅ 7. 调用 LLM 生成答案
        answer_parts = []
        async for piece in stream_from_deepseek(prompt):
            answer_parts.append(piece)
        final_answer = "".join(answer_parts)
        
        logger.info(f"✅ 生成答案完成，长度: {len(final_answer)}")
        
        # ✅ 8. 格式化引用来源（只返回前2个）
        formatted_sources = format_source_docs(reranked, top_n=2)
        
        return {
            "answer": final_answer, 
            "source_docs": formatted_sources
        }
        
    except Exception as e:
        logger.error(f"❌ 聊天接口错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


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
        
        query_emb = emb_client.embed_texts([query])[0]
        
        if file_id:
            vector_hits = chroma.query(
                query_emb, 
                top_k=top_k,
                where={"file_id": file_id}
            )
            es_hits = es_search_text(
                settings.ES_INDEX, 
                query, 
                top_k=top_k,
                filter_query={"term": {"file_id": file_id}}
            )
        else:
            vector_hits = chroma.query(query_emb, top_k=top_k)
            es_hits = es_search_text(settings.ES_INDEX, query, top_k=top_k)
        
        ranked_lists = [
            [h['id'] for h in vector_hits], 
            [h['id'] for h in es_hits]
        ]
        fused = rrf_fusion(ranked_lists)
        
        candidates = {}
        for h in vector_hits:
            candidates[h['id']] = {
                "id": h['id'], 
                "text": h['meta'].get('text', ''),
                "meta": h['meta']
            }
        for h in es_hits:
            candidates[h['id']] = {
                "id": h['id'], 
                "text": h['source'].get('text', ''),
                "meta": h['source'].get('meta', {})
            }
        
        cand_list = sorted(
            list(candidates.values()), 
            key=lambda x: fused.get(x['id'], 0), 
            reverse=True
        )[:top_k]
        
        reranked = reranker.rerank(query, cand_list, top_n=top_k)
        prompt = build_prompt(query, reranked[:2])
        
        generator = stream_from_deepseek(prompt)
        return await sse_response(generator)
        
    except Exception as e:
        logger.error(f"❌ 流式接口错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")