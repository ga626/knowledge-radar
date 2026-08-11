from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.kr_source_ecology_eval import evaluate_trace_dir, source_ecology_cases


def test_source_ecology_eval_cases_are_discoverable() -> None:
    cases = source_ecology_cases()

    assert {case["id"] for case in cases} >= {"media_detail_evidence_repair", "public_discourse_source_ecology"}


def test_source_ecology_eval_reports_missing_trace(tmp_path: Path) -> None:
    result = evaluate_trace_dir(tmp_path)

    assert result["status"] == "FAIL"
    assert result["missing_trace_count"] >= 1
    assert any(item["status"] == "MISSING_TRACE" for item in result["results"])


def test_source_ecology_eval_accepts_covered_ecologies(tmp_path: Path) -> None:
    (tmp_path / "media_detail_evidence_repair.json").write_text(
        json.dumps(
            {
                "tool_calls": ["search_bilibili", "get_content_detail"],
                "evidence_surfaces": ["media_detail", "source_ecology"],
                "output_artifacts": ["evidence_register", "quality_check"],
                "repair_loop": True,
                "skip_reasons": [{"surface": "comments", "reason": "not needed for this trace"}],
                "evidence_items": [{"id": "E001", "source_ecology": "bilibili_video_ecology"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "public_discourse_source_ecology.json").write_text(
        json.dumps(
            {
                "tool_calls": ["kr_web_search", "search_wechat_articles", "search_zhihu", "search_bilibili"],
                "evidence_surfaces": ["source_ecology"],
                "output_artifacts": ["evidence_register"],
                "skip_reasons": [{"surface": "media_detail", "reason": "source ecology search was sufficient"}],
                "evidence_items": [
                    {"id": "E001", "source_ecology": "wechat_public_article_ecology"},
                    {"id": "E002", "source_ecology": "zhihu_discussion_ecology"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_trace_dir(tmp_path)

    assert result["status"] == "PASS", result
    assert result["pass_count"] == result["case_count"]
