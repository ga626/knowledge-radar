from runtime.page_access_state import adapt_xhs_page_state, classify_page_access_state


def test_soft_security_marker_with_results_is_ok_warning() -> None:
    result = classify_page_access_state(
        platform="liepin",
        operation="search",
        blocked_marker=True,
        result_item_count=1,
        card_count=8,
    )

    assert result["status"] == "ok"
    assert result["platform_state"] == "soft_security_prompt_with_results"
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert result["manual_action_required"] is False
    assert result["page_state"]["result_readability"] == "readable"


def test_security_marker_without_readability_requires_interaction() -> None:
    result = classify_page_access_state(
        platform="boss",
        operation="search",
        blocked_marker=True,
        result_item_count=0,
        card_count=0,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "platform_verification_required"
    assert result["platform_state"] == "hard_security_block"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_weak_security_marker_without_readability_is_ambiguous_not_manual() -> None:
    result = classify_page_access_state(
        platform="liepin",
        operation="search",
        blocked_marker=True,
        result_item_count=0,
        card_count=0,
        security_evidence_strength="weak",
    )

    assert result["status"] == "ambiguous"
    assert result["failure_type"] == "ambiguous_page_state"
    assert result["platform_state"] == "suspected_manual_gate_not_confirmed"
    assert result["manual_action_required"] is False
    assert result["manual_confidence"] == "suspected"


def test_strong_security_evidence_without_readability_requires_interaction() -> None:
    result = classify_page_access_state(
        platform="liepin",
        operation="search",
        blocked_marker=True,
        security_evidence_strength="strong",
        captcha_element_count=1,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "platform_verification_required"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_login_marker_without_readability_requires_interaction() -> None:
    result = classify_page_access_state(
        platform="maimai",
        operation="search",
        login_marker=True,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "login_required"
    assert result["platform_state"] == "login_required"
    assert result["manual_action_required"] is True


def test_weak_login_marker_without_readability_is_ambiguous_not_manual() -> None:
    result = classify_page_access_state(
        platform="liepin",
        operation="search",
        login_marker=True,
        login_evidence_strength="weak",
    )

    assert result["status"] == "ambiguous"
    assert result["failure_type"] == "ambiguous_page_state"
    assert result["suspected_manual_kind"] == "login"
    assert result["manual_action_required"] is False


def test_rate_limit_is_retry_later_not_manual_login() -> None:
    result = classify_page_access_state(
        platform="zhilian",
        operation="search",
        rate_limit_marker=True,
    )

    assert result["status"] == "retry_later"
    assert result["failure_type"] == "rate_limited"
    assert result["manual_action_required"] is False
    assert result["safe_to_retry"] is True


def test_detail_content_readability_beats_corner_login_prompt() -> None:
    result = classify_page_access_state(
        platform="boss",
        operation="detail",
        login_marker=True,
        content_chars=500,
        content_readable=True,
    )

    assert result["status"] == "ok"
    assert result["platform_state"] == "soft_login_prompt_with_results"
    assert result["manual_action_required"] is False


def test_empty_detail_stays_non_manual() -> None:
    result = classify_page_access_state(platform="liepin", operation="detail")

    assert result["status"] == "empty"
    assert result["failure_type"] == "empty_detail"
    assert result["manual_action_required"] is False


def test_structured_recruitment_search_without_readability_is_tool_failure() -> None:
    result = classify_page_access_state(
        platform="boss",
        operation="search",
        result_item_count=0,
        card_count=0,
        link_count=0,
        structured_list_expected=True,
        query_reflected=True,
    )

    assert result["status"] == "failed"
    assert result["failure_type"] == "tool_failure_needs_repair"
    assert result["platform_state"] == "search_route_unreadable"
    assert result["manual_action_required"] is False


def test_structured_recruitment_search_with_empty_marker_can_be_true_empty() -> None:
    result = classify_page_access_state(
        platform="boss",
        operation="search",
        result_item_count=0,
        card_count=0,
        structured_list_expected=True,
        empty_marker=True,
    )

    assert result["status"] == "empty"
    assert result["failure_type"] == "empty_results"
    assert result["manual_action_required"] is False


def test_xhs_adapter_preserves_manual_semantics() -> None:
    result = adapt_xhs_page_state(
        {
            "schema": "xhs-page-state/v1",
            "platform_state": "platform_verification_required",
            "failure_subtype": "captcha_element_detected",
            "manual_action_required": True,
            "safe_to_retry": False,
            "safe_to_switch_account": True,
        }
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "platform_verification_required"
    assert result["platform_state"] == "platform_verification_required"
    assert result["page_state"]["source_platform_state"] == "platform_verification_required"


def test_xhs_adapter_maps_http_429_to_retry_later_when_non_manual() -> None:
    result = adapt_xhs_page_state(
        {
            "schema": "xhs-page-state/v1",
            "platform_state": "platform_verification_required",
            "failure_subtype": "http_429",
            "manual_action_required": False,
            "safe_to_retry": True,
        }
    )

    assert result["status"] == "retry_later"
    assert result["failure_type"] == "rate_limited"
    assert result["platform_state"] == "rate_limited"
