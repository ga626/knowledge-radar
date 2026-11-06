"""原sidecar入口的回归：不再用隐藏扩词/错误后换身份“兜底”。"""
import pytest
from collectors.platform import gh_cli_sidecar as search
from collectors.platform.github_api import GitHubError
from runtime.cost_latency import TTLCache


class Client:
    transport='rest'
    identity='account-a'
    attempts=[]
    def __init__(self):
        self.calls=[]
        self.rows=[{'id':1, 'full_name':'o/weather', 'private':False, 'description':'Weather parser', 'stargazers_count':0},
                   {'id':2, 'full_name':'o/research', 'private':False, 'description':'deep research autonomous agent report generation', 'stargazers_count':99999}]
    def json(self, endpoint, *, params):
        self.calls.append((endpoint,params))
        return {'items':self.rows, 'total_count':200, 'incomplete_results':False}, {'link':'<https://api.github.com/search/repositories?page=2>; rel="next"'}


@pytest.fixture
def client(monkeypatch):
    value=Client()
    monkeypatch.setattr(search.GitHubClient, 'configured', lambda **kw: value)
    monkeypatch.setattr(search, '_SEARCH_CACHE', TTLCache('test', ttl_s=300))
    return value


def test_query_qualifiers_and_upstream_order_are_preserved(client):
    query='org:o language:Python "error handling"'
    result=search.search_repositories(query, limit=2)
    assert len(client.calls)==1
    assert client.calls[0][1]['q']==query+' is:public'
    assert result['items'][0]['title']=='o/weather'
    assert result['metadata']['query_rewritten'] is False
    assert result['metadata']['upstream_order_preserved'] is True
    assert result['items'][0]['content_entrypoints']['readme']['arguments']['github']['kind']=='readme'


@pytest.mark.parametrize('query', ['中文 数据库', 'pentest vulnerability', 'job search', 'awesome skills'])
def test_no_query_theme_or_language_bias(client, query):
    result=search.search_repositories(query, limit=2)
    assert result['items'][0]['title']=='o/weather'
    assert len(client.calls)==1 and result['query']==query


def test_page_and_source_limit_are_explicit(client):
    result=search.search_repositories('weather', limit=2, page=2)
    assert client.calls[0][1]['page']==2
    assert result['next_page']==3 and result['total_count']==200
    assert result['coverage']['all_github_repositories_exhausted'] is False
    with pytest.raises(GitHubError): search.search_repositories('x', limit=20, page=51)


def test_true_empty_is_success_without_extra_calls(client):
    client.rows=[]
    result=search.search_repositories('no matches', limit=2)
    assert result['status']=='ok' and result['items']==[]
    assert result['fallback_used'] is False and len(client.calls)==1


def test_error_never_becomes_empty_success(client, monkeypatch):
    def failure(*a, **kw): raise GitHubError('PERMISSION_DENIED', '403 access denied')
    monkeypatch.setattr(client, 'json', failure)
    with pytest.raises(GitHubError) as exc: search.search_repositories('x')
    assert exc.value.code=='PERMISSION_DENIED'


def test_identity_cache_and_refresh(client):
    search.search_repositories('weather')
    cached=search.search_repositories('weather')
    assert len(client.calls)==1 and cached['metadata']['network_reverified'] is False
    client.identity='account-b'
    search.search_repositories('weather')
    assert len(client.calls)==2
    search.search_repositories('weather', refresh=True)
    assert len(client.calls)==3
    for row in search._SEARCH_CACHE._items.values():row['created_at']=0
    # TTL is enforced by the cache instead of allow_stale=True.
    search._SEARCH_CACHE.ttl_s=-1
    search.search_repositories('weather')
    assert len(client.calls)==4


def test_incomplete_search_and_private_scope_cannot_look_complete(client, monkeypatch):
    monkeypatch.setattr(client, 'json', lambda *a, **kw: ({'items':[], 'incomplete_results':True, 'total_count':0},{}))
    assert search.search_repositories('x')['status']=='partial'
    with pytest.raises(GitHubError):search.search_repositories('is:private')
