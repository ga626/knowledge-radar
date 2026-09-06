"""Sanitized product-facing status for the local setup and health page."""

from __future__ import annotations

import os
import json
import importlib.util
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any

from onboarding.configuration import public_provider_guides, public_snapshot
from runtime.media_cache import cleanup_expired_media_cache, media_cache_root
from runtime.executables import resolve_managed_chrome
from runtime.paths import project_root, whisper_model_cache_dir


CAPABILITY_PACKS = (
    {
        "id": "core_web",
        "label": "网页发现",
        "description": "先接通一个网页发现来源，即可开始开放网页、公众号发现与公开网页研究。",
        "fields": ("TAVILY_API_KEY", "ANYSEARCH_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY", "SEARXNG_BASE_URL"),
        "needs": "至少一个网页发现来源或自建 SearXNG",
        "boundary": "第三方额度、网络与来源可访问性仍会影响结果。",
        "status_kind": "setup",
        "tool_count": 4,
        "tools": ("kr_research", "kr_web_search", "search_wechat_articles", "extract_web_page"),
        "provider_ids": ("tavily", "anysearch", "brave", "exa", "searxng"),
    },
    {
        "id": "research_sources",
        "label": "资料来源",
        "description": "按研究问题补充学术、GitHub 与 YouTube；不是开始普通网页研究的前置条件。",
        "fields": ("YOUTUBE_API_KEY", "OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "BAIDU_QIANFAN_BEARER_TOKEN", "SERPAPI_API_KEY", "KR_CORE_API_KEY"),
        "needs": "YouTube 需要 Key；学术默认可走公开元数据；GitHub 推荐本机 gh 登录",
        "boundary": "学术授权、GitHub 限流和公开视频地域限制分别显示，不混成“已配置”。",
        "status_kind": "optional",
        "tool_count": 4,
        "tools": ("search_academic", "search_github_repositories", "search_youtube", "get_content_detail"),
        "provider_ids": ("youtube", "openalex", "semantic_scholar", "baidu_scholar", "serpapi_scholar", "core"),
    },
    {
        "id": "accounts_browser",
        "label": "平台账号",
        "description": "管理小红书、知乎、BOSS、猎聘等需要人工验证的平台会话。",
        "fields": (),
        "needs": "受管本地浏览器 Profile；仅在页面明确要求时，由你扫码、登录或完成验证",
        "boundary": "不保存密码、不绕过验证码或风控；空结果不是要求重新登录的理由。",
        "status_kind": "manual",
        "tool_count": 5,
        "tools": ("search_xiaohongshu", "manage_xiaohongshu_accounts", "search_zhihu", "search_recruitment", "get_content_detail"),
        "provider_ids": ("tikhub",),
    },
    {
        "id": "content_intelligence",
        "label": "模型服务",
        "description": "按需配置图文、视频或文本理解所使用的模型服务。浏览器、转写模型等本地依赖在“本地组件”中单独管理。",
        "fields": ("DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY", "MIMO_API_KEY", "API147_KEY", "LLM_API_KEY"),
        "needs": "基础文本不需要模型；需要深度理解时，选择一个适合任务的模型服务",
        "boundary": "可能产生媒体缓存、后台任务或费用；每次任务会说明实际采用的路径。",
        "status_kind": "optional",
        "tool_count": 2,
        "tools": ("get_content_detail", "get_task_status"),
        "provider_ids": ("dashscope", "siliconflow", "mimo", "api147", "compatible_llm"),
    },
)


def installation_summary() -> dict[str, Any]:
    """Return the active product identity without revealing a local path.

    The loopback console is also usable from a source checkout, so the
    installation root is opt-in.  A Release launcher supplies it and receives
    only version/channel/path hashes, never a configuration value or a path.
    """
    install_root_raw = os.environ.get("KR_INSTALL_ROOT", "").strip()
    if not install_root_raw:
        return {
            "available": False,
            "message": "源码兼容模式：安装身份会在已安装产品中显示。",
        }
    try:
        install_root = Path(install_root_raw).expanduser().resolve()
        active = json.loads((install_root / "active.json").read_text(encoding="utf-8"))
        if active.get("schema") != "knowledgeradar-active-install/v1":
            raise ValueError("unsupported active record")
        data_root = Path(str(active.get("data_root") or "")).resolve()
        return {
            "available": True,
            "version": str(active.get("version") or "unknown"),
            "channel": str(active.get("channel") or "unknown"),
            "data_root_hash": str(active.get("data_root_hash") or ""),
            "data_root_present": data_root.is_dir(),
            "rollback_available": (install_root / "backup" / "active.previous.json").is_file(),
            "message": "当前产品身份已加载；更新不会覆盖数据根。",
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "available": False,
            "message": "无法读取安装身份；请在安装器状态页检查后重试。",
        }


def _configured_keys(snapshot: dict[str, Any]) -> set[str]:
    return {str(field.get("key")) for field in snapshot.get("fields", []) if field.get("configured")}


def capability_packs(snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Describe readiness only; it never exposes values or changes enabled features."""
    snapshot = snapshot or public_snapshot()
    configured = _configured_keys(snapshot)
    rows: list[dict[str, Any]] = []
    for pack in CAPABILITY_PACKS:
        fields = tuple(pack["fields"])
        configured_count = len(configured.intersection(fields))
        status_kind = str(pack["status_kind"])
        ready = bool(configured_count) if fields else status_kind == "ready"
        if status_kind == "manual":
            status = "needs_interaction"
        elif status_kind == "optional" and not ready:
            status = "optional"
        else:
            status = "ready" if ready else "needs_setup"
        rows.append(
            {
                "id": pack["id"],
                "label": pack["label"],
                "description": pack["description"],
                "needs": pack["needs"],
                "boundary": pack["boundary"],
                "status": status,
                "status_kind": status_kind,
                "configured_field_count": configured_count,
                "field_count": len(fields),
                "tool_count": int(pack["tool_count"]),
                "tools": list(pack["tools"]),
                "provider_ids": list(pack["provider_ids"]),
            }
        )
    return rows


def optional_capabilities() -> list[dict[str, Any]]:
    """Return user-selectable downloads, never a generic host-software bucket."""
    root_raw = os.environ.get("KR_DATA_ROOT", "").strip()
    if not root_raw:
        return []
    data_root = Path(root_raw).expanduser()
    state_path = data_root / "state" / "capabilities.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    rows = state.get("capabilities", {}) if isinstance(state, dict) else {}
    xhs_ready = (
        isinstance(rows.get("xhs_bridge"), dict)
        and rows["xhs_bridge"].get("status") == "APPLIED"
        and (data_root / "capabilities" / "xhs-bridge" / "xhs_mcp_bridge.cjs").is_file()
    )
    browser_root = data_root / "playwright"
    browser_ready = isinstance(rows.get("browser"), dict) and rows["browser"].get("status") == "APPLIED" and browser_root.is_dir()
    media_downloader_ready = bool(importlib.util.find_spec("yt_dlp"))
    transcription_runtime_ready = bool(importlib.util.find_spec("faster_whisper"))
    model_cache = whisper_model_cache_dir()
    transcription_model_ready = model_cache.is_dir() and any(model_cache.iterdir())
    return [
        {
            "id": "browser",
            "label": "Playwright Chromium",
            "description": "为动态网页采集提供隔离 Chromium；普通静态网页研究不需要它。",
            "status": "ready" if browser_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "boundary": "在本控制台确认后下载到产品数据目录；不自动登录，也不调用付费 API。",
            "restart_required": False,
        },
        {
            "id": "xhs_bridge",
            "label": "小红书诊断 bridge",
            "description": "为小红书本地诊断增加受限 bridge；不是普通搜索或账号登录的前置条件。",
            "status": "ready" if xhs_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "depends_on": ("node",),
            "boundary": "在本控制台确认后下载 bridge 依赖；不会登录、绕过验证或自动启用生产兜底。",
            "restart_required": True,
        },
        {
            "id": "media_downloader",
            "label": "媒体下载器",
            "description": "为需要取得公开音视频的媒体理解任务提供下载能力；有现成字幕或公开文本时不需要它。",
            "status": "ready" if media_downloader_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "boundary": "仅在本控制台确认后安装到产品运行环境；实际任务仍遵守平台与内容访问规则。",
            "restart_required": False,
        },
        {
            "id": "transcription_runtime",
            "label": "本地转写运行时",
            "description": "启用本地音视频转写时必需；不使用本地转写则无需安装。",
            "status": "ready" if transcription_runtime_ready else "not_installed",
            "requirement": "启用本地转写后必需",
            "install_mode": "managed",
            "boundary": "仅在本控制台确认后安装到产品运行环境；不下载模型权重，也不会上传媒体。",
            "restart_required": False,
        },
        {
            "id": "transcription_model",
            "label": "基础转写模型（base）",
            "description": "本地转写运行时所需的模型权重；选择本地转写后必需，可在这里预先下载。",
            "status": "ready" if transcription_model_ready else "not_installed",
            "requirement": "启用本地转写后必需",
            "install_mode": "managed",
            "depends_on": ("transcription_runtime",),
            "boundary": "在本控制台确认后下载并只保存在产品数据目录；不会等到任务开始才悄悄下载。",
            "restart_required": False,
        },
    ]


def local_component_catalog() -> dict[str, Any]:
    """Organize local setup by user goal, with every download owned by the console."""
    chrome = resolve_managed_chrome()
    node_available = bool(shutil.which("node"))
    ffmpeg_available = bool(shutil.which("ffmpeg"))
    selectable = {row["id"]: row for row in optional_capabilities()}
    return {
        "summary": "插件本体只安装控制台和基础研究路径；所有额外功能组件都在这里按你的选择下载或完成安装引导。",
        "groups": [
            {
                "id": "web_automation",
                "label": "网页采集与自动化",
                "description": "处理动态网页或需要隔离浏览器的公开网页流程。",
                "items": [selectable["browser"]],
            },
            {
                "id": "platform_sessions",
                "label": "平台账号与浏览器协作",
                "description": "用于需要人工登录、扫码或平台诊断的流程。每项前置都在对应功能内说明。",
                "items": [
                    {
                        "id": "chrome",
                        "label": "Google Chrome",
                        "description": "平台账号登录、扫码和安全验证所需的受管浏览器。",
                        "status": "ready" if chrome else "not_installed",
                        "requirement": "启用平台账号后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；产品不会读取密码、Cookie 或绕过验证。",
                        "guide": {"official_url": "https://www.google.com/chrome/", "official_label": "打开 Chrome 官方下载页", "steps": ("打开官方安装页并下载适用于 Windows 的 Chrome 安装程序。", "按安装程序完成安装，不要把浏览器资料导入产品目录。", "回到本页刷新检测；后续只在页面明确要求时人工登录或扫码。")},
                    },
                    {
                        "id": "node",
                        "label": "Node.js 与 npm",
                        "description": "仅在启用小红书诊断 bridge 时需要的运行环境。",
                        "status": "ready" if node_available else "not_installed",
                        "requirement": "启用小红书诊断 bridge 后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；它本身不会自动启用任何平台访问。",
                        "guide": {"official_url": "https://nodejs.org/en/download", "official_label": "打开 Node.js 官方下载页", "steps": ("下载并安装 Node.js 的长期支持版（LTS）。", "安装时保留 npm；完成后重新打开本地控制台。", "回到本页刷新检测，再为小红书诊断 bridge 生成安装计划。")},
                    },
                    selectable["xhs_bridge"],
                ],
            },
            {
                "id": "media_understanding",
                "label": "本地媒体理解与转写",
                "description": "只有在需要下载公开媒体、提取关键帧或使用本地转写时才安装。",
                "items": [
                    selectable["media_downloader"],
                    selectable["transcription_runtime"],
                    selectable["transcription_model"],
                    {
                        "id": "ffmpeg",
                        "label": "FFmpeg",
                        "description": "本地音视频转码与关键帧提取所需的工具。",
                        "status": "ready" if ffmpeg_available else "not_installed",
                        "requirement": "启用本地媒体处理后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；不会自动扫描或处理你的媒体文件。",
                        "guide": {"official_url": "https://ffmpeg.org/download.html", "official_label": "打开 FFmpeg 官方下载页", "steps": ("从官方页面进入适用于 Windows 的可信发行渠道。", "按发行方说明安装，并确保 ffmpeg 可通过系统 PATH 或明确配置找到。", "回到本页刷新检测，再启用本地媒体处理或转写。")},
                    },
                ],
            },
            {
                "id": "experimental",
                "label": "实验与候选",
                "description": "未进入默认研究链路，不提供普通安装入口。",
                "items": [
                    {"id": "camoufox", "label": "Camoufox 浏览器候选", "description": "只用于隔离诊断或候选验证。", "status": "disabled", "requirement": "实验", "install_mode": "disabled", "boundary": "默认关闭，不会自动用于平台访问。"},
                    {"id": "funasr", "label": "FunASR 引擎候选", "description": "本地转写备选引擎，尚需独立验证。", "status": "disabled", "requirement": "实验", "install_mode": "disabled", "boundary": "默认关闭，不向普通使用者承诺可用。"},
                    {"id": "sherpa_onnx", "label": "sherpa-onnx 引擎候选", "description": "本地转写备选引擎，当前只保留隔离验证路径。", "status": "disabled", "requirement": "实验", "install_mode": "disabled", "boundary": "默认关闭，不提供安装入口。"},
                ],
            },
        ],
    }


def _run_product_installer(arguments: list[str], *, timeout: int) -> dict[str, Any]:
    """Call the active product installer and keep paths/output inside this process."""
    install_root = os.environ.get("KR_INSTALL_ROOT", "").strip()
    program_root = os.environ.get("KR_PROJECT_ROOT", "").strip()
    if not install_root or not program_root:
        raise RuntimeError("源码兼容模式不提供产品维护操作。")
    installer = Path(program_root) / "scripts" / "product_install.py"
    if not installer.is_file():
        raise RuntimeError("当前产品安装器不可用。")
    result = subprocess.run(
        [sys.executable, str(installer), *arguments, "--install-root", install_root],
        cwd=Path(program_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("产品维护命令未返回有效状态。") from error
    if result.returncode or payload.get("status") == "FAIL":
        raise RuntimeError(str(payload.get("error") or "产品维护操作未完成。"))
    return payload


def data_root_move_console_plan(target_root: str) -> dict[str, Any]:
    """Return a path-free migration plan; applying remains a separate CLI confirmation."""
    target_root = target_root.strip()
    if not target_root or any(char in target_root for char in "\r\n\x00"):
        raise ValueError("请输入一个有效的新数据根目录。")
    payload = _run_product_installer(["data-move-plan", "--data-root", target_root], timeout=180)
    source = payload.get("source", {})
    target = payload.get("target", {})
    locks = payload.get("browser_lock_relative_paths", [])
    return {
        "status": "PLAN",
        "source": {"files": int(source.get("files", 0)), "bytes": int(source.get("bytes", 0))},
        "target": {"exists": bool(target.get("exists")), "free_bytes": int(target.get("free_bytes", 0))},
        "required_free_bytes": int(payload.get("required_free_bytes", 0)),
        "browser_lock_count": len(locks) if isinstance(locks, list) else 0,
        "confirmation_token": str(payload.get("confirmation_token", "")),
        "next_step": "计划不会迁移数据。请复制确认令牌，按产品安装文档中的 data-move-apply 命令执行。",
    }


def optional_capability_plan(capability: str) -> dict[str, Any]:
    payload = _run_product_installer(["capability-plan", "--capability", capability], timeout=60)
    details = payload.get("details", {})
    return {
        "status": "PLAN",
        "capability": str(payload.get("capability") or capability),
        "label": str(details.get("label") or "可选能力"),
        "network_download": bool(details.get("network_download")),
        "login_required": bool(details.get("login_required")),
        "may_use_paid_api": bool(details.get("may_use_paid_api")),
        "restart_required": bool(details.get("restart_required")),
        "confirmation_token": str(payload.get("confirmation_token") or ""),
        "boundary": str(details.get("boundary") or "仅在明确确认后才会下载安装。"),
    }


def optional_capability_apply(capability: str, confirmation: str) -> dict[str, Any]:
    if not confirmation or len(confirmation) > 128:
        raise ValueError("请先生成并确认当前能力安装计划。")
    payload = _run_product_installer(
        ["capability-apply", "--capability", capability, "--confirmation", confirmation], timeout=900
    )
    return {
        "status": str(payload.get("status") or "APPLIED"),
        "capability": str(payload.get("capability") or capability),
        "restart_required": bool(payload.get("restart_required")),
    }


def diagnostic_snapshot() -> dict[str, Any]:
    """A copyable, path-free diagnostic record safe for local viewing or export."""
    snapshot = public_snapshot()
    return {
        "schema": "knowledgeradar-local-diagnostic/v1",
        "installation": installation_summary(),
        "configured_field_count": len(_configured_keys(snapshot)),
        "supported_field_count": len(snapshot.get("fields", [])),
        "capability_packs": [{"id": row["id"], "status": row["status"]} for row in capability_packs(snapshot)],
        "optional_capabilities": [{"id": row["id"], "status": row["status"]} for row in optional_capabilities()],
        "privacy": "不含配置值、绝对路径、文件名、账号、Cookie、Profile、日志或任务内容。",
    }


def _read_dashboard_rows(database: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Read only an already-existing runtime database; never create product state."""
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(database, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(query, parameters).fetchall())
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return []


_TASK_LABELS = {
    "bilibili_transcribe": "视频转写",
    "bilibili_qwen_video_analysis": "视频理解",
    "bilibili_comment_filter": "评论筛选",
}
_TASK_STATUS_LABELS = {
    "queued": "排队中",
    "running": "处理中",
    "completed": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "skipped": "已跳过",
}


def _task_age_label(age_seconds: float) -> str:
    """Return a coarse elapsed label without leaking wall-clock activity history."""
    seconds = max(0, int(age_seconds or 0))
    if seconds < 60:
        return "刚刚更新"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前更新"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前更新"
    return f"{min(seconds // 86400, 99)} 天前更新"


def _public_task_row(row: sqlite3.Row, *, now: float) -> dict[str, Any]:
    """Expose task progress without targets, URLs, IDs, paths, or error text."""
    task_type = str(row["task_type"] or "")
    platform = str(row["platform"] or "")
    status = str(row["status"] or "")
    updated_at = float(row["updated_at"] or now)
    started_at = float(row["started_at"] or row["created_at"] or updated_at)
    finished_at = float(row["finished_at"] or updated_at)
    duration_s = max(0, int(finished_at - started_at)) if status in {"completed", "failed", "cancelled", "skipped"} else max(0, int(now - started_at))
    return {
        "label": _TASK_LABELS.get(task_type, "后台研究处理"),
        "platform": "B 站" if task_type.startswith("bilibili_") else (platform[:32] or "本机任务"),
        "status": status if status in _TASK_STATUS_LABELS else "unknown",
        "status_label": _TASK_STATUS_LABELS.get(status, "状态未知"),
        "updated_label": _task_age_label(now - updated_at),
        "duration_seconds": min(duration_s, 7 * 86400),
    }


def _dashboard_task_activity(since: float) -> dict[str, Any]:
    from runtime.tasks import default_task_db_path

    database = Path(default_task_db_path())
    completed = _read_dashboard_rows(
        database,
        "SELECT COUNT(*) AS count FROM runtime_tasks WHERE status='completed' AND COALESCE(finished_at, updated_at) >= ?",
        (since,),
    )
    pending = _read_dashboard_rows(
        database,
        "SELECT status, COUNT(*) AS count FROM runtime_tasks WHERE status IN ('queued', 'running') GROUP BY status",
    )
    rows = _read_dashboard_rows(
        database,
        """SELECT task_type, platform, status, created_at, started_at, updated_at, finished_at
           FROM runtime_tasks ORDER BY updated_at DESC LIMIT 8""",
    )
    now = time.time()
    tasks = [_public_task_row(row, now=now) for row in rows]
    return {
        "available": database.is_file(),
        "completed": int(completed[0]["count"] or 0) if completed else 0,
        "active": sum(int(row["count"] or 0) for row in pending),
        "active_tasks": [row for row in tasks if row["status"] in {"queued", "running"}],
        "recent_tasks": tasks,
    }


def _dashboard_trace_activity(since: float) -> dict[str, Any]:
    from runtime.tool_trace import default_tool_trace_path

    trace_path = Path(default_tool_trace_path())
    if not trace_path.is_file():
        return {"available": False, "successful": 0, "top_tools": []}
    successes = 0
    by_tool: dict[str, int] = {}
    try:
        with trace_path.open("r", encoding="utf-8") as stream:
            for raw in stream:
                try:
                    row = json.loads(raw)
                    timestamp = str(row.get("timestamp") or "")
                    event_time = __import__("datetime").datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if event_time < since or str(row.get("parent_trace_id") or "") or row.get("status") != "ok":
                    continue
                name = str(row.get("tool_name") or "unknown")[:80]
                successes += 1
                by_tool[name] = by_tool.get(name, 0) + 1
    except OSError:
        return {"available": False, "successful": 0, "top_tools": []}
    return {"available": True, "successful": successes, "top_tools": [{"label": name, "count": count} for name, count in sorted(by_tool.items(), key=lambda item: (-item[1], item[0]))[:3]]}


def _dashboard_usage_activity(since: float) -> dict[str, Any]:
    from runtime.usage_tracker import default_usage_db_path

    database = Path(default_usage_db_path())
    rows = _read_dashboard_rows(
        database,
        "SELECT capability, COUNT(*) AS count FROM usage_records WHERE created_at >= ? GROUP BY capability ORDER BY count DESC LIMIT 3",
        (since,),
    )
    return {"available": database.is_file(), "top_capabilities": [{"label": str(row["capability"] or "unknown")[:80], "count": int(row["count"] or 0)} for row in rows]}


def _control_plane_capabilities(
    packs: list[dict[str, Any]], components: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Map existing product readiness to honest, user-facing control-plane states.

    "已接入" means that the necessary local configuration exists.  It does not
    claim that a remote provider has been called or that a logged-in platform
    can be used without an interaction; those require a real task or explicit
    user action respectively.
    """
    rows: list[dict[str, str]] = []
    for pack in packs:
        status = str(pack["status"])
        if status == "ready":
            state, detail = "connected", "已接入"
        elif status == "needs_interaction":
            state, detail = "manual", "需要登录"
        elif status == "optional":
            state, detail = "optional", "按需启用"
        else:
            state, detail = "needs_setup", "需要配置"
        rows.append({"id": str(pack["id"]), "label": str(pack["label"]), "state": state, "detail": detail})
    for component in components:
        ready = component["status"] == "ready"
        rows.append(
            {
                "id": str(component["id"]),
                "label": str(component["label"]),
                "state": "local_ready" if ready else "not_installed",
                "detail": "本地组件就绪" if ready else "尚未安装",
            }
        )
    return rows


def dashboard_snapshot(*, window_days: int = 7) -> dict[str, Any]:
    """Return a bounded, redacted dashboard view from existing local records.

    This endpoint intentionally never reads task targets, URLs, account data,
    configuration values, filenames, Profile information, or error text.
    """
    window_days = max(1, min(int(window_days or 7), 30))
    since = time.time() - window_days * 86400
    configuration = public_snapshot()
    packs = capability_packs(configuration)
    components = optional_capabilities()
    installation = installation_summary()
    task_activity = _dashboard_task_activity(since)
    trace_activity = _dashboard_trace_activity(since)
    usage_activity = _dashboard_usage_activity(since)
    core_pack = next((pack for pack in packs if pack["id"] == "core_web"), None)
    control_plane = _control_plane_capabilities(packs, components)
    next_action = (
        {"view": "services", "label": "配置网页搜索", "reason": "先配置至少一个网页搜索来源，才能启用核心网页研究。"}
        if core_pack and core_pack["status"] != "ready"
        else {"view": "services", "label": "查看能力中心", "reason": "核心网页研究已具备最小配置；可按需要补充其他能力。"}
    )
    return {
        "schema": "knowledgeradar-dashboard-snapshot/v1",
        "window": {"days": window_days, "label": f"近 {window_days} 天", "recorded_locally_only": True},
        "installation": installation,
        "research_readiness": "ready" if core_pack and core_pack["status"] == "ready" else "needs_setup",
        "packs": [{"id": pack["id"], "label": pack["label"], "status": pack["status"]} for pack in packs],
        "control_plane": {
            "capabilities": control_plane,
            "connected_count": sum(item["state"] in {"connected", "local_ready"} for item in control_plane),
            "attention_count": sum(item["state"] not in {"connected", "local_ready"} for item in control_plane),
        },
        "next_action": next_action,
        "activity": {"tasks": task_activity, "tools": trace_activity, "usage": usage_activity},
        "pending": {"active_tasks": task_activity["active"], "restart_required": any(bool(item.get("restart_required")) and item.get("status") != "ready" for item in components)},
        "privacy": "仅汇总本机已有记录；不返回查询、网址、任务内容、账号、文件名、Profile、Cookie、配置值或密钥。",
    }


def console_configuration_snapshot() -> dict[str, Any]:
    """Provide one versioned public configuration/guide contract for the console."""
    snapshot = public_snapshot()
    return {
        "schema": "knowledgeradar-console-configuration/v2",
        "packs": capability_packs(snapshot),
        "providers": public_provider_guides(),
        "status_legend": {
            "ready": "本机已接入",
            "needs_setup": "需要配置",
            "optional": "按需启用",
            "needs_interaction": "需要人工登录",
        },
    }


def _tree_bytes(path: Path, *, excluded: tuple[Path, ...] = ()) -> int:
    if not path.exists():
        return 0
    try:
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file() and not any(excluded_root == item or excluded_root in item.parents for excluded_root in excluded)
        )
    except OSError:
        return 0


def _storage_ownership_manifest() -> dict[str, Any]:
    manifest = project_root() / "config" / "storage-ownership.manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("product storage ownership manifest is unavailable") from error
    if payload.get("schema") != "knowledgeradar-storage-ownership/v1" or not isinstance(payload.get("categories"), list):
        raise RuntimeError("product storage ownership manifest is invalid")
    return payload


def storage_summary() -> dict[str, Any]:
    """Return category totals only, never local paths, names, or file contents."""
    configured_root = os.environ.get("KR_DATA_ROOT", "").strip()
    if not configured_root:
        return {
            "schema": "knowledgeradar-local-storage-summary/v1",
            "available": False,
            "categories": [],
            "total_bytes": 0,
            "cleanup_scope": "源码兼容模式不会扫描工作区；安装产品后可在此查看产品数据根的空间摘要。",
        }
    data_root = Path(configured_root).expanduser()
    manifest = _storage_ownership_manifest()
    rows: list[dict[str, Any]] = []
    for category in manifest["categories"]:
        relative = str(category["relative_path"])
        path = media_cache_root() if category["id"] == "media_cache" else data_root / relative
        excluded = tuple(data_root / str(item) for item in category.get("exclude_relative_paths", []))
        rows.append({"id": category["id"], "label": category["label"], "policy": category["policy"], "bytes": _tree_bytes(path, excluded=excluded)})
    return {
        "schema": "knowledgeradar-local-storage-summary/v1",
        "available": True,
        "categories": rows,
        "total_bytes": sum(row["bytes"] for row in rows),
        "cleanup_scope": "只对已登记且过期的媒体缓存生成清理计划；执行时先移入可恢复隔离区。密钥、Profile、浏览器资料、模型、备份和未知文件始终受保护。",
    }


def expired_media_cleanup(*, apply: bool) -> dict[str, Any]:
    """Plan or quarantine expired media cache without deleting user data.

    ``runtime.media_cache`` remains a development helper with direct deletion
    semantics.  Product users instead receive a reversible lifecycle: only
    manifest-known expired files are eligible and application moves them below
    the active data root's quarantine directory.
    """
    if not os.environ.get("KR_DATA_ROOT", "").strip():
        return {
            "status": "SKIPPED",
            "expired_file_count": 0,
            "kept_file_count": 0,
            "error_count": 0,
            "scope": "源码兼容模式不清理工作区；请在已安装产品中使用此操作。",
        }
    data_root = Path(os.environ["KR_DATA_ROOT"]).resolve()
    cache_root = media_cache_root().resolve()
    if cache_root != data_root and data_root not in cache_root.parents:
        return {"status": "SKIPPED", "expired_file_count": 0, "kept_file_count": 0, "error_count": 0, "scope": "媒体缓存不在当前产品数据根，拒绝自动处理。"}
    result = cleanup_expired_media_cache(root=cache_root, dry_run=True)
    manifest = cache_root / "manifest.jsonl"
    known = set()
    if manifest.is_file():
        from runtime.media_cache import iter_manifest_records

        for row in iter_manifest_records(cache_root) or ():
            raw = str(row.get("path") or "")
            if raw:
                known.add(Path(raw).resolve())
    eligible = [Path(raw).resolve() for raw in result["deleted"] if Path(raw).resolve() in known]
    errors: list[str] = []
    if apply and eligible:
        quarantine = data_root / "quarantine" / "media-cache" / str(int(time.time()))
        for path in eligible:
            try:
                relative = path.relative_to(cache_root)
                target = quarantine / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
            except (OSError, ValueError) as error:
                errors.append(str(error))
        receipt = data_root / "receipts" / f"media-cache-quarantine-{int(time.time())}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            '{"schema":"knowledgeradar-media-cache-quarantine/v1","status":"APPLIED","restore_window":"manual until user purges quarantine","file_count":' + str(len(eligible) - len(errors)) + "}\n",
            encoding="utf-8",
        )
    return {
        "status": "QUARANTINED" if apply else "PLAN",
        "expired_file_count": len(eligible),
        "kept_file_count": len(result["kept"]),
        "error_count": len(result["errors"]) + len(errors),
        "scope": "只处理 manifest 已登记的过期媒体缓存，并先移入可恢复隔离区；不包含密钥、Profile、浏览器资料、日志、模型、备份或未知文件。",
    }
