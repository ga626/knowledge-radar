import httpx
from filter_comments import filter_valuable_comments


def test_comments_stay_complete_without_any_cloud_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("comments must not trigger a cloud request")
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "old-key")
    monkeypatch.setenv("MIMO_API_KEY", "old-key")
    comments = [
        {"id": "1", "user": "甲", "content": "赞", "likes": 1},
        {"id": "2", "user": "乙", "content": "不同意见", "likes": 0},
        {"id": "1", "user": "甲", "content": "赞", "likes": 1},
        {"id": "3", "user": "甲", "content": "赞", "likes": 1},
    ]
    result = filter_valuable_comments(comments, api_key="do-not-echo")
    assert [r["id"] for r in result["kept_comments"]] == ["1", "2", "3"]
    assert len(result["results"]) == len(comments)
    assert result["relevance_assessed"] is False
    assert result["estimated_cost_rmb"] == 0
    assert "do-not-echo" not in str(result)


def test_empty_comments_and_authors_are_not_conflated():
    assert filter_valuable_comments([])["kept"] == 0
    comments = [{"user": "甲", "content": "同文"}, {"user": "乙", "content": "同文"}]
    assert filter_valuable_comments(comments)["kept"] == 2
