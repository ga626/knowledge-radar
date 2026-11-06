"""公开候选知乎通道不得依赖私有签名或旧Cookie。"""
from collectors.platform import zhihu
from runtime.login_state import LoginProbe


def test_public_zhihu_uses_only_borrowed_native(monkeypatch):
    calls=[]
    monkeypatch.setattr(zhihu, '_invoke', lambda op, **kw: calls.append((op,kw)) or {'ok':True,'data':{'empty_confirmed':True}})
    result=zhihu.legacy_search_zhihu('Python',limit=2)
    assert result['items']==[] and result['metadata']['empty_confirmed']
    assert calls[0][0]=='search' and calls[0][1]['url'].startswith('https://www.zhihu.com/search?')
    assert not hasattr(zhihu,'read_zhihu_cookies_from_cdp') and not hasattr(zhihu,'zhihu_sign')


def test_zhihu_login_probe_is_passive_and_native(monkeypatch):
    calls=[]
    monkeypatch.setattr(zhihu,'probe_auth',lambda:calls.append('passive') or {'status':'unknown'})
    probe=LoginProbe(platform='zhihu',chrome_debug_url=lambda _: (_ for _ in ()).throw(AssertionError('CDP')))
    assert probe.inspect(cdp_status='ok')['status']=='unknown'
    assert calls==['passive']
