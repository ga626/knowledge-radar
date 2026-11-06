"""正常原文的媒体身份不能被邻近视频、外站或持久签名替代。"""
import pytest
from collectors.platform import wechat_media
from kr_core.source_urls import public_evidence_payload

ARTICLE = "https://mp.weixin.qq.com/s/research"
CDN = "https://mpvideo.qpic.cn/clip.mp4?vid=wxv_123&dis_k=secret-key&dis_t=1234"
MANIFEST = [{"type":"video", "locator":"wechat:video:0", "media_id":"wxv_123", "url":"", "media_access":"embedded_player_not_media"}]


def test_only_same_article_and_exact_media_id_resolves():
    def invoke(**kw):
        assert kw["url"] == ARTICLE and kw["isolated_work"]
        return {"ok":True, "data":{"url":ARTICLE,"article":True,"items":[
            {"type":"video","media_id":"wxv_other","url":CDN},
            {"type":"video","media_id":"wxv_123","url":CDN}]}}
    r = wechat_media.resolve(ARTICLE, MANIFEST, invoke=invoke)
    assert r["items"][0]["url"] == CDN
    assert r["items"][0]["locator"] == "wechat:video:0"
    assert "secret-key" not in str(public_evidence_payload(r))
    assert "dis_t" not in str(public_evidence_payload(r))


@pytest.mark.parametrize("data,expected", [
    ({"url":ARTICLE,"article":True,"risk":True}, "anti_bot_verification"),
    ({"url":ARTICLE+"-other","article":True}, "media_article_identity_mismatch"),
    ({"url":ARTICLE,"article":False}, "media_article_identity_mismatch"),
])
def test_no_resource_resolution_through_gate_or_another_article(data, expected):
    r = wechat_media.resolve(ARTICLE, MANIFEST, invoke=lambda **k:{"ok":True,"data":data})
    assert r["error"]["type"] == expected and not r["items"]


@pytest.mark.parametrize("url", ["http://mpvideo.qpic.cn/a.mp4", "https://qpic.cn.evil.test/a.mp4", "https://qpic.cn@evil.test/a.mp4", "blob:https://mp.weixin.qq.com/a", "https://mpvideo.qpic.cn:444/a"])
def test_media_resource_scope_rejects_unapproved_urls(url):
    assert not wechat_media.approved_resource(url)
