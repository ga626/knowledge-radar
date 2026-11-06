from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from runtime.research_ledger import close_task, open_task, record_candidates, record_tool_receipt, review_task


def test_atomic_ledger_write_recovers_transient_windows_sharing_violation(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    task=open_task(objective='本地记录',budget='fast',considered=[])
    replace=Path.replace
    failures=[]
    def transient(path,target):
        if not failures:
            failures.append(str(path))
            raise PermissionError('temporary Windows sharing violation')
        return replace(path,target)
    monkeypatch.setattr(Path,'replace',transient)
    receipt=record_tool_receipt(task_id=task['research_task_id'],trace_id='transient',tool='search_xiaohongshu',status='ok')
    assert receipt['receipt']['trace_id']=='transient'
    assert not list((tmp_path/'research_tasks').glob('*.tmp'))


def test_atomic_ledger_write_surfaces_persistent_permission_failure(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    calls=[]
    def denied(path,target):
        calls.append(path)
        raise PermissionError('permanent denial')
    monkeypatch.setattr(Path,'replace',denied)
    with pytest.raises(PermissionError):
        open_task(objective='本地记录',budget='fast',considered=[])
    assert len(calls)<=6
    assert not list((tmp_path/'research_tasks').glob('*.tmp'))


def test_research_ledger_preserves_cross_process_appends(monkeypatch, tmp_path):
    """Two workers appending the same research task must not lose receipts."""
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="并发账本", budget="fast", task_id="cross-process-ledger", considered=[])
    code = """import os, sys, time
sys.path.insert(0, os.environ["KR_TEST_SOURCE"])
from runtime.research_ledger import record_tool_receipt
task_id, prefix = sys.argv[1:]
for index in range(8):
    result = record_tool_receipt(task_id=task_id, trace_id=f"{prefix}-{index}", tool="kr_web_search", status="ok")
    assert result["status"] == "recorded", result
    time.sleep(0.005)
"""
    env = os.environ.copy()
    env["KR_STATE_DIR"] = str(tmp_path)
    checkout = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(checkout / "src")
    env["KR_TEST_SOURCE"] = str(checkout / "src")
    processes = [
        subprocess.Popen([sys.executable, "-c", code, task["research_task_id"], prefix], cwd=checkout, env=env)
        for prefix in ("worker-a", "worker-b")
    ]
    assert [process.wait(timeout=20) for process in processes] == [0, 0]
    from runtime.research_ledger import read_task
    stored = read_task(task_id=task["research_task_id"])
    assert {row["trace_id"] for row in stored["tool_receipts"]} == {
        *(f"worker-a-{index}" for index in range(8)),
        *(f"worker-b-{index}" for index in range(8)),
    }


def _passing_quality_receipt(tmp_path):
    report = tmp_path / "report.md"
    evidence = tmp_path / "report.evidence.json"
    receipt = tmp_path / "quality.receipt.json"
    report.write_text("# test", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")
    receipt.write_text(json.dumps({
        "status": "pass", "report_path": str(report.resolve()), "evidence_path": str(evidence.resolve()),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    return report, evidence, receipt


def test_closeout_preserves_chinese_reopen_condition_and_agent_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="中文重开条件", budget="deep", considered=[{"source_ecology": "web"}])
    condition = "用户需要真实平台样本时，重新检查这个缺口"
    result = close_task(
        task_id=task["research_task_id"], stop_rationale="本轮只读考古完成", key_claims=[],
        ecology_outcomes=[{"source_ecology": "web", "outcome": "strategic_skip",
                           "reason": "已有本地证据", "claim_gap_ids": ["gap-initial"],
                           "reopen_condition": condition}])
    assert result["status"] == "recorded"
    assert result["closeout"]["ecology_outcomes"][0]["reopen_condition"] == condition
    assert result["closeout"]["completion_facts"]["stop_authority"] == "agent"
    assert result["closeout"]["completion_facts"]["report_check"] == "unverified"


def test_stale_revision_cannot_replace_newer_evidence(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="版本保护", budget="fast", considered=[])
    stale = dict(task)
    record_tool_receipt(task_id=task["research_task_id"], trace_id="new", tool="kr_web_search", status="ok")
    with ledger._ledger_guard(task["research_task_id"]), pytest.raises(ValueError, match="revision_conflict"):
        ledger._write(stale)
    assert ledger.read_task(task_id=task["research_task_id"])["tool_receipts"][0]["trace_id"] == "new"


def test_corrupt_ledger_is_not_silently_recreated(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="损坏保护", budget="fast", considered=[])
    path = ledger._path(task["research_task_id"])
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        open_task(objective="不要覆盖", budget="fast", considered=[], task_id=task["research_task_id"])
    assert path.read_text(encoding="utf-8") == "{broken"


def test_research_ledger_fails_closed_until_every_considered_ecology_has_outcome(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(
        objective="真实研究",
        budget="deep",
        task_id="test-ledger",
        considered=[
            {"source_ecology": "generic_web_ecology", "candidate_tools": ["kr_web_search"]},
            {"source_ecology": "zhihu_discussion_ecology", "candidate_tools": ["search_zhihu"]},
        ],
    )
    incomplete = close_task(
        task_id=task["research_task_id"],
        ecology_outcomes=[{"source_ecology": "generic_web_ecology", "outcome": "used"}],
        stop_rationale="已有足够证据",
        key_claims=[{"id": "C1", "importance": "high", "supporting_evidence_ids": ["E1"]}],
        quality_status="pass",
    )
    assert incomplete["status"] == "recorded_with_gaps"
    assert incomplete["closeout"]["missing_outcomes"] == ["generic_web_ecology", "zhihu_discussion_ecology"]

    generic = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-generic", tool="kr_web_search", status="ok")["receipt"]
    zhihu = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-zhihu", tool="search_zhihu", status="ok")["receipt"]
    report, evidence, quality_receipt = _passing_quality_receipt(tmp_path)

    complete = close_task(
        task_id=task["research_task_id"],
        ecology_outcomes=[
            {"source_ecology": "generic_web_ecology", "outcome": "used", "receipt_ids": [generic["receipt_id"]]},
            {"source_ecology": "zhihu_discussion_ecology", "outcome": "not_relevant", "reason": "本任务不需要观点谱系", "receipt_ids": [zhihu["receipt_id"]], "claim_gap_ids": ["gap-initial"], "reopen_condition": "claim_scope_changes"},
        ],
        stop_rationale="关键结论已有独立支撑，余下生态边际收益低。",
        key_claims=[{"id": "C1", "importance": "high", "supporting_evidence_ids": ["E1", "E2"], "supporting_receipt_ids": [generic["receipt_id"]]}],
        quality_status="pass",
        transcript_status="partial",
        report_path=str(report), evidence_path=str(evidence), quality_receipt_path=str(quality_receipt),
    )
    assert complete["status"] == "recorded"


def test_progress_review_explains_candidate_only_gap_without_prescribing_a_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="真实研究", budget="deep", task_id="review-ledger", considered=[])
    tool_receipt = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-candidate", tool="search_bilibili", status="ok")["receipt"]
    record_candidates(
        task_id=task["research_task_id"],
        source_ecology="bilibili_video_ecology",
        tool="search_bilibili",
        items=[{"id": "video-1", "title": "候选"}],
        query="视频证据",
        receipt_id=tool_receipt["receipt_id"],
    )

    review = review_task(task_id=task["research_task_id"], phase="before_delivery")

    assert review["stop_assessment"] == "not_ready"
    assert {item["code"] for item in review["gaps"]} >= {"candidate_only", "counterevidence_not_recorded"}
    assert "tool" not in review["autonomy_boundary"].lower() or "chooses" in review["autonomy_boundary"]


@pytest.mark.parametrize("phase", ["after_archaeology", "after_first_candidates", "before_delivery"])
def test_gap_assessment_matches_explicit_review_without_mutating_snapshot(monkeypatch, tmp_path, phase):
    from copy import deepcopy
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="同一诊断，两种交接", budget="fast", considered=[])
    payload = ledger.read_task(task_id=task["research_task_id"])
    original = deepcopy(payload)
    before = ledger._path(task["research_task_id"]).read_bytes()

    assessment = ledger.assess_task_gaps(payload, phase=phase)

    assert payload == original
    assert ledger._path(task["research_task_id"]).read_bytes() == before
    assert assessment["read_only"] is True
    assert "event" not in assessment
    reviewed = review_task(task_id=task["research_task_id"], phase=phase)
    assert reviewed["status"] == "reviewed" and reviewed["read_only"] is False
    assert reviewed["event"]["kind"] == "stop_reviewed"
    for key in ("gaps", "counts", "stop_assessment", "coverage_scope", "unregistered_coverage"):
        assert reviewed[key] == assessment[key]
    assert len(ledger.read_task(task_id=task["research_task_id"])["events"]) == len(original["events"]) + 1


def test_gap_assessment_never_treats_recorded_checks_as_evidence_saturation():
    from runtime.research_ledger import assess_task_gaps
    payload = {
        "research_task_id": "kr-research-assessment",
        "candidate_pool": [{"stage": "detail_extracted", "verification": "receipt_verified"}],
        "events": [{"kind": "claim_gap_opened"}, {"kind": "counterevidence"}],
    }
    ready = assess_task_gaps(payload, phase="before_delivery")
    assert ready["gaps"] == []
    assert ready["stop_assessment"] == "registered_checks_satisfied"
    assert ready["coverage_scope"] == "registered_items_only"
    assert ready["unregistered_coverage"] == "unknown"
    assert "Agent still chooses" in ready["autonomy_boundary"]
    payload["pending_submissions"] = [{"trace_id": "private", "result": {"url": "https://private.invalid"}}]
    pending = assess_task_gaps(payload, phase="before_delivery")
    assert pending["stop_assessment"] == "not_ready"
    assert [row["code"] for row in pending["gaps"]] == ["pending_receipt_submissions"]
    assert pending["counts"]["pending_submission_count"] == 1
    assert "private" not in json.dumps(pending)


def test_submission_replays_interrupted_candidate_link_without_duplicate_discovery(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='交接中断', budget='fast', considered=[])
    arguments = dict(task_id=task['research_task_id'], trace_id='stable-search', tool='kr_web_search',
                     status='ok', result={'items': [{'url': 'https://example.org/a'}]})
    original = ledger._write
    def interrupt_marker(payload):
        if any(row.get('candidate_association') for row in payload.get('tool_receipts', [])):
            raise OSError('interrupt after candidate commit, before submission marker')
        return original(payload)
    monkeypatch.setattr(ledger, '_write', interrupt_marker)
    with pytest.raises(OSError):
        ledger.submit_tool_result(**arguments)
    before = ledger.read_task(task_id=task['research_task_id'])
    assert len(before['tool_receipts']) == len(before['candidate_pool']) == 1
    assert 'candidate_association' not in before['tool_receipts'][0]
    monkeypatch.setattr(ledger, '_write', original)
    assert ledger.submit_tool_result(**arguments)['status'] == 'recorded'
    after = ledger.read_task(task_id=task['research_task_id'])
    assert after['candidate_pool'][0]['seen_count'] == 1
    assert after['events'] == before['events']
    ledger.submit_tool_result(**{**arguments, 'trace_id': 'another-real-search'})
    assert ledger.read_task(task_id=task['research_task_id'])['candidate_pool'][0]['seen_count'] == 2


def test_submission_failure_return_is_not_success_and_legacy_receipt_can_be_completed(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='旧回执补交', budget='fast', considered=[])
    args = dict(task_id=task['research_task_id'], trace_id='legacy', tool='kr_web_search',
                status='ok', result={'items': [{'url': 'https://example.org/a'}]})
    record_tool_receipt(**args)
    original = ledger.link_tool_result
    monkeypatch.setattr(ledger, 'link_tool_result', lambda **_: {'association': 'unique_source_identity', 'status': 'declared_only'})
    assert ledger.submit_tool_result(**args)['status'] == 'recording_failed'
    monkeypatch.setattr(ledger, 'link_tool_result', original)
    assert ledger.submit_tool_result(**args)['status'] == 'recorded'
    assert len(ledger.read_task(task_id=task['research_task_id'])['tool_receipts']) == 1


def test_sync_submission_failure_is_persisted_and_repaired_without_reexecution(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='同步回执补偿', budget='fast', considered=[])
    args = dict(task_id=task['research_task_id'], trace_id='sync-detail', tool='get_content_detail',
                status='ok', result={'url': 'https://example.org/a', 'content': '正文不能进入待补记录',
                                     'evidence_assets': []})
    original = ledger.link_tool_result
    monkeypatch.setattr(ledger, 'link_tool_result', lambda **_: {'association': 'unique_source_identity', 'status': 'declared_only'})
    failed = ledger.submit_tool_result(**args, _record_pending=True)
    assert failed['status'] == 'recording_failed'
    pending = ledger.read_task(task_id=task['research_task_id'])['pending_submissions']
    assert len(pending) == 1 and '正文不能进入待补记录' not in json.dumps(pending, ensure_ascii=False)
    monkeypatch.setattr(ledger, 'link_tool_result', original)
    repaired = ledger.repair_pending_submissions()
    assert repaired['repaired'] == ['sync-detail'] and repaired['remaining'] == []
    payload = ledger.read_task(task_id=task['research_task_id'])
    assert payload.get('pending_submissions') == [] and len(payload['tool_receipts']) == 1


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'withdrawn'])
def test_submission_requires_actual_fixed_evidence_and_rolls_back_batch_references(monkeypatch, tmp_path, damage):
    from runtime import research_ledger as ledger
    from kr_core.evidence_assets import EvidenceAssets
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path/'assets'))
    assets = EvidenceAssets()
    first = assets.put('https://example.org/a', '保留原件')
    second = assets.put('https://example.org/b', '将损坏的原件')
    task = open_task(objective='固定证据校验', budget='fast', considered=[])
    path = assets._file(second['raw_sha256'])
    if damage == 'missing':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'broken')
    else:
        assets.withdraw(second['source_id'])
    with pytest.raises(ValueError):
        ledger.submit_tool_result(task_id=task['research_task_id'], trace_id='fixed', tool='get_content_detail',
            status='ok', result={'evidence_assets': [first, second]})
    assert ledger.read_task(task_id=task['research_task_id'])['tool_receipts'] == []
    assert assets.retention_plan()['reference_count'] == 0


