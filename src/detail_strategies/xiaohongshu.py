"""Xiaohongshu detail strategy."""

from __future__ import annotations

import json
import os
import re
import subprocess
from runtime.process import silent_subprocess_run
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from kr_core import DetailRequest, DetailResponse, EvidenceItem
from multimodal.pipeline import MultimodalPipeline
from runtime.degradation import get_degradation_policy
from runtime.chrome_manager import XHS_CHROME_DEBUG_PORT
from runtime.xhs_operation_coordinator import xhs_operation
from runtime.xhs_page_state import classify_xhs_page_state
from runtime.xhs_health import get_xhs_detail_health_tracker, record_xhs_regression_sample
from runtime.xhs_selector_contract import XHS_SELECTOR_BUNDLE_VERSION
from runtime.xhs_candidates import normalize_xhs_image_assets
from runtime.xhs_tikhub_fallback import execute_tikhub_xhs_detail_fallback

XHS_DETAIL_BRIDGE_BREAKER_KEY = "collector:xhs.detail_bridge"


@dataclass(frozen=True)
class XiaohongshuDetailDeps:
    bridge_path: str
    node_exe: str
    recover_xsec_token: Callable[[str], str]
    detail_needs_fallback: Callable[[Dict], bool]
    extract_via_cdp: Callable[[str, str, str], Optional[Dict]]
    ocr_first_image: Callable[..., Dict]
    attach_routing: Callable[[str, Dict], Dict]
    evidence_builder: Callable[[str, str, Dict], EvidenceItem]
    log_info: Callable[[str], None]
    log_warning: Callable[[str], None]
    log_error: Callable[[str], None]
    auto_switch_account: Callable[..., Dict[str, Any]] | None = None
    request_user_login: Callable[..., Dict] | None = None
    selected_profile_id: Callable[[], str] | None = None
    allow_auto_user_login_request: bool = False


