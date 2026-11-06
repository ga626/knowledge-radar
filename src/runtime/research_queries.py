"""查询族只描述可执行意图；未知生态保留为机会，不按套餐关闭。"""
from __future__ import annotations
import hashlib
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ResearchQuery(BaseModel):
    """执行者查询族；枚举和约束直接投影到MCP/CLI同源合同。"""
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500, pattern=r'\S')
    intent: Literal['discovery', 'counterevidence', 'implementation', 'experience',
                    'citation_follow', 'gap_follow', 'pagination']
    language: str = Field(default='unknown', max_length=24)
    source_ecology: str = '*'
    family_id: str = ''
    parent_event_id: str = ''
    gap_id: str = ''


def query_families(objective, supplied=None):
    if supplied is None:
        # 默认只提出一个原语种草案；不伪造翻译或已完成跨语扩搜。
        query = re.sub(r'\s+', ' ', str(objective)).strip()[:240]
        return [{'family_id': 'query-' + hashlib.sha256(query.encode()).hexdigest()[:16], 'query': query, 'intent': 'discovery', 'language': 'zh' if re.search('[一-鿿]', query) else 'en', 'source_ecology': '*', 'parent_event_id': '', 'gap_id': '', 'origin': 'agent_draft', 'translation_verified': False}]
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 40:
        raise ValueError('invalid_query_families')
    result = []
    for row in supplied:
        if isinstance(row, ResearchQuery):
            row = row.model_dump()
        if not isinstance(row, dict):
            raise ValueError('invalid_query_family')
        query = str(row.get('query') or '').strip()
        if not query or len(query) > 500:
            raise ValueError('query_must_be_1_to_500_characters')
        if row.get('intent') not in {'discovery', 'counterevidence', 'implementation', 'experience', 'citation_follow', 'gap_follow', 'pagination'}:
            raise ValueError('query_intent_required')
        result.append({'family_id': str(row.get('family_id') or 'query-' + hashlib.sha256((query+'|'+str(row.get('source_ecology'))+'|'+str(row.get('intent'))).encode()).hexdigest()[:16]), 'query': query, 'intent': row['intent'], 'language': str(row.get('language') or 'unknown')[:24], 'source_ecology': str(row.get('source_ecology') or '*'), 'parent_event_id': str(row.get('parent_event_id') or ''), 'gap_id': str(row.get('gap_id') or ''), 'origin': 'executor_supplied', 'translation_verified': False})
    return result


def query_for_ecology(families, ecology):
    return next((row for row in families if row['source_ecology'] == ecology), next((row for row in families if row['source_ecology'] == '*'), families[0]))
