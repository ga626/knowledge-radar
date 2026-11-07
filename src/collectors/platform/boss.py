"""BOSS DOM collector through the shared OpenCLI daily-browser bridge.

本站点只保留固定 DOM 操作、城市及职位语义；公共通道不包含站点知识。
"""
from __future__ import annotations

import time
import hashlib
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import json
from runtime.native_site import NativeSite
from runtime.recruitment_city import resolve_recruitment_city

HOME = "https://www.zhipin.com/web/geek/jobs"
LOGIN_URL = 'https://www.zhipin.com/web/user/'


def authenticated(data):
    return bool(data.get('authenticated_ui') and not data.get('login') and not data.get('risk'))


def search_url(keyword: str, city: str = "") -> str:
    resolution = resolve_recruitment_city("boss", city)
    if city and resolution.get("status") == "missing":
        raise ValueError("city_mapping_missing")
    params = {"query": keyword}
    if resolution.get("param_value"):
        params["city"] = resolution["param_value"]
    return HOME + "?" + urlencode(params)


def _failure(code: str, *, request_id: str = "", retryable: bool = False, **extra):
    manual = code in {'login_required','AUTH_REQUIRED'}
    cooling = code in {'anti_bot_verification','SECURITY_BLOCK','rate_limited'}
    return {
        "platform": "BOSS直聘",
        "status": "failed",
        "failure_type": code,
        "error": {"type": code, "failure_type": code, "manual_action_required": manual},
        "items": [],
        "retryable": retryable,
        "manual_action_required": manual,
        "resume_policy": "bounded_read_only_probe" if cooling else "stop_no_auto_resume" if code == 'request_outcome_unknown' else "explicit_retry",
        "metadata": {"transport": "opencli", "ownership": "borrowed", "request_id": request_id, **extra},
    }


class BossOpenCLI:
    def request(self, operation, payload, *, platform='boss', timeout_s=30):
        if operation == 'probe':
            return {'ok': False, 'code': 'auth_state_unconfirmed', 'request_executed': False}
        if platform != 'boss' or operation not in {'search','detail','probe','auth_verify','login','attention','release','release_login'}:
            return {'ok': False, 'code': 'unsupported_boss_operation', 'request_executed': False}
        url = str(payload.get('url') or '')
        if url and (urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in {'www.zhipin.com','zhipin.com'}):
            return {'ok': False, 'code': 'invalid_boss_url', 'request_executed': False}
        from runtime.opencli_bridge import invoke_page
        source = Path(__file__).with_name('boss_dom.js').read_text(encoding='utf-8').rstrip().rstrip(';')
        expression = '(async()=>{const site=' + source + ';return await site.extract(' + json.dumps(operation) + ');})()'
        return invoke_page(session='kr:boss', operation=operation, url=url, expression=expression,
            wait_selector='.job-card-wrapper,.job-sec-text,.sign-dialog,.sign-form,.geetest_panel,.verify-wrap,.job-empty-wrapper',
            passive_manual_login=True, isolated_work=True)

    def health(self):
        from runtime.xhs_opencli import connection_status
        status = connection_status()
        return {'ok': status['connected'], 'connected': status['connected'], 'transport': 'opencli',
                'auth_state': 'not_probed', 'connection_reason': status['connection_reason']}

    def cancel_task(self, task_id):
        return {'task_id': task_id, 'browser_closed': False, 'cleanup': 'owned_session_idle_timeout'}

    def cancel(self, request_id):
        return {'request_id': request_id, 'browser_closed': False, 'cleanup': 'owned_session_idle_timeout'}


class BossSite(NativeSite):
    def bridge(self): return BossOpenCLI()
    def transport_name(self): return 'opencli'
    def resource_name(self): return 'opencli:boss'

    def runtime_identity(self):
        from runtime.opencli_bridge import connection_config
        config = connection_config()
        from runtime.browser_channel import browser_resource_contract
        contract = browser_resource_contract(ownership='borrowed', resource_id='opencli:boss', generation=str(config['context_id']), driver='opencli')
        return {**contract, 'backend': 'opencli', 'context': config['context_id'],
                'dom_sha256': hashlib.sha256(Path(__file__).with_name('boss_dom.js').read_bytes()).hexdigest(),
                'auth_generation': self.state().get('auth_generation', '')}


def _bridge():
    return BossOpenCLI()


def _site():
    return BossSite('boss', 'BOSS直聘', HOME, LOGIN_URL, authenticated)


def state_path(): return _site().state_path()
def admission(): return _site().admission()
def runtime_identity(): return _site().runtime_identity()
def interaction_status(): return _site().interaction_status()
def request_login(*, activate=False): return _site().request_login(activate=activate)
def complete_login(): return _site().complete_login()
def cancel_login(): return _site().cancel_login()
def probe_auth(): return _site().probe_auth()