def test_replayed_detail_preserves_subsequent_review_and_does_not_accept_different_result(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    from kr_core.evidence_assets import EvidenceAssets
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path/'assets'))
    task = open_task(objective='关联不倒退', budget='fast', considered=[])
    task_id = task['research_task_id']
    ledger.submit_tool_result(task_id=task_id, trace_id='search', tool='kr_web_search', status='ok',
        result={'items': [{'url': 'https://example.org/a'}]})
    ref = EvidenceAssets().put('https://example.org/a', '原文')
    args = dict(task_id=task_id, trace_id='detail', tool='get_content_detail', status='ok',
                result={'url': 'https://example.org/a', 'evidence_assets': [ref]})
    receipt = ledger.submit_tool_result(**args)['receipt']
    candidate = ledger.read_task(task_id=task_id)['candidate_pool'][0]
    ledger.update_candidate_stage(task_id=task_id, candidate_id=candidate['candidate_id'], stage='cross_checked',
        evidence_receipt_ids=[receipt['receipt_id']], independence_rationale='explicit independent review')
    before = ledger._path(task_id).read_bytes()
    assert ledger.submit_tool_result(**args)['status'] == 'recorded'
    assert ledger._path(task_id).read_bytes() == before
    assert EvidenceAssets().retention_plan()['reference_count'] == 1
    rejected = ledger.submit_tool_result(**{**args, 'result': {**args['result'], 'content': 'different result'}})
    assert rejected['reason'] == 'receipt_identity_conflict'
    assert ledger._path(task_id).read_bytes() == before


