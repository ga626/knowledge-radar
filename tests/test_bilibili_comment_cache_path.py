from kr_core import DetailRequest
from collectors.platform import bilibili


def test_collection_keeps_raw_comments_without_loading_filter(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(bilibili, 'get_bilibili_info', lambda bvid: {'title':'test','desc':'intro'})
    monkeypatch.setattr(bilibili, 'collect_comments', lambda bvid, **kw: {'comments':[{'content':'all original comments'}]})
    monkeypatch.setattr(bilibili, 'filter_bilibili_comments', lambda *a,**kw: (_ for _ in ()).throw(AssertionError('model in collection')))
    result = bilibili.collect_video_detail(DetailRequest(url='https://www.bilibili.com/video/BV1ELGY6sExb', platform='B站', enable_comment_filtering=True))
    assert result['comments']==[{'content':'all original comments'}]
    assert result['filtered_comments']==[]
