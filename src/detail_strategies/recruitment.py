"""Recruitment platform detail strategies."""

from __future__ import annotations

from typing import Callable, Dict

from kr_core import DetailRequest, DetailResponse, EvidenceItem


class RecruitmentDetailStrategy:
    """Detail extractor wrapper for browser-backed recruitment pages."""

    def __init__(
        self,
        *,
        platform: str,
        extractor: Callable[[str], Dict],
        evidence_builder: Callable[[str, str, Dict], EvidenceItem],
    ) -> None:
        self.platform = platform
        self._extractor = extractor
        self._evidence_builder = evidence_builder

    def extract(self, request: DetailRequest) -> DetailResponse:
        result = self._extractor(request.url)
        platform = str(result.get("platform") or request.platform or self.platform)
        normalized = _normalize_recruitment_detail(platform, request.url, result)
        return DetailResponse.from_legacy(
            platform,
            request.url,
            normalized,
            evidence=self._evidence_builder(request.url, platform, normalized),
            metadata={"strategy": "recruitment_browser_detail", "platform": platform},
        )


def _normalize_recruitment_detail(platform: str, url: str, result: Dict) -> Dict:
    content = str(result.get("content") or result.get("jd") or result.get("description") or "")
    data = {
        **result,
        "platform": platform,
        "url": str(result.get("url") or url),
        "title": str(result.get("title") or ""),
        "salary": str(result.get("salary") or ""),
        "content": content,
        "desc": content[:240],
        "content_type": "recruitment_job_detail",
    }
    status = str(result.get("status") or "").lower()
    if status == "needs_interaction":
        data.setdefault("failure_type", result.get("failure_type") or "platform_verification_required")
        data.setdefault("error", "招聘详情页需要人工验证或登录")
        if result.get("hint"):
            data.setdefault("hint", result.get("hint"))
    elif status in {"failed", "empty"}:
        data.setdefault("failure_type", "empty_detail" if status == "empty" else "request_failed")
        data.setdefault("error", result.get("error") or "招聘详情页提取失败")
        if result.get("hint"):
            data.setdefault("hint", result.get("hint"))
    elif not _usable_recruitment_detail(data):
        # Keep this final gate in the strategy wrapper as a defense against a
        # collector regression.  A login/register shell may have a title and
        # a little text, but it is not a usable job detail and must never be
        # promoted to an `ok` DetailResponse.
        if _looks_like_login_shell(data):
            data.update(
                {
                    "status": "needs_interaction",
                    "failure_type": "login_required",
                    "platform_state": "login_required_for_detail",
                    "manual_action_required": True,
                    "error": "招聘详情页只返回登录或注册页面，未取得职位正文",
                    "hint": "请通过统一浏览器人工交互入口完成登录后重试。",
                }
            )
        else:
            data.update(
                {
                    "status": "empty",
                    "failure_type": "empty_detail",
                    "platform_state": "empty_detail",
                    "manual_action_required": False,
                    "error": "招聘详情页正文不足，不能作为可用职位结果",
                }
            )
    return data


def _usable_recruitment_detail(data: Dict) -> bool:
    content = str(data.get("content") or "").strip()
    if len(content) < 80:
        return False
    return not _looks_like_login_shell(data)


def _looks_like_login_shell(data: Dict) -> bool:
    text = " ".join(
        str(data.get(key) or "") for key in ("title", "content", "desc", "error", "hint")
    ).lower()
    login_markers = ("扫码登录", "密码登录", "验证码登录", "登录/注册", "立即登录", "注册")
    job_markers = ("职位详情", "职位描述", "岗位职责", "任职要求", "job description", "responsibilities")
    return any(marker in text for marker in login_markers) and not any(marker in text for marker in job_markers)