def test_concurrent_processes_publish_one_logical_submission(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='并发交接', budget='fast', considered=[])
    code = '''import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['KR_TEST_SOURCE'])
from runtime import research_ledger
assert Path(research_ledger.__file__).resolve().is_relative_to(Path(os.environ['KR_TEST_SOURCE']).resolve())
from runtime.research_ledger import submit_tool_result
for _ in range(4):
    result = submit_tool_result(task_id=sys.argv[1], trace_id='same-submission', tool='kr_web_search', status='ok',
        result={'items': [{'url': 'https://example.org/a'}]})
    assert result['status'] == 'recorded', result
'''
    source = str(Path(__file__).resolve().parents[1]/'src')
    env = {**os.environ, 'PYTHONPATH': source, 'KR_TEST_SOURCE': source, 'PYTHONIOENCODING': 'utf-8'}
    processes = [subprocess.Popen([sys.executable, '-c', code, task['research_task_id']],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        _, error = process.communicate(timeout=20)
        assert process.returncode == 0, error.decode('utf-8')
    payload = ledger.read_task(task_id=task['research_task_id'])
    assert len(payload['tool_receipts']) == len(payload['candidate_pool']) == 1
    assert payload['candidate_pool'][0]['seen_count'] == 1
    assert sum(row['kind'] == 'candidate_page_received' for row in payload['events']) == 1


def test_required_coverage_gap_keeps_evidence_but_does_not_upgrade_candidate_as_complete(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    from kr_core.evidence_assets import EvidenceAssets
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path/'assets'))
    task = open_task(objective='按请求区分完整', budget='fast', considered=[])
    task_id = task['research_task_id']
    url = 'https://example.org/a'
    ledger.submit_tool_result(task_id=task_id, trace_id='search', tool='kr_web_search', status='ok',
        result={'items': [{'url': url}]})
    ref = EvidenceAssets().put(url, '正文可独立使用')
    result = ledger.submit_tool_result(task_id=task_id, trace_id='detail', tool='get_content_detail', status='ok',
        result={'url': url, 'evidence_assets': [ref],
                'content_coverage': {'request_satisfied': False, 'required_gaps': ['comments']}})
    assert result['status'] == 'recorded' and result['receipt']['status'] == 'partial'
    current = ledger.read_task(task_id=task_id)
    assert current['candidate_pool'][0]['stage'] == 'degraded_or_blocked'
    assert current['tool_receipts'][-1]['evidence_versions'] == [ref['version_id']]


def test_late_waiting_submission_does_not_downgrade_terminal_evidence(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    from kr_core.evidence_assets import EvidenceAssets
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path/'assets'))
    task_id = open_task(objective='迟到等待', budget='fast', considered=[])['research_task_id']
    url = 'https://example.org/a'
    ledger.submit_tool_result(task_id=task_id, trace_id='search', tool='kr_web_search', status='ok',
        result={'items': [{'url': url}]})
    ref = EvidenceAssets().put(url, '正文')
    args = dict(task_id=task_id, tool='get_content_detail', receipt_kind='background_completion')
    ledger.submit_tool_result(**args, trace_id='task:result', status='ok',
        result={'url': url, 'evidence_assets': [ref], 'task': {'task_id': 'task', 'status': 'completed'}})
    waiting = ledger.submit_tool_result(**args, trace_id='task:waiting', status='partial',
        result={'url': url, 'evidence_assets': [ref], 'task': {'task_id': 'task', 'status': 'waiting_for_user'}})
    assert waiting['candidate_association']['association'] == 'superseded_background_result'
    assert ledger.read_task(task_id=task_id)['candidate_pool'][0]['stage'] == 'detail_extracted'


