from __future__ import annotations

import json

from runtime.research_ledger import open_task, record_candidates, record_tool_receipt, update_candidate_stage


def test_github_discovery_records_structured_provenance_and_upgrades_repository_candidate(monkeypatch, tmp_path):
    from runtime.research_ledger import submit_tool_result
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    task = open_task(objective='GitHub repository', budget='balanced', considered=[])
    tid = task['research_task_id']
    repository = 'https://github.com/py-pdf/pypdf'
    result = {'items': [{'url': repository, 'provider_provenance': {'provider_id': 'github', 'public_only': True}}]}
    first = submit_tool_result(task_id=tid, trace_id='discovery', tool='search_github_repositories', status='ok', result=result)
    assert first['status'] == 'recorded'
    from kr_core.evidence_assets import EvidenceAssets
    ref = EvidenceAssets().put(repository, 'public README body', title='README')
    detail = submit_tool_result(task_id=tid, trace_id='detail', tool='get_content_detail', status='ok', result={
        'platform': 'GitHub', 'url': repository + '/blob/' + 'a' * 40 + '/README.md',
        'source_url': repository, 'evidence_assets': [ref],
        'provenance': {'github': {'repository': 'py-pdf/pypdf', 'public_only': True, 'object_kind': 'readme'}}})
    assert detail['candidate_association']['association'] == 'unique_source_identity'
    assert detail['candidate_association']['status'] == 'recorded'
    other = submit_tool_result(task_id=tid, trace_id='other-repo', tool='get_content_detail', status='ok', result={
        'platform':'GitHub', 'url':'https://github.com/other/repo/blob/'+'a'*40+'/README.md',
        'evidence_assets':[ref], 'provenance':{'github':{'repository':'other/repo','public_only':True}}})
    assert other['candidate_association']['association'] == 'unmatched'
    issue_url = repository + '/issues/1'
    submit_tool_result(task_id=tid, trace_id='issue-discovery', tool='search_github_repositories', status='ok', result={'items':[{'url':issue_url}]})
    issue = submit_tool_result(task_id=tid, trace_id='issue-detail', tool='get_content_detail', status='ok', result={
        'platform':'GitHub', 'url':issue_url, 'evidence_assets':[ref],
        'provenance':{'github':{'repository':'py-pdf/pypdf','public_only':True,'object_kind':'issue'}}})
    assert issue['candidate_association']['association'] == 'unique_source_identity'
    assert issue['candidate_association']['candidate_id'] != detail['candidate_association']['candidate_id']


def test_candidate_ledger_deduplicates_and_never_persists_raw_query_or_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    task = open_task(objective="真实调研", budget="deep", task_id="candidate-ledger", considered=[])
    discovery = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-discovery", tool="search_zhihu", status="ok")["receipt"]
    detail = record_tool_receipt(task_id=task["research_task_id"], trace_id="trace-detail", tool="get_content_detail", status="ok")["receipt"]

    receipt = record_candidates(
        task_id=task["research_task_id"],
        source_ecology="zhihu_discussion_ecology",
        tool="search_zhihu",
        query="不应写入账本的原始查询",
        language="zh-CN",
        intent_label="user_experience",
        items=[
            {"url": "https://www.zhihu.com/question/123", "title": "不应写入账本的标题"},
            {"url": "https://www.zhihu.com/question/123", "title": "重复候选"},
        ],
        receipt_id=discovery["receipt_id"],
    )

    assert len(receipt["candidates"]) == 2
    assert receipt["candidates"][0]["candidate_id"] == receipt["candidates"][1]["candidate_id"]
    updated = update_candidate_stage(
        task_id=task["research_task_id"],
        candidate_id=receipt["candidates"][0]["candidate_id"],
        stage="detail_extracted",
        tool="get_content_detail",
        evidence_receipt_ids=[detail["receipt_id"]],
    )
    assert updated["status"] == "recorded"

    persisted = json.loads((tmp_path / "research_tasks" / "candidate-ledger.json").read_text(encoding="utf-8"))
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert "不应写入账本的原始查询" not in serialized
    assert "https://www.zhihu.com/question/123" not in serialized
    assert "detail_extracted" in serialized
    assert "candidate_page_received" in serialized
    assert "hmac-sha256:" in serialized
