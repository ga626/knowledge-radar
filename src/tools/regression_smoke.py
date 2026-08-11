"""Lightweight MCP smoke runner for fixed regression samples."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, Iterable, List, Set

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


DEFAULT_SAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "regression_samples",
    "platform_smoke_samples.jsonl",
)
DEFAULT_MCP_URL = "http://127.0.0.1:18765/mcp"


def load_samples(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


async def run_smoke(
    samples: Iterable[Dict[str, Any]],
    *,
    mcp_url: str = DEFAULT_MCP_URL,
    timeout_s: float = 60.0,
    sample_timeout_s: float = 30.0,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    ordered_samples = sorted(list(samples), key=lambda item: bool(item.get("expected_degraded")))
    for sample in ordered_samples:
        results.append(await _run_one_sample(sample, mcp_url=mcp_url, timeout_s=timeout_s, sample_timeout_s=sample_timeout_s))
    return results


async def _run_one_sample(
    sample: Dict[str, Any],
    *,
    mcp_url: str,
    timeout_s: float,
    sample_timeout_s: float,
) -> Dict[str, Any]:
    platform = str(sample.get("platform") or "")
    query = str(sample.get("query") or "")
    limit = int(sample.get("limit") or 1)
    if sample.get("mcp_search_exposed") is False:
        return {**sample, "status": "skipped", "reason": "MCP search tool is not exposed yet"}
    tool_name = _tool_for_platform(platform)
    if not tool_name:
        return {**sample, "status": "skipped", "reason": f"unsupported platform for smoke runner: {platform}"}
    expected_degraded = bool(sample.get("expected_degraded"))
    per_sample_timeout = float(sample.get("timeout_s") or sample_timeout_s)
    try:
        response = await asyncio.wait_for(
            _call_tool_in_new_session(
                mcp_url,
                tool_name,
                _arguments_for_tool(tool_name, query, limit),
                timeout_s=min(float(timeout_s), per_sample_timeout + 5.0),
            ),
            timeout=per_sample_timeout + 10.0,
        )
        item_count = _count_items(response)
        expected = int(sample.get("expected_min_results") or 0)
        error = _extract_error(response)
        status = "ok" if item_count >= expected and not error else "degraded"
        if expected_degraded and status == "degraded":
            status = "expected_degraded"
        return {
            **sample,
            "status": status,
            "tool": tool_name,
            "item_count": item_count,
            "expected_min_results": expected,
            "expected_degraded": expected_degraded,
            "first_url": _first_url(response),
            "error": error,
        }
    except asyncio.TimeoutError:
        status = "expected_degraded" if expected_degraded else "failed"
        return {
            **sample,
            "status": status,
            "tool": tool_name,
            "expected_degraded": expected_degraded,
            "error": f"sample timed out after {per_sample_timeout}s",
        }
    except BaseException as exc:
        status = "expected_degraded" if expected_degraded else "failed"
        return {**sample, "status": status, "tool": tool_name, "expected_degraded": expected_degraded, "error": str(exc)}


async def _call_tool_in_new_session(
    mcp_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    timeout_s: float,
) -> Dict[str, Any]:
    async with streamablehttp_client(mcp_url, timeout=timeout_s, sse_read_timeout=timeout_s) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _call_mcp_tool(session, tool_name, arguments)


def _tool_for_platform(platform: str) -> str:
    return {
        "B站": "search_bilibili",
        "知乎": "search_zhihu",
        "小红书": "search_xiaohongshu",
        "YouTube": "search_youtube",
        "web": "kr_web_search",
    }.get(platform, "")


def _arguments_for_tool(tool_name: str, query: str, limit: int) -> Dict[str, Any]:
    if tool_name == "kr_web_search":
        return {"query": query, "limit": limit}
    if tool_name == "search_xiaohongshu":
        return {"keyword": query, "limit": limit, "search_type": "all"}
    return {"keyword": query, "limit": limit}


async def _call_mcp_tool(session: ClientSession, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    result = await session.call_tool(tool_name, arguments)
    return _extract_mcp_content({"content": [item.model_dump() for item in result.content]})


def _extract_mcp_content(result: Dict[str, Any]) -> Dict[str, Any]:
    content = result.get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return result if isinstance(result, dict) else {}


def _count_items(response: Dict[str, Any]) -> int:
    items = response.get("items")
    if isinstance(items, list):
        return len(items)
    results = response.get("results")
    if isinstance(results, list):
        return len(results)
    return 0


def _first_url(response: Dict[str, Any]) -> str:
    for key in ("items", "results"):
        rows = response.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return str(rows[0].get("url") or "")
    return ""


def _extract_error(response: Dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("error") or error)
    return str(error or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default=DEFAULT_SAMPLES)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--sample-timeout", type=float, default=30.0)
    parser.add_argument("--platform", action="append", default=[], help="Only run samples for this platform. Can be repeated.")
    parser.add_argument("--include", action="append", default=[], help="Only run sample ids listed here. Can be repeated or comma-separated.")
    parser.add_argument("--exclude", action="append", default=[], help="Skip sample ids listed here. Can be repeated or comma-separated.")
    args = parser.parse_args()
    samples = _filter_samples(
        load_samples(args.samples),
        platforms=_split_args(args.platform),
        include_ids=_split_args(args.include),
        exclude_ids=_split_args(args.exclude),
    )
    results = asyncio.run(
        run_smoke(
            samples,
            mcp_url=args.mcp_url,
            timeout_s=args.timeout,
            sample_timeout_s=args.sample_timeout,
        )
    )
    print(json.dumps({"status": _overall_status(results), "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(row.get("status") in {"ok", "skipped", "expected_degraded"} for row in results) else 1


def _split_args(values: List[str]) -> Set[str]:
    items: Set[str] = set()
    for value in values or []:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                items.add(item)
    return items


def _filter_samples(
    samples: List[Dict[str, Any]],
    *,
    platforms: Set[str],
    include_ids: Set[str],
    exclude_ids: Set[str],
) -> List[Dict[str, Any]]:
    selected = []
    for sample in samples:
        sample_id = str(sample.get("id") or "")
        platform = str(sample.get("platform") or "")
        if platforms and platform not in platforms:
            continue
        if include_ids and sample_id not in include_ids:
            continue
        if exclude_ids and sample_id in exclude_ids:
            continue
        selected.append(sample)
    return selected


def _overall_status(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "skipped"
    if all(row.get("status") == "ok" for row in results):
        return "ok"
    if any(row.get("status") == "failed" for row in results):
        return "failed"
    if any(row.get("status") == "ok" for row in results):
        return "degraded"
    if all(row.get("status") in {"skipped", "expected_degraded"} for row in results):
        return "degraded"
    return "failed"


if __name__ == "__main__":
    raise SystemExit(main())
