# app/services/es_retriever.py

from elasticsearch import Elasticsearch
from app.core.config import settings
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

def es_search_text(
    index: str, 
    query: str, 
    top_k: int = 5,
    filter_query: Optional[Dict] = None  # ✅ 添加 filter_query 参数
) -> List[Dict]:
    """
    Elasticsearch 全文检索
    
    Args:
        index: 索引名称
        query: 查询文本
        top_k: 返回结果数量
        filter_query: 过滤条件，例如 {"term": {"file_id": "xxx"}}
        
    Returns:
        [{"id": "xxx", "source": {...}, "score": 0.95}, ...]
    """
    try:
        es = Elasticsearch([settings.ES_HOST])
        
        # ✅ 构建查询体
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"text": query}}
                    ]
                }
            },
            "size": top_k
        }
        
        # ✅ 如果有过滤条件，添加到 bool 查询中
        if filter_query:
            body["query"]["bool"]["filter"] = [filter_query]
            logger.info(f"🔍 ES 使用过滤条件: {filter_query}")
        
        res = es.search(index=index, body=body)
        
        hits = []
        for hit in res['hits']['hits']:
            hits.append({
                'id': hit['_id'],
                'source': hit['_source'],
                'score': hit['_score']
            })
        
        logger.info(f"✅ ES 检索完成，返回 {len(hits)} 个结果")
        return hits
        
    except Exception as e:
        logger.error(f"❌ ES 检索失败: {e}", exc_info=True)
        return []


def rrf_fusion(ranked_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    """
    RRF (Reciprocal Rank Fusion) 融合算法
    
    Args:
        ranked_lists: 多个排序列表 [["id1", "id2"], ["id3", "id1"]]
        k: RRF 常数
        
    Returns:
        {"id1": 0.95, "id2": 0.85, ...}
    """
    scores = {}
    
    for ranked_list in ranked_lists:
        for rank, doc_id in enumerate(ranked_list, start=1):
            if doc_id not in scores:
                scores[doc_id] = 0
            scores[doc_id] += 1 / (k + rank)
    
    return scores