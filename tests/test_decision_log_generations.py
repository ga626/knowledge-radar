from kr_core.decision_log import DecisionLogEvent, DecisionLogger


def test_decision_summary_exposes_generation_and_reproducibility(tmp_path):
    logger = DecisionLogger(str(tmp_path / "decision.jsonl"))
    logger.record(DecisionLogEvent(event_type="detail", platform="B站", url="u", success=False, error="provider", code_generation="g2", reproducible=True))
    logger.record(DecisionLogEvent(event_type="detail", platform="B站", url="u", success=True, code_generation="g2"))
    summary = logger.summarize(10)
    assert summary["by_generation"]["g2"] == 2
    assert summary["reproducible_failure_count"] == 1
