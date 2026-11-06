"""下载已从VideoProcessor迁到公共获取器；保留请求头／代理／单视频约束覆盖。"""
import sys
from pathlib import Path
from types import SimpleNamespace
from media_bundle import MediaRef,MediaKind
from runtime import media_acquisition,proxy_config,leases,resource_concurrency

def test_common_video_download_preserves_headers_proxy_and_cache(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path))
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR',str(tmp_path/'media'))
    coordinator=leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(leases,'_COORDINATOR',coordinator)
    monkeypatch.setattr(resource_concurrency,'get_runtime_lease_coordinator',lambda:coordinator)
    monkeypatch.setattr('generic_web.url_policy.validate_detail_target',lambda *a:None)
    monkeypatch.setattr(proxy_config,'get_yt_dlp_proxy',lambda:'http://127.0.0.1:7897')
    captured={};calls=[]
    class Downloader:
        def __init__(self,options):captured.update(options)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def extract_info(self,url,download):
            calls.append(url)
            path=Path(captured['outtmpl'].replace('%(ext)s','mp4'));path.write_bytes(b'media')
            return {'requested_downloads':[{'filepath':str(path)}]}
    monkeypatch.setitem(sys.modules,'yt_dlp',SimpleNamespace(YoutubeDL=Downloader))
    ref=MediaRef(MediaKind.VIDEO,'https://www.bilibili.com/video/BV1ELGY6sExb',metadata={'headers':{'Referer':'https://www.bilibili.com/','Cookie':'must-not-forward'}})
    result=media_acquisition.acquire_media_ref(ref)
    assert Path(result['path']).read_bytes()==b'media'
    assert captured['noplaylist'] and captured['proxy']=='http://127.0.0.1:7897'
    assert captured['http_headers']=={'Referer':'https://www.bilibili.com/'}
    assert coordinator.resource_leases('media_asset')==[]
    assert media_acquisition.acquire_media_ref(ref)['cache_hit']
    assert len(calls)==1
