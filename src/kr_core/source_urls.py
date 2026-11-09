"""公共来源身份与脱敏；不依赖任何平台获取器。"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE = re.compile(r"token|secret|password|passwd|authorization|credential|signature|api[_-]?key|access[_-]?key|session|cookie|auth|^code$|^dis_[kt]$", re.I)

def redact_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([(k, "[redacted]" if SENSITIVE.search(k) else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "[invalid_url]"


def canonical_url(url: str) -> str:
    """公开身份不含访问令牌；原始 fetch URL 只留在取数调用内。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return url
        safe = urlsplit(redact_url(url))
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not SENSITIVE.search(k)])
        return urlunsplit((safe.scheme, safe.netloc, safe.path, query, ""))
    except (ValueError, TypeError):
        return "[invalid_url]"


def discovery_url(url: str) -> str:
    """候选身份额外忽略已知营销参数；不改取数URL或固定证据版本身份。"""
    safe = canonical_url(url)
    try:
        parsed = urlsplit(safe)
        query = [(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid","fbclid","msclkid"}]
        return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query),""))
    except ValueError:
        return safe


def _public_text_url(match, *, preserve_location=False):
    """Keep Markdown/prose delimiters out of a URL before query redaction."""
    candidate = match.group()
    depths = {'(': 0, '[': 0, '{': 0}
    closing = {')': '(', ']': '[', '}': '{'}
    end = len(candidate)
    for index, char in enumerate(candidate):
        if char in depths:
            depths[char] += 1
        elif char in closing:
            opening = closing[char]
            if not depths[opening]:
                end = index
                break
            depths[opening] -= 1
    url = candidate[:end].rstrip('.,;!，。；！')
    if preserve_location:
        try:
            parsed = urlsplit(url)
            protected_query = any(SENSITIVE.search(k) for k, _ in parse_qsl(parsed.query, keep_blank_values=True))
            protected_fragment = any(SENSITIVE.search(k) for k, _ in parse_qsl(parsed.fragment, keep_blank_values=True))
            if not (parsed.username or parsed.password or protected_query or protected_fragment):
                return url + candidate[len(url):]
            safe = urlsplit(canonical_url(url))
            projected = urlunsplit((*safe[:4], '' if protected_fragment else parsed.fragment))
            return projected + candidate[len(url):]
        except ValueError:
            return '[invalid_url]' + candidate[len(url):]
    return canonical_url(url) + candidate[len(url):]


def public_document_text(text):
    """可读正文投影：保留公开链接及段落/源码定位，只清理受保护的访问参数。"""
    return re.sub(r"https?://[^\s<>\"']+", lambda match: _public_text_url(match, preserve_location=True), text)


def public_evidence_payload(value):
    """在持久证据边界递归移除访问凭据，并清理文本内的受保护链接。"""
    if isinstance(value, dict):
        return {k: public_evidence_payload(v) for k, v in value.items()
                if not re.fullmatch(r"(?:fetch_url|xsec_token|access_token|refresh_token|api_key|authorization|cookie|cookies|password|secret)", str(k), re.I)}
    if isinstance(value, (tuple, list)):
        return [public_evidence_payload(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"https?://[^\s<>\"']+", _public_text_url, value)
    return value


def _public_github_original(value):
    provenance = value.get('provenance')
    github = provenance.get('github') if isinstance(provenance, dict) else None
    return (value.get('platform') == 'GitHub' and value.get('content_fidelity') == 'public_source_original'
            and isinstance(github, dict) and github.get('public_only') is True)


def public_detail_payload(value):
    """公开GitHub原件正文不参与URL清洗，元数据继续脱敏。"""
    clean = public_evidence_payload(value)
    if _public_github_original(value):
        if isinstance(value.get('content'), str):
            clean['content'] = value['content']
        for original, redacted in zip(value.get('comments') or [], clean.get('comments') or []):
            for key in ('body', 'diff_hunk'):
                if isinstance(original.get(key), str):
                    redacted[key] = original[key]
    return clean


def public_result_payload(value):
    """执行回执与后台制品共用公开结果投影，保留显式声明的GitHub原文。"""
    if isinstance(value, dict):
        clean = public_detail_payload(value)
        original_github = _public_github_original(value)
        for key, item in value.items():
            if original_github and key == 'comments':
                continue
            if key in clean and isinstance(item, (dict, list, tuple)):
                clean[key] = public_result_payload(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [public_result_payload(item) for item in value]
    return public_evidence_payload(value)
