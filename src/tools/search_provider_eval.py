"""Small search-provider evaluation harness for KnowledgeRadar."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)
sys.path.insert(0, SRC_ROOT)


def main() -> int:
    from runtime.env_loader import load_runtime_env
    from search_providers import WebSearchRequest, provider_status, search_web

    load_runtime_env()
    parser = argparse.ArgumentParser(description="Evaluate configured KnowledgeRadar search providers.")
    parser.add_argument("queries", nargs="*", default=["Model Context Protocol documentation"])
    parser.add_argument("--providers", default=os.environ.get("KR_WEB_SEARCH_PROVIDERS", "tavily,brave,exa,anysearch,searxng"))
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    providers = [item.strip() for item in args.providers.split(",") if item.strip()]
    report = {
        "provider_status": provider_status(),
        "queries": [],
    }
    for query in args.queries:
        query_report = {"query": query, "providers": []}
        for provider in providers:
            started = time.time()
            result = search_web(WebSearchRequest(query=query, limit=args.limit, provider=provider)).to_mcp_dict()
            query_report["providers"].append({
                "provider": provider,
                "elapsed_s": round(time.time() - started, 3),
                "total": result.get("total", 0),
                "error": result.get("error"),
                "first_title": (result.get("items") or [{}])[0].get("title", ""),
                "first_url": (result.get("items") or [{}])[0].get("url", ""),
            })
        report["queries"].append(query_report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
