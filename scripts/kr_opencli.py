"""Prepare reviewed OpenCLI locally; bind a user-confirmed daily Chrome.

Never installs an extension, launches a browser or transfers credentials.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from runtime import xhs_opencli as oc
from runtime.leases import get_runtime_lease_coordinator, default_owner


def validate(root):
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != "@jackwener/opencli" or package.get("version") != "2.0.2":
        raise ValueError("reviewed_opencli_2.0.2_required")
    for name in ("dist/src/daemon.js", "dist/src/browser/bridge.js", "node_modules", "LICENSE"):
        if not (root/name).exists():
            raise ValueError("built_dependency_missing:" + name)
    if not re.search(r"retryOnBlock\s*=\s*false", (root/"clis/xiaohongshu/risk-control.js").read_text(encoding="utf-8")):
        raise ValueError("reviewed_fail_fast_default_required")
    digest = hashlib.sha256()
    for name in ("search", "note", "comments", "download", "risk-control", "note-helpers"):
        rel = "clis/xiaohongshu/" + name + ".js"
        digest.update(rel.encode())
        digest.update((root / rel).read_bytes())
    fingerprint = digest.hexdigest()
    if fingerprint != oc.REVIEWED_ADAPTER_SHA256:
        raise ValueError("unreviewed_adapter_identity")
    return fingerprint


def prepare(source):
    source = Path(source).resolve()
    fingerprint = validate(source)
    destination = oc.state_dir() / "dependency" / ("2.0.2-" + fingerprint[:16])
    coordinator = get_runtime_lease_coordinator()
    grant = coordinator.acquire_exclusive("dependency_prepare", str(destination.resolve()),
        owner=default_owner("prepare_opencli", request_id=uuid.uuid4().hex), ttl_s=300)
    if not grant.acquired:
        return {"status":"busy", "request_executed":False}
    try:
        if not destination.exists():
            temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".pending")
            temporary.mkdir(parents=True)
            for name in ("package.json", "package-lock.json", "LICENSE", "dist", "node_modules"):
                item = source/name
                if item.is_dir():
                    shutil.copytree(item, temporary/name, ignore=shutil.ignore_patterns(".cache"))
                elif item.is_file():
                    shutil.copy2(item, temporary/name)
            shutil.copytree(source/"clis/xiaohongshu", temporary/"clis/xiaohongshu")
            if validate(temporary) != fingerprint:
                raise ValueError("dependency_copy_identity_mismatch")
            temporary.replace(destination)
        if validate(destination) != fingerprint:
            raise ValueError("existing_dependency_identity_mismatch")
        oc._save(oc.state_dir()/"dependency.json", {"schema":"kr-opencli-dependency/v1",
            "root":str(destination.resolve()), "adapter_sha256":fingerprint,
            "upstream_sha":oc.UPSTREAM_SHA, "license":"Apache-2.0"})
        return {"status":"prepared", "version":"2.0.2", "adapter_sha256":fingerprint,
            "root":str(destination.resolve()), "extension_installed":False}
    finally:
        coordinator.release(grant.lease_id)


def start_daemon():
    root = Path(oc.setting("KR_XHS_OPENCLI_ROOT"))
    if validate(root) != oc.setting("KR_XHS_OPENCLI_ADAPTER_SHA256"):
        raise ValueError("dependency_identity_mismatch")
    node = os.environ.get("KR_NODE_EXE") or shutil.which("node")
    if not node: raise ValueError("node_missing")
    error = oc._ensure_daemon(root, node)
    return {"status":"failed" if error else "ready", "reason":error,"browser_launched":False}


def bind(context_id, browser):
    if browser != "chrome": raise ValueError("daily_chrome_confirmation_required")
    import httpx
    with httpx.Client(timeout=3,trust_env=False) as client:
        response = client.get("http://127.0.0.1:19825/status",params={"contextId":context_id},headers={"X-OpenCLI":"1"})
        response.raise_for_status()
        state = response.json()
    if not (state.get("extensionConnected") and not state.get("profileDisconnected")
        and state.get("daemonVersion") == "2.0.2" and state.get("extensionVersion") in {"1.0.26"}
        and state.get("contextId") == context_id):
        raise ValueError("selected_browser_not_connected_or_version_mismatch")
    old = oc.setting("KR_XHS_OPENCLI_CONTEXT_ID")
    if old and old != context_id: raise ValueError("existing_binding_requires_explicit_unbind")
    oc._save(oc.state_dir()/"binding.json", {"schema":"kr-opencli-binding/v1",
        "context_id":context_id, "browser":"chrome", "ownership":"borrowed"})
    return {"status":"bound", "context_id":context_id,"auth_state":"not_probed",
        "browser_confirmation":"user_selected_chrome; daemon_does_not_attest_browser_family"}



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare","start","status","inventory","bind"))
    parser.add_argument("--source-root")
    parser.add_argument("--context-id")
    parser.add_argument("--browser",choices=("chrome",))
    args=parser.parse_args()
    if args.action=="prepare":
        if not args.source_root: parser.error("prepare requires --source-root")
        result=prepare(args.source_root)
    elif args.action=="start": result=start_daemon()
    elif args.action=="status": result=oc.connection_status()
    elif args.action=="inventory": result=oc.invoke("inventory")
    else:
        if not args.context_id: parser.error("bind requires --context-id")
        result=bind(args.context_id,args.browser)
    print(json.dumps(result,ensure_ascii=False))
    return 1 if result.get("status")=="failed" else 0


if __name__=="__main__": raise SystemExit(main())
