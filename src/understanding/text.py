"""通用HTML纯文本辅助，不含平台I/O。"""
import re

def strip_html_text(value: str) -> str:
    """Convert simple HTML-rich platform content into readable plain text."""
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html
    text = _html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