def test_empty_discovery_is_recorded_once_even_when_submission_is_replayed(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task_id = open_task(objective='空搜索也要留痕', budget='fast', considered=[])['research_task_id']
    for _ in range(2):
        ledger.submit_tool_result(task_id=task_id, trace_id='empty-search', tool='kr_web_search',
            status='ok', result={'items': []})
    payload = ledger.read_task(task_id=task_id)
    pages = [event for event in payload['events'] if event['kind'] == 'candidate_page_received']
    assert len(pages) == 1 and pages[0]['metadata']['candidate_count'] == 0


def test_repair_reaches_late_files_and_isolates_failed_item(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    for i in range(101):
        task = ledger.open_task(objective="scan", budget="fast", task_id=f"kr-research-{i:03d}", considered=[])
        if i in {0, 50, 100}:
            ledger._record_pending_submission(task_id=task["research_task_id"], trace_id=f"trace-{i}",
                tool="search_demo", status="ok", result={"items": []}, receipt_options={}, reason="fixture")
    original = ledger._submit_tool_result_once
    def fail_first(**kw):
        if kw["trace_id"] == "trace-0":
            raise OSError("one corrupt destination")
        return original(**kw)
    monkeypatch.setattr(ledger, "_submit_tool_result_once", fail_first)
    result = ledger.repair_pending_submissions(limit=50)
    assert set(result["repaired"]) == {"trace-50", "trace-100"}
    assert result["remaining"] == ["trace-0"] and result["has_pending"]
    assert ledger.read_task(task_id="kr-research-050")["pending_submissions"] == []


def test_failed_pending_storage_is_explicit_not_fake_recovery(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = ledger.open_task(objective="disk", budget="fast", considered=[])
    def unavailable(*args, **kw):
        raise OSError("disk unavailable")
    monkeypatch.setattr(ledger, "_submit_tool_result_once", unavailable)
    monkeypatch.setattr(ledger, "_record_pending_submission", unavailable)
    result = ledger.submit_tool_result(task_id=task["research_task_id"], trace_id="same", tool="detail_demo",
        status="ok", result={"content": "already collected"}, _record_pending=True)
    assert result["status"] == "recording_failed"
    assert result["recovery"] == {"status": "not_persisted", "automatic_replay": False, "retry_collection": False}


@pytest.mark.parametrize("state,required,expected", [
    ("running", True, "recorded_with_gaps"), ("failed", True, "recorded_with_gaps"),
    ("cancelled", True, "recorded_with_gaps"), ("waiting_for_user", True, "recorded_with_gaps"),
    ("running", False, "recorded"), ("failed", False, "recorded"),
    ("completed", True, "recorded"),
])
def test_closeout_distinguishes_required_and_optional_work(monkeypatch, tmp_path, state, required, expected):
    from runtime import tasks
    from types import SimpleNamespace
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="completion", budget="fast", considered=[])
    record_tool_receipt(task_id=task["research_task_id"], trace_id="trace", tool="detail_demo", status="ok",
        result={"task": {"task_id": "background"}})
    monkeypatch.setattr(tasks, "get_task_store", lambda: SimpleNamespace(get_task=lambda _: {
        "task_id": "background", "status": state, "metadata": {"blocks_final_report": required}}))
    result = close_task(task_id=task["research_task_id"], ecology_outcomes=[], stop_rationale="Agent decision",
        key_claims=[], quality_status="pass")
    assert result["status"] == expected
    assert result["closeout"]["completion_facts"]["research_decision"] == "agent_owned"
    assert result["closeout"]["completion_facts"]["delivery"] == "not_evaluated"
    assert bool(result["closeout"]["optional_background_tasks"]) == (not required)


def test_closeout_pending_association_cannot_be_called_recorded(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="pending", budget="fast", considered=[])
    ledger._record_pending_submission(task_id=task["research_task_id"], trace_id="pending", tool="detail_demo",
        status="ok", result={}, receipt_options={}, reason="fixture")
    result = close_task(task_id=task["research_task_id"], ecology_outcomes=[], stop_rationale="Agent may stop",
        key_claims=[], quality_status="pass")
    assert result["status"] == "recorded_with_gaps"
    assert result["closeout"]["pending_submission_ids"] == ["pending"]


def test_closeout_follows_completed_continuation(monkeypatch, tmp_path):
    from runtime import tasks
    from types import SimpleNamespace
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="resume", budget="fast", considered=[])
    record_tool_receipt(task_id=task["research_task_id"], trace_id="trace", tool="detail_demo", status="partial",
        result={"task": {"task_id": "before"}})
    rows = {"before": {"status": "skipped", "metadata": {"continuation_task_id": "after"}},
            "after": {"status": "completed", "metadata": {"blocks_final_report": True}}}
    monkeypatch.setattr(tasks, "get_task_store", lambda: SimpleNamespace(get_task=rows.get))
    result = close_task(task_id=task["research_task_id"], ecology_outcomes=[], stop_rationale="done", key_claims=[], quality_status="pass")
    assert result["status"] == "recorded"


def test_repair_rotates_when_failed_item_cannot_update_attempts(monkeypatch, tmp_path):
    from runtime import research_ledger as ledger
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    for index in range(3):
        task = ledger.open_task(objective="rotation", budget="fast", task_id=f"kr-research-{index}", considered=[])
        ledger._record_pending_submission(task_id=task["research_task_id"], trace_id=f"trace-{index}",
            tool="search_demo", status="ok", result={"items": []}, receipt_options={}, reason="fixture")
    original = ledger._submit_tool_result_once
    def unavailable_first(**kwargs):
        if kwargs["trace_id"] == "trace-0":
            raise OSError("destination unavailable")
        return original(**kwargs)
    def cannot_record_attempt(*args, **kwargs):
        raise OSError("destination still unavailable")
    monkeypatch.setattr(ledger, "_submit_tool_result_once", unavailable_first)
    monkeypatch.setattr(ledger, "_bump_pending_submission", cannot_record_attempt)
    results = [ledger.repair_pending_submissions(limit=1) for _ in range(4)]
    assert {trace for result in results for trace in result["repaired"]} == {"trace-1", "trace-2"}
    assert any(result["errors"] for result in results)
    assert ledger.has_pending_submissions()
