"""微信候选源码真实验收；显式live、隔离存证，复用已有浏览器绑定，不安装或绕验证。"""

from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["search", "detail", "opencli"], required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--query", default="微信开放文档")
    parser.add_argument("--source", choices=["auto", "web", "weread"], default="web")
    parser.add_argument("--runtime-env", type=Path)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--image-index", type=int, default=0)
    parser.add_argument("--attachments", action="store_true")
    parser.add_argument("--transcript", action="store_true")
    parser.add_argument("--comments", action="store_true", help="真实普通读者留言窗口与固定版本回读")
    parser.add_argument("--media-limit", type=int, default=30)
    parser.add_argument("--component-receipts", type=Path, help="显式复用已获准组件回执；不安装或修改组件")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output_exists_preserve_receipt")
    # 先只读既有固定依赖/无密钥绑定，再把业务状态隔离；不复制Profile或认证。
    from runtime.opencli_bridge import connection_config

    connection = connection_config()
    if args.component_receipts:
        os.environ["KR_OPTIONAL_COMPONENT_RECEIPTS_DIR"] = str(args.component_receipts.resolve())
        if args.transcript:
            from runtime.optional_media import validate_receipt
            from runtime.model_roles import ASR_COMPONENT
            validate_receipt(ASR_COMPONENT)
    args.output.mkdir(parents=True)
    for key, part in {
        "KR_STATE_DIR": "state",
        "KR_LOG_DIR": "logs",
        "KR_EVIDENCE_ROOT": "assets",
        "KR_MEDIA_CACHE_DIR": "media",
        "KR_RUNTIME_LEASE_DB_PATH": "leases.sqlite3",
        "KR_OWNER_ROOT": "owner",
        "KR_PROFILE_REGISTRY_PATH": "absent-profiles.json",
    }.items():
        os.environ[key] = str(args.output / part)
    os.environ.update(
        KR_OPENCLI_ROOT=str(connection["root"]),
        KR_OPENCLI_CONTEXT_ID=str(connection["context_id"]),
        KR_OPENCLI_STATE_DIR=str(connection["state_root"]),
        KR_DISABLE_MODEL_PREWARM="1",
        KR_DESKTOP_NOTIFICATIONS_DISABLED="1",
    )
    if args.runtime_env:
        os.environ["KR_RUNTIME_ENV_PATH"] = str(args.runtime_env)
    from runtime import composition  # noqa: F401
    from runtime.execution_owner import source_identity, OwnerClient
    # 候选同样通过受支持的owner执行；短命脚本不作日常Chrome的父进程。
    owner=OwnerClient(timeout=90)
    owner.start()
    invoke=lambda tool,arguments:owner.call(tool,arguments)
    from kr_core.evidence_assets import EvidenceAssets
    from generic_web.url_policy import public_evidence_payload

    started = time.monotonic()
    if args.mode == "search":
        arguments = {"query": args.query, "source": args.source, "limit": 3, "timeout_s": 40}
        tool = "search_wechat_articles"
    else:
        if not args.url:
            parser.error("detail requires --url")
        arguments = {
            "url": args.url,
            "include_ocr": args.ocr,
            "include_transcript": args.transcript,
            "include_comments": args.comments,
            **({"comments":{"root_limit":3,"reply_limit":2,"total_limit":12,"max_pages":3}} if args.comments else {}),
            "wechat": {
                "transport": "opencli" if args.mode == "opencli" else "http",
                "image_indexes": [args.image_index] if args.ocr else None,
                "include_attachments": args.attachments,
                "media_limit": args.media_limit,
            },
        }
        tool = "get_content_detail"
    result = invoke(tool, arguments)
    if result.get("accepted") and result.get("task_id"):
        end = time.monotonic() + 300
        while time.monotonic() < end:
            status = owner.call('get_task_status',{'task_id':result['task_id']})
            if status.get("status") not in {"queued", "running"}:
                result = {**status.get("result", {}), "task_id": result["task_id"], "task_status": status.get("status")}
                break
            time.sleep(0.5)
    result = public_evidence_payload(result)
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    rereads = []
    for ref in result.get("evidence_assets", []):
        part = EvidenceAssets(read_only=True).fetch(ref["version_id"], max_chars=24000)
        text = part.get("text", "")
        while part.get("next_start") is not None:
            part = EvidenceAssets(read_only=True).fetch(ref["version_id"], start=part["next_start"], max_chars=24000)
            text += part.get("text", "")
        rereads.append(
            {
                "kind": ref.get("kind"),
                "version_id": ref["version_id"],
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "raw_sha_matches": hashlib.sha256(text.encode()).hexdigest() == ref.get("raw_sha256"),
                "display_prefix_matches": text.startswith(result.get("content", ""))
                if ref.get("kind") == "content"
                else None,
                "display_is_shortened": bool(result.get("disclosure", {}).get("partial_view")),
            }
        )
    receipt = {
        "access_path": "candidate_application_not_native_mcp",
        "source_identity": source_identity(),
        "tool": tool,
        "arguments": public_evidence_payload(arguments),
        "elapsed_s": round(time.monotonic() - started, 3),
        "status": result.get("status"),
        "task_status": result.get("task_status"),
        "error": result.get("error"),
        "failure_type": result.get("failure_type"),
        "candidates": len(result.get("items", [])),
        "content_chars": len(result.get("content", "")),
        "body_verified": result.get("body_verified"),
        "readback": rereads,
        "transport_metadata": result.get("transport_metadata"),
        "ocr": result.get("ocr"),
        "transcript_result": result.get("transcript_result"),
        "attachment_result": result.get("attachment_result"),
        "media_gaps": result.get("media_gaps"),
        "comments_count":len(result.get('comments') or []),
        "comments_metadata":result.get('comments_metadata'),
        "content_coverage":result.get('content_coverage'),
        "result_sha256": hashlib.sha256((args.output / "result.json").read_bytes()).hexdigest(),
        "installation_changed": False,
    }
    (args.output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt['candidate_owner_stop']=owner.stop()
    (args.output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    # 不能用正文成功掩盖OCR/附件失败，也不能以退出0代替业务回执。
    return (
        0
        if result.get("status") == "ok"
        and (
            result.get("items")
            or (result.get("body_verified") and rereads and all(r["raw_sha_matches"] for r in rereads))
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
