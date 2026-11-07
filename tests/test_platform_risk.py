from runtime.platform_risk import (
    build_manual_interaction_envelope,
    compute_platform_cooldown,
    normalize_platform_risk_event,
)


def test_manual_event_does_not_cooldown() -> None:
    event = normalize_platform_risk_event(
        platform="xhs",
        operation="search",
        reason_code="login_required",
        outcome="failed",
    )

    cooldown = compute_platform_cooldown(event, base_s=1800, maximum_s=7200, previous_cooldown_s=1800, now=1000)

    assert event.manual_action_required is True
    assert event.recoverability == "manual_interaction"
    assert cooldown["cooldown_seconds"] == 0
    assert cooldown["source"] == "manual_interaction"


def test_platform_verification_is_bounded_cooldown_not_manual_unlock():
    event=normalize_platform_risk_event(platform='boss',operation='search',reason_code='platform_verification_required',outcome='failed')
    assert not event.manual_action_required and event.recoverability=='cooldown'
    cooldown=compute_platform_cooldown(event,base_s=60,maximum_s=300,jitter_ratio=0,now=1000)
    assert cooldown['next_retry_at']==1060


def test_recruitment_gate_persists_verification_cooldown_without_manual_override(tmp_path,monkeypatch):
    from runtime import recruitment_governance as gate
    monkeypatch.setenv('KR_TASK_DB_PATH',str(tmp_path/'tasks.sqlite3'))
    monkeypatch.setattr(gate.time,'time',lambda:1000)
    gate.record_search_outcome('boss','blocked','platform_verification_required')
    state=gate.check_search_gate('boss')
    assert not state['allowed'] and 60<=state['cooldown_remaining_s']<=300
    monkeypatch.setattr(gate.time,'time',lambda:1301)
    assert gate.check_search_gate('boss')['allowed']


def test_retry_after_wins_over_backoff() -> None:
    event = normalize_platform_risk_event(
        platform="semanticscholar",
        operation="api_search",
        reason_code="HTTP_429",
        outcome="failed",
        retry_after_s=42,
    )

    cooldown = compute_platform_cooldown(event, base_s=5, maximum_s=300, previous_cooldown_s=60, jitter_ratio=0, now=1000)

    assert cooldown["cooldown_seconds"] == 42
    assert cooldown["next_retry_at"] == 1042
    assert cooldown["source"] == "retry_after"


def test_dynamic_backoff_doubles_previous_until_cap() -> None:
    event = normalize_platform_risk_event(
        platform="boss",
        operation="search",
        reason_code="temporary_blocked",
        outcome="failed",
    )

    cooldown = compute_platform_cooldown(event, base_s=1800, maximum_s=7200, previous_cooldown_s=3600, jitter_ratio=0, now=1000)

    assert cooldown["cooldown_seconds"] == 7200
    assert cooldown["source"] == "dynamic_backoff"


def test_manual_interaction_envelope_is_resumable() -> None:
    envelope = build_manual_interaction_envelope(
        platform="xhs",
        reason_code="security_verification",
        original_tool="search_xiaohongshu",
        original_args={"keyword": "coffee", "search_type": "image"},
    )

    assert envelope["status"] == "NEEDS_INTERACTION"
    assert envelope["manual_action_required"] is True
    assert envelope["retry_mode"] == "complete_browser_interaction:xhs"
    assert envelope["original_tool"] == "search_xiaohongshu"
    assert envelope["original_args_hash"]
    assert envelope["resume_policy"] == "retry_once_after_complete"
