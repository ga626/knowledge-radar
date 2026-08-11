from __future__ import annotations

import json

from runtime.research_ledger import open_task, record_candidates, record_tool_receipt, update_candidate_stage


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
