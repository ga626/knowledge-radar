"""Small schema helpers for strategy-tree manifests.

The registry is intentionally permissive: it standardizes the envelope,
governance fields, and node affordances without forcing platform-specific
logic into one executor.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Iterable


REQUIRED_TREE_FIELDS = {
    "tree_id",
    "version",
    "tool_name",
    "platform",
    "operation",
    "mode",
    "owner_code_paths",
    "tests",
    "nodes",
}

REQUIRED_NODE_FIELDS = {"node_id", "kind", "module", "decision_role"}


def _module_target_error(target: str) -> str:
    if not target or "." not in target:
        return "module target must be a dotted module attribute"
    module_name, _, attr_name = target.rpartition(".")
    try:
        module = import_module(module_name)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    if not hasattr(module, attr_name):
        return f"missing attribute: {attr_name}"
    return ""


def compact_tree(tree: dict[str, Any]) -> dict[str, Any]:
    nodes = tree.get("nodes") if isinstance(tree.get("nodes"), list) else []
    return {
        "tree_id": tree.get("tree_id", ""),
        "version": tree.get("version", ""),
        "tool_name": tree.get("tool_name", ""),
        "platform": tree.get("platform", ""),
        "operation": tree.get("operation", ""),
        "mode": tree.get("mode", ""),
        "node_count": len(nodes),
        "root_node": nodes[0].get("node_id", "") if nodes and isinstance(nodes[0], dict) else "",
        "owner_code_paths": list(tree.get("owner_code_paths") or []),
        "tests": list(tree.get("tests") or []),
    }


def validation_report(
    bundle: dict[str, Any],
    *,
    actual_tools: Iterable[str] | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    known_tools = set(actual_tools or [])
    root = Path(repo_root) if repo_root else None
    trees = bundle.get("trees") if isinstance(bundle.get("trees"), list) else []

    seen_ids: set[str] = set()
    for index, tree in enumerate(trees):
        missing = sorted(REQUIRED_TREE_FIELDS.difference(tree))
        tree_id = str(tree.get("tree_id") or f"index:{index}")
        if tree_id in seen_ids:
            errors.append({"tree_id": tree_id, "type": "duplicate_tree_id"})
        seen_ids.add(tree_id)
        if missing:
            errors.append({"tree_id": tree_id, "type": "missing_tree_fields", "fields": missing})
        tool_name = str(tree.get("tool_name") or "")
        if known_tools and tool_name and tool_name not in known_tools and not tool_name.startswith("virtual."):
            warnings.append({"tree_id": tree_id, "type": "tool_not_in_actual_surface", "tool_name": tool_name})
        if str(tree.get("mode") or "") == "enforce" and not tree.get("enforcement_ack"):
            errors.append({"tree_id": tree_id, "type": "enforcement_without_ack"})
        nodes = tree.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            errors.append({"tree_id": tree_id, "type": "empty_nodes"})
        else:
            for node in nodes:
                if not isinstance(node, dict):
                    errors.append({"tree_id": tree_id, "type": "invalid_node"})
                    continue
                node_missing = sorted(REQUIRED_NODE_FIELDS.difference(node))
                if node_missing:
                    errors.append(
                        {
                            "tree_id": tree_id,
                            "node_id": node.get("node_id", ""),
                            "type": "missing_node_fields",
                            "fields": node_missing,
                        }
                    )
                    continue
                target = str(node.get("module") or "")
                target_error = _module_target_error(target)
                if target_error:
                    errors.append(
                        {
                            "tree_id": tree_id,
                            "node_id": node.get("node_id", ""),
                            "type": "module_target_unresolved",
                            "module": target,
                            "error": target_error,
                        }
                    )
        if root:
            for rel_path in list(tree.get("owner_code_paths") or []) + list(tree.get("tests") or []):
                if not (root / str(rel_path)).exists():
                    errors.append({"tree_id": tree_id, "type": "declared_path_missing", "path": str(rel_path)})

    return {
        "schema": "knowledgeradar-strategy-tree-validation/v1",
        "status": "PASS" if not errors else "FAIL",
        "tree_count": len(trees),
        "errors": errors,
        "warnings": warnings,
    }
