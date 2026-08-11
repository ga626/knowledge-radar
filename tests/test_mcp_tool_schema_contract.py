from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_mcp_tool_schema import validate_tool_schemas


def test_public_mcp_tool_schemas_are_model_facing_contracts() -> None:
    result = validate_tool_schemas()

    assert result["status"] == "PASS", result["issues"]
    assert {"health_check", "get_capabilities", "search_xiaohongshu", "search_zhihu"} <= set(result["tool_names"])
