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
