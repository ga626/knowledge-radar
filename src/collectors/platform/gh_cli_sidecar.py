"""GitHub 仓库发现；兼容原导入入口，gh/REST 共享同一只读合同。"""
from __future__ import annotations

import os
import re
import time

from collectors.platform.github_api import CONTRACT, GitHubClient, GitHubError, _gh_exe, enabled
from kr_core.models import utc_now_iso
from runtime.cost_latency import TTLCache, attach_runtime_metadata, budget_envelope, stable_key
from runtime.execution_context import check_execution

_SEARCH_CACHE = TTLCache('github.search', ttl_s=300, max_items=128)


def health(*, stale_ok=False, force_refresh=False):
    """轻量配置检查不验证账号、不搜索；业务能力以真实调用为准。"""
    transport = os.environ.get('KR_GITHUB_TRANSPORT', 'auto')
    configured = enabled('KR_GITHUB_ENABLED') and transport in {'auto', 'rest', 'gh'}
    if transport == 'gh':
        configured = configured and enabled('KR_GH_CLI_ENABLED') and bool(_gh_exe())
    return {'schema': 'knowledgeradar-github-health/v2', 'status': 'ok' if configured else 'degraded',
            'enabled': enabled('KR_GITHUB_ENABLED'), 'configured': configured, 'available': configured,
            'transport': transport, 'authentication': 'resolved_at_request_start',
            'business_verified': False, 'detail': 'Configured read-only API transport; no search or authentication probe performed.',
            'failure_code': '' if configured else 'PROVIDER_UNAVAILABLE'}


def _normalize_repo(item, provider):
    full_name = str(item.get('full_name') or '')
    if item.get('private') is not False or item.get('visibility', 'public') != 'public':
        raise GitHubError('PRIVATE_SCOPE_NOT_AUTHORIZED', 'Private repository results are outside this public research scope.')
    return {'title': full_name, 'url': item.get('html_url') or 'https://github.com/' + full_name,
            'snippet': str(item.get('description') or ''), 'source_provider': provider,
            'published_at': '', 'retrieved_at': utc_now_iso(), 'score': item.get('stargazers_count'),
            'content_type': 'github_repository',
            'content_entrypoints': {'overview': 'https://github.com/' + full_name,
                'readme': {'tool': 'get_content_detail', 'arguments': {'url': 'https://github.com/' + full_name, 'github': {'kind': 'readme'}}},
                'issues': 'https://github.com/' + full_name + '/issues'},
            'raw': {'full_name': full_name, 'repository_id': item.get('id'), 'language': item.get('language'),
                    'stargazers_count': item.get('stargazers_count'), 'updated_at': item.get('updated_at'),
                    'default_branch': item.get('default_branch'), 'license': (item.get('license') or {}).get('spdx_id'),
                    'visibility': item.get('visibility', 'public'), 'source': 'github_repository_api',
                    'provider_provenance': {'provider_id': 'github', 'object_kind': 'repository',
                        'repository_id': item.get('id'), 'repository': full_name, 'public_only': True}}}


def search_repositories(query, *, limit=5, page=1, refresh=False):
    check_execution()
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise GitHubError('INVALID_QUERY', 'Provide a GitHub query of 1–512 characters; qualifiers are preserved.')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise GitHubError('INVALID_ARGUMENTS', 'limit must be an integer from 1 to 20.')
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page * limit > 1000:
        raise GitHubError('INVALID_ARGUMENTS', 'page must stay within GitHub search\'s first 1000 results; narrow the query instead.')
    if re.search(r'(?<![\w"\'])\b(?:is|visibility):(?:private|internal)\b', query, re.I):
        raise GitHubError('PRIVATE_SCOPE_NOT_AUTHORIZED', 'Private/internal repository research requires separate authorization.')
    started = time.time()
    client = GitHubClient.configured()
    key = stable_key(CONTRACT, query, limit, page, client.identity, client.transport)
    if not refresh:
        cached = _SEARCH_CACHE.get(key, allow_stale=False)
        if cached is not None:
            cached.setdefault('metadata', {})['network_reverified'] = False
            return cached
    # A visible authorization boundary, not thematic expansion or ranking policy.
    effective = query if re.search(r'\bis:public\b', query, re.I) else query + ' is:public'
    payload, headers = client.json('search/repositories', params={'q': effective, 'per_page': limit, 'page': page})
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise GitHubError('SCHEMA_CHANGED', 'Repository search response lacks an items array.')
    items = [_normalize_repo(row, 'github_' + client.transport) for row in payload['items']]
    total_count = payload.get('total_count')
    incomplete = payload.get('incomplete_results') is not False
    more = bool(re.search(r'<[^>]+>;\s*rel="next"', headers.get('link', '')))
    next_page = page + 1 if more and page * limit < 1000 else None
    result = {'query': query, 'provider': 'github', 'platform': 'GitHub', 'status': 'partial' if incomplete else 'ok',
        'items': items, 'total': len(items), 'total_count': total_count, 'page': page,
        'incomplete_results': incomplete, 'next_page': next_page,
        'coverage': {'returned': len(items), 'pagination_exhausted': not more,
                     'search_window_limit': 1000, 'all_github_repositories_exhausted': False},
        'fallback_used': False, 'attempted_providers': ['github_' + client.transport],
        'metadata': {'strategy': 'github_readonly_api', 'transport': client.transport,
            'scope': 'public_only', 'effective_query': effective, 'query_rewritten': False,
            'authorization_filter_applied': effective != query, 'upstream_order_preserved': True,
            'attempts': client.attempts, 'attempted_queries_count': 1, 'network_reverified': True,
            'contract': CONTRACT}}
    cache = _SEARCH_CACHE.set(key, result)
    return attach_runtime_metadata(result, tool_name='search_github_repositories', capability_id='github.search',
        started=started, budget=budget_envelope('balanced'), cache=cache)