def _map_response(response: dict, *, operation: str):
    if not response.get("ok"):
        code = str(response.get("code") or response.get("failure_type") or "native_command_failed")
        mapping = {
            "native_host_unavailable": "native_host_unavailable",
            "native_host_unreachable": "native_host_unreachable",
            "browser_disconnected": "browser_disconnected",
            "native_command_timeout": "native_command_timeout",
            "cancelled": "cancelled",
            "SECURITY_BLOCK": "SECURITY_BLOCK",
            "AUTH_REQUIRED": "AUTH_REQUIRED",
        }
        result = _failure(mapping.get(code, code), request_id=str(response.get("request_id") or ""), retryable=False)
        if response.get('resume_policy'):
            result['resume_policy'] = response['resume_policy']
        if response.get('request_executed') is False or code in {'native_host_unavailable', 'native_host_unreachable', 'browser_busy', 'invalid_boss_url'}:
            result['request_executed'] = False
        return result
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if data.get("risk"):
        from runtime.file_write_lock import file_write_lock
        from runtime.native_site import write_state
        with file_write_lock(state_path()):
            write_state(state_path(), {**_site().state(), 'risk': True, 'reason': 'SECURITY_BLOCK', 'at': time.time()})
        return _failure("SECURITY_BLOCK", request_id=str(response.get("request_id") or ""))
    if data.get("login") or data.get("detail_login_required"):
        return _failure("AUTH_REQUIRED", request_id=str(response.get("request_id") or ""))
    if operation == "search":
        items = data.get("items") or []
        if not items and not data.get('empty'):
            return _failure('page_structure_or_not_ready', request_id=str(response.get('request_id') or ''))
        normalized = []
        for item in items:
            item = dict(item)
            raw = str(item.get('salary_raw') or item.get('salary') or '')
            if any('\ue000' <= c <= '\uf8ff' for c in raw):
                item.update(salary='', salary_raw=raw, salary_status='obfuscated_dom', salary_claim_allowed=False)
            else:
                item.update(salary_raw=raw, salary_claim_allowed=bool(raw))
            item['missing_fields'] = [key for key in ('title', 'company', 'salary') if not item.get(key)]
            normalized.append(item)
        return {
            "platform": "BOSS直聘", "status": "ok" if items else ("empty" if data.get("empty") else "failed"),
            "items": normalized, "total": len(normalized),
            "empty_reason": "no_matching_jobs" if not items and data.get("empty") else "",
            "metadata": {"transport": "opencli", "source": "dom", "query": data.get("query", ""), "observed_url": data.get("url", "")},
        }
    content = str(data.get("content") or "")
    if not content:
        return _failure("empty_detail", request_id=str(response.get("request_id") or ""), observed_url=data.get("url", ""))
    from collectors.platform.job_domain import job_detail_quality as _job_detail_quality
    quality = _job_detail_quality(content, extraction_source=str(data.get('content_source') or 'content_script_dom'), title=data.get("title", ""))
    if not quality['ok']:
        result = _failure('empty_detail', request_id=str(response.get('request_id') or ''))
        result.update(content=content, detail_quality=quality, url=data.get('url', ''))
        return result
    if any('\ue000' <= c <= '\uf8ff' for c in str(data.get('salary') or '')):
        data = {**data, 'salary_raw': data['salary'], 'salary': '', 'salary_status': 'obfuscated_dom'}
    return {
        **data,
        "status": "ok", "platform": "BOSS直聘", "content_type": "recruitment_job_detail",
        "detail_quality": quality,
        'salary_claim_allowed': bool(data.get('salary')) and data.get('salary_status') != 'obfuscated_dom',
        'missing_fields': [key for key in ('title', 'company', 'salary') if not data.get(key)],
        "metadata": {"transport": "opencli", "source": "dom", "observed_url": data.get("url", "")},
    }


def _search(keyword: str, city: str = "", limit: int = 10):
    try:
        url = search_url(keyword, city)
    except ValueError:
        return {**_failure("city_mapping_missing"), 'request_executed': False}
    started = time.monotonic()
    response = _site().invoke("search", url=url, keyword=keyword, city=city, limit=limit)
    data=response.get('data') or {}
    if response.get('ok') and data.get('url') == 'about:blank':
        return _site().failure('browser_navigation_interrupted')
    if response.get('ok') and not data.get('risk') and not data.get('login') and data.get('query') != keyword:
        return _site().failure('search_query_not_applied')
    result = _map_response(response, operation="search")
    if result.get("status") in {"ok", "empty"}:
        items = result.get("items") or []
        from collectors.platform.job_domain import _apply_city_filter
        items,city_filter=_apply_city_filter(items,city)
        omitted=max(0,len(items)-max(1,min(20,limit)))
        result["items"] = items[: max(1, min(20, limit))]
        result["total"] = len(result["items"])
        result['metadata'].update(city_filter={**city_filter,'dropped':city_filter['dropped_count']},result_limit={'omitted':omitted})
    if result.get('failure_type') == 'login_required':result['interaction']=request_login()
    result.setdefault("metadata", {})["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def _detail(url: str):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.zhipin.com", "zhipin.com"} or not parsed.path.startswith("/job_detail/"):
        return {**_failure("invalid_boss_detail_url"), 'request_executed': False}
    started = time.monotonic()
    response = _site().invoke("detail", url=url)
    data=response.get('data') or {}
    if response.get('ok') and not data.get('risk') and not data.get('login'):
        if data.get('url') == 'about:blank':return _site().failure('browser_navigation_interrupted')
        if urlsplit(data.get('url','')).path != parsed.path:return _site().failure('page_identity_changed')
    result = _map_response(response, operation="detail")
    if result.get('failure_type') == 'login_required':result['interaction']=request_login()
    result.setdefault("metadata", {})["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result


def cancel(request_id: str):
    return _bridge().cancel(request_id)


def cancel_task(task_id: str):
    return _bridge().cancel_task(task_id)


def health():
    return _bridge().health()


def search(keyword: str, city: str = '', limit: int = 10):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('boss', 'search', lambda: _search(keyword, city, limit), keyword=keyword, city=city)


def detail(url: str):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('boss', 'detail', lambda: _detail(url))
