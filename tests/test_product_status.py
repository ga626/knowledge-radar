from onboarding import product_status


def test_capability_packs_report_configuration_without_values() -> None:
    snapshot = {
        "fields": [
            {"key": "TAVILY_API_KEY", "configured": True},
            {"key": "DASHSCOPE_API_KEY", "configured": False},
        ]
    }

    rows = {row["id"]: row for row in product_status.capability_packs(snapshot)}

    assert rows["core_web"]["status"] == "ready"
    assert rows["video_media"]["status"] == "needs_setup"
    assert "TAVILY_API_KEY" not in str(rows)


def test_storage_summary_does_not_scan_source_checkout_without_product_data_root(monkeypatch) -> None:
    monkeypatch.delenv("KR_DATA_ROOT", raising=False)

    summary = product_status.storage_summary()

    assert summary["available"] is False
    assert summary["categories"] == []
    assert product_status.expired_media_cleanup(apply=True)["status"] == "SKIPPED"
