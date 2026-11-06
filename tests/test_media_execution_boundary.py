"""跨来源执行、真实解码和持久阶段交接；只模拟网络和模型推理边界。"""
import ast
import base64
import io
import json
import time
from pathlib import Path
import pytest
from PIL import Image, ImageDraw
from media_bundle import MediaBundle, MediaRef, MediaKind, MediaLocationType, TextItem, TextKind
from media_router import MediaActionPlan, MediaOperation, InformationSourceMode, StoragePolicy
from runtime import media_execution, media_acquisition, optional_media, leases, resource_concurrency
from runtime.resource_concurrency import ResourceDeferred

@pytest.fixture(autouse=True)
def isolate(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path/'data'))
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_LOG_DIR',str(tmp_path/'logs'))
    coordinator=leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(leases,'_COORDINATOR',coordinator)
    monkeypatch.setattr(resource_concurrency,'get_runtime_lease_coordinator',lambda:coordinator)

def plan(bundle,*operations,download=True):
    return MediaActionPlan.from_bundle(bundle,source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO,MediaKind.VIDEO),operations=operations,
        storage_policy=StoragePolicy.MEDIA_CACHE_TTL if download else StoragePolicy.NO_DOWNLOAD)

def local_ref(path,kind):
    return MediaRef(kind,str(path),location_type=MediaLocationType.LOCAL_PATH,
        metadata={'sha256':optional_media.file_sha(path),'approved_input_root':str(path.parent)})

def test_existing_transcript_avoids_download_and_model(monkeypatch):
    monkeypatch.setattr(media_acquisition,'acquire_media_ref',lambda *a:pytest.fail('unnecessary download'))
    monkeypatch.setattr(optional_media,'run_optional',lambda *a:pytest.fail('unnecessary ASR'))
    bundle=MediaBundle.build(platform='B站',source_url='https://example.com',texts=[TextItem('full transcript',TextKind.SUBTITLE)],audio_refs=[MediaRef(MediaKind.AUDIO,'https://example.com/audio')])
    result=media_execution.execute_media_plan(bundle,plan(bundle,MediaOperation.RUN_ASR))
    assert result['transcript']=='full transcript' and result['steps']==[]

def test_no_media_text_request_is_not_forced_through_model(monkeypatch):
    monkeypatch.setattr(optional_media,'run_optional',lambda *a:pytest.fail('model'))
    bundle=MediaBundle.build(platform='local',source_url='',texts=[TextItem('all text')])
    assert media_execution.execute_media_plan(bundle,plan(bundle,MediaOperation.READ_BASIC_TEXT))['status']=='acquired'


def test_shared_asset_cache_binds_effective_language_and_model(monkeypatch, tmp_path):
    path = tmp_path / 'speech.wav'
    path.write_bytes(b'audio fixture')
    asset = {'path': str(path), 'sha256': optional_media.file_sha(path)}
    from runtime.model_roles import ASR_COMPONENT
    calls = []
    monkeypatch.setattr(optional_media, 'run_optional', lambda payload: calls.append(payload) or {'status': 'acquired', 'text': 'hello 中文'})
    first = media_execution.process_local_asset(asset, ASR_COMPONENT, language='auto')
    cached = media_execution.process_local_asset(asset, ASR_COMPONENT, language='mixed', cached=first)
    assert cached['reused'] and len(calls) == 1
    second = media_execution.process_local_asset(asset, ASR_COMPONENT, language='en', cached=first)
    assert len(calls) == 2 and second['processing_identity']['language'] == 'en'
    legacy = {key: value for key, value in first.items() if key != 'processing_identity'}
    media_execution.process_local_asset(asset, ASR_COMPONENT, cached=legacy)
    assert len(calls) == 3
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='identity_mismatch'):
        media_execution.process_local_asset(asset, ASR_COMPONENT, cached=first)

def test_forbidden_download_is_visible_and_does_not_fetch(monkeypatch):
    monkeypatch.setattr(media_acquisition,'acquire_media_ref',lambda *a:pytest.fail('network'))
    bundle=MediaBundle.build(platform='test',source_url='',audio_refs=[MediaRef(MediaKind.AUDIO,'https://example.com/a')])
    result=media_execution.execute_media_plan(bundle,plan(bundle,MediaOperation.RUN_ASR,download=False))
    assert result['status']=='partial' and result['steps'][0]['error_code']=='media_plan_download_not_allowed'

def test_video_frames_really_run_ocr_with_time_locators_and_resume(monkeypatch,tmp_path):
    from runtime import media_decode
    video=tmp_path/'source.mp4';video.write_bytes(b'fixture')
    picture=io.BytesIO();Image.new('RGB',(100,60),'white').save(picture,format='JPEG')
    monkeypatch.setattr(media_decode,'sample_frames',lambda *a,**kw:[{'pts_s':3.5,'image_base64':base64.b64encode(picture.getvalue()).decode()}])
    calls=[]
    def ocr(payload):
        calls.append(payload)
        assert Path(payload['input_path']).suffix=='.jpg'
        return {'text':'画面原文','component':'siliconflow-ocr','status':'acquired'}
    monkeypatch.setattr(optional_media,'run_optional',ocr)
    bundle=MediaBundle.build(platform='test',source_url='https://example.com/v',video_refs=[local_ref(video,MediaKind.VIDEO)])
    action=plan(bundle,MediaOperation.SAMPLE_FRAMES,MediaOperation.RUN_OCR)
    first=media_execution.execute_media_plan(bundle,action)
    second=media_execution.execute_media_plan(bundle,action)
    assert first['status']=='acquired' and first['ocr_text']=='画面原文'
    assert first['ocr_text_locators'][0]['start_s']==3.5
    assert len(calls)==1 and all(row['resumed'] for row in second['steps'])

