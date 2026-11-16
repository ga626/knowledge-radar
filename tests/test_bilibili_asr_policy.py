"""字幕语言／阻断／历史入口均委托公共详情任务，不再测试退役marker调度。"""
from runtime.detail_composition import build_detail_strategy

import pytest
from kr_core import DetailRequest
from collectors.platform import bilibili as bili

@pytest.mark.parametrize('language,text',[('en','English text'),('zh','中文字幕')])
def test_subtitle_language_and_real_times_survive_collection(monkeypatch,tmp_path,language,text):
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path))
    monkeypatch.setattr(bili,'get_bilibili_info',lambda _: {'title':'title','desc':'description','cid':1})
    def subtitle(*a,**kw):
        assert kw['language']==language
        return {'hit':True,'language':language,'text':text,'timestamped_segments':[{'text':text,'start_s':2,'end_s':4}]}
    monkeypatch.setattr(bili,'_probe_bilibili_subtitle',subtitle)
    result=bili.collect_video_detail(DetailRequest(url='https://www.bilibili.com/video/BV1ELGY6sExb',platform='B站',options={'include_transcript':True,'language':language}))
    assert result['transcript']==text
    assert result['transcript_metadata']['segments'][0]['start_s']==2
    assert result['part_scope']['complete'] is True
    assert result['part_scope']['completed_pages'] == [1]

def test_old_transcribe_entry_submits_same_video_use_case(monkeypatch):
    from runtime import video_tasks
    calls=[]
    monkeypatch.setattr(video_tasks,'submit_video_detail',lambda *a,**kw:calls.append((a,kw)) or {'task_id':'durable-123'})
    assert 'durable-123' in bili.transcribe_bilibili('BV1ELGY6sExb',language='en')
    assert calls[0][1]['include_transcript'] and calls[0][1]['language']=='en'

def test_verification_on_subtitle_never_switches_to_download(monkeypatch):
    monkeypatch.setattr(bili,'get_bilibili_info',lambda _: {'title':'title','desc':'keep','cid':1})
    monkeypatch.setattr(bili,'_probe_bilibili_subtitle',lambda *a,**kw:{'hit':False,'failure_type':'verification_required'})
    monkeypatch.setattr('runtime.media_execution.execute_media_plan',lambda *a,**kw:pytest.fail('must stop at verification'))
    result=build_detail_strategy('B站').extract(DetailRequest(platform='B站',url='https://www.bilibili.com/video/BV1ELGY6sExb',options={'include_transcript':True})).data
    assert result['failure_type']=='verification_required' and result['content']=='keep'


def test_multipart_metadata_and_collection_are_not_discarded(monkeypatch):
    from types import SimpleNamespace
    payload = {'title': 'course', 'desc': 'x' * 400, 'duration': 30, 'cid': 11,
        'pages': [{'page': 1, 'cid': 11, 'part': 'one', 'duration': 10},
                  {'page': 2, 'cid': 22, 'part': 'two', 'duration': 20}],
        'ugc_season': {'id': 7, 'title': 'series', 'sections': [{'episodes': [
            {'bvid': 'BV1ELGY6sExb', 'title': 'course'}, {'bvid': 'BV1Fdd2Y7Emg', 'title': 'next'}]}]}}
    monkeypatch.setattr(bili.httpx, 'get', lambda *a, **kw: SimpleNamespace(json=lambda: {'code': 0, 'data': payload}))
    info = bili.get_bilibili_info('BV1ELGY6sExb')
    assert len(info['desc']) == 400
    assert [part['cid'] for part in info['parts']] == [11, 22]
    assert len(info['collection']['episodes']) == 2


def test_explicit_second_part_selects_correct_subtitle_and_media(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(bili, 'get_bilibili_info', lambda _: {'title': 'course', 'desc': 'description', 'cid': 11,
        'parts': [{'page': 1, 'cid': 11, 'duration': 10}, {'page': 2, 'cid': 22, 'duration': 20}]})
    seen = []
    monkeypatch.setattr(bili, '_probe_bilibili_subtitle', lambda *a, **kw: seen.append(kw['info']['cid']) or {'hit': False})
    result = bili.collect_video_detail(DetailRequest(platform='B站', url='https://www.bilibili.com/video/BV1ELGY6sExb?p=2', options={'include_transcript': True}))
    assert seen == [22]
    assert result['duration'] == 20
    assert all(row['url'].endswith('?p=2') for row in result['media_manifest'])
    assert result['part_scope']['selected_pages'] == [2]


def test_all_parts_are_enhanced_and_never_overwrite_previous_transcripts(monkeypatch, tmp_path):
    from detail_strategies import video
    from kr_core import DetailResponse
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path))
    monkeypatch.setattr(bili, 'get_bilibili_info', lambda _: {'title': 'course', 'desc': 'description', 'cid': 11,
        'parts': [{'page': 1, 'cid': 11, 'duration': 10}, {'page': 2, 'cid': 22, 'duration': 20}]})
    monkeypatch.setattr(bili, '_probe_bilibili_subtitle', lambda *a, **kw: {'hit': False})
    seen = []
    original = video.enhance_video_detail
    def enhance(request, data):
        if data.get('part_details'):
            return original(request, data)
        seen.append(request.url)
        return DetailResponse(platform='B站', url=request.url, data={**data,
            'transcript': '正文' + str(data['cid']), 'enhancement_status': 'ok', 'status': 'completed',
            'transcript_metadata': {'segments': [{'start': 0, 'end': 4, 'start_s': 0, 'end_s': 2}]}})
    monkeypatch.setattr(video, 'enhance_video_detail', enhance)
    result = build_detail_strategy('B站').extract(DetailRequest(platform='B站', url='https://www.bilibili.com/video/BV1ELGY6sExb', options={'include_transcript': True})).data
    assert len(seen) == 2
    assert result['transcript'] == '正文11\n正文22'
    assert result['part_scope']['completed_pages'] == [1, 2]
    assert result['part_scope']['complete'] is True
    assert result['transcript_metadata']['parts'][1]['start'] == 5


def test_blocked_video_preserves_explicit_no_retry_to_outer_scheduler(monkeypatch):
    from detail_strategies.video import VideoDetailStrategy
    strategy = VideoDetailStrategy('YouTube', collect=lambda _: {'title': 'video', 'desc': 'description',
        'transcript_metadata': {'failure_type': 'rate_limited_or_ip_blocked', 'automatic_retry': False,
            'failure_phase': 'track_discovery', 'permitted_alternatives': ['author_transcript']}})
    result = strategy.extract(DetailRequest(platform='YouTube', url='https://www.youtube.com/watch?v=UBx4Zudvf8s')).data
    assert result['automatic_retry'] is False
    assert result['resume_policy'] == 'stop_no_auto_resume'
