"""KnowledgeRadar runtime health checks.

The MCP tool entrypoint lives in server.py; this module owns the diagnostic
workflow and receives business/platform callables as dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from runtime.process import silent_subprocess_run
import time
from typing import Callable, Dict

import httpx

from capabilities import build_tool_surface

from .dependency_preflight import dependency_preflight_summary
from .health_layers import PlatformLayerSpec, build_platform_health_layers, layer
from .login_state import LoginProbe
from .proxy_config import proxy_health_summary
from .browser_health import camoufox_v2_health
from .project_state import project_governance_manifest
from .status_schema import (
    aggregate_validation_status,
    canonical_status_counts,
    classify_runtime_payload,
    legacy_health_status,
)


@dataclass(frozen=True)
class HealthCheckDeps:
    bili_headers: Dict
    xhs_bridge_path: str
    project_root: str
    runtime_log_dir: str
    mcp_server_log_path: str
    decision_log_path: str
    decision_logger: object
    provider_status: Callable[[], Dict]
    academic_provider_status: Callable[[], Dict]
    chrome_debug_port: Callable[[str], str]
    chrome_debug_url: Callable[[str], str]
    chrome_runtime_summary: Callable[[], Dict]
    ensure_chrome_debugging: Callable[[str], bool]
    finish_chrome_automation: Callable[[str, str], None]
    find_chrome_exe: Callable[[], str | None]
    cleanup_chrome_platform: Callable[[str], None]
    read_zhihu_cookies_from_cdp: Callable[[], str | None]
    read_zhihu_cookies_from_profile: Callable[[], str | None]
    zhihu_sign: Callable[[str, str], Dict]
    zhihu_search_api: Callable[[str, str, int], list]
    inspect_zhihu_cookie_health: Callable[[int], Dict]
    inspect_xhs_login_health: Callable[[str, int], Dict]
    probe_xhs_page_state: Callable[[str], Dict]
    bring_chrome_to_front: Callable[[str], Dict]
    background_chrome: Callable[[str], Dict]
    legacy_search_bilibili: Callable[..., Dict]
    legacy_search_zhihu: Callable[..., Dict]
    legacy_search_xiaohongshu: Callable[..., Dict]
    youtube_configured: Callable[[], bool]
    platform_capabilities: Callable[[], Dict]
    task_queue_summary: Callable[[], Dict]
    usage_summary: Callable[[], Dict]
    monitor_summary: Callable[[], Dict]
    degradation_summary: Callable[[], Dict]
    xhs_detail_health_summary: Callable[[], Dict]
    xhs_chain_health_summary: Callable[[], Dict]
    evidence_store_health: Callable[[], Dict]
    academic_evidence_summary: Callable[[], Dict]
    search_cache_summary: Callable[[], Dict]
    gh_cli_sidecar_health: Callable[[], Dict]


class HealthCheckService:
    def __init__(self, deps: HealthCheckDeps, *, force_zhihu_login_probe: bool = False):
        self.deps = deps
        self.checks: Dict[str, Dict] = {}
        self.force_zhihu_login_probe = force_zhihu_login_probe

    def run(self) -> Dict:
        d = self.deps
        checks = self.checks

        checks["bilibili"] = self._check_bilibili()
        checks["proxy_config"] = self._check_proxy_config()
        checks["dependency_preflight"] = self._check_dependency_preflight()
        checks["node"] = self._check_node()
        checks["runtime_logs"] = self._check_runtime_logs()
        checks["decision_log"] = self._check_decision_log()
        checks["task_queue"] = self._check_task_queue()
        checks["usage_summary"] = self._check_usage_summary()
        checks["monitor_summary"] = self._check_monitor_summary()
        checks["degradation_summary"] = self._check_degradation_summary()
        checks["evidence_store"] = self._check_evidence_store()
        checks["academic_evidence"] = self._check_academic_evidence()
        checks["search_cache"] = self._check_search_cache()
        checks["xiaohongshu_detail_health"] = self._check_xiaohongshu_detail_health()
        checks["xiaohongshu_chain_health"] = self._check_xiaohongshu_chain_health()
        checks["sidecar_contract"] = self._check_sidecar_contract()
        checks["gh_cli_sidecar"] = self._check_gh_cli_sidecar()
        checks["project_governance"] = self._check_project_governance()
        checks["camoufox_v2"] = self._check_camoufox_v2()
        checks["web_search_provider"] = self._check_web_search_provider()
        checks["academic_provider"] = self._check_academic_provider()
        checks["generic_web_collector"] = self._check_generic_web_collector()
        checks["chrome_runtime"] = self._check_chrome_runtime()
        checks["chrome_cdp_xhs"] = self._check_chrome_cdp("xhs")
        checks["chrome_cdp_zhihu"] = self._check_chrome_cdp("zhihu")
        if self.force_zhihu_login_probe:
            checks["chrome_cdp_zhihu"] = self._ensure_and_check_chrome_cdp("zhihu")
            try:
                checks["zhihu"] = self._check_zhihu()
            finally:
                try:
                    d.finish_chrome_automation("zhihu", "health_check_zhihu_login_probe")
                except Exception:
                    pass
        else:
            checks["zhihu"] = self._check_zhihu()
        checks["xiaohongshu"] = self._check_xiaohongshu()
        checks["youtube"] = self._check_youtube()
        checks["search_probe_bilibili"] = self._probe_search_skipped("B站")
        checks["search_probe_zhihu"] = self._probe_search_skipped("知乎")
        checks["search_probe_xiaohongshu"] = self._probe_search_skipped("小红书")

        rollup = _validation_rollup(checks)
        status = legacy_health_status(rollup["status_class"])

        return {
            "status": status,
            "checks": checks,
            "validation_rollup": {
                "schema": "knowledgeradar-validation-rollup/v1",
                "overall_status": status,
                "status_class": rollup["status_class"],
                "counts": rollup["counts"],
                "checks": rollup["checks"],
                "status_policy": "PASS and EXPECTED_DEGRADED are non-blocking; FAIL and NEEDS_INTERACTION block. Legacy health status maps PASS=ok, EXPECTED_DEGRADED/NEEDS_INTERACTION=degraded, FAIL=down.",
                "project_governance": checks.get("project_governance", {}),
            },
            "platform_health_layers": self._build_platform_health_layers(),
            "capabilities": {
                "schema_version": "knowledgeradar-capabilities/v1",
                "tool_surface": build_tool_surface(),
                "platforms": d.platform_capabilities(),
                "detail": {
                    "standard_models": ["DetailRequest", "DetailResponse", "EvidenceItem"],
                    "external_signature_stable": True,
                    "evidence_attached": True,
                },
                "decision_logging": {
                    "schema": "knowledgeradar-decision-log/v1",
                    "path": d.decision_log_path,
                    "records": ["detail_extract"],
                    "analysis_tool": "analyze_decision_logs",
                },
                "sidecar_governance": {
                    "schema": "knowledgeradar-sidecar-governance/v1",
                    "contract_path": _sidecar_contract_path(d.project_root),
                    "standard_models": ["SearchResponse", "DetailResponse", "EvidenceItem"],
                    "required_fields": ["preflight", "health", "timeout", "schema_normalize", "error_normalize", "kill_switch"],
                    "default_policy": "L7 candidates are disabled or isolated until health, schema, error and account-isolation gates pass.",
                },
                "project_governance": project_governance_manifest(),
            },
        }

    def _build_platform_health_layers(self) -> Dict[str, Dict[str, Dict]]:
        checks = self.checks
        return build_platform_health_layers(
            checks,
            [
                PlatformLayerSpec(
                    platform="B站",
                    login=lambda c: layer("ok", "B站搜索当前不强依赖登录态"),
                    search=lambda c: layer(
                        c.get("bilibili", {}).get("status", "unknown"),
                        c.get("bilibili", {}).get("detail", ""),
                        probe=c.get("search_probe_bilibili", {}),
                    ),
                    detail=lambda c: layer("ok", "B站详情依赖公开视频接口和转写任务队列"),
                    multimodal=lambda c: layer(
                        c.get("task_queue", {}).get("status", "unknown"),
                        "B站多模态/转写通过任务队列观测",
                        task_queue=c.get("task_queue", {}),
                    ),
                ),
                PlatformLayerSpec(
                    platform="知乎",
                    login=lambda c: layer(
                        c.get("zhihu", {}).get("status", "unknown"),
                        c.get("zhihu", {}).get("detail", ""),
                        login_state=c.get("zhihu", {}).get("login_state"),
                        cookie_source=c.get("zhihu", {}).get("cookie_source"),
                        cdp_status=c.get("zhihu", {}).get("cdp_status"),
                    ),
                    search=lambda c: layer(
                        c.get("search_probe_zhihu", {}).get("status", c.get("zhihu", {}).get("status", "unknown")),
                        c.get("search_probe_zhihu", {}).get("detail", "知乎搜索可用性依赖登录态和签名 API"),
                        probe=c.get("search_probe_zhihu", {}),
                    ),
                    detail=lambda c: layer(c.get("zhihu", {}).get("status", "unknown"), "知乎详情依赖 Cookie/CDP 和正文解析"),
                    multimodal=lambda c: layer("not_applicable", "知乎当前以文本详情为主"),
                ),
                PlatformLayerSpec(
                    platform="小红书",
                    login=lambda c: layer(
                        c.get("xiaohongshu", {}).get("status", "unknown"),
                        c.get("xiaohongshu", {}).get("detail", ""),
                        login_state=c.get("xiaohongshu", {}).get("login_state"),
                        platform_state=c.get("xiaohongshu", {}).get("platform_state"),
                        manual_action_required=c.get("xiaohongshu", {}).get("manual_action_required"),
                    ),
                    search=lambda c: layer(
                        c.get("search_probe_xiaohongshu", {}).get("status", c.get("xiaohongshu", {}).get("status", "unknown")),
                        c.get("search_probe_xiaohongshu", {}).get("detail", "小红书搜索依赖 Scrapling/CDP/bridge"),
                        probe=c.get("search_probe_xiaohongshu", {}),
                        chain=c.get("xiaohongshu_chain_health", {}).get("discovery", {}),
                    ),
                    detail=lambda c: layer(
                        c.get("xiaohongshu_detail_health", {}).get("status", "unknown"),
                        c.get("xiaohongshu_detail_health", {}).get("detail", "小红书详情健康摘要不可用"),
                        success_rate=c.get("xiaohongshu_detail_health", {}).get("success_rate"),
                        avg_latency_s=c.get("xiaohongshu_detail_health", {}).get("avg_latency_s"),
                        anti_bot_count=c.get("xiaohongshu_detail_health", {}).get("anti_bot_count"),
                        empty_detail_count=c.get("xiaohongshu_detail_health", {}).get("empty_detail_count"),
                    ),
                    multimodal=lambda c: layer(
                        c.get("task_queue", {}).get("status", "unknown"),
                        "小红书首图 OCR/图片理解通过 TaskStore fan-in 观测；平台可用性见 detail/search 层",
                        task_queue=c.get("task_queue", {}),
                        degradation=c.get("degradation_summary", {}),
                    ),
                ),
                PlatformLayerSpec(
                    platform="YouTube",
                    login=lambda c: layer("not_applicable", "YouTube Data API v3 使用 API Key/OAuth，不依赖浏览器登录态"),
                    search=lambda c: layer(
                        c.get("youtube", {}).get("status", "unknown"),
                        c.get("youtube", {}).get("detail", ""),
                    ),
                    detail=lambda c: layer(
                        c.get("youtube", {}).get("status", "unknown"),
                        "YouTube 详情依赖 videos.list；字幕依赖 captions.list + transcript fallback",
                    ),
                    multimodal=lambda c: layer(
                        c.get("task_queue", {}).get("status", "unknown"),
                        "YouTube 视频画面理解复用 L2 多模态任务队列",
                        task_queue=c.get("task_queue", {}),
                    ),
                ),
            ],
        )

    def _check_bilibili(self) -> Dict:
        try:
            resp = httpx.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=self.deps.bili_headers,
                timeout=5,
            )
            if resp.status_code == 200:
                return {"status": "ok", "detail": "B站 API 可访问"}
            if resp.status_code in (429, 401, 403, 412):
                return {"status": "degraded", "detail": f"B站 API 风控/限流 HTTP {resp.status_code}", "retryable": True}
            return {"status": "degraded", "detail": f"B站 API HTTP {resp.status_code}", "retryable": True}
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            return {"status": "down", "detail": f"B站 API 网络异常: {e}", "retryable": True}
        except Exception as e:
            return {"status": "down", "detail": f"B站 API 检查失败: {e}"}

    def _check_proxy_config(self) -> Dict:
        try:
            summary = proxy_health_summary()
            if summary.get("configured"):
                summary["detail"] = f"运行时代理已配置，来源={summary.get('source')}"
            else:
                summary["detail"] = "运行时代理未配置，将使用直连或各库默认 trust_env 行为"
            return summary
        except Exception as e:
            return {"status": "degraded", "detail": f"代理配置检查失败: {e}", "retryable": True}

    def _check_dependency_preflight(self) -> Dict:
        try:
            return dependency_preflight_summary()
        except Exception as e:
            return {"status": "degraded", "detail": f"依赖 preflight 检查失败: {e}", "retryable": True}

    def _check_node(self) -> Dict:
        try:
            proc = silent_subprocess_run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if proc.returncode == 0:
                return {"status": "ok", "detail": proc.stdout.strip() or "node available"}
            return {"status": "down", "detail": (proc.stderr or proc.stdout or "node exited non-zero").strip()}
        except Exception as e:
            return {"status": "down", "detail": f"Node 不可用: {e}"}

    def _check_youtube(self) -> Dict:
        try:
            if self.deps.youtube_configured():
                return {
                    "status": "ok",
                    "detail": "YouTube Data API Key 已配置；搜索/详情调用时使用 search.list/videos.list/captions.list",
                }
            return {
                "status": "degraded",
                "detail": "YouTube Data API Key 未配置；设置 YOUTUBE_API_KEY 后启用",
                "error_type": "not_configured",
                "retryable": False,
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"YouTube 配置检查失败: {e}", "retryable": True}

    def _check_runtime_logs(self) -> Dict:
        d = self.deps
        try:
            os.makedirs(d.runtime_log_dir, exist_ok=True)
            with open(d.mcp_server_log_path, "a", encoding="utf-8") as f:
                f.write("")
            return {
                "status": "ok",
                "detail": "运行日志归档可写",
                "log_dir": d.runtime_log_dir,
                "server_log": d.mcp_server_log_path,
            }
        except Exception as e:
            return {
                "status": "degraded",
                "detail": f"运行日志归档不可写: {e}",
                "log_dir": d.runtime_log_dir,
                "server_log": d.mcp_server_log_path,
                "retryable": True,
            }

    def _check_decision_log(self) -> Dict:
        return self.deps.decision_logger.health()

    def _check_task_queue(self) -> Dict:
        try:
            summary = self.deps.task_queue_summary()
            active = int(summary.get("active") or 0)
            stale_count = int(summary.get("stale_count") or 0)
            stale = summary.get("stale") or []
            detail = f"任务队列可用，active={active}, stale={stale_count}, total={summary.get('total', 0)}"
            result = {**summary, "status": "ok", "detail": detail}
            if stale:
                result["stale_preview"] = stale[:3]
            result["recent_failed_preview"] = (summary.get("recent_failed") or [])[:3]
            return result
        except Exception as e:
            return {"status": "degraded", "detail": f"任务队列摘要不可用: {e}", "retryable": True}

    def _check_project_governance(self) -> Dict:
        try:
            manifest = project_governance_manifest()
            return {
                "status": "ok",
                "detail": "项目治理状态包和 AI 开工/收口脚本已声明",
                "status_dir": manifest["status_dir"],
                "scripts": manifest["scripts"],
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"项目治理状态不可读: {e}", "retryable": True}

    def _check_usage_summary(self) -> Dict:
        try:
            summary = self.deps.usage_summary()
            by_model = summary.get("by_model") or []
            top = by_model[:3]
            return {
                "status": "ok",
                "detail": f"usage 可用，calls={summary.get('total_calls', 0)}, tokens={summary.get('total_tokens', 0)}",
                "total_calls": summary.get("total_calls", 0),
                "total_tokens": summary.get("total_tokens", 0),
                "top_models": top,
                "db_path": summary.get("db_path", ""),
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"usage summary 不可用: {e}", "retryable": True}

    def _check_monitor_summary(self) -> Dict:
        try:
            summary = self.deps.monitor_summary()
            groups = summary.get("groups") or []
            top = groups[:5]
            return {
                "status": "ok",
                "detail": f"monitor 可用，groups={len(groups)}, recent={len(summary.get('recent') or [])}",
                "groups": top,
                "db_path": summary.get("db_path", ""),
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"monitor summary 不可用: {e}", "retryable": True}

    def _check_degradation_summary(self) -> Dict:
        try:
            summary = self.deps.degradation_summary()
            return {
                "status": "ok",
                "detail": f"degradation 可用，open_breakers={summary.get('open_breaker_count', 0)}",
                "open_breaker_count": summary.get("open_breaker_count", 0),
                "open_breakers": summary.get("open_breakers", [])[:5],
                "db_path": summary.get("db_path", ""),
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"degradation summary 不可用: {e}", "retryable": True}

    def _check_evidence_store(self) -> Dict:
        try:
            return self.deps.evidence_store_health()
        except Exception as e:
            return {"status": "degraded", "detail": f"证据仓库不可用: {e}", "retryable": True}

    def _check_academic_evidence(self) -> Dict:
        try:
            return self.deps.academic_evidence_summary()
        except Exception as e:
            return {"status": "degraded", "detail": f"学术 evidence 摘要不可用: {e}", "retryable": True}

    def _check_search_cache(self) -> Dict:
        try:
            return self.deps.search_cache_summary()
        except Exception as e:
            return {"status": "degraded", "detail": f"搜索缓存不可用: {e}", "retryable": True}

    def _check_xiaohongshu_detail_health(self) -> Dict:
        try:
            summary = self.deps.xhs_detail_health_summary()
            return {
                "status": summary.get("status", "ok"),
                "detail": summary.get("detail", "小红书详情健康摘要可用"),
                "total": summary.get("total", 0),
                "success_rate": summary.get("success_rate"),
                "avg_latency_s": summary.get("avg_latency_s"),
                "anti_bot_count": summary.get("anti_bot_count", 0),
                "empty_detail_count": summary.get("empty_detail_count", 0),
                "path": summary.get("path", ""),
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"小红书详情健康摘要不可用: {e}", "retryable": True}

    def _check_xiaohongshu_chain_health(self) -> Dict:
        try:
            summary = self.deps.xhs_chain_health_summary()
            return {
                "status": summary.get("status", "ok"),
                "detail": summary.get("detail", "小红书链路健康摘要可用"),
                "discovery": summary.get("discovery", {}),
                "detail_layer": summary.get("detail_layer", {}),
                "path": summary.get("path", ""),
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"小红书链路健康摘要不可用: {e}", "retryable": True}

    def _check_sidecar_contract(self) -> Dict:
        path = _sidecar_contract_path(self.deps.project_root)
        exists = os.path.isfile(path)
        return {
            "status": "ok" if exists else "degraded",
            "detail": "L7 sidecar contract available" if exists else "L7 sidecar contract missing",
            "path": path,
            "schema": "knowledgeradar-sidecar-governance/v1",
            "required_fields": ["preflight", "health", "timeout", "schema_normalize", "error_normalize", "kill_switch"],
            "candidate_policy": "external sidecars must pass isolation and health gates before strategy-tree admission",
        }

    def _check_gh_cli_sidecar(self) -> Dict:
        try:
            return self.deps.gh_cli_sidecar_health()
        except Exception as e:
            return {"status": "degraded", "detail": f"gh CLI sidecar health 不可用: {e}", "retryable": True}

    def _check_camoufox_v2(self) -> Dict:
        try:
            return camoufox_v2_health()
        except Exception as e:
            return {"status": "degraded", "detail": f"Camoufox v2 health 不可用: {e}", "retryable": True}

    def _check_web_search_provider(self) -> Dict:
        status = self.deps.provider_status()
        provider_rows = {name: item for name, item in status.items() if isinstance(item, dict) and not name.startswith("_")}
        quota = status.get("_quota") if isinstance(status.get("_quota"), dict) else {}
        tavily_quota = quota.get("tavily") if isinstance(quota.get("tavily"), dict) else {}
        configured = [name for name, item in provider_rows.items() if item.get("configured")]
        available = [name for name, item in provider_rows.items() if item.get("available")]
        unconfigured = [name for name, item in provider_rows.items() if not item.get("configured")]
        optional_degraded = [
            name
            for name, item in provider_rows.items()
            if item.get("degraded_ok") and not item.get("available")
        ]
        if tavily_quota.get("status") in {"daily_exhausted", "monthly_exhausted"} and "tavily" in provider_rows:
            optional_degraded.append("tavily")
        optional_degraded = sorted(set(optional_degraded))
        try:
            from search_providers.planner import auto_search_plan

            default_waves = auto_search_plan(provider_rows).to_dict().get("waves", [])
        except Exception:
            default_waves = []
        if available:
            return {
                "status": "ok",
                "detail": f"通用联网搜索 provider 可用: {', '.join(available)}",
                "providers": available,
                "configured_providers": configured,
                "unconfigured_providers": unconfigured,
                "optional_degraded_providers": optional_degraded,
                "default_waves": default_waves,
                "provider_strategy": "profile_parallel_waves_with_paid_tavily_supplement",
                "provider_status": status,
                "quota": quota,
                "validation_status": "PASS",
                "status_class": "PASS",
                "validation_semantics": "optional_degraded_providers, host absent cards, and Tavily daily/monthly quota exhaustion do not fail health when at least one generic web provider is available",
            }
        exhausted = tavily_quota.get("status") in {"daily_exhausted", "monthly_exhausted"}
        if configured:
            return {
                "status": "degraded",
                "detail": (
                    "通用联网搜索 provider 已配置但不可用；Tavily 配额已耗尽"
                    if exhausted
                    else f"通用联网搜索 provider 已配置但不可用: {', '.join(configured)}"
                ),
                "providers": [],
                "configured_providers": configured,
                "unconfigured_providers": unconfigured,
                "optional_degraded_providers": optional_degraded,
                "default_waves": default_waves,
                "provider_strategy": "profile_parallel_waves_with_paid_tavily_supplement",
                "provider_status": status,
                "quota": quota,
                "degraded_reason": "quota_exhausted" if exhausted else "configured_provider_unavailable",
                "retryable": True,
                "validation_status": "EXPECTED_DEGRADED",
                "status_class": "EXPECTED_DEGRADED",
            }
        return {
            "status": "degraded",
            "detail": "通用联网搜索 provider 未配置：缺少 TAVILY_API_KEY 和 SEARXNG_BASE_URL",
            "providers": [],
            "configured_providers": [],
            "unconfigured_providers": unconfigured,
            "optional_degraded_providers": optional_degraded,
            "default_waves": default_waves,
            "provider_strategy": "profile_parallel_waves_with_paid_tavily_supplement",
            "provider_status": status,
            "quota": quota,
            "degraded_reason": "no_generic_web_provider_configured",
            "retryable": True,
            "validation_status": "EXPECTED_DEGRADED",
            "status_class": "EXPECTED_DEGRADED",
        }

    def _check_academic_provider(self) -> Dict:
        try:
            status = self.deps.academic_provider_status()
            available = [name for name, item in status.items() if item.get("available")]
            counts = canonical_status_counts(status.values())
            if available:
                return {
                    "status": "ok",
                    "detail": f"学术元数据 provider 可用: {', '.join(available)}",
                    "providers": available,
                    "provider_status": status,
                    "status_counts": counts,
                    "validation_status": "PASS",
                    "status_class": "PASS",
                }
            return {
                "status": "degraded",
                "detail": "学术元数据 provider 不可用",
                "provider_status": status,
                "status_counts": counts,
                "validation_status": "EXPECTED_DEGRADED",
                "status_class": "EXPECTED_DEGRADED",
                "retryable": True,
            }
        except Exception as e:
            return {"status": "degraded", "detail": f"学术 provider 检查失败: {e}", "retryable": True}

    def _check_generic_web_collector(self) -> Dict:
        try:
            import bs4  # noqa: F401
            import lxml  # noqa: F401
            import trafilatura  # noqa: F401
            from readability import Document  # noqa: F401

            return {
                "status": "ok",
                "detail": "通用网页抽取依赖可用：Jina Reader + trafilatura + readability + static_html fallback",
                "collectors": ["jina_reader", "trafilatura", "readability", "static_html"],
                "strategy_order": ["jina_reader", "trafilatura", "readability", "static_html", "dynamic_playwright_explicit"],
            }
        except Exception as e:
            return {
                "status": "degraded",
                "detail": f"通用网页抽取 fallback 依赖不完整: {e}",
                "collectors": ["jina_reader", "static_html"],
                "strategy_order": ["jina_reader", "static_html", "dynamic_playwright_explicit"],
                "retryable": True,
            }

    def _check_chrome_runtime(self) -> Dict:
        try:
            return self.deps.chrome_runtime_summary()
        except Exception as e:
            return {"status": "degraded", "detail": f"Chrome runtime 摘要不可用: {e}", "retryable": True}

    def _check_chrome_cdp(self, platform: str = "xhs") -> Dict:
        d = self.deps
        debug_port = d.chrome_debug_port(platform)
        debug_url = d.chrome_debug_url(platform)
        try:
            resp = httpx.get(f"{debug_url}/json/version", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return {"status": "ok", "detail": data.get("Browser", "Chrome CDP 可访问"), "port": debug_port}
            return {"status": "degraded", "detail": f"Chrome CDP HTTP {resp.status_code}", "port": debug_port}
        except Exception as e:
            chrome = d.find_chrome_exe()
            if chrome:
                return {
                    "status": "ok",
                    "detail": f"Chrome 已安装但 CDP 当前未连接；按需调用会自动启动: {e}",
                    "port": debug_port,
                    "skipped": True,
                    "on_demand": True,
                }
            return {"status": "down", "detail": f"Chrome/CDP 不可用: {e}", "port": debug_port}

    def _ensure_and_check_chrome_cdp(self, platform: str = "xhs") -> Dict:
        if not self.deps.ensure_chrome_debugging(platform):
            return {
                "status": "degraded",
                "detail": "Chrome 受管实例启动失败或 CDP 未就绪",
                "port": self.deps.chrome_debug_port(platform),
                "retryable": True,
            }
        return self._check_chrome_cdp(platform)

    def _check_zhihu(self) -> Dict:
        d = self.deps
        cdp_check = self.checks.get("chrome_cdp_zhihu", {})
        cdp_status = "not_connected" if cdp_check.get("skipped") else cdp_check.get("status", "unknown")
        if self.force_zhihu_login_probe and cdp_status != "ok":
            return {
                "status": "degraded",
                "detail": "知乎真实登录态探针未通过：受管 CDP 未连接或启动失败",
                "platform": "zhihu",
                "probe": "cdp_cookie_and_live_search_api",
                "login_state": "not_checked",
                "platform_state": "cdp_unavailable",
                "login_required": False,
                "cdp_status": cdp_status,
                "chrome_cdp": cdp_check,
                "retryable": True,
            }
        return LoginProbe(
            platform="zhihu",
            chrome_debug_url=d.chrome_debug_url,
            inspect_cookie_health=d.inspect_zhihu_cookie_health,
            read_cookie_from_cdp=d.read_zhihu_cookies_from_cdp,
            read_cookie_from_profile=d.read_zhihu_cookies_from_profile,
            sign_cookie=d.zhihu_sign,
            zhihu_search_api=d.zhihu_search_api,
        ).inspect(cdp_status=cdp_status)

    def _check_xiaohongshu(self) -> Dict:
        d = self.deps
        cdp_check = self.checks.get("chrome_cdp_xhs", {})
        cdp_status = "not_connected" if cdp_check.get("skipped") else cdp_check.get("status")
        return LoginProbe(
            platform="xiaohongshu",
            chrome_debug_url=d.chrome_debug_url,
            inspect_login_health=d.inspect_xhs_login_health,
            probe_page_state=d.probe_xhs_page_state,
            bring_to_front=d.bring_chrome_to_front,
            send_to_background=d.background_chrome,
            bridge_path=d.xhs_bridge_path,
        ).inspect(cdp_status=cdp_status)

    def _probe_search(self, platform: str, keyword: str) -> Dict:
        started = time.time()
        try:
            if platform == "B站":
                result = self.deps.legacy_search_bilibili(keyword, page_size=1)
            elif platform == "知乎":
                if self.checks.get("zhihu", {}).get("status") != "ok":
                    return {"status": "degraded", "detail": "跳过真实搜索探活：知乎登录态/签名未就绪", "skipped": True}
                result = self.deps.legacy_search_zhihu(keyword, limit=1)
            elif platform == "小红书":
                if self.checks.get("xiaohongshu", {}).get("status") != "ok":
                    return {"status": "degraded", "detail": "跳过真实搜索探活：小红书依赖未就绪", "skipped": True}
                result = self.deps.legacy_search_xiaohongshu(keyword, limit=1, search_type="all")
            else:
                return {"status": "down", "detail": f"未知平台探活: {platform}"}

            elapsed = round(time.time() - started, 2)
            if isinstance(result, dict) and result.get("error"):
                error = result.get("error") or {}
                return {
                    "status": "degraded",
                    "detail": f"{platform} 真实搜索探活失败: {error.get('error') or error}",
                    "elapsed_s": elapsed,
                    "error": error,
                }
            items = result.get("items", []) if isinstance(result, dict) else []
            if items:
                first = items[0] if isinstance(items[0], dict) else {}
                return {
                    "status": "ok",
                    "detail": f"{platform} 真实搜索探活成功",
                    "elapsed_s": elapsed,
                    "keyword": keyword,
                    "total": len(items),
                    "first_title": first.get("title", ""),
                    "first_url": first.get("url", ""),
                }
            return {
                "status": "degraded",
                "detail": f"{platform} 真实搜索探活返回空结果",
                "elapsed_s": elapsed,
                "keyword": keyword,
            }
        except Exception as e:
            return {
                "status": "down",
                "detail": f"{platform} 真实搜索探活异常: {e}",
                "elapsed_s": round(time.time() - started, 2),
            }

    def _probe_search_skipped(self, platform: str) -> Dict:
        return {
            "status": "ok",
            "detail": f"{platform} 真实搜索探活未在 health_check 内执行；请调用对应 search_* 工具验证",
            "skipped": True,
            "reason": "health_check_fast_path",
        }


def _repo_path(project_root: str, *parts: str) -> str:
    root = project_root
    if os.path.basename(os.path.normpath(root)).lower() == "src":
        root = os.path.dirname(root)
    return os.path.join(root, *parts)


def _sidecar_contract_path(project_root: str) -> str:
    current = _repo_path(project_root, "docs", "归档", "2026-05", "标准与方法论", "2026-05-22_第七层边车接入契约.md")
    if os.path.isfile(current):
        return current
    legacy = _repo_path(project_root, "docs", "external_provider_contract.md")
    if os.path.isfile(legacy):
        return legacy
    return current


def _overall_status(checks: Dict[str, Dict]) -> str:
    neutral = {"not_configured", "skipped", "not_applicable"}
    statuses = [
        str(check.get("status", "down"))
        for check in checks.values()
        if str(check.get("status", "down")) not in neutral
    ]
    if not statuses:
        return "ok"
    if all(status == "ok" for status in statuses):
        return "ok"
    if any(status in {"ok", "degraded"} for status in statuses):
        return "degraded"
    return "down"


def _validation_rollup(checks: Dict[str, Dict]) -> Dict[str, object]:
    rows = []
    for name, check in (checks or {}).items():
        if not isinstance(check, dict):
            continue
        classification = classify_runtime_payload(
            check,
            required=_check_is_required(name, check),
            main_chain=_check_is_required(name, check),
            configured=True,
            has_declared_reason=bool(check.get("detail") or check.get("reason") or check.get("degraded_reason") or check.get("validation_reason")),
            optional=_check_is_optional(name, check),
        )
        rows.append({"name": name, **classification})
    return {
        "schema": "knowledgeradar-validation-rollup/v1",
        "status_class": aggregate_validation_status(rows),
        "counts": canonical_status_counts(rows),
        "checks": {
            row["name"]: {
                "status_class": row["status_class"],
                "raw_status": row["raw_status"],
                "reason": row.get("reason") or "",
                "blocks_overall_pass": row["blocks_overall_pass"],
            }
            for row in rows
        },
    }


def _check_is_required(name: str, check: Dict[str, object]) -> bool:
    if check.get("skipped") or str(check.get("status") or "") in {"not_applicable", "not_configured"}:
        return False
    return name in {
        "bilibili",
        "dependency_preflight",
        "node",
        "runtime_logs",
        "decision_log",
        "task_queue",
        "evidence_store",
        "search_cache",
        "sidecar_contract",
        "project_governance",
        "web_search_provider",
        "academic_provider",
        "generic_web_collector",
        "tool_surface",
    }


def _check_is_optional(name: str, check: Dict[str, object]) -> bool:
    return not _check_is_required(name, check) or bool(check.get("retryable")) or bool(check.get("skipped"))
