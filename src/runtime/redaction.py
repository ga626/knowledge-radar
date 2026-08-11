"""Small, dependency-free redaction helpers for local diagnostics and logs."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit, urlunsplit


_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(authorization|bearer|token|secret|password|api[_-]?key|cookie|session|web_session|a1|b1|webid|x-s|x-t)\b"
    r"\s*([:=])\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{6,}")
_PATH_RE = re.compile(r"(?i)(?:[a-z]:\\users\\[^\\\s]+|/(?:users|home)/[^/\s]+)(?:[/\\][^\s,;]*)?")


def redact_url(value: object) -> str:
    """Keep only the stable URL origin/path; query strings can carry secrets."""
    text = str(value or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return "[invalid-url]"
    if not parts.scheme or not parts.netloc:
        return "[non-public-url]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact_text(value: object) -> str:
    """Remove likely credentials and user-home paths from a diagnostic string."""
    text = str(value or "")
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return _PATH_RE.sub("[LOCAL_PATH]", text)


class RedactingLogFilter(logging.Filter):
    """Redact fully formatted records before any handler writes or emits them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "[log redaction failed]"
            record.args = ()
        return True
