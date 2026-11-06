import httpx
import pytest

from academic_providers.chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from academic_providers.models import AcademicSearchRequest


@pytest.fixture(autouse=True)
def no_proxy_for_mock_transport(monkeypatch):
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)


class Provider(ChineseOpenAccessProvider):
    name = 'probe'
    config = OpenAccessPlatformConfig(name='probe', display_name='probe', homepage='https://papers.example/search')


@pytest.mark.parametrize('status', [403, 429, 500])
def test_http_failure_cannot_be_reported_as_zero_results(monkeypatch, status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(Exception, match='provider_fetch_failed') as failure:
        Provider().search(AcademicSearchRequest(query='OCR'))
    assert failure.value.diagnostics[0]['http_status'] == status
    assert failure.value.diagnostics[0]['type'] == ('rate_limited' if status == 429 else 'access_denied' if status == 403 else 'provider_http_error')


def test_real_empty_page_remains_empty(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='<html>No results</html>', request=request))
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=transport, **kwargs))
    assert Provider().search(AcademicSearchRequest(query='OCR')) == []


def test_timeout_and_partial_fetch_are_visible(monkeypatch):
    class PartialProvider(Provider):
        def search_urls(self, request):
            return ['https://papers.example/broken', 'https://papers.example/ok']
    def handler(request):
        if request.url.path == '/broken':
            raise httpx.ReadTimeout('fixture', request=request)
        return httpx.Response(200, text='<html>No results</html>', request=request)
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    result = PartialProvider().search(AcademicSearchRequest(query='OCR'))
    assert result == [] and result.diagnostics[0]['type'] == 'timeout'
    assert result.diagnostics[0]['retryable'] is True
