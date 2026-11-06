"""MCP facade grouping without changing public tool registrations."""

from __future__ import annotations

from typing import Any, Dict


def public_tool_groups() -> Dict[str, list[str]]:
    from runtime.agent_contracts import groups
    return groups()


def facade_manifest(actual_tools: list[str] | tuple[str, ...] | None = None) -> Dict[str, Any]:
    actual = list(actual_tools or [])
    grouped = {tool for tools in public_tool_groups().values() for tool in tools}
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
