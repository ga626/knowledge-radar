"""视频用例的媒体边界：不再依赖入口内的旧策略注入。"""
from runtime.detail_composition import build_detail_strategy

import pytest
from kr_core import DetailRequest
from detail_strategies.video import enhance_video_detail

@pytest.mark.parametrize('platform,module', [('B站','bilibili'),('YouTube','youtube')])
def test_video_collection_hands_off_without_downloading(monkeypatch,tmp_path,platform,module):
    from importlib import import_module
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path))
    collector=import_module('collectors.platform.'+module)
    monkeypatch.setattr(collector,'collect_video_detail',lambda request:{'title':'test','desc':'intro','media_manifest':[{'type':'audio','url':request.url}]})
    monkeypatch.setattr('runtime.media_execution.execute_media_plan',lambda *a,**kw:pytest.fail('enhancement in collection'))
    request=DetailRequest(platform=platform,url='https://www.youtube.com/watch?v=abc12345678' if platform=='YouTube' else 'https://www.bilibili.com/video/BV1ELGY6sExb',options={'include_transcript':True,'_detail_stage':'collection'})
    result=build_detail_strategy(platform).extract(request).data
    assert result['_enhancement_pending'] and result['content']=='intro'
    assert result['media_manifest'] and result['enhancement_status']=='queued'


def test_video_description_is_separate_archive_asset(monkeypatch, tmp_path):
    from kr_core.evidence_store import EvidenceStore
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path))
    store = EvidenceStore(str(tmp_path / 'records.jsonl'))
    response = {'title': '视频标题', 'desc': '这是平台简介', 'content': '这是平台简介',
                'video_description': '这是平台简介', 'transcript': '真正的字幕内容'}
    store.append_detail(platform='B站', url='https://www.bilibili.com/video/BV1ELGY6sExb', response=response)
    kinds = {row['kind'] for row in response['evidence_assets']}
    assert 'video_description' in kinds and 'content' not in kinds and 'transcript' in kinds

def test_missing_asr_is_explicit_and_keeps_collected_text(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path))
    from runtime import media_acquisition, optional_media
    source=tmp_path/'audio.wav';source.write_bytes(b'audio')
    monkeypatch.setattr(media_acquisition,'acquire_media_ref',lambda ref:{'path':str(source),'sha256':optional_media.file_sha(source),'download_s':0})
    def missing(*a,**kw):raise ValueError('optional_component_not_installed_and_approved')
    monkeypatch.setattr(optional_media,'run_optional',missing)
    result=enhance_video_detail(DetailRequest(url='https://example.com/video',platform='B站',options={'include_transcript':True}),{'title':'intro','content':'keep original','media_manifest':[{'type':'audio','url':'https://example.com/video'}]}).data
    assert result['content']=='keep original' and result['enhancement_status']=='partial'
    assert result['media_execution']['steps'][0]['error_code']=='optional_component_not_installed_and_approved'
