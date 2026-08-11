import filter_comments


class FakePolicy:
    def is_open(self, key):
        return {"open": False}

    def mark_success(self, *args, **kwargs):
        return {}

    def mark_failure(self, *args, **kwargs):
        return {}

    def record_degradation(self, *args, **kwargs):
        return {}


class FakeUsageTracker:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)
        return kwargs


def test_comment_filter_defaults_to_bailian_model(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("KR_COMMENT_FILTER_MODELS", raising=False)

    result = filter_comments.filter_valuable_comments(
        [{"user": "u", "content": "这个配置步骤有坑，建议先检查 API 版本", "likes": 1}],
        verbose=False,
    )

    assert result["fallback"] is True
    assert result["provider"] == "bailian"
    assert result["model"] == "qwen3.5-flash"
    assert result["fallback_reason"] == "api_key_missing"


def test_comment_filter_records_bailian_usage_and_cached_tokens(monkeypatch) -> None:
    fake_usage = FakeUsageTracker()
    captured = {}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("KR_COMMENT_FILTER_MODELS", "bailian:qwen-turbo,bailian:qwen3.5-flash")
    monkeypatch.setattr(filter_comments, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(filter_comments, "get_usage_tracker", lambda: fake_usage)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"results":[{"index":1,"verdict":"keep","reason":"提供实践建议"}]}'
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 5000,
                    "completion_tokens": 20,
                    "total_tokens": 5020,
                    "prompt_tokens_details": {"cached_tokens": 4200},
                },
            }

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(filter_comments.httpx, "post", fake_post)

    result = filter_comments.filter_valuable_comments(
        [{"user": "u", "content": "这里需要先升级依赖，否则会报错", "likes": 3}],
        verbose=False,
    )

    assert result["provider"] == "bailian"
    assert result["model"] == "qwen3.5-flash"
    assert result["kept"] == 1
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert fake_usage.records[0]["metadata"]["provider"] == "bailian"
    assert fake_usage.records[0]["metadata"]["cached_tokens"] == 4200
    assert fake_usage.records[0]["metadata"]["cache_hit_ratio"] == 0.84
    assert result["input_tokens"] == 5000
    assert result["output_tokens"] == 20
    assert result["cached_tokens"] == 4200
    assert result["cache_hit_ratio"] == 0.84
    assert result["usage"]["raw"]["total_tokens"] == 5020
    assert result["estimated_cost_rmb"] == 0.001512
