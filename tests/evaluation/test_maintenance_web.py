"""真实 loopback API 合同；不启动平台、浏览器或正式安装。"""
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

from runtime import evaluation_candidates as candidates
from runtime.evaluation_maintenance import Maintenance
from onboarding.setup_wizard import WizardServer


def test_dashboard_is_readonly_exposes_unknown_layers_and_auditable_queue(tmp_path,monkeypatch):
    from runtime.evaluation_view import evaluation_dashboard
    monkeypatch.setattr(candidates,'candidate_path',lambda:tmp_path/'db')
    monkeypatch.setenv('KR_EVALUATION_STORE',str(tmp_path/'runs'))
    view = evaluation_dashboard(tmp_path)
    assert len(view['levels']) == 4
    assert all(row['assessment']=='NOT_MEASURED' for row in view['levels'])
    assert view['maintenance']['executor']=='waiting_for_agent'
    assert not (tmp_path/'db').exists()


def test_frontend_has_four_review_views_no_polling_or_score_edits(monkeypatch):
    from onboarding.console_page import render_console_page, _asset
    monkeypatch.setenv('KR_CONSOLE_ROLE','dev')
    _asset.cache_clear()
    page = render_console_page('token')
    for label in ['当前水平','题库与变更','抽查与纠错','趋势比较','复制纠错请求']:
        assert label in page
    script = (Path(__file__).resolve().parents[2]/'src/onboarding/static/evaluation_summary.js').read_text(encoding='utf-8')
    assert 'setInterval' not in script and 'setTimeout' not in script


def test_feedback_api_is_authenticated_durable_and_does_not_claim_dispatch(tmp_path,monkeypatch):
    from tests.evaluation.test_maintenance import create
    monkeypatch.setattr(candidates,'candidate_path',lambda:tmp_path/'db')
    monkeypatch.setenv('KR_CONSOLE_ROLE','dev')
    m=Maintenance();create(m)
    server=WizardServer(('127.0.0.1',0))
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        origin=f'http://127.0.0.1:{server.server_port}'
        body=json.dumps({'case_id':'content.shell','revision':1,'text':'这个题的判据不对','request_id':'user-one'})
        def request(headers):
            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            connection.request('POST','/api/evaluation-feedback',body=body,headers=headers)
            response=connection.getresponse();result=(response.status,json.loads(response.read()));connection.close();return result
        assert request({'Origin':origin})[0]==403
        status,value=request({'Origin':origin,'X-KR-Setup-Token':server.setup_token,'Content-Type':'application/json'})
        assert status==200 and value['status']=='waiting_for_agent'
        assert m.view()['feedback'][0]['text']=='这个题的判据不对'
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)
