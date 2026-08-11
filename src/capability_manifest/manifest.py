"""Lightweight manifest summary for capability declarations."""

from __future__ import annotations

from typing import Any, Dict


def manifest_summary(
    *,
    tool_surface: Dict[str, Any],
    source_ecologies: Dict[str, Any],
    validation_semantics: Dict[str, Any],
    research_quality: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-capability-manifest/v1",
        "status": "compatibility_manifest",
        "sections": {
            "tool_surface": {
                "schema": tool_surface.get("schema", ""),
                "tool_count": tool_surface.get("actual_mcp_tool_count", 0),
            },
            "source_ecologies": {
                "schema": source_ecologies.get("schema", ""),
                "ecology_count": len(source_ecologies.get("ecologies") or {}),
            },
            "validation_semantics": {
                "schema": validation_semantics.get("schema", ""),
                "status_classes": list((validation_semantics.get("status_classes") or {}).keys()),
            },
            "research_quality": {
                "schema": research_quality.get("schema", ""),
                "status": research_quality.get("status", ""),
            },
        },
        "migration_rule": "Keep capabilities.py as the compatibility facade while declarations move into smaller manifest modules.",
    }
