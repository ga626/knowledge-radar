"""MCP facade grouping without changing public tool registrations."""

from __future__ import annotations

from typing import Any, Dict


TOOL_GROUPS: Dict[str, list[str]] = {
    "runtime": ["health_check", "get_capabilities", "get_task_status", "analyze_decision_logs"],
    "open_web": ["kr_web_search", "extract_web_page", "extract_dynamic_page"],
    "platform_search": [
        "search_github_repositories",
        "search_youtube",
        "search_wechat_articles",
        "search_academic",
        "search_bilibili",
        "search_xiaohongshu",
        "search_zhihu",
        "search_recruitment",
    ],
    "detail": ["get_content_detail"],
}


def public_tool_groups() -> Dict[str, list[str]]:
    return {name: list(tools) for name, tools in TOOL_GROUPS.items()}


def facade_manifest(actual_tools: list[str] | tuple[str, ...] | None = None) -> Dict[str, Any]:
    actual = list(actual_tools or [])
    grouped = {tool for tools in TOOL_GROUPS.values() for tool in tools}
    return {
        "schema": "knowledgeradar-mcp-facade/v1",
        "status": "compatibility_facade",
        "source_of_truth": "src/server.py @mcp.tool() registrations",
        "groups": public_tool_groups(),
        "actual_tool_count": len(actual),
        "ungrouped_tools": [tool for tool in actual if tool not in grouped],
        "missing_from_actual": [tool for tool in sorted(grouped) if actual and tool not in actual],
        "migration_rule": "Move implementation behind grouped modules while keeping src/server.py as the registration facade.",
    }
