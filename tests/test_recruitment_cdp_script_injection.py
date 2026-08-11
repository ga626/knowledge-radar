from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_zhilian_page_extractor_uses_injected_selector_payload() -> None:
    source = (ROOT / "src" / "collectors" / "platform" / "zhilian.py").read_text(encoding="utf-8")
    assert "const selectorPayload = ${JSON.stringify(selectors)};" in source
    assert "new Set((selectorPayload.card || []).flatMap" in source
    assert "selectorPayload.title || []" in source
    assert "selectorPayload.salary || []" in source
    assert "selectorPayload.company || []" in source
    assert "selectorPayload.area || []" in source
    assert "selectorPayload.link || []" in source
    assert "const maxItems = ${limit};" in source
    assert "items.length >= maxItems" in source
    assert "new Set((selectors.card || []).flatMap" not in source
    assert "pickText(card, selectors." not in source
    assert "pickEl(card, selectors." not in source
    assert "items.length >= limit" not in source


def test_liepin_wait_probe_uses_injected_card_selectors() -> None:
    source = (ROOT / "src" / "collectors" / "platform" / "liepin.py").read_text(encoding="utf-8")
    assert "const cardSelectors = ${JSON.stringify(selectors.card || [])};" in source
    assert "new Set(cardSelectors.flatMap" in source
    assert "登录\\\\/注册|登录获取更匹配职位" in source
    assert "new Set((selectors.card || []).flatMap" not in source