def test_completed_ocr_not_resent_when_later_image_defers(monkeypatch,tmp_path):
    refs=[]
    for index in range(2):
        path=tmp_path/f'{index}.png';Image.new('RGB',(20,20),('white','red')[index]).save(path)
        refs.append(local_ref(path,MediaKind.IMAGE))
    calls=[];blocked=True
    def ocr(payload):
        calls.append(payload['input_sha256'])
        if payload['input_sha256']==refs[1].metadata['sha256'] and blocked:raise ResourceDeferred('frame_vision')
        return {'text':payload['input_sha256'],'status':'acquired'}
    monkeypatch.setattr(optional_media,'run_optional',ocr)
    bundle=MediaBundle.build(platform='local',source_url='',image_refs=refs)
    action=plan(bundle,MediaOperation.RUN_OCR)
    with pytest.raises(ResourceDeferred):media_execution.execute_media_plan(bundle,action)
    blocked=False
    result=media_execution.execute_media_plan(bundle,action)
    assert result['status']=='acquired' and calls.count(refs[0].metadata['sha256'])==1

def test_pyav_audio_resample_and_exact_chunk_boundaries(tmp_path):
    import wave
    from runtime.media_decode import audio_chunks
    path=tmp_path/'stereo.wav'
    with wave.open(str(path),'wb') as out:
        out.setnchannels(2);out.setsampwidth(2);out.setframerate(8000);out.writeframes(b'\0'*8000*2*2*3)
    chunks=list(audio_chunks(path,seconds=2))
    assert [row[0] for row in chunks]==[0,2]
    assert [len(row[1]) for row in chunks]==[32000,16000]
    assert all(row[2]==16000 and row[1].ndim==1 for row in chunks)

def test_pyav_frames_skip_black_keep_static_slide_and_dark_text(tmp_path):
    import av
    from runtime.media_decode import sample_frames
    path=tmp_path/'slides.mp4'
    with av.open(str(path),'w') as container:
        stream=container.add_stream('mpeg4',rate=4);stream.width=320;stream.height=180;stream.pix_fmt='yuv420p'
        for index in range(24):
            image=Image.new('RGB',(320,180),'black' if index<8 or index>=16 else 'white')
            if index>=16:ImageDraw.Draw(image).text((40,70),'CODE 123456789',fill='white',stroke_width=1)
            frame=av.VideoFrame.from_image(image)
            for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode():container.mux(packet)
    frames=sample_frames(path,max_frames=8)
    assert 2<=len(frames)<=8
    assert all(row['pts_s']>=2 for row in frames)
    assert any(row['pts_s']>=4 for row in frames)
    assert len({row['image_base64'] for row in frames})==len(frames)

def test_public_processing_has_no_reverse_platform_or_server_imports():
    src=Path(__file__).resolve().parents[1]/'src'
    for name in ['media_execution','media_acquisition','media_decode','optional_media_worker','pdf_text','execution_context','component_environment']:
        tree=ast.parse((src/'runtime'/f'{name}.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            modules=[node.module or ''] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else []
            assert not any(module=='server' or module.startswith(('collectors.platform','platform_adapters','detail_strategies')) for module in modules),(name,modules)

@pytest.mark.parametrize('platform,module',[('B站','bilibili'),('YouTube','youtube')])
def test_durable_video_archives_collection_then_defers_without_rescan(monkeypatch,tmp_path,platform,module):
    from importlib import import_module
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from kr_core import DetailRequest
    from dataclasses import asdict
    from generic_web import url_policy
    from runtime import media_execution
    monkeypatch.setattr(url_policy,'validate_detail_target',lambda *a:None)
    collector=import_module('collectors.platform.'+module)
    calls=[];blocked=True
    def collect(request):
        calls.append(request.url)
        return {'title':'study','desc':'original introduction','comments':[{'content':'raw comment'}],
            'media_manifest':[{'type':'audio','url':request.url}]}
    monkeypatch.setattr(collector,'collect_video_detail',collect)
    def execute(*a,**kw):
        if blocked:raise ResourceDeferred('local_heavy')
        return {'status':'acquired','steps':[],'artifacts':[],'transcript':'complete transcript','transcript_metadata':{'status':'aligned','segments':[{'start':0,'end':19,'start_s':0,'end_s':2}]}}
    monkeypatch.setattr(media_execution,'execute_media_plan',execute)
    monkeypatch.setattr(resource_concurrency,'resource_available',lambda resource:not blocked)
    store=TaskStore(tmp_path/'tasks.sqlite3');service=TaskService(store)
    task=service.submit('content_detail',asdict(DetailRequest(platform=platform,url='https://example.com/video',options={'include_transcript':True})),timeout_s=15)
    task_id=task['task_id'];until=time.monotonic()+8
    while time.monotonic()<until:
        row=store.get_task(task_id)
        if row['metadata'].get('phase')=='waiting_resource':break
        time.sleep(.02)
    assert row['metadata']['phase']=='waiting_resource',row
    partial=service.result(task_id)['result']
    assert partial['content']=='original introduction' and partial['evidence_assets']
    light=service.submit('fixture_echo',{'value':'light'},timeout_s=5) if __import__('os').environ.get('KR_TASK_TEST_MODE')=='1' else None
    blocked=False
    until=time.monotonic()+8
    while time.monotonic()<until and store.get_task(task_id)['status'] not in {'completed','failed'}:time.sleep(.02)
    result=service.result(task_id)
    assert result['status']=='completed',result
    assert result['result']['transcript']=='complete transcript' and len(calls)==1
    assert any(asset['kind']=='transcript' for asset in result['result']['evidence_assets'])