class XiaohongshuDetailStrategy:
    platform = "小红书"

    def __init__(self, deps: XiaohongshuDetailDeps) -> None:
        self.deps = deps

    def extract(self, request: DetailRequest) -> DetailResponse:
        result: Dict = {"platform": self.platform, "title": "", "desc": "", "transcript": "", "url": request.url}
        started = __import__("time").time()
        try:
            data = self._extract(request.url, result, request=request, auto_multimodal=request.auto_multimodal)
        except subprocess.TimeoutExpired:
            data = self._handle_timeout(request.url, result)
        except Exception as exc:
            self.deps.log_error(f"小红书详情提取异常: {exc}")
            data = {"platform": self.platform, "error": f"小红书详情提取异常: {exc}", "url": request.url}

        if isinstance(data, dict):
            # The existing bridge/CDP implementation does not always expose
            # browser-navigation and render telemetry.  Preserve that absence
            # as "not_observed" instead of inferring success from an empty
            # payload, while making selector/body/OCR outcomes independently
            # auditable.
            data["detail_stage_receipt"] = self._detail_stage_receipt(data)
        elapsed_s = __import__("time").time() - started
        try:
            error_type = ""
            if isinstance(data, dict):
                error_type = str(data.get("failure_type") or "")
                error_text = str(data.get("error") or "")
                if error_type:
                    pass
                elif classify_xhs_page_state(error_text).get("platform_state") == "not_found":
                    error_type = "dead_link"
                elif "Bridge 输出解析失败" in error_text:
                    error_type = "bridge_parse_failed"
                elif "Bridge 调用超时" in error_text:
                    error_type = "bridge_timeout"
                elif "详情为空" in error_text:
                    error_type = "empty_detail"
                elif error_text:
                    error_type = "request_failed"
            get_xhs_detail_health_tracker().record(
                success=not bool(data.get("error")),
                elapsed_s=elapsed_s,
                error_type=error_type,
                failure_subtype=str(data.get("failure_subtype") or error_type or ""),
                page_state=data.get("page_state") if isinstance(data.get("page_state"), dict) else None,
                selector_hit_count=_safe_optional_int(_first_present(data, "selector_hit_count", "selectorHitCount")),
                text_len=_safe_optional_int(data.get("text_len")),
                fallback_attempts=data.get("fallback_attempts") if isinstance(data.get("fallback_attempts"), list) else None,
                note_id=data.get("note_id", "") if isinstance(data, dict) else "",
                url=request.url,
            )
            record_xhs_regression_sample(
                kind="detail",
                url=request.url,
                title=str(data.get("title") or ""),
                note_id=str(data.get("note_id") or ""),
                status="ok" if not bool(data.get("error")) else "failed",
                detail=str(data.get("error") or ""),
                content_chars=len(str(data.get("content") or data.get("desc") or "")),
                ocr_text_chars=len(str((data.get("ocr") or {}).get("text") if isinstance(data.get("ocr"), dict) else "")),
                transcript_chars=len(str(data.get("transcript") or "")),
            )
        except Exception:
            pass
        return DetailResponse.from_legacy(
            self.platform,
            request.url,
            data,
            evidence=self.deps.evidence_builder(request.url, self.platform, data),
            metadata={"strategy": "xiaohongshu_detail"},
        )

    @staticmethod
    def _detail_stage_receipt(data: Dict[str, Any]) -> Dict[str, Any]:
        attempts = data.get("fallback_attempts") if isinstance(data.get("fallback_attempts"), list) else []
        selector_hits = _safe_optional_int(_first_present(data, "selector_hit_count", "selectorHitCount"))
        text_len = _safe_optional_int(data.get("text_len"))
        content_len = len(str(data.get("content") or data.get("desc") or ""))
        ocr = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
        return {
            "schema": "knowledgeradar-xhs-detail-stage-receipt/v1",
            "navigation": {"status": str(data.get("navigation_status") or "not_observed_by_current_adapter")},
            "render": {"status": str(data.get("render_status") or "not_observed_by_current_adapter")},
            "body_selection": {
                "status": "ok" if selector_hits and selector_hits > 0 else "not_selected_or_not_observed",
                "selector_hit_count": selector_hits,
                "selector_bundle_version": str(data.get("selector_bundle_version") or XHS_SELECTOR_BUNDLE_VERSION),
            },
            "cleaning": {"status": "ok" if content_len > 0 else "empty_or_not_observed", "content_chars": content_len, "reported_text_len": text_len},
            "ocr_task": {"status": str(ocr.get("status") or "not_started"), "reason": str((data.get("ocr_decision") or {}).get("reason") or "")},
            "fallback_attempts": [
                {"strategy": str(item.get("strategy") or ""), "status": str(item.get("status") or ""), "reason": str(item.get("reason") or "")}
                for item in attempts
                if isinstance(item, dict)
            ],
        }

    def _extract(self, url: str, result: Dict, *, request: DetailRequest, auto_multimodal: bool = False) -> Dict:
        note_id, xsec_token, xsec_source = self._parse_url(url)
        if not note_id:
            return {"platform": self.platform, "error": f"无法从 URL 提取小红书笔记 ID: {url}", "url": url}
        if not xsec_token:
            recovered = self.deps.recover_xsec_token(note_id)
            if recovered:
                xsec_token = recovered
                self.deps.log_info("  小红书缺少 xsec_token，已从当前搜索页恢复")

        self.deps.log_info(f"  小红书笔记 ID: {note_id}")
        fallback_attempts: List[Dict[str, Any]] = []
        with xhs_operation("detail", note_id=note_id):
            cdp_note = self._try_cdp_fallback(note_id, xsec_token, xsec_source, reason="primary_cdp_snapshot", attempts=fallback_attempts)
            if cdp_note:
                return self._fill_result(url, result, cdp_note, auto_multimodal=auto_multimodal, request=request)
            detail = self._call_bridge(note_id, xsec_token, xsec_source)
        if detail is None:
            return self._failure_payload(
                url=url,
                note_id=note_id,
                error="Bridge 输出解析失败",
                failure_type="bridge_parse_failed",
                failure_subtype="bridge_parse_failed",
                fallback_attempts=fallback_attempts,
            )
        bridge_attempt = {
            "strategy": "bridge_detail_fallback",
            "status": "ok" if detail.get("status") == "ok" else "failed",
            "reason": "after_cdp_empty",
            "bridge_port": str(os.environ.get("KR_CHROME_DEBUG_PORT") or XHS_CHROME_DEBUG_PORT),
            "failure_subtype": str(detail.get("failure_type") or ""),
        }
        fallback_attempts.append(bridge_attempt)
        if detail.get("status") != "ok":
            bridge_error = f"Bridge 返回错误: {detail.get('error', 'unknown')}"
            page_state = classify_xhs_page_state(bridge_error, url=url)
            manual = bool(page_state.get("manual_action_required"))
            tikhub_attempt = self._try_tikhub_detail_fallback(note_id, xsec_token, xsec_source, url=url, attempts=fallback_attempts)
            if tikhub_attempt:
                return self._fill_result(url, result, tikhub_attempt, auto_multimodal=auto_multimodal, request=request)
            switch = self._auto_switch_account(
                purpose="detail",
                reason_code=str(page_state.get("failure_subtype") or "DETAIL_WEAK").upper(),
                note_id=note_id,
                failure_subtype=str(detail.get("failure_type") or "request_failed"),
            )
            return self._failure_payload(
                url=url,
                note_id=note_id,
                error=bridge_error,
                failure_type=detail.get("failure_type") or "request_failed",
                failure_subtype=detail.get("failure_type") or "request_failed",
                page_state=page_state,
                manual_action_required=manual,
                fallback_attempts=fallback_attempts,
                detail=detail,
                extra={"account_auto_switch": switch},
            )
        note_data = detail.get("noteData", {})
        if not self.deps.detail_needs_fallback(note_data):
            return self._fill_result(url, result, note_data, auto_multimodal=auto_multimodal, request=request)
        tikhub_note = self._try_tikhub_detail_fallback(note_id, xsec_token, xsec_source, url=url, attempts=fallback_attempts)
        if tikhub_note:
            return self._fill_result(url, result, tikhub_note, auto_multimodal=auto_multimodal, request=request)
        self._auto_switch_account(
            purpose="detail",
            reason_code="DETAIL_WEAK",
            note_id=note_id,
            failure_subtype="empty_detail",
        )
        return self._empty_detail_failure(
            url=url,
            note_id=note_id,
            xsec_token=xsec_token,
            note_data=note_data if isinstance(note_data, dict) else {},
            fallback_attempts=fallback_attempts,
        )

    def _empty_detail_failure(
        self,
        *,
        url: str,
        note_id: str,
        xsec_token: str,
        note_data: Dict[str, Any],
        fallback_attempts: List[Dict[str, Any]],
    ) -> Dict:
        safe_note = note_data if isinstance(note_data, dict) else {}
        state_text = "\n".join(
            str(safe_note.get(key) or "")
            for key in ("title", "content", "desc", "error", "message", "textSample")
        )
        page_state = classify_xhs_page_state(
            state_text,
            title=str(safe_note.get("title") or ""),
            url=url,
            final_url=str(safe_note.get("url") or url),
            selector_hit_count=_safe_optional_int(safe_note.get("selector_hit_count")),
            body_text_len=_safe_optional_int(safe_note.get("text_len")),
            captcha_element_count=_safe_optional_int(safe_note.get("captcha_element_count")),
            loading_state=str(safe_note.get("loading_state") or ""),
        )
        manual = bool(page_state.get("manual_action_required"))
        failure_subtype = self._classify_empty_detail_subtype(
            note_id=note_id,
            xsec_token=xsec_token,
            page_state=page_state,
            attempts=fallback_attempts,
            note_data=safe_note,
        )
        login_request = None
        if manual and self.deps.allow_auto_user_login_request and self.deps.request_user_login:
            try:
                profile_id = str(self.deps.selected_profile_id() or "") if self.deps.selected_profile_id else ""
                trigger_evidence = self._manual_auth_evidence(page_state)
                login_request = self.deps.request_user_login(
                    "xhs",
                    str(page_state.get("platform_state") or "manual_action_required"),
                    target_profile_id=profile_id,
                    trigger_evidence=trigger_evidence,
                    source="get_content_detail.empty_detail",
                )
            except Exception as exc:
                login_request = {"status": "error", "detail": str(exc)}
        failure_type = "dead_link" if failure_subtype == "not_found_or_deleted" else "empty_detail"
        return self._failure_payload(
            url=url,
            note_id=note_id,
            error="小红书详情为空或页面不可访问",
            failure_type=failure_type,
            failure_subtype=failure_subtype,
            page_state=page_state,
            manual_action_required=manual,
            fallback_attempts=fallback_attempts,
            extra={
                "hint": "可能是 xsec_token 失效、笔记被删除、页面结构变化或平台返回空页面；只有 classifier 命中登录/扫码/安全验证时才需要人工操作。",
                "platform_state": str(page_state.get("platform_state") or "empty_detail"),
                "login_required": str(page_state.get("platform_state") or "") == "login_required",
                "browser_action": login_request,
                "recommended_action": "health_check(mode='request_browser_interaction:xhs:platform_verification_required')" if manual else "retry_with_fresh_search_result_or_inspect_bridge",
            },
        )

    @staticmethod
    def _manual_auth_evidence(page_state: Dict[str, Any]) -> List[str]:
        """Keep foreground requests tied to a concrete page signal."""
        platform_state = str(page_state.get("platform_state") or "")
        if platform_state == "login_required":
            return ["xhs_detail_page_login_required=true"]
        if platform_state in {"platform_verification_required", "app_scan_required"}:
            return [f"xhs_detail_page_state={platform_state}"]
        return []

    def _failure_payload(
        self,
        *,
        url: str,
        note_id: str,
        error: str,
        failure_type: str,
        failure_subtype: str,
        fallback_attempts: List[Dict[str, Any]],
        page_state: Dict[str, Any] | None = None,
        manual_action_required: bool = False,
        detail: Dict[str, Any] | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> Dict:
        selector_hit_count = self._max_attempt_int(fallback_attempts, "selector_hit_count")
        text_len = self._max_attempt_int(fallback_attempts, "text_len")
        page_state = page_state or classify_xhs_page_state(error, url=url)
        user_message = self._user_message_for_failure(failure_subtype, page_state)
        payload = {
            "platform": self.platform,
            "error": error,
            "url": url,
            "failure_type": failure_type,
            "failure_subtype": failure_subtype,
            "note_id": note_id,
            "page_state": page_state,
            "manual_action_required": bool(manual_action_required),
            "fallback_attempts": fallback_attempts,
            "selector_bundle_version": XHS_SELECTOR_BUNDLE_VERSION,
            "selector_hit_count": selector_hit_count,
            "text_len": text_len,
            "user_message": user_message,
            "diagnostics": {
                "failure_subtype": failure_subtype,
                "page_state": page_state,
                "fallback_attempts": fallback_attempts,
                "selector_hit_count": selector_hit_count,
                "text_len": text_len,
                "selector_bundle_version": XHS_SELECTOR_BUNDLE_VERSION,
                "bridge_port": str(os.environ.get("KR_CHROME_DEBUG_PORT") or XHS_CHROME_DEBUG_PORT),
            },
        }
        if detail is not None:
            payload["detail"] = detail
        if extra:
            payload.update(extra)
        return payload

    def _try_cdp_fallback(self, note_id: str, xsec_token: str, xsec_source: str, *, reason: str, attempts: List[Dict[str, Any]]) -> Optional[Dict]:
        self.deps.log_warning(f"  小红书 Bridge 详情不可用({reason})，尝试 CDP 兜底提取")
        attempt: Dict[str, Any] = {"strategy": "cdp_detail_fallback", "reason": reason, "status": "failed"}
        try:
            fallback_note = self.deps.extract_via_cdp(note_id, xsec_token, xsec_source)
        except Exception as exc:
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            attempts.append(attempt)
            return None
        if fallback_note and not self.deps.detail_needs_fallback(fallback_note):
            attempt["status"] = "ok"
            attempt["content_chars"] = len(str(fallback_note.get("content") or fallback_note.get("desc") or ""))
            attempt["selector_bundle_version"] = str(fallback_note.get("selector_bundle_version") or XHS_SELECTOR_BUNDLE_VERSION)
            attempt["selector_hit_count"] = _safe_int(fallback_note.get("selector_hit_count"), 0)
            attempt["text_len"] = _safe_int(fallback_note.get("text_len"), 0)
            attempts.append(attempt)
            return fallback_note
        attempt["status"] = "empty"
        if isinstance(fallback_note, dict):
            attempt["snapshot_status"] = str(fallback_note.get("snapshot_status") or "")
            attempt["text_len"] = _safe_int(fallback_note.get("text_len"), 0)
            attempt["selector_bundle_version"] = str(fallback_note.get("selector_bundle_version") or XHS_SELECTOR_BUNDLE_VERSION)
            attempt["selector_hit_count"] = _safe_int(fallback_note.get("selector_hit_count"), 0)
            attempt["failure_subtype"] = "selector_miss" if attempt["selector_hit_count"] <= 0 else "page_content_empty"
            attempt["text_sample_chars"] = len(str(fallback_note.get("textSample") or ""))
            selector_keys = fallback_note.get("selector_keys") or []
            if isinstance(selector_keys, list):
                attempt["selector_keys"] = selector_keys[:8]
            page_url = str(fallback_note.get("url") or "")
            if page_url:
                attempt["page_url"] = page_url[:240]
        attempts.append(attempt)
        return None

    def _try_tikhub_detail_fallback(self, note_id: str, xsec_token: str, xsec_source: str, *, url: str, attempts: List[Dict[str, Any]]) -> Optional[Dict]:
        attempt: Dict[str, Any] = {"strategy": "tikhub_detail_break_glass", "status": "skipped", "reason": "after_browser_detail_failed"}
        try:
            result = execute_tikhub_xhs_detail_fallback(note_id, xsec_token=xsec_token, xsec_source=xsec_source, share_text=url)
        except Exception as exc:
            attempt.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            attempts.append(attempt)
            return None
        attempt.update(
            {
                "status": str(result.get("status") or "unknown"),
                "reason_code": str(result.get("reason_code") or ""),
                "api_call_count": int(result.get("api_call_count") or 0),
            }
        )
        attempts.append(attempt)
        if result.get("status") != "ok":
            return None
        note_data = result.get("noteData") if isinstance(result.get("noteData"), dict) else {}
        return note_data if note_data and not self.deps.detail_needs_fallback(note_data) else None

    def _parse_url(self, url: str) -> tuple[str, str, str]:
        note_match = re.search(r"/(?:explore|discovery/item)/([a-f0-9]{24})", url)
        note_id = note_match.group(1) if note_match else ""
        xsec_token_m = re.search(r"xsec_token=([^&]+)", url)
        xsec_token = xsec_token_m.group(1) if xsec_token_m else ""
        xsec_source_m = re.search(r"xsec_source=([^&]+)", url)
        xsec_source = xsec_source_m.group(1) if xsec_source_m else "pc_search"
        return note_id, xsec_token, xsec_source

    def _call_bridge(self, note_id: str, xsec_token: str, xsec_source: str) -> Optional[Dict]:
        policy = get_degradation_policy()
        breaker = policy.is_open(XHS_DETAIL_BRIDGE_BREAKER_KEY)
        if breaker.get("open"):
            return {
                "status": "error",
                "error": f"XHS detail bridge breaker open: {breaker.get('last_reason') or 'recent failures'}",
                "failure_type": "bridge_disabled_by_breaker",
                "breaker": breaker,
            }
        env = os.environ.copy()
        env["NODE_OPTIONS"] = ""
        xhs_port = str(os.environ.get("KR_XHS_CHROME_DEBUG_PORT") or XHS_CHROME_DEBUG_PORT)
        env["KR_XHS_CHROME_DEBUG_PORT"] = xhs_port
        env["KR_CHROME_DEBUG_PORT"] = xhs_port
        cmd = [self.deps.node_exe, self.deps.bridge_path, "detail", note_id, xsec_token, xsec_source]
        proc = silent_subprocess_run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        stdout_text = (proc.stdout or "").strip()
        stderr_text = (proc.stderr or "").strip()
        detail = self._extract_json(stdout_text, stderr_text=stderr_text)
        if detail is None:
            policy.mark_failure(
                XHS_DETAIL_BRIDGE_BREAKER_KEY,
                "xhs_detail_bridge",
                "bridge_parse_failed",
                metadata={"note_id": note_id, "stage": "bridge_parse"},
                failure_threshold=3,
                cooldown_seconds=21600,
            )
            return None
        if detail.get("status") == "ok":
            policy.mark_success(
                XHS_DETAIL_BRIDGE_BREAKER_KEY,
                "xhs_detail_bridge",
                {"note_id": note_id},
            )
        else:
            policy.mark_failure(
                XHS_DETAIL_BRIDGE_BREAKER_KEY,
                "xhs_detail_bridge",
                str(detail.get("error") or "bridge_request_failed")[:240],
                metadata={"note_id": note_id, "stage": "bridge_call", "status": detail.get("status")},
                failure_threshold=3,
                cooldown_seconds=21600,
            )
        return detail

    def _extract_json(self, text: str, stderr_text: str = "") -> Optional[Dict]:
        sources = [text]
        if stderr_text and stderr_text != text:
            sources.append(stderr_text)
        decoder = json.JSONDecoder()
        for src in sources:
            if not src or not src.strip():
                continue
            src = src.strip()
            candidates = []
            for idx in range(len(src) - 1, -1, -1):
                if src[idx] != "{":
                    continue
                try:
                    obj, _ = decoder.raw_decode(src[idx:])
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    candidates.append((idx, obj))
            for _, obj in candidates:
                if "status" in obj:
                    return obj
            for _, obj in candidates:
                if any(key in obj for key in ("noteData", "comments", "items")):
                    return obj
            if candidates:
                return candidates[0][1]
            for line in reversed(src.split("\n")):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        pass
            for match in re.finditer(r"\{.+\}", src, re.DOTALL):
                candidate = match.group(0)
                if '"status"' in candidate:
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
        return None

    def _handle_timeout(self, url: str, result: Dict) -> Dict:
        note_id, xsec_token, xsec_source = self._parse_url(url)
        get_degradation_policy().mark_failure(
            XHS_DETAIL_BRIDGE_BREAKER_KEY,
            "xhs_detail_bridge",
            "bridge_timeout",
            metadata={"note_id": note_id, "stage": "bridge_call"},
            failure_threshold=3,
            cooldown_seconds=21600,
        )
        self.deps.log_warning("  小红书 Bridge 调用超时，尝试 CDP 兜底提取")
        fallback_note = self.deps.extract_via_cdp(note_id, xsec_token, xsec_source)
        if fallback_note:
            return self._fill_result(url, result, fallback_note, auto_multimodal=False)
        return self._failure_payload(
            url=url,
            note_id=note_id,
            error="小红书 Bridge 调用超时（>60s）且 CDP 兜底失败",
            failure_type="bridge_timeout",
            failure_subtype="bridge_timeout",
            fallback_attempts=[],
        )

    def _fill_result(self, url: str, result: Dict, note_data: Dict, *, auto_multimodal: bool = False, request: DetailRequest | None = None) -> Dict:
        pipeline = MultimodalPipeline(self.platform)
        note_id, _, _ = self._parse_url(url)
        if note_id:
            result["note_id"] = note_id
        result["title"] = note_data.get("title", "")
        result["desc"] = note_data.get("desc", "")
        result["content"] = note_data.get("content", "")
        result["author"] = note_data.get("author", "")
        image_assets, images, image_quality = normalize_xhs_image_assets(note_data)
        result["images"] = images
        result["image_assets"] = image_assets
        result["image_quality"] = image_quality
        result["selector_bundle_version"] = note_data.get("selector_bundle_version") or XHS_SELECTOR_BUNDLE_VERSION
        if "selector_hit_count" in note_data:
            result["selector_hit_count"] = _safe_int(note_data.get("selector_hit_count"), 0)
        if "text_len" in note_data:
            result["text_len"] = _safe_int(note_data.get("text_len"), 0)
        text_chars = len(str(result.get("content") or result.get("desc") or ""))
        # Do not let a bridge payload with observed bad detail signals become
        # a successful result.  Older paid fallback payloads may not expose
        # selector telemetry; preserve that boundary as unobserved instead of
        # inventing a selector miss.
        selector_observed = "selector_hit_count" in note_data or "selectorHitCount" in note_data
        if selector_observed or "text_len" in note_data:
            page_state = classify_xhs_page_state(
                str(result.get("content") or result.get("desc") or ""),
                selector_hit_count=_safe_optional_int(_first_present(note_data, "selector_hit_count", "selectorHitCount")),
                body_text_len=_safe_optional_int(note_data.get("text_len")) or text_chars,
                loading_state=str(note_data.get("loading_state") or "complete"),
            )
            quality = page_state["detail_quality"]
            result["detail_quality"] = quality
            if quality["status"] != "PASS":
                subtype = str(page_state.get("failure_subtype") or "short_text")
                return self._failure_payload(
                    url=url,
                    note_id=note_id,
                    error="小红书详情正文质量不足，未作为成功详情返回",
                    failure_type="empty_detail",
                    failure_subtype=subtype,
                    page_state=page_state,
                    fallback_attempts=[],
                    extra={"detail_quality": quality},
                )
        ocr_decision = self._ocr_decision(
            images=result["images"],
            text_chars=text_chars,
            auto_multimodal=auto_multimodal,
            note_data=note_data,
        )
        result["ocr"] = pipeline.run(
            "image_ocr",
            trigger=ocr_decision["trigger"],
            enabled=bool(ocr_decision["enabled"]),
            fn=lambda: self._call_ocr(result["images"], url, note_id, request),
        ) or {
            "status": "skipped",
            "reason": ocr_decision["reason"],
            "elapsed_s": 0,
        }
        result["ocr_decision"] = ocr_decision
        result["multimodal_pipeline"] = pipeline.to_dict()
        self.deps.log_info(f"  小红书提取完成: title={result['title'][:50]}")
        return self.deps.attach_routing(url, result)

    def _classify_empty_detail_subtype(
        self,
        *,
        note_id: str,
        xsec_token: str,
        page_state: Dict[str, Any],
        attempts: List[Dict[str, Any]],
        note_data: Dict[str, Any],
    ) -> str:
        platform_state = str(page_state.get("platform_state") or "")
        if platform_state == "login_required":
            return "login_required"
        if platform_state == "platform_verification_required":
            return "anti_bot_verification"
        if platform_state == "app_scan_required":
            return "app_scan_required"
        if platform_state == "not_found" or "页面不见了" in str(note_data.get("title") or ""):
            return "not_found_or_deleted"
        if not xsec_token:
            return "xsec_missing"
        selector_hit_count = self._max_attempt_int(attempts, "selector_hit_count")
        text_len = self._max_attempt_int(attempts, "text_len")
        if selector_hit_count <= 0 and platform_state == "ok":
            return "selector_miss"
        if text_len <= 0:
            return "page_text_empty"
        return "unknown_empty_detail"

    def _user_message_for_failure(self, failure_subtype: str, page_state: Dict[str, Any]) -> str:
        mapping = {
            "selector_miss": "小红书页面已打开，但当前选择器没有命中正文；可能是页面结构变化。",
            "page_text_empty": "小红书详情页没有读到正文文本；建议换一条搜索结果或稍后重试。",
            "xsec_missing": "小红书详情缺少 xsec_token；建议从最新搜索结果重新打开详情。",
            "login_required": "小红书详情需要登录后查看；请先完成登录再重试。",
            "anti_bot_verification": "小红书触发安全验证；请先处理浏览器验证，当前不应继续重试。",
            "app_scan_required": "该小红书详情要求使用 App 或扫码查看，浏览器链路无法直接读取。",
            "not_found_or_deleted": "该小红书笔记不存在、已删除或当前不可访问。",
            "bridge_timeout": "小红书详情 bridge 超时，CDP 兜底也未读到内容。",
            "bridge_parse_failed": "小红书详情 bridge 返回无法解析，CDP 兜底也未读到内容。",
        }
        if failure_subtype in mapping:
            return mapping[failure_subtype]
        state = str(page_state.get("platform_state") or "")
        if state == "ok":
            return "小红书详情读取失败，但未发现需要用户登录或验证的信号。"
        return "小红书详情读取失败，请查看 diagnostics 判断下一步。"

    def _max_attempt_int(self, attempts: List[Dict[str, Any]], key: str) -> int:
        values = []
        for attempt in attempts:
            if isinstance(attempt, dict) and key in attempt:
                values.append(_safe_int(attempt.get(key), 0))
        return max(values) if values else 0

    def _ocr_decision(
        self,
        *,
        images: List[str],
        text_chars: int,
        auto_multimodal: bool,
        note_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        image_count = len([image for image in images or [] if str(image or "").strip()])
        policy = str(os.environ.get("KR_XHS_OCR_TRIGGER_POLICY") or "image_presence").strip().lower()
        threshold = _safe_int(os.environ.get("KR_XHS_OCR_TEXT_SHORT_THRESHOLD"), 80)
        note_type = str(note_data.get("note_type") or note_data.get("noteType") or note_data.get("type") or "").strip().lower()
        if not auto_multimodal:
            enabled = False
            reason = "auto_multimodal_disabled"
        elif image_count <= 0:
            enabled = False
            reason = "no_images"
        elif policy in {"off", "disabled", "none"}:
            enabled = False
            reason = "ocr_policy_disabled"
        elif policy in {"text_short", "short_text"}:
            enabled = text_chars < threshold
            reason = "text_short" if enabled else "text_sufficient_by_policy"
        elif policy in {"image_note", "image_type"}:
            enabled = note_type in {"image", "normal"} or (not note_type and image_count > 0)
            reason = "image_note" if enabled else "non_image_note"
        else:
            enabled = True
            reason = "images_present"
            policy = "image_presence"
        return {
            "schema": "xhs-ocr-decision/v1",
            "enabled": bool(enabled),
            "reason": reason,
            "trigger": f"auto_multimodal_{policy}",
            "policy": policy,
            "image_count": image_count,
            "text_chars": text_chars,
            "text_short_threshold": threshold,
            "note_type": note_type,
        }

    def _call_ocr(self, images: List[str], url: str, note_id: str, request: DetailRequest | None) -> Dict:
        scope_metadata: Dict[str, Any] = {}
        if request and isinstance(request.options, dict):
            raw_scope = request.options.get("task_scope")
            if isinstance(raw_scope, dict):
                scope_metadata.update(raw_scope)
        scope_metadata.update(
            {
                "source_url": url,
                "content_id": note_id,
                "blocks_final_report": bool(request.auto_multimodal) if request else True,
                "result_reread_tool": "get_content_detail",
                "approach": "derived_text",
                "media_operation": "xhs_image_ocr",
            }
        )
        try:
            return self.deps.ocr_first_image(images, task_metadata=scope_metadata)
        except TypeError:
            return self.deps.ocr_first_image(images)

    def _auto_switch_account(self, *, purpose: str, reason_code: str, note_id: str, failure_subtype: str) -> Dict[str, Any]:
        if not self.deps.auto_switch_account:
            return {"status": "skipped", "reason": "auto_switch_hook_missing"}
        try:
            return self.deps.auto_switch_account(
                purpose=purpose,
                reason_code=reason_code,
                last_tool="get_content_detail:xiaohongshu",
                notes=[f"note_id={note_id}", f"failure_subtype={failure_subtype}"],
            )
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _first_present(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data.get(key)
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    for key in keys:
        if key in diagnostics:
            return diagnostics.get(key)
    return None
