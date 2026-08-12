"""Sanitized product-facing status for the local setup and health page."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from onboarding.configuration import public_snapshot, runtime_env_path
from runtime.media_cache import cleanup_expired_media_cache, media_cache_root


CAPABILITY_PACKS = (
    {
        "id": "core_web",
        "label": "核心网页研究",
        "description": "至少配置一个网页搜索来源后可用于一般网页发现与提取。",
        "fields": ("TAVILY_API_KEY", "ANYSEARCH_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY", "SEARXNG_BASE_URL"),
        "needs": "至少一个网页搜索来源",
        "boundary": "第三方额度、网络与来源可访问性仍会影响结果。",
    },
    {
        "id": "academic",
        "label": "学术资料",
        "description": "接入学术元数据与开放获取线索。",
        "fields": ("OPENALEX_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"),
        "needs": "可选 API Key；未配置时可用来源会减少",
        "boundary": "付费墙、机构授权与检索额度不会被绕过。",
    },
    {
        "id": "video_media",
        "label": "视频与多模态理解",
        "description": "为视频、图片或文本理解配置可选模型服务。",
        "fields": ("YOUTUBE_API_KEY", "DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY", "LLM_API_KEY"),
        "needs": "按使用的平台或模型填写",
        "boundary": "模型调用可能计费；媒体缓存只保留在本机。",
    },
    {
        "id": "login_platforms",
        "label": "登录平台与招聘",
        "description": "小红书、知乎、招聘等能力复用本地浏览器 Profile。",
        "fields": (),
        "needs": "首次实际使用时按页面提示完成自己的登录",
        "boundary": "验证码、风控和平台限制会进入人工操作或降级状态。",
    },
)


def _configured_keys(snapshot: dict[str, Any]) -> set[str]:
    return {str(field.get("key")) for field in snapshot.get("fields", []) if field.get("configured")}


def capability_packs(snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Describe readiness only; it never exposes values or changes enabled features."""
    snapshot = snapshot or public_snapshot()
    configured = _configured_keys(snapshot)
    rows: list[dict[str, Any]] = []
    for pack in CAPABILITY_PACKS:
        fields = tuple(pack["fields"])
        ready = bool(configured.intersection(fields)) if fields else False
        rows.append(
            {
                "id": pack["id"],
                "label": pack["label"],
                "description": pack["description"],
                "needs": pack["needs"],
                "boundary": pack["boundary"],
                "status": "ready" if ready else "needs_setup",
                "configured_field_count": len(configured.intersection(fields)),
                "field_count": len(fields),
            }
        )
    return rows


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


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
    categories = {
        "浏览器资料": data_root / "browser_data",
        "运行状态": data_root / "state",
        "日志": data_root / "logs",
        "通用缓存": data_root / "cache",
        "媒体缓存": media_cache_root(),
        "模型": data_root / "models",
        "Playwright": data_root / "playwright",
    }
    rows = [{"label": label, "bytes": _tree_bytes(path)} for label, path in categories.items()]
    return {
        "schema": "knowledgeradar-local-storage-summary/v1",
        "available": True,
        "categories": rows,
        "total_bytes": sum(row["bytes"] for row in rows),
        "cleanup_scope": "仅可清理已过期的媒体缓存；不会删除密钥、Profile、浏览器资料、日志或模型。",
    }


def expired_media_cleanup(*, apply: bool) -> dict[str, Any]:
    """Clean only expired media-cache files and return counts rather than paths."""
    if not os.environ.get("KR_DATA_ROOT", "").strip():
        return {
            "status": "SKIPPED",
            "expired_file_count": 0,
            "kept_file_count": 0,
            "error_count": 0,
            "scope": "源码兼容模式不清理工作区；请在已安装产品中使用此操作。",
        }
    result = cleanup_expired_media_cache(dry_run=not apply)
    return {
        "status": "APPLIED" if apply else "PLAN",
        "expired_file_count": len(result["deleted"]),
        "kept_file_count": len(result["kept"]),
        "error_count": len(result["errors"]),
        "scope": "仅媒体缓存中的过期文件；不包含密钥、Profile、浏览器资料、日志或模型。",
    }
