"""Common login/CDP probing for platform collectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class LoginProbe:
    platform: str
    chrome_debug_url: Callable[[str], str]
    inspect_cookie_health: Optional[Callable[..., Dict]] = None
    inspect_login_health: Optional[Callable[..., Dict]] = None
    read_cookie_from_cdp: Optional[Callable[[], str | None]] = None
    read_cookie_from_profile: Optional[Callable[[], str | None]] = None
    sign_cookie: Optional[Callable[[str, str], Dict]] = None
    zhihu_search_api: Optional[Callable[[str, str, int], list]] = None
    probe_page_state: Optional[Callable[[str], Dict]] = None
    bring_to_front: Optional[Callable[[str], Dict]] = None
    send_to_background: Optional[Callable[[str], Dict]] = None
    auto_foreground_on_verification: bool = False
    bridge_path: str = ""

    def inspect(self, *, cdp_status: str = "unknown", warn_within_hours: int = 72) -> Dict:
        if self.platform == "zhihu":
            return self._inspect_zhihu(cdp_status=cdp_status, warn_within_hours=warn_within_hours)
        if self.platform in {"xiaohongshu", "xhs"}:
            return self._inspect_xhs(cdp_status=cdp_status, warn_within_hours=warn_within_hours)
        return {
            "status": "unknown",
            "detail": f"未注册的登录态探针: {self.platform}",
            "platform": self.platform,
            "cdp_status": cdp_status,
        }

    def _inspect_zhihu(self, *, cdp_status: str, warn_within_hours: int) -> Dict:
        if cdp_status != "ok":
            return {
                "status": "ok",
                "detail": "知乎 CDP 当前未连接；summary 健康检查不启动浏览器，请用 health_check(mode='zhihu_login_probe') 执行真实登录态探针",
                "platform": "zhihu",
                "probe": "cdp_cookie_and_live_search_api",
                "login_state": "not_checked",
                "platform_state": "not_checked",
                "login_required": False,
                "cdp_status": cdp_status,
                "skipped": True,
            }

        cookie_health: Dict = {}
        try:
            if self.inspect_cookie_health:
                cookie_health = self.inspect_cookie_health(warn_within_hours)
            cookie_source = ""
            cookie_str = self.read_cookie_from_cdp() if self.read_cookie_from_cdp else ""
            if cookie_str:
                cookie_source = "cdp"
            elif self.read_cookie_from_profile:
                cookie_str = self.read_cookie_from_profile() or ""
                if cookie_str:
                    cookie_source = "profile"

            if not cookie_str:
                return {
                    "status": "degraded",
                    "detail": "知乎登录态探针未通过：Cookie 不存在或不可读",
                    "platform": "zhihu",
                    "probe": "cdp_cookie_and_live_search_api",
                    "login_state": "unknown" if cdp_status != "ok" else "missing_cookie",
                    "login_required": cdp_status == "ok",
                    "cdp_status": cdp_status,
                    "cookie_source": "",
                    "cookie_health": cookie_health,
                    "retryable": True,
                }

            cookie_count = len(cookie_str.split("; "))
            cookie_names = {
                part.split("=", 1)[0].strip()
                for part in cookie_str.split("; ")
                if "=" in part and part.split("=", 1)[0].strip()
            }
            required_names = {"z_c0", "d_c0"}
            missing_required = sorted(required_names - cookie_names)
            if missing_required:
                return {
                    "status": "degraded",
                    "detail": f"知乎登录态探针未通过：缺少关键 Cookie {', '.join(missing_required)}",
                    "platform": "zhihu",
                    "probe": "cdp_cookie_and_live_search_api",
                    "login_state": "missing_auth_cookie",
                    "platform_state": "login_required",
                    "login_required": True,
                    "cdp_status": cdp_status,
                    "cookie_source": cookie_source,
                    "cookie_health": cookie_health,
                    "cookie_count": cookie_count,
                    "retryable": True,
                }
            sign = self.sign_cookie("/api/v4/search_v3?q=health", cookie_str) if self.sign_cookie else {}
            if not sign.get("x-zse-96"):
                return {
                    "status": "degraded",
                    "detail": "知乎登录态探针未通过：签名结果缺少 x-zse-96",
                    "platform": "zhihu",
                    "probe": "cdp_cookie_and_live_search_api",
                    "login_state": "unknown",
                    "login_required": False,
                    "cdp_status": cdp_status,
                    "cookie_source": cookie_source,
                    "cookie_health": cookie_health,
                    "cookie_count": cookie_count,
                    "retryable": True,
                }

            live_items = None
            if self.zhihu_search_api:
                try:
                    live_items = self.zhihu_search_api("health", cookie_str, 1)
                except Exception as exc:
                    message = str(exc)
                    auth_failed = any(token in message for token in ("400", "401", "403", "鉴权", "Cookie"))
                    return {
                        "status": "degraded",
                        "detail": f"知乎真实登录态探针失败: {message}",
                        "platform": "zhihu",
                        "probe": "cdp_cookie_and_live_search_api",
                        "login_state": "login_required" if auth_failed else "unknown",
                        "platform_state": "login_required" if auth_failed else "probe_failed",
                        "login_required": auth_failed,
                        "cdp_status": cdp_status,
                        "cookie_source": cookie_source,
                        "cookie_health": cookie_health,
                        "cookie_count": cookie_count,
                        "retryable": True,
                    }

            return {
                "status": "ok",
                "detail": "知乎登录态真实探针通过：CDP Cookie 可读、关键 Cookie 存在且低成本 API 验证成功",
                "platform": "zhihu",
                "probe": "cdp_cookie_and_live_search_api",
                "login_state": "authenticated",
                "platform_state": "authenticated",
                "login_required": False,
                "cdp_status": cdp_status,
                "cookie_source": cookie_source,
                "cookie_health": cookie_health,
                "cookie_count": cookie_count,
                "live_probe_items": len(live_items or []),
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "detail": f"知乎登录态探针异常: {exc}",
                "platform": "zhihu",
                "probe": "cdp_cookie_and_live_search_api",
                "login_state": "unknown",
                "platform_state": "unknown",
                "login_required": False,
                "cdp_status": cdp_status,
                "cookie_health": cookie_health,
                "retryable": True,
            }

    def _inspect_xhs(self, *, cdp_status: str, warn_within_hours: int) -> Dict:
        if self.bridge_path and not __import__("os").path.isfile(self.bridge_path):
            return {
                "status": "down",
                "detail": "小红书 bridge 文件不存在",
                "platform": "xiaohongshu",
                "probe": "web_user_me",
                "login_state": "unknown",
                "login_required": False,
                "bridge": self.bridge_path,
            }
        if cdp_status != "ok":
            return {
                "status": "ok",
                "detail": "小红书 CDP 当前未连接；health_check 不启动浏览器，search_xiaohongshu 会按需启动并验证登录态",
                "platform": "xiaohongshu",
                "probe": "web_user_me",
                "login_state": "not_checked",
                "login_required": False,
                "skipped": True,
                "cdp_status": cdp_status,
                "bridge": self.bridge_path,
            }

        try:
            cdp_url = self.chrome_debug_url("xhs")
            login_health = self.inspect_login_health(cdp_url, warn_within_hours) if self.inspect_login_health else {}
            state = self.probe_page_state(cdp_url) if self.probe_page_state else {}
            ok = bool(state.get("ok"))
            verification_required = bool(state.get("has_verify_prompt"))
            base = {
                "platform": "xiaohongshu",
                "probe": "web_user_me",
                "cdp_status": cdp_status,
                "bridge": self.bridge_path,
                "http_status": state.get("status"),
                "code": state.get("code"),
                "msg": state.get("msg", ""),
                "nickname": state.get("nickname", ""),
                "user_id": state.get("user_id", ""),
                "login_health": login_health,
            }
            if ok and verification_required:
                foreground = self.bring_to_front("xhs") if self.auto_foreground_on_verification and self.bring_to_front else {}
                return {
                    **base,
                    "status": "degraded",
                    "detail": "小红书 user/me 已登录，但当前页面出现平台验证/APP扫码查看提示；搜索或详情可能需要手动验证",
                    "login_state": "authenticated",
                    "platform_state": "platform_verification_required",
                    "login_required": False,
                    "manual_action_required": True,
                    "foreground_action": foreground,
                    "recommended_action": "health_check(mode='request_browser_interaction:xhs:platform_verification_required')",
                }
            if verification_required and not ok:
                foreground = self.bring_to_front("xhs") if self.auto_foreground_on_verification and self.bring_to_front else {}
                return {
                    **base,
                    "status": "degraded",
                    "detail": "小红书页面出现平台验证/APP扫码查看提示；请在当前 Chrome 窗口完成手动验证后重试",
                    "login_state": "unknown",
                    "platform_state": "platform_verification_required",
                    "login_required": False,
                    "manual_action_required": True,
                    "foreground_action": foreground,
                    "recommended_action": "health_check(mode='request_browser_interaction:xhs:platform_verification_required')",
                }
            background = self.send_to_background("xhs") if ok and self.send_to_background else {}
            return {
                **base,
                "status": "ok" if ok and login_health.get("status") == "ok" else "degraded",
                "detail": "小红书登录态探针通过" if ok else "小红书登录态探针未通过：接口返回游客或鉴权失败",
                "login_state": "authenticated" if ok else "guest_or_expired",
                "platform_state": "authenticated" if ok else "blocked_or_expired",
                "login_required": not ok,
                "manual_action_required": False,
                "background_action": background,
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "detail": f"小红书登录态探针异常：{exc}",
                "platform": "xiaohongshu",
                "probe": "web_user_me",
                "login_state": "unknown",
                "platform_state": "unknown",
                "login_required": False,
                "cdp_status": cdp_status,
                "bridge": self.bridge_path,
                "retryable": True,
            }
