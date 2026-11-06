import json
import subprocess

from runtime.xhs_candidates import normalize_xhs_search_candidates


def test_search_state_keeps_signature_when_dom_link_has_none():
    from runtime.xhs_candidates import xhs_search_state_items_js
    state = {'search': {'feeds': {'_value': [{'id': '0123456789abcdef01234567',
        'xsecToken': 'private-marker', 'noteCard': {'displayTitle': 'Obsidian 知识库', 'user': {'nickname': '作者'}}}]}}}
    script = 'global.window = ' + json.dumps({'__INITIAL_STATE__': state}) + '; console.log(JSON.stringify(' + xhs_search_state_items_js() + '));'
    completed = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8', check=True)
    rows = normalize_xhs_search_candidates(json.loads(completed.stdout))
    assert rows[0]['note_id'] == '0123456789abcdef01234567'
    assert 'xsec_token=private-marker' in rows[0]['url']


def test_scrapling_search_protects_fetch_token_before_public_redaction(monkeypatch):
    from collectors.platform import xiaohongshu as xhs
    from generic_web.url_policy import public_evidence_payload
    rows = xhs._protect_xhs_search_fetch([{'note_id': '0123456789abcdef01234567', 'fetch_url': 'https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=private-marker'}])
    assert xhs._cached_xhs_token('0123456789abcdef01234567') == 'private-marker'
    assert 'private-marker' not in json.dumps(public_evidence_payload(rows))
    assert 'xsec_token=private-marker' in xhs.protected_detail_url('https://www.xiaohongshu.com/explore/0123456789abcdef01234567')


def test_detail_entry_hands_cached_signature_to_durable_worker(monkeypatch, tmp_path):
    import server
    from collectors.platform import xiaohongshu as xhs
    from runtime import task_service
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path / 'data'))
    captured = {}
    class Service:
        def submit(self, operation, payload, **kwargs):
            from dataclasses import asdict
            from kr_core import DetailRequest
            from platform_adapters.xiaohongshu_adapter import prepare_detail
            payload=asdict(prepare_detail(DetailRequest(**payload)))
            captured.update(operation=operation, payload=payload, scope=kwargs['scope'])
            return {'task_id': 'durable-test', 'status': 'queued', 'task_type': 'durable_content_detail'}
        def result(self, task_id):
            return {'status': 'queued'}
    monkeypatch.setattr(task_service, 'TaskService', Service)
    xhs._protect_xhs_search_fetch([{'note_id': '0123456789abcdef01234567', 'fetch_url': 'https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=private-handoff'}])
    result = server.get_content_detail('https://www.xiaohongshu.com/explore/0123456789abcdef01234567', include_ocr=True, research_task_id='kr-research-test')
    assert 'xsec_token=private-handoff' in captured['payload']['url']
    assert captured['scope']['research_task_id'] == 'kr-research-test'
    # 返回给 Codex 的结果不包含签名；实际 worker stdin 保密另由 durable 回归覆盖。
    assert 'private-handoff' not in json.dumps(result)
