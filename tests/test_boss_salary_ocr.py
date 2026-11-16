import base64
import io
from PIL import Image
import pytest
from test_xhs_runner_contract import fixture_root


def fixture_image():
    stream=io.BytesIO();Image.new('RGB',(80,20),'white').save(stream,format='PNG')
    return {'data':base64.b64encode(stream.getvalue()).decode(), 'mime_type':'image/png',
            'source_url':'https://www.zhipin.com/job_detail/fixture.html','clip':{'x':0,'y':0,'width':80,'height':20}}


def test_obfuscated_salary_reuses_media_ocr_and_preserves_provenance(monkeypatch,tmp_path):
    from collectors.platform import boss
    from runtime import optional_media, media_cache
    monkeypatch.setattr(media_cache,'media_cache_subdir',lambda name:tmp_path)
    observed=[]
    def run(payload):
        assert optional_media.approved_source(payload).is_file()
        observed.append(payload)
        return {'text':'25-40K·14薪','evidence_assets':[{'version_id':'fixture-ocr'}], 'component':'paddleocr-vl-1.5'}
    monkeypatch.setattr(optional_media,'run_optional',run)
    data=boss._salary_ocr({'salary':'\ue001K', 'salary_status':'obfuscated_dom','url':fixture_image()['source_url'],
                          'salary_capture_image':fixture_image()})
    assert data['salary']=='25-40K·14薪'
    assert data['salary_status']=='ocr_observed' and data['salary_source']=='rendered_region_ocr'
    assert data['salary_evidence']['evidence_assets']==[{'version_id':'fixture-ocr'}]
    assert data['salary_evidence']['ocr_confidence']=='not_reported'
    assert data['salary_evidence']['input_sha256']==observed[0]['input_sha256']
    assert 'salary_capture_image' not in data


@pytest.mark.parametrize('text',['公司介绍 25-40K','25-40K 50-80K','\ue001K',''])
def test_unreliable_ocr_never_becomes_salary(monkeypatch,tmp_path,text):
    from collectors.platform import boss
    from runtime import optional_media, media_cache
    monkeypatch.setattr(media_cache,'media_cache_subdir',lambda name:tmp_path)
    monkeypatch.setattr(optional_media,'run_optional',lambda payload:{'text':text})
    data=boss._salary_ocr({'salary':'\ue001K','salary_status':'obfuscated_dom','url':fixture_image()['source_url'],
                         'salary_capture_image':fixture_image()})
    assert data['salary']=='' and data['salary_claim_allowed'] is False
    assert data['salary_status']=='ocr_unverified'


def test_mismatched_screenshot_never_sent_to_ocr(monkeypatch):
    from collectors.platform import boss
    data=boss._salary_ocr({'salary':'\ue001K','salary_status':'obfuscated_dom','url':'https://www.zhipin.com/job_detail/other.html',
                         'salary_capture_image':fixture_image()})
    assert data['salary_status']=='ocr_unavailable'
    assert data['salary_evidence']['reason']=='salary_capture_identity_mismatch'


def test_shared_runner_captures_only_same_page_salary_region(fixture_root,monkeypatch):
    import json,shutil,subprocess
    from pathlib import Path
    node=shutil.which('node')
    if not node:pytest.skip('node unavailable')
    url=fixture_image()['source_url']
    bridge=fixture_root/'dist/src/browser/bridge.js'
    code=bridge.read_text(encoding='utf-8')
    code=code.replace('evaluate:async js=>{', 'evaluate:async js=>{'+
        'if(js==="fixture-salary")return {url:'+json.dumps(url)+',salary_capture:{x:10,y:20,width:80,height:20}};'+
        'if(js==="location.href")return '+json.dumps(url)+';')
    code=code.replace('return {};},','return method==="Page.captureScreenshot"?{data:"cG5nLWZpeHR1cmU="}:{};},')
    bridge.write_text(code,encoding='utf-8')
    log=fixture_root/'calls.jsonl';monkeypatch.setenv('KR_RUNNER_FIXTURE_LOG',str(log))
    runner=Path(__file__).resolve().parents[1]/'src/runtime/opencli_page_runner.mjs'
    result=subprocess.run([node,'--import',(fixture_root/'window-helper-fixture.mjs').as_uri(),str(runner)],
        input=json.dumps({'root':str(fixture_root),'contextId':'daily-fixture','session':'kr:boss','operation':'detail',
            'expression':'fixture-salary','url':url,'isolatedWork':True,'captureRegionField':'salary_capture',
            'pythonExecutable':'unused-python'}),capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)
    assert output['ok'],output
    capture=output['data']['salary_capture_image']
    assert capture['source_url']==url and capture['clip']['width']==80
    calls=[json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    screenshots=[x for x in calls if x.get('method')=='Page.captureScreenshot']
    assert len(screenshots)==1 and screenshots[0]['args']['captureBeyondViewport'] is False
    assert any(x['kind']=='close-owned' for x in calls)
    assert not any(x.get('action')=='present' and x.get('foreground') for x in calls)
