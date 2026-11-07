"""小红书唯一OpenCLI执行路径；借用日常浏览器，不拥有用户进程。"""
from __future__ import annotations
import hashlib
import json
import sys
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from threading import RLock
from urllib.parse import urlsplit
from runtime.paths import runtime_state_dir
from runtime.process import silent_subprocess_run
from runtime.browser_channel import BorrowedBrowser
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Annotated


class XhsReadOptions(BaseModel):
    """小红书读取窗口；仅决定请求范围，不改变平台或模型配置。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    image_indexes: list[Annotated[int, Field(ge=0)]] | None = Field(default=None, max_length=100,
        description='OCR原始图片的零基索引；不填为全部内容图，不包含评论图。空列表表示本次不选图。')
    max_images: int | None = Field(default=None, ge=1, le=100,
        description='本次下载/识别图片的显式执行上限；超过时保留未执行缺口，不缩小请求分母。')
    comment_limit: int = Field(default=10, ge=1, le=50, description='请求的主评论窗口，非全站评论总量。')
    comment_budget: int = Field(default=100, ge=1, le=200, description='包含回复的总返回条数上限。')
    refresh_collection: bool = Field(default=False,
        description='显式重新读取页面；默认在同一任务范围内复用一天内已采集快照，改选图/模型只补加工，不暗中刷新。复用会返回原采集时间。')

    @field_validator('image_indexes')
    @classmethod
    def unique_images(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('image_indexes_must_be_unique')
        return value

UPSTREAM_SHA = "97dc0cd5214b9602bcda78d7422f231ee8a803e9"
REVIEWED_ADAPTER_SHA256 = "473be8a1ff6185e2242c7ecc9914bd8b4d38f75a2c9ce81531249cf48b88083b"
RISK_CODES = {"SECURITY_BLOCK", "CAPTCHA_REQUIRED", "anti_bot_verification"}
UNKNOWN_CODES = {"COMMAND_RESULT_UNKNOWN", "opencli_caller_timeout", "caller_timeout", "opencli_invalid_output"}
PAID_REASONS = RISK_CODES | {"RATE_LIMITED", "AUTH_REQUIRED", "signed_link_expired_search_again", "empty_detail",
    "dead_link", "empty_results", "EMPTY_RESULT", "COMMENTS_NOT_LOADED", "browser_disconnected"}


def _media_failure_reason(exc):
    """Return bounded machine reasons without paths, request URLs or credentials."""
    from runtime.cloud_ocr import CloudOcrError
    from runtime.resource_concurrency import ResourceDeferred
    if isinstance(exc, ResourceDeferred):
        raise exc
    from generic_web.url_policy import URLPolicyError
    message = str(exc)
    if isinstance(exc, CloudOcrError):
        return exc.error_code
    if isinstance(exc, URLPolicyError):
        reasons = {
            "public DNS validation unavailable for proxy Fake-IP target": "public_dns_validation_unavailable",
            "target DNS unavailable": "target_dns_unavailable",
            "HTTPS downgrade redirect forbidden": "https_downgrade_redirect_forbidden",
            "non-public target address forbidden": "non_public_target_forbidden",
            "response exceeds bounded extraction size": "media_size_limit_exceeded",
        }
        return reasons.get(message, "media_url_policy_rejected")
    if isinstance(exc, ValueError) and message in {
        "ffmpeg_not_installed", "optional_component_not_installed_and_approved",
        "optional_component_execution_failed", "optional_component_empty_or_failed",
        "optional_model_identity_mismatch", "optional_freeze_identity_mismatch",
        "optional_component_role_revision_mismatch", "optional_component_locked_dependency_mismatch",
        "input_identity_mismatch", "input_outside_approved_root_or_too_large",
        "image_format_unsupported", "video_not_mp4", "media_source_refresh_required",
        "video_decode_no_media_stream", "video_decode_no_frames",
    }:
        return message
    return type(exc).__name__

def state_dir():
    return Path(os.environ.get("KR_OPENCLI_STATE_DIR") or os.environ.get("KR_XHS_OPENCLI_STATE_DIR") or runtime_state_dir() / "xhs_opencli")

def _json(path, default=None):
    if not path.is_file():
        return default or {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"corrupt": True}

def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix("." + uuid.uuid4().hex + ".pending")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)

def setting(name):
    """显式环境覆盖；连接默认使用本运行目录内的无密钥配置。"""
    if os.environ.get(name):
        return os.environ[name]
    if name in {"KR_XHS_OPENCLI_ROOT", "KR_XHS_OPENCLI_CONTEXT_ID"}:
        from runtime.opencli_bridge import connection_config
        config = connection_config()
        return str(config["root" if name == "KR_XHS_OPENCLI_ROOT" else "context_id"])
    key = {"KR_XHS_OPENCLI_ROOT":"root", "KR_XHS_OPENCLI_ADAPTER_SHA256":"adapter_sha256",
        "KR_XHS_OPENCLI_CONTEXT_ID":"context_id"}.get(name)
    filename = "binding.json" if key == "context_id" else "dependency.json"
    return str(_json(state_dir() / filename).get(key, "")) if key else ""


def runtime_identity():
    return {"backend": "opencli", "upstream": UPSTREAM_SHA,
        "adapter": setting("KR_XHS_OPENCLI_ADAPTER_SHA256"),
        "context": setting("KR_XHS_OPENCLI_CONTEXT_ID"),
        "runner": hashlib.sha256(Path(__file__).with_name("xhs_opencli_runner.mjs").read_bytes()).hexdigest(),
        "auth_revision": _json(state_dir() / "auth.json").get("revision", 0)}

def browser():
    identity = runtime_identity()
    return BorrowedBrowser(identity["context"], "kr:xiaohongshu", identity["adapter"], driver="opencli")


def account_binding_id():
    context = setting("KR_XHS_OPENCLI_CONTEXT_ID")
    return "opencli-xhs-" + hashlib.sha256(context.encode()).hexdigest()[:12] if context else ""


def _observe_account(state, reason=""):
    if not account_binding_id():
        return
    from runtime.account_observations import observe_account
    try:
        observe_account(account_binding_id(), "xhs", state, source="opencli_daily_browser", reason=reason)
    except Exception:
        # Account projection must not discard already acquired business evidence.
        pass

def connection_status():
    """无副作用检查；在线扩展不等于账号已登录。"""
    import httpx
    context = setting("KR_XHS_OPENCLI_CONTEXT_ID")
    root = Path(setting("KR_XHS_OPENCLI_ROOT") or ".")
    configured = bool(context and (root / "package.json").is_file())
    status = {}
    if configured:
        try:
            with httpx.Client(timeout=.4, trust_env=False) as client:
                response = client.get("http://127.0.0.1:19825/status", params={"contextId": context}, headers={"X-OpenCLI": "1"})
            if response.status_code == 200:
                status = response.json()
        except (httpx.HTTPError, ValueError):
            pass
    connected = bool(status.get("extensionConnected") and not status.get("profileDisconnected")
        and status.get("daemonVersion") == "2.0.2" and status.get("extensionVersion") in {"1.0.26"})
    reason = ('ready' if connected else 'binding_missing' if not configured else
        'daemon_not_running_or_unreachable' if not status else
        'daemon_identity_mismatch' if status.get('daemonVersion') != '2.0.2' else
        'bound_profile_disconnected' if status.get('profileDisconnected') else
        'extension_reconnecting' if not status.get('extensionConnected') else 'extension_identity_mismatch')
    return {"status": "ok" if connected else "not_connected", "configured": configured,
        "connected": connected, "backend": "opencli", "ownership": "borrowed",
        "connection_reason": reason, "execution_prepares_daemon": True,
        "auth_state": "not_probed", "risk_stopped": bool(_stop_gate()),
        "login": _json(state_dir() / "login.json"), "single_account": True,
        "detail": "日常Chrome扩展已连接；账号登录未探测。" if connected else
            "后台服务未运行或暂不可达；执行入口会按需准备，不代表Chrome关闭或扩展未安装。" if not status and configured else
            "扩展尚未连到获准绑定；执行入口会等待重连，超时后再按分层诊断处理。"}

def cancel_login():
    pending = _json(state_dir() / "login.json")
    if pending:
        pending["expires_at"] = 0
        pending["cancelled"] = True
        _save(state_dir() / "login.json", pending)
    return {"status": "cancelled", "browser_closed": False, "owned_tab_cleanup": "native_idle_timeout"}

def latch(reason):
    from runtime.file_write_lock import file_write_lock
    with file_write_lock(state_dir() / "risk"):
        previous = _json(state_dir() / "stop.json")
        if previous.get("unknown_outcome"):
            return
        # 一条请求的一次观察；拒绝后的查询/入场检查不会更新期限。
        now = time.time()
        if previous.get("reason") == reason and _stop_gate():
            return
        seconds = 60
        _save(state_dir() / "stop.json", {"reason": reason, "at": now,
            "retry_at": now + seconds, "unknown_outcome": reason in UNKNOWN_CODES,
            "context_id": setting("KR_XHS_OPENCLI_CONTEXT_ID")})


def _stop_gate():
    path = state_dir() / "stop.json"
    state = _json(path)
    if not state:
        return {}
    from runtime.platform_risk import risk_retry_at
    if state.get("unknown_outcome") or state.get("reason") in UNKNOWN_CODES:
        return {"code": "COMMAND_RESULT_UNKNOWN", "retry_after_s": 0}
    until = risk_retry_at(state, observed_at=path.stat().st_mtime)
    if until <= time.time():
        return {}
    return {"code": "RATE_LIMITED" if state.get("reason") == "RATE_LIMITED" else "platform_cooldown",
            "retry_after_s": until-time.time()}

def _failure(code, *, executed=True, data=None):
    restricted = code in RISK_CODES
    if executed and (restricted or code == "RATE_LIMITED" or code in UNKNOWN_CODES):
        latch(code)
    if executed and (restricted or code == "AUTH_REQUIRED"):
        _observe_account("needs_verification" if restricted else "needs_login", code)
    kind = ("request_outcome_unknown" if code in UNKNOWN_CODES else "rate_limited" if code == "RATE_LIMITED" else
            "anti_bot_verification" if restricted else "login_required" if code == "AUTH_REQUIRED" else code)
    return {"ok": False, "status": "needs_interaction" if code == "AUTH_REQUIRED" else "failed",
        "platform": "小红书", "error": {"error": code, "type": kind, "retryable": False},
        "failure_type": kind, "manual_action_required": code == "AUTH_REQUIRED",
        "request_executed": executed, "data": data or {},
        "next_action": "inspect_original_request_receipt_do_not_replay" if code in UNKNOWN_CODES else
            "wait_for_cooldown_then_new_bounded_attempt" if code in {"RATE_LIMITED", "platform_cooldown"} else "inspect_platform_state",
        "metadata": {"transport": "opencli", "fallback_used": False}}

def _run(operation, **kwargs):
    root = Path(setting("KR_XHS_OPENCLI_ROOT")).resolve()
    package = root / "package.json"
    if not package.is_file() or _json(package).get("version") != "2.0.2":
        return _failure("opencli_pinned_dependency_missing", executed=False)
    digest = hashlib.sha256()
    try:
        for name in ("search", "note", "comments", "download", "risk-control", "note-helpers"):
            rel = "clis/xiaohongshu/" + name + ".js"
            digest.update(rel.encode())
            digest.update((root / rel).read_bytes())
    except OSError:
        return _failure("opencli_pinned_dependency_missing", executed=False)
    fingerprint = digest.hexdigest()
    if fingerprint != setting("KR_XHS_OPENCLI_ADAPTER_SHA256"):
        return _failure("opencli_adapter_identity_mismatch", executed=False)
    context = setting("KR_XHS_OPENCLI_CONTEXT_ID")
    if operation != "inventory" and not context:
        return _failure("opencli_daily_browser_binding_required", executed=False)
    node = os.environ.get("KR_NODE_EXE") or shutil.which("node")
    if not node:
        return _failure("node_missing", executed=False)
    start_service = kwargs.pop("start_service", False)
    if operation in {"session_probe", "resume"}:
        pending = _json(state_dir() / "login.json")
        if pending.get("expires_at", 0) > time.time():
            kwargs["idleTimeoutS"] = max(1, int(pending["expires_at"] - time.time()))
    payload = {"pythonExecutable": sys.executable, "root": str(root), "operation": operation, "contextId": context, **kwargs}
    started = time.monotonic()
    if operation != "inventory" or start_service:
        ready = _ensure_daemon(root, node)
        if ready:
            return _failure(ready, executed=False)
    if operation in {'search', 'detail', 'comments', 'session_probe', 'resume'} and not connection_status().get('connected'):
        from runtime.browser_channel import ensure_borrowed_browser
        # 固定扩展1.0.26的重连退避上限15秒+0.5秒抖动；不因原8秒窗口误报。
        ready = ensure_borrowed_browser(lambda: bool(connection_status().get('connected')), timeout_s=18)
        if not ready['ok']:
            return {**_failure(ready['code'], executed=False), 'browser_readiness': ready}
    try:
        response = silent_subprocess_run([node, str(Path(__file__).with_name("xhs_opencli_runner.mjs"))],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            encoding="utf-8", timeout=150, cwd=str(root))
    except subprocess.TimeoutExpired:
        return _failure("opencli_caller_timeout")
    except OSError:
        return _failure("opencli_process_unavailable", executed=False)
    try:
        result = json.loads(response.stdout)
    except (ValueError, TypeError):
        return _failure("opencli_invalid_output")
    if not result.get("ok"):
        diagnostic = result.get('diagnostic')
        runner_metadata = result.get('metadata') or {}
        result = _failure(str(result.get("code") or "opencli_failed"), data=result.get("data"), executed=bool(result.get("request_executed")))
        for key in ('browser_timings', 'cleanup_warnings', 'work_session', 'work_page_closed',
                    'retained_for_interaction', 'retention_max_s', 'automatic_retry'):
            if key in runner_metadata:
                result['metadata'][key] = runner_metadata[key]
        if isinstance(diagnostic, dict):
            result['metadata']['diagnostic'] = diagnostic
    result.setdefault("status", "ok" if result.get("ok") else "failed")
    result.setdefault("metadata", {}).update(transport="opencli", upstream_sha=UPSTREAM_SHA,
        adapter_sha256=fingerprint, elapsed_s=round(time.monotonic()-started, 3),
        fallback_used=False, ownership="borrowed", browser_process_owned=False)
    return result

def _ensure_daemon(root, node):
    from runtime.opencli_bridge import ensure_daemon
    return ensure_daemon(root, node, state_root=state_dir())


def invoke(operation, **kwargs):
    control = operation in {"inventory", "inspect", "release", "resume"}
    gate = _stop_gate() if not control else {}
    if gate:
        return {**_failure(gate["code"], executed=False), "retry_after_s": gate["retry_after_s"]}
    if operation in {"search", "detail"} and _json(state_dir() / "login.json"):
        state = interaction_state()
        if state != "ready":
            return _failure("AUTH_REQUIRED", executed=False)
    if operation == "inventory":
        return _run(operation, **kwargs)
    if not browser().context_id:
        return _failure("opencli_daily_browser_binding_required", executed=False)
    try:
        with browser().lease(operation):
            response = _run(operation, **kwargs)
            # Record the browser observation before releasing its exclusive lease.
            # Slow downloads/models and checkpoint replay must not overwrite a
            # newer login/challenge observation from the next browser operation.
            data = response.get("data")
            business_observed = (
                operation == "detail" and isinstance(data, dict) and bool(data.get("content"))
            ) or (
                operation == "search" and isinstance(data, list) and any(
                    isinstance(row, dict) and re.search(r"/(?:explore|search_result)/[a-f0-9]{24}", str(row.get("url") or ""))
                    for row in data
                )
            )
            if response.get("ok") and business_observed:
                _observe_account("business_verified")
            return response
    except RuntimeError as exc:
        if str(exc) != "browser_session_busy":
            raise
        return _failure("browser_session_busy", executed=False)

def request_login():
    from runtime.leases import get_runtime_lease_coordinator, default_owner
    coordinator = get_runtime_lease_coordinator()
    grant = coordinator.acquire_exclusive("browser_interaction", browser().context_id+":kr:xiaohongshu",
        owner=default_owner("request_login", request_id=uuid.uuid4().hex), ttl_s=180)
    if not grant.acquired:
        return {"status":"waiting_for_user", "reason":"login_window_opening", "expires_at":time.time()+300}
    try:
        return _request_login_once()
    finally:
        coordinator.release(grant.lease_id)


def _request_login_once():
    pending = _json(state_dir() / "login.json")
    if pending.get("expires_at", 0) > time.time():
        return {"status": "waiting_for_user", **pending}
    response = invoke("login")
    if not response.get("ok"):
        return response
    if response.get("data", {}).get("authenticated_ui"):
        return complete_login()
    pending = {"expires_at": time.time()+300, "context": browser().context_id,
        "instruction": "请在已打开的小红书页面扫码；完成后确认登录并继续任务。"}
    _save(state_dir() / "login.json", pending)
    return {"status": "waiting_for_user", **pending}

def complete_login():
    # 仅显式人工确认可解除风控锁；探针不执行导航，不绕验证。
    response = invoke("resume")
    if not response.get("ok"):
        return response
    if not response.get("data", {}).get("authenticated_ui"):
        return _failure("auth_state_unknown", executed=False)
    pending = _json(state_dir() / "login.json")
    if pending.get("context") and pending["context"] != browser().context_id:
        return _failure("browser_generation_changed", executed=False)
    _save(state_dir() / "auth.json", {"revision": time.time()})
    _observe_account("auth_verified")
    for name in ("stop.json", "login.json"):
        (state_dir() / name).unlink(missing_ok=True)
    return {"status": "completed", "authenticated": True, "resumable": True}

def interaction_state():
    # Risk latches never auto-clear. Authentication wait is bounded and holds no worker/lease.
    if _stop_gate():
        return "blocked"
    pending = _json(state_dir() / "login.json")
    if not pending:
        return "ready" if _json(state_dir() / "auth.json").get("revision") else "none"
    if pending.get("context") != browser().context_id:
        return "blocked"
    if pending.get("expires_at", 0) <= time.time():
        return "expired"
    response = invoke("session_probe")
    if response.get("ok") and response.get("data", {}).get("authenticated_ui"):
        return "ready" if complete_login().get("authenticated") else "blocked"
    return "waiting"

def _recover_or_fallback(response, operation, *, task_id="", **payload):
    if response.get("ok"):
        return response
    error = response.get("error") or {}
    code = error.get("error", "") if isinstance(error, dict) else str(error)
    if code in UNKNOWN_CODES or response.get("failure_type") == "request_outcome_unknown":
        return response
    if code in RISK_CODES or code in {'RATE_LIMITED','platform_cooldown'}:
        response['resume_policy']='bounded_read_only_probe'
        response['retry_after_s']=_stop_gate().get('retry_after_s',60)
        response['next_action']='wait_for_cooldown_then_new_bounded_attempt'
        return response
    pending = _json(state_dir() / "login.json")
    if code == "AUTH_REQUIRED" and not pending:
        response["login_recovery"] = request_login()
        # Give a bounded interactive recovery its first opportunity before spending.
        if response["login_recovery"].get("status") == "waiting_for_user":
            return response
    if code == "AUTH_REQUIRED" and pending.get("expires_at", 0) > time.time():
        response["login_recovery"] = {"status": "waiting_for_user", **pending}
        return response
    if code not in PAID_REASONS:
        return response
    from runtime.xhs_tikhub_fallback import execute_paid_operation
    paid = execute_paid_operation(operation, payload, task_id=task_id, trigger=code)
    response["paid_fallback"] = {k:v for k,v in paid.items() if k not in {"data"}}
    if paid.get("ok"):
        return {**paid, "metadata": {**response.get("metadata", {}), "fallback_used": True,
            "provider": "tikhub", "primary_failure": code}}
    response["status"] = "failed"
    response["resume_policy"] = "stop_no_auto_resume"
    # An expired login no longer leaves the task waiting forever.
    if pending and pending.get("expires_at", 0) <= time.time():
        response["manual_action_required"] = False
        response["status"] = "failed"
    return response

def search(keyword, limit, search_type, *, options=None):
    options = options or {}
    response = invoke("search", keyword=keyword, limit=min(20, limit), search_type=search_type)
    if response.get("ok") and not response.get("data"):
        response = _failure("empty_results")
    response = _recover_or_fallback(response, "search", task_id=options.get("research_task_id", ""),
        keyword=keyword, limit=min(20,limit), search_type=search_type)
    if not response.get("ok"):
        return {**response, "items": [], "total": 0}
    from generic_web.url_policy import canonical_url
    items = []
    rows = response.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or []
    for row in rows:
        fetch_url = str(row.get("url") or "")
        note = re.search(r"/(?:explore|search_result)/([a-f0-9]{24})", fetch_url)
        if not note:
            continue
        public_url = "https://www.xiaohongshu.com/explore/" + note[1]
        items.append({"title": row.get("title",""), "author": row.get("author",""),
            "url": canonical_url(public_url), "canonical_url": canonical_url(public_url),
            "fetch_url": fetch_url, "note_id": note[1], "likes": row.get("likes"),
            "platform":"小红书", "type": search_type if search_type in {"image","video"} else "unknown",
            "published_at":"", "date_provenance":"not_observed"})
    if not items:
        return {**_failure("empty_results"), "items": [], "total": 0}
    # Preserve the existing private, in-memory signed handoff for isolated workers.
    from collectors.platform.xiaohongshu import _protect_xhs_search_fetch
    _protect_xhs_search_fetch(items)
    return {"items":items,"total":len(items),"platform":"小红书","metadata":{"transport":"opencli",**response.get("metadata",{})}}

def validate_media_url(url):
    target = urlsplit(url)
    if target.scheme not in {"http", "https"} or target.username or target.password or not (
        (target.hostname or "").endswith(".xhscdn.com") or (target.hostname or "").endswith(".xiaohongshu.com")):
        raise ValueError("xhs_media_host_rejected")


def media_destination(note_id, kind, root=None):
    if not re.fullmatch(r"[a-f0-9]{24}", note_id):
        raise ValueError("invalid_xhs_note_id")
    from runtime.media_cache import media_cache_subdir
    if root is None:
        return media_cache_subdir("xhs", content_id=note_id)
    path = Path(root) / "xhs" / note_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_options(options):
    value = dict(options.get('xhs') or {})
    common=options.get('comments') or {}
    if common:
        if common.get('root_limit',20)>50 or common.get('total_limit',100)>200:
            raise ValueError('xiaohongshu_comments_max_50_roots_200_total')
        value.update(comment_limit=common.get('root_limit',value.get('comment_limit',10)),
                     comment_budget=common.get('total_limit',value.get('comment_budget',100)))
    return XhsReadOptions.model_validate(value).model_dump()


def _normalize_manifest(items, note_id):
    """公开清单只有稳定引用；真实签名地址仅保存在本地私有获取表。"""
    from runtime.file_write_lock import file_write_lock
    path = state_dir() / 'media-fetch.json'
    output = []
    with file_write_lock(path):
        private = _json(path)
        private = {k:v for k,v in private.items() if isinstance(v, dict) and time.time()-v.get('at', 0) < 86400}
        image_index, video_index = 0, 0
        for item in items:
            row = dict(item)
            kind, url = row.get('type'), str(row.get('url') or '')
            if kind not in {'image', 'video'}:
                continue
            index = image_index if kind == 'image' else video_index
            if kind == 'image': image_index += 1
            else: video_index += 1
            row.setdefault('image_index' if kind == 'image' else 'representation_index', index)
            row.setdefault('role', 'content_image' if kind == 'image' else 'video_representation')
            if url.startswith('urn:kr:xhs-media:'):
                output.append(row); continue
            native = str(row.get('native_file_id') or '')
            identity = [note_id, kind, row['role'], native or url, row.get('rendition','page_default')]
            media_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            ref = 'urn:kr:xhs-media:' + media_id
            private[ref] = {'url': url, 'at': time.time(), 'native_file_id': native}
            row.update(url=ref, fetch_ref=ref, media_id=media_id)
            output.append(row)
        _save(path, private)
    return output


def _media_plan(items, options):
    scope = _read_options(options)
    wanted = scope['image_indexes']
    cap = scope['max_images']
    if cap is None:
        # 旧环境仅作资源保护，绝不缩小请求集合；默认全图。
        try: cap = max(1, min(100, int(os.environ.get('KR_XHS_OCR_MAX_IMAGES', '100'))))
        except ValueError: cap = 100
    rows, selected_images, video_selected = [], 0, False
    image_count = sum(item.get('type') == 'image' for item in items)
    for index, item in enumerate(items):
        row = dict(item)
        kind = row.get('type')
        if kind not in {'image', 'video'}: continue
        image_index = row.get('image_index', sum(r.get('type') == 'image' for r in rows))
        asked = bool(options.get('include_ocr')) and (wanted is None or image_index in wanted) if kind == 'image' else bool(options.get('include_transcript')) and not video_selected
        chosen = asked and (selected_images < cap if kind == 'image' else True)
        if kind == 'image' and chosen: selected_images += 1
        if kind == 'video' and asked: video_selected = True
        row.update(manifest_index=index, requested=asked, selected=chosen, status='pending' if chosen else 'not_executed' if asked else 'not_requested',
            reason='' if chosen else 'image_execution_limit' if asked else 'alternate_representation' if kind == 'video' and options.get('include_transcript') else 'not_requested')
        row.setdefault('image_index' if kind == 'image' else 'representation_index', image_index if kind == 'image' else index)
        rows.append(row)
    for index in wanted or []:
        if index >= image_count:
            rows.append({'type':'image','image_index':index,'role':'content_image','requested':bool(options.get('include_ocr')),
                'selected':False,'status':'missing','reason':'requested_image_index_not_observed'})
    return rows


def acquire_media(items, note_id, *, options=None, previous=(), on_progress=None):
    from runtime.media_acquisition import acquire_asset_batch
    options = options if options is not None else {'include_ocr':True,'include_transcript':True}
    ledger = _media_plan(items, options)
    jobs = [(index, row) for index, row in enumerate(ledger) if row['selected']]
    def progress(results):
        for row in results: ledger[row['manifest_index']] = row
        if on_progress: on_progress([dict(row) for row in ledger])
    def fetch(job):
        _, item = job
        row = _acquire_media_one(item['type'], item.get('url',''), note_id, previous, item=item)
        if row['status'] == 'acquired' and not row.get('reused'):
            from runtime.media_cache import record_media_cache_entry
            record_media_cache_entry(row['path'], kind='xhs_' + row['type'], content_id=note_id)
        return {**item, **row}
    results = acquire_asset_batch(jobs, fetch, on_progress=progress) if jobs else []
    if not jobs or not on_progress:
        progress(results)
    return ledger


def _probe_video(path, expected_duration_s=None):
    import av
    with av.open(str(path)) as container:
        streams = [s for s in container.streams if s.type in {'video','audio'}]
        if not streams: raise ValueError('video_decode_no_media_stream')
        selected = next((s for s in streams if s.type == 'audio'), streams[0])
        frame = next(container.decode(selected), None)
        if frame is None: raise ValueError('video_decode_no_frames')
        duration = float(container.duration / av.time_base) if container.duration else None
        result = {'status':'decodable','validation':'first_frame_and_container_metadata',
            'duration_s':duration, 'full_stream_integrity_proven':False, 'expected_duration_s':expected_duration_s,
            'duration_match':'unknown'}
        if isinstance(expected_duration_s, (float,int)) and expected_duration_s > 0 and duration:
            result['duration_match'] = abs(duration-expected_duration_s) <= max(2, expected_duration_s*.02)
        return result


def _acquire_media_one(kind, url, note_id, previous, *, item=None):
    from runtime.media_acquisition import acquire_http_asset
    from runtime.execution_context import ExecutionStopped
    from runtime.resource_concurrency import ResourceDeferred
    item = item or {}
    try:
        fetch_url = url
        if url.startswith('urn:kr:xhs-media:'):
            record = _json(state_dir() / 'media-fetch.json').get(url) or {}
            fetch_url = record.get('url', '') if time.time()-record.get('at', 0) < 86400 else ''
        # 先核验已有原件；签名变化只有原生文件身份相同才可复用。
        candidates = [row for row in previous if row.get('type') == kind and
            ((item.get('media_id') and row.get('media_id') == item['media_id']) or row.get('source_url') == url)]
        previous_rows = [{**row, 'source_url':fetch_url or url} for row in candidates]
        if not fetch_url and not previous_rows: raise ValueError('media_source_refresh_required')
        if fetch_url: validate_media_url(fetch_url)
        target = urlsplit(fetch_url)
        if target.scheme == 'http':
            fetch_url = target._replace(scheme='https', netloc=target.hostname + (f':{target.port}' if target.port and target.port != 80 else '')).geturl()
        row = acquire_http_asset(kind, fetch_url or url, media_destination(note_id, kind), previous=previous_rows,
            headers={'Referer':'https://www.xiaohongshu.com/'})
        row['source_url'] = url
        if kind == 'video':
            row['decode_validation'] = _probe_video(row['path'], item.get('expected_duration_s'))
            if row['decode_validation']['duration_match'] is False:
                row.update(status='failed',reason='video_duration_mismatch')
        return row
    except (ResourceDeferred, ExecutionStopped): raise
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        return {'type':kind,'source_url':url,'status':'failed','reason':_media_failure_reason(exc),
            **({'http_status':status,'failure_type':'media_access_denied' if status in {401,403} else 'media_http_error',
                'recovery':'refresh_source_manifest_explicitly' if status in {401,403,404,410} else 'inspect_failure'} if status else {})}


def _normalize_comments(data, scope):
    if 'comments' not in data: return
    raw = data.get('comments') or []
    if raw and all(row.get('snapshot_id') and row.get('id_scope') for row in raw): return
    for row in raw:
        if row.get('images'):
            row['images'] = [item['url'] for item in _normalize_manifest([
                {'type':'image','role':'comment_image','url':url} for url in row['images']], data.get('note_id','comment_snapshot'))]
    snapshot = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    normalized, roots = [], []
    for index, item in enumerate(raw[:scope['comment_budget']]):
        row = dict(item)
        native = row.get('native_comment_id')
        root_index = row.pop('root_index', None)
        row.update(comment_id=str(native) if native else f'snapshot:{snapshot}:{index}',
            id_scope='platform' if native else 'snapshot', snapshot_id=snapshot)
        if not row.get('is_reply'):
            roots.append(row['comment_id']); row['root_comment_id'] = row['comment_id']
            row.update(parent_comment_id=None, relationship_confidence='root')
        else:
            root = roots[root_index] if isinstance(root_index,int) and 0 <= root_index < len(roots) else None
            row['root_comment_id'] = root
            row['parent_comment_id'] = row.get('native_parent_comment_id')
            row['relationship_confidence'] = 'native_parent' if row['parent_comment_id'] else 'dom_root_only' if root else 'unknown'
        normalized.append(row)
    data['comments'] = normalized
    observed_zero = data.get('comments_coverage') == 'observed_zero_comments'
    root_count = sum(not row.get('is_reply') for row in normalized)
    total = data.get('comment_count')
    total = int(total) if str(total).isdigit() else None
    budget_hit = len(raw) > scope['comment_budget'] or data.get('comments_budget_truncated', False)
    observation = data.get('comments_observation') or {}
    full_observed = (total is not None and len(raw) == total and observation.get('skipped_empty') == 0
        and observation.get('expanders_remaining') == 0 and not budget_hit)
    window = observed_zero or full_observed or (root_count >= scope['comment_limit'] and not budget_hit)
    # 未满窗口不能从昵称、点赞数或页面总数臆断已穷尽。
    data['comments_complete'] = window
    data['comments_coverage'] = 'observed_zero_comments' if observed_zero else 'bounded_top_level_with_replies'
    data['comments_metadata'] = {'schema':'knowledgeradar-comments/v1','requested_roots':scope['comment_limit'],
        'total_budget':scope['comment_budget'],'returned_roots':root_count,'returned_comments':len(normalized),
        'observed_total':total,'complete_source':observed_zero or full_observed,'request_window_complete':window,
        'stop_reason':'total_budget' if budget_hit else 'observed_total' if full_observed else 'root_window' if window else 'upstream_bounded_no_exhaustion_proof',
        'has_more':False if observed_zero or full_observed else None,'comment_images_ocr':'not_requested'}


def _detail_checkpoint(request):
    from runtime.operation_checkpoint import OperationCheckpoint
    from generic_web.url_policy import canonical_url
    options = request.options
    scope = _read_options(options)
    identity = runtime_identity()
    identity['collection_contract'] = 3
    identity.pop('auth_revision', None)
    # 加工范围、模型版本和语言由SHA加工缓存管理，不作浏览器采集身份。
    return OperationCheckpoint(state_dir() / 'checkpoints',
        {'url':canonical_url(request.url).split('?')[0],
         'include_comments':bool(options.get('include_comments',request.enable_comment_filtering)),
         'comment_limit':scope['comment_limit'],'comment_budget':scope['comment_budget'],
         'comment_reply_limit':(options.get('comments') or {}).get('reply_limit',10),
         'comment_max_pages':(options.get('comments') or {}).get('max_pages',5),
         'task_scope_id':(options.get('task_scope') or {}).get('task_scope_id',''),
         'research_task_id':options.get('research_task_id','')}, identity)



def detail(request):
    started = time.monotonic()
    timings = {}
    options = request.options
    if options.get("preparation_failure"):
        return _failure(options["preparation_failure"], executed=False)
    checkpoint = _detail_checkpoint(request)
    previous = checkpoint.read() if options.get('resume_checkpoint') or not _read_options(options)['refresh_collection'] else {}
    note_match = re.search(r"/([a-f0-9]{24})(?:[?/#]|$)",request.url)
    if not note_match:
        return _failure("invalid_xhs_note_id", executed=False)
    note_id = note_match[1]
    from collectors.platform.xiaohongshu import _cached_xhs_record
    note_type = options.get("note_type") or _cached_xhs_record(note_id)[2]
    browser_started = time.monotonic()
    if previous and set(previous.get("completed_stages",[])) >= {"body","media"} and (
        not options.get("include_comments",request.enable_comment_filtering) or "comments" in previous.get("completed_stages",[])):
        response = {"ok":True,"data":previous,"metadata":{"transport":"opencli","resumed":True}}
    else:
        response = invoke("detail", url=request.url,
        include_comments=bool(options.get("include_comments", request.enable_comment_filtering)),
        comment_limit=_read_options(options)['comment_limit'], comment_budget=_read_options(options)['comment_budget'],
        comment_reply_limit=(options.get('comments') or {}).get('reply_limit',10),
        comment_max_pages=(options.get('comments') or {}).get('max_pages',5), checkpoint=previous)
    response = _recover_or_fallback(response, "detail",
        task_id=(options.get("task_scope") or {}).get("research_task_id") or options.get("research_task_id",""),
        note_id=note_id, note_type=note_type,
        include_comments=bool(options.get("include_comments",request.enable_comment_filtering)),
        partial=response.get("data") or {})
    data = dict(response.get("data") or {})
    if response.get('ok'):
        from datetime import datetime, timezone
        data.setdefault('captured_at',datetime.now(timezone.utc).isoformat())
        data['collection_reused'] = bool((response.get('metadata') or {}).get('resumed'))
        data['source_revalidated'] = not data['collection_reused']
    timings["browser_s"] = round(time.monotonic()-browser_started, 3)
    data["media_manifest"] = _normalize_manifest(data.get("media_manifest") or [], note_id)
    _normalize_comments(data, _read_options(options))
    if options.get('include_comments') and data.get('comments_metadata'):
        observation=data.get('comments_observation') or {}
        data['comments_metadata']['pagination_unit']='bounded_dom_scroll_rounds; not_api_pages'
        if observation.get('expanders_remaining') or observation.get('skipped_empty'):
            data['comments_complete']=False
            data['comments_metadata'].update(request_window_complete=False,stop_reason='unexpanded_or_unread_comments')
    data['collection_version'] = hashlib.sha256(json.dumps({k:data.get(k) for k in
        ('note_id','title','content','desc','media_manifest','comments')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    checkpoint.write(data)
    data.update(platform="小红书", url=request.url, metadata=response.get("metadata") or {"transport":"opencli"})
    if not response.get("ok"):
        data.update({k:response[k] for k in ("status","error","failure_type","manual_action_required",
            "request_executed","login_recovery","paid_fallback","resume_policy") if k in response})
        data["resume_checkpoint"] = {"key":checkpoint.key,"stages":data.get("completed_stages",[])}
        return data
    manifest = data.get("media_manifest") or []
    if ('body' in data.get('completed_stages', []) and manifest and 'desc' in data
            and isinstance(data['desc'], str) and not data['desc'].strip() and not data.get('content')):
        data['content_observation'] = 'observed_empty_original_text'
    data["images"] = [row["url"] for row in manifest if row.get("type") == "image"]
    data["video"] = [row["url"] for row in manifest if row.get("type") == "video"]
    data["timings"] = timings
    if options.get("_detail_stage") == "collection" and (
        options.get("include_ocr", request.auto_multimodal) or
        options.get("include_transcript", request.auto_multimodal)
    ):
        data.update(status="partial", enhancement_status="queued", _enhancement_pending=True)
        timings["collection_wall_s"] = round(time.monotonic() - started, 3)
        checkpoint.write(data)
        return data
    _download_detail_media(request, data, checkpoint, previous, timings)
    return _enhance_detail(request, data, checkpoint, previous, timings, started)


def _download_detail_media(request, data, checkpoint, previous, timings):
    manifest = data.get("media_manifest") or []
    download_started = time.monotonic()
    def download_progress(rows):
        data["downloaded_media"] = rows
        data["media_ledger"] = rows
        checkpoint.write(data)
    assets = acquire_media(manifest, data["note_id"], options={**request.options, 'include_ocr':request.options.get('include_ocr',request.auto_multimodal),
                                    'include_transcript':request.options.get('include_transcript',request.auto_multimodal)},
                           previous=previous.get("downloaded_media") or [], on_progress=download_progress)
    timings["media_download_wall_s"] = round(time.monotonic()-download_started, 3)
    data["downloaded_media"] = assets
    data['media_ledger'] = assets
    data['media_download_coverage'] = {'discovered':len(manifest),'requested':sum(bool(r.get('requested')) for r in assets),
        'acquired':sum(r.get('status')=='acquired' for r in assets),'failed':sum(r.get('status')=='failed' for r in assets),
        'not_executed':sum(r.get('status') in {'pending','not_executed','missing'} for r in assets)}
    checkpoint.write(data)


def enhance_detail(request, collected):
    """消费已采集的清单、复用已下载材料；不再连接浏览器。"""
    checkpoint = _detail_checkpoint(request)
    previous = checkpoint.read() or dict(collected)
    if collected.get('collection_version') and previous.get('collection_version') != collected['collection_version']:
        return {**collected,'_enhancement_pending':False,'status':'partial',
            'enhancement_status':'collection_snapshot_superseded',
            'enhancement_gap':'采集版本已变化；保留原快照，请显式选择要增强的新版本。'}
    # 资源让出后保留已完成增强；下载进度不能用旧采集快照覆盖它们。
    data = {**collected, **previous}
    data.pop("_enhancement_pending", None)
    data.pop("content_coverage", None)
    data.pop("evidence_assets", None)
    data.pop("evidence_storage_gaps", None)
    timings = dict(data.get("timings") or {})
    started = time.monotonic()
    _download_detail_media(request, data, checkpoint, previous, timings)
    return _enhance_detail(request, data, checkpoint, previous, timings, started)


def _enhance_detail(request, data, checkpoint, previous, timings, started):
    data.pop("_enhancement_pending", None)
    options = request.options
    assets = data.get("downloaded_media") or []
    manifest = data.get("media_manifest") or []
    data["timings"] = timings
    from runtime.media_execution import enhance_asset_batch, execute_media_lanes
    from runtime.model_roles import CLOUD_OCR_COMPONENT, ASR_COMPONENT
    progress_lock = RLock()
    enhancement_started = time.monotonic()

    def enhance(kind, component):
        lane_started = time.monotonic()
        try:
            enhance_lane(kind, component)
        finally:
            with progress_lock:
                timings["ocr_s" if kind == "image" else "asr_s"] = round(time.monotonic()-lane_started, 3)
                checkpoint.write(data)

    def enhance_lane(kind, component):
        selected = [row for row in assets if row['type'] == kind and row['status'] == 'acquired' and row.get('requested',True)]
        requested = [row for row in assets if row['type'] == kind and row.get('requested',True)]
        key = 'ocr' if kind == 'image' else 'transcript_result'
        previous_results = previous.get(key) or {}
        previous_results = previous_results.get('results') or [previous_results]
        def progress(results):
            from kr_core.evidence_locators import document_text
            located = [{**row, **{k:selected[index][k] for k in ('image_index','media_id','source_url','role','decode_validation') if k in selected[index]}}
                       for index,row in enumerate(results)]
            success = {'ok','completed','success','acquired'}
            complete = len(located) == len(requested) and all(row.get('status') in success for row in located)
            if kind == 'video' and located:
                duration = (selected[0].get('decode_validation') or {}).get('duration_s')
                segments = located[0].get('content_locators') or []
                if duration and segments and abs(segments[-1].get('end_s',0)-duration) > max(2,duration*.02):
                    complete = False
                    located[0]['coverage_gap'] = 'transcribed_duration_does_not_match_container'
            absent = not requested and not any(row.get('type')==kind for row in manifest) and 'media' in data.get('completed_stages',[])
            state = 'not_applicable' if absent else 'ok' if complete else 'partial'
            coverage = {'requested':len(requested),'acquired':len(selected),'attempted':sum(row.get('status')!='skipped' for row in located),
                'succeeded':sum(row.get('status') in success for row in located),
                'failed':sum(row.get('status')=='failed' for row in located)+sum(row.get('status') in {'failed','missing'} for row in requested),
                'not_executed':sum(row.get('status')=='skipped' for row in located)+sum(row.get('status') in {'not_executed','pending'} for row in requested)}
            with progress_lock:
                for index,row in enumerate(located):
                    selected[index]['processing'] = {k:v for k,v in row.items() if k not in {'text','source_url','path'}}
                if kind == 'image':
                    blocks = [{'text':r['text'],'kind':'ocr','image_index':r.get('image_index',index)} for index,r in enumerate(located)
                              if r.get('status') in success and r.get('text')]
                    data['ocr_text'],data['ocr_text_locators'] = document_text(blocks)
                    data[key] = {'status':state,'reason':'no_image_in_observed_manifest' if absent else '',
                        'request_complete':complete or absent, 'results':located,'coverage':coverage,
                        'images_processed':coverage['attempted'],'text':data['ocr_text']}
                    data['ocr_provenance'] = {'ocr_images':[{k:r[k] for k in ('image_index','media_id','input_sha256','model','provider','request_profile','page_metrics') if k in r}
                        for r in located if r.get('status') in success]}
                else:
                    result = located[0] if located else {}
                    data[key] = {**result,'status':state,'request_complete':complete or absent,'coverage':coverage,
                        'reason':'no_video_in_observed_manifest' if absent else result.get('reason','')}
                    data['transcript'] = result.get('text','')
                    data['transcript_metadata'] = {'source':result.get('engine') or component,'language':result.get('detected_language',''),
                        'language_hint':options.get('language'),'detected_languages':result.get('detected_languages',[]),
                        'status':'aligned' if result.get('content_locators') else 'missing', 'segments':result.get('content_locators',[]),
                        'timestamp_precision':result.get('timestamp_precision','input_chunk_only'),
                        'decode_validation':selected[0].get('decode_validation') if selected else None}
                checkpoint.write(data)
        enhance_asset_batch(selected, component, language=options.get('language'), previous=previous_results,
                            on_progress=progress, failure_reason=_media_failure_reason)
        if not selected: progress([])

    lanes = [(kind, component) for kind, enabled, component in (
        ("image",options.get("include_ocr",request.auto_multimodal),CLOUD_OCR_COMPONENT),
        ("video",options.get("include_transcript",request.auto_multimodal),ASR_COMPONENT),
    ) if enabled]
    execute_media_lanes([lambda kind=kind, component=component: enhance(kind, component) for kind, component in lanes])
    timings["enhancement_wall_s"] = round(time.monotonic()-enhancement_started, 3)
    timings["total_s"] = round(time.monotonic()-started, 3)
    if "collection_wall_s" in timings:
        timings["total_s"] += timings["collection_wall_s"]
    data["status"] = "ok"
    enhancements = [data[key] for key, enabled in (("ocr", options.get("include_ocr", request.auto_multimodal)),
        ("transcript_result", options.get("include_transcript", request.auto_multimodal))) if enabled and key in data]
    data["enhancement_status"] = "partial" if any(row.get("status") not in {
        "ok", "completed", "success", "acquired", "not_applicable"} for row in enhancements) else "ok"
    if data['enhancement_status'] == 'partial' or (options.get('include_comments') and not data.get('comments_complete',True)):
        data['status'] = 'partial'
    checkpoint.write(data)
    return data
