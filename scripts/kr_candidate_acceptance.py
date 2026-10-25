"""冻结制品的隔离SDK验收；不写Codex配置、不登录、不自动收费。"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def unwrap(result):
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured.get("result", structured)
    for block in result.content:
        if getattr(block, "type", "") == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                pass
    return {}


async def probe(program, python, state, *, live):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env.update(PYTHONPATH=str(program / "src"), KR_PROJECT_ROOT=str(program), KR_SOURCE_ROOT=str(program / "src"),
               KR_DATA_ROOT=str(state / "data"), KR_STATE_DIR=str(state / "data/state"), KR_LOG_DIR=str(state / "logs"),
               KR_BROWSER_DATA_DIR=str(state / "browser_data"), KR_PROFILE_REGISTRY_PATH=str(state / "data/config/profile_registry.json"),
               KR_RUNTIME_ENV_PATH=str(state / "data/config/runtime.env"), KR_MCP_TRANSPORT="stdio", KR_DESKTOP_NOTIFICATIONS_DISABLED="1")
    rows = []
    bootstrap="import sys,runpy;sys.path.insert(0,"+repr(str(program/'src'))+");runpy.run_path("+repr(str(program/'src/server.py'))+",run_name='__main__')"
    parameters = StdioServerParameters(command=str(python), args=["-X", "utf8", '-c', bootstrap], cwd=str(program), env=env)
    started = time.monotonic()
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            catalog = await session.list_tools()
            rows.append({"case": "sdk_catalog", "success": len(catalog.tools) == 23, "tools": [t.name for t in catalog.tools], "startup_s": round(time.monotonic()-started, 3)})
            for name, args in [("health_check", {"mode": "summary"}), ("get_capabilities", {"summary": True})]:
                start = time.monotonic()
                result = await session.call_tool(name, args)
                rows.append({"case": name, "success": not result.isError, "elapsed_s": round(time.monotonic()-start, 3), "result": unwrap(result)})
            if live:
                for url in ["https://pip.pypa.io/en/stable/topics/repeatable-installs/", "https://raw.githubusercontent.com/NanmiCoder/MediaCrawler/main/LICENSE"]:
                    result = await session.call_tool("extract_web_page", {"url": url, "use_jina": False, "timeout": 20})
                    data = unwrap(result)
                    text = str(data.get("content") or "")
                    rows.append({"case": "public_extract", "url": url, "success": not result.isError and not data.get("error") and len(text) > 80,
                                 "content_chars": len(text), "content_sha256": hashlib.sha256(text.encode()).hexdigest(), "error": data.get("error"), "collector": data.get("collector")})
                start = time.monotonic()
                detail_args = {"url": "https://www.bilibili.com/video/BV1HhaA6hEsn", "include_ocr": True, "include_transcript": False, "include_comments": False, "deadline_s": 60}
                pending = await session.call_tool("get_content_detail", detail_args)
                data = unwrap(pending)
                rows.append({"case": "durable_accept", "success": bool(data.get("task_id")) or bool(data.get("content")), "elapsed_s": round(time.monotonic()-start, 3), "result": data})
                start = time.monotonic()
                health = await session.call_tool("health_check", {"mode": "summary"})
                rows.append({"case": "health_during_task", "success": not health.isError, "elapsed_s": round(time.monotonic()-start, 3)})
                if data.get("task_id"):
                    task_id = data["task_id"]
                    for _ in range(65):
                        status = unwrap(await session.call_tool("get_task_status", {"task_id": task_id}))
                        if status.get('status') in {"completed", "failed", "cancelled", "interrupted", "waiting_for_user"}:
                            break
                        await asyncio.sleep(1)
                    detail=status.get('result') or {}
                    rows.append({"case": "durable_final", "success": status.get('status')=='completed' and bool(detail.get('title')) and detail.get('url')==detail_args['url'] and not detail.get('error'), "result": status})
                    repeat=unwrap(await session.call_tool('get_content_detail', detail_args))
                    reread=unwrap(await session.call_tool('get_task_status', {'task_id':task_id}))
                    rows.append({'case':'completed_request_idempotency','success':repeat.get('task_id')==task_id and reread.get('status')=='completed' and reread.get('result')==detail,'task_id':repeat.get('task_id')})
    return {"schema": "knowledgeradar-candidate-sdk-acceptance/v1", "access_path": "isolated_sdk_stdio", "native_host_acceptance": False,
            "program": str(program), "python": str(python), "live_public_only": live, "checks": rows,
            "status": "PASS" if all(row["success"] for row in rows) else "FAIL"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--program-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--live-public", action="store_true")
    args = parser.parse_args()
    args.state_root.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(probe(args.program_root.resolve(), args.python.resolve(), args.state_root.resolve(), live=args.live_public))
    except Exception as error:
        result={'schema':'knowledgeradar-candidate-sdk-acceptance/v1','status':'FAIL','native_host_acceptance':False,
                'checks':[{'case':'sdk_connection','success':False,'reason':type(error).__name__}], 'error':str(error)[:2000]}
    provenance=args.program_root/'package-provenance.json'
    if provenance.is_file():
        result['package_provenance_sha256']=hashlib.sha256(provenance.read_bytes()).hexdigest()
        result['source_commit']=json.loads(provenance.read_text(encoding='utf-8')).get('source_commit')
    (args.state_root / "sdk-acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": [{k:v for k,v in r.items() if k not in {"result"}} for r in result["checks"]]}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
