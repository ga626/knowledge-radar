"""显式搜索约束；后过滤不冒充上游召回保证。"""
from dataclasses import replace
from urllib.parse import urlsplit
import re

from .catalog import SEARCH_CATALOG


def prepare_request(request):
    if not request.query.strip() or request.freshness not in {'','day','week','month','year'}:
        raise ValueError('query is required; freshness must be day, week, month, year or empty')
    options = dict(request.options)
    for key in ('include_domains', 'exclude_domains'):
        domains = options.get(key) or []
        if not isinstance(domains, list) or len(domains) > 20:
            raise ValueError(key+' must be a list of up to 20 host names')
        normalized = []
        for host in domains:
            if not isinstance(host, str) or not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or '.' not in host:
                raise ValueError(key+' contains an invalid domain')
            normalized.append(host.lower().rstrip('.'))
        options[key] = normalized
    return replace(request, query=request.query.strip(), limit=max(1,min(20,request.limit)), options=options)


def constraint_report(request, provider, *, sent=False):
    capabilities = SEARCH_CATALOG.get(provider.split(':')[0], {}).get('capabilities', {})
    out = {}
    for name, value in [('language', request.language), ('freshness', request.freshness),
                        ('domains', request.options.get('include_domains') or request.options.get('exclude_domains'))]:
        if value:
            support = capabilities.get(name, 'unsupported')
            out[name] = {'requested': value, 'method': support,
                'sent': value if sent and support not in {'unsupported', 'post_filter'} else None,
                'provider_supported': support, 'observed': 'not_evaluated', 'verified': False,
                'recall_guaranteed': False, 'hard_constraint_satisfied': False}
    return out


def filter_results(request, items):
    includes = request.options.get('include_domains') or []
    excludes = request.options.get('exclude_domains') or []
    def matches(host, domains):
        return any(host == d or host.endswith('.'+d) for d in domains)
    filtered = []
    for item in items:
        try:
            host = (urlsplit(item.url).hostname or '').lower()
        except ValueError:
            continue
        if includes and not matches(host, includes) or excludes and matches(host, excludes):
            continue
        filtered.append(item)
    return filtered
