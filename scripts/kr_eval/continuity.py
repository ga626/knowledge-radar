"""接通既有账本：历史保护、证据导入和只读摘要。原run永不改写。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import json
import re
import shutil
import uuid

from .core import (SCHEMA, Ledger, dependencies, digest, environment, file_hash, inventory,
                   now, read_json, safe_path, snapshot, status, write_new)
from .runner import comparison_identity


def import_history(ledger, source):
    source = Ledger(source)
    if source.path == ledger.path:
        raise ValueError("source and destination must differ")
    history = source.history()  # 先核对所有run及引用证据，失败不迁移。
    events = list((source.path / "baselines").glob("*.json"))
    for path in events:
        event = read_json(path)
        if source.read(event["run_id"])["integrity"] != event["integrity"]:
            raise ValueError("baseline integrity mismatch")
    for run in history:
        destination = ledger.path / "runs" / run["run_id"]
        if destination.exists() and ledger.read(run["run_id"])["integrity"] != run["integrity"]:
            raise ValueError("conflicting historical run")
    copied = []
    for run in history:
        destination = ledger.path / "runs" / run["run_id"]
        if destination.exists():
            continue
        # 只搬账本及其引用文件，不复制sandbox HOME、依赖或其他运行态。
        staging = ledger.path / (".import-" + uuid.uuid4().hex)
        staging.mkdir(parents=True)
        try:
            origin = source.path / "runs" / run["run_id"]
            names = {"run.json", *(x["path"] for row in run["results"] for x in row.get("evidence", []))}
            names.update(x['path'] for x in run.get('ci_evidence', []))
            if (origin / "started.json").is_file():
                names.add("started.json")
            for name in names:
                src, dst = safe_path(origin, name), safe_path(staging, name)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                if file_hash(src) != file_hash(dst):
                    raise ValueError("history copy hash mismatch")
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        ledger.read(run["run_id"])
        copied.append(run["run_id"])
    for path in events:
        dest = ledger.path / "baselines" / path.name
        if dest.exists():
            if file_hash(path) != file_hash(dest):
                raise ValueError("baseline event name conflict")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    receipt = {"schema": SCHEMA, "event": "history_imported", "at": now(), "source": str(source.path),
               "copied": copied, "verified": [{"run_id": r["run_id"], "integrity": r["integrity"],
                  "run_file_sha256": file_hash(source.path / "runs" / r["run_id"] / "run.json")} for r in history],
               "baseline_events": len(events), "historical_identity_preserved": True}
    write_new(ledger.path / "imports" / (uuid.uuid4().hex + ".json"), receipt)
    return receipt


def import_speech_dataset(ledger, manifest):
    """保护已有真人语音输入和待核对标注，不重下载、不重标金标、不运行ASR。"""
    manifest = Path(manifest).resolve()
    source = read_json(manifest)
    annotations_path = manifest.with_name("human-annotations.json")
    annotations = read_json(annotations_path)
    if not source.get("dataset_revision") or not source.get("license") or not source.get("samples"):
        raise ValueError("dataset provenance required")
    annotation_map = {r["id"]: r for r in annotations["samples"]}
    assets, samples = [(manifest, "source_manifest"), (annotations_path, "annotations")], []
    for sample in source["samples"]:
        audio = Path(sample["path"]).resolve()
        if not audio.is_relative_to(manifest.parent) or not audio.is_file() or file_hash(audio) != sample["audio_sha256"]:
            raise ValueError("dataset audio missing/changed/outside source")
        annotation = annotation_map[sample["id"]]
        if annotation["audio_sha256"] != sample["audio_sha256"]:
            raise ValueError("annotation input identity mismatch")
        assets.append((audio, "audio"))
        samples.append({"sample_id": sample["id"], "language": sample["language"],
                        "input_sha256": sample["audio_sha256"], "duration_s": sample["duration_s"],
                        "annotation_status": annotation["human_review_status"],
                        "human_gold_available": bool(annotation.get("human_reference") and annotation.get("human_review_status") == "CONFIRMED")})
    for name in ("en-results.json", "zh-results.json"):
        path = manifest.with_name(name)
        if path.is_file(): assets.append((path, "historical_output_not_gold"))
    references = []
    for original, role in assets:
        checksum = file_hash(original)
        dest = ledger.path / "assets" / (checksum + original.suffix)
        if dest.exists():
            if file_hash(dest) != checksum: raise ValueError("dataset asset conflict")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, dest)
            if file_hash(dest) != checksum: raise ValueError("dataset copy mismatch")
        references.append({"role": role, "original": str(original), "path": dest.relative_to(ledger.path).as_posix(), "sha256": checksum})
    record = {"schema": "knowledgeradar-dataset-snapshot/v1", "dataset_id": "speech.google-fleurs",
              "source_revision": source["dataset_revision"], "license": source["license"],
              "annotation_status": annotations["status"], "samples": samples, "assets": references,
              "quality_verdict": "NOT_RUN", "replayed": False}
    identity = digest(record)
    target = ledger.path / "datasets" / (identity + ".json")
    if not target.exists(): write_new(target, {**record, "integrity": identity})
    return {"dataset_id": record["dataset_id"], "snapshot": str(target), "samples": len(samples), "quality_verdict": "NOT_RUN",
            "human_gold_available": sum(r["human_gold_available"] for r in samples), "input_mapping": {r["original"]: str(ledger.path / r["path"]) for r in references}}


def publish_summary(root, suite, ledger):
    history = ledger.history()
    superseded = {r["supersedes"] for r in history if r.get("supersedes")}
    external = [{"run_id": r["run_id"], "target": r["target"], "checked_at": r["finished_at"],
                 "counts": r["counts"], "applicability": "historical_external_observation"}
                for r in history if r["target"]["kind"] != "development_source" and r["run_id"] not in superseded]
    summary = {**status(root, suite, ledger), "generated_at": now(), "external_observations": external,
               "source_root": str(root.resolve()), "watched_files": snapshot(root)["files"]}
    payload = {**summary, "integrity": digest(summary)}
    ledger.path.mkdir(parents=True, exist_ok=True)
    temporary = ledger.path / (".summary-" + uuid.uuid4().hex)
    write_new(temporary, payload)
    os.replace(temporary, ledger.path / "summary.json")
    return payload


def _field(value, path):
    for key in path.split("."):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def prepare_native_receipt(suite, transcript_path, call_id, case_id, target, output):
    """从已有宿主日志提取精确调用对；不执行工具，不把自填PASS当回执。"""
    case = next(c for c in suite["cases"] if c["case_id"] == case_id)
    contract = case.get("receipt_contract")
    if not contract or case["layer"] != "native_mcp":
        raise ValueError("case needs a reviewed native response contract")
    session_id, request, response = None, None, None
    with Path(transcript_path).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                session_id = payload.get("id")
            if payload.get("call_id") != call_id:
                continue
            if payload.get("type") == "custom_tool_call": request = payload
            if payload.get("type") == "custom_tool_call_output": response = payload
    if not session_id or not request or not response:
        raise ValueError("host call pair missing")
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("receipt output must be empty")
    tool = contract["tool"]
    script = request.get("input", "")
    match = re.search(r'tools\.mcp__knowledgeradar__' + re.escape(tool) + r'\((\{[^;]*\})\)', script)
    if not match:
        raise ValueError("native dispatch missing")
    args = json.loads(re.sub(r'([,{]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', match.group(1)))
    candidates = []
    for block in response.get("output", []):
        try:
            raw = json.loads(block.get("text", ""))
            content = raw.get("content", []) if isinstance(raw, dict) else raw
            for item in content:
                if item.get("type") == "text": candidates.append(json.loads(item["text"]))
        except (ValueError, TypeError, AttributeError):
            continue
    if len(candidates) != 1:
        raise ValueError("ambiguous/truncated native response")
    raw_response = candidates[0]
    # 时间取工具原始回执，不以导入时间冒充最近实测。
    checked_at = raw_response.get("runtime", {}).get("generated_at")
    if not checked_at:
        raise ValueError("native observation timestamp missing")
    write_new(output / "transcript.json", {"session_id": session_id, "request": request, "response": response})
    write_new(output / "response.json", raw_response)
    host = {"session_id": session_id, "call_id": call_id, "call_id_kind": "codex_exec_wrapper",
        "tool": tool, "target": target, "access_path": "codex_native_tool", "input_sha256": digest(args),
        "response_sha256": file_hash(output / "response.json"),
        "transcript": {"path": "transcript.json", "sha256": file_hash(output / "transcript.json")}}
    _native_transcript(host, output, raw_response, tool)
    write_new(output / "host.json", host)
    envelope = {"schema": "knowledgeradar-evaluation-receipt/v1", "case_id": case_id,
        "contract_hash": digest(contract), "oracle_version": case["oracle_version"], "input_sha256": digest(args),
        "checked_at": checked_at, "target": target,
        "response": {"path": "response.json", "sha256": file_hash(output / "response.json")},
        "host_record": {"path": "host.json", "sha256": file_hash(output / "host.json")}}
    write_new(output / "receipt.json", envelope)
    return {"receipt": str(output / "receipt.json"), "case_id": case_id, "call_id": call_id}


def _native_transcript(host, base, response, tool):
    reference = host["transcript"]
    path = safe_path(base, reference["path"])
    if file_hash(path) != reference["sha256"]:
        raise ValueError("host transcript hash mismatch")
    transcript = read_json(path)
    request, output = transcript["request"], transcript["response"]
    if not host.get("session_id") == transcript["session_id"] or not request.get("call_id") == output.get("call_id") == host["call_id"]:
        raise ValueError("host transcript call pairing mismatch")
    # 单原生调用的宿主exec回执；call_id是宿主包装调用ID，非KR伪造的server ID。
    script = request.get("input", "")
    match = re.fullmatch(r'text\(await tools\.mcp__knowledgeradar__' + re.escape(tool) + r'\((\{[^;]*\})\)\);?', script.strip())
    if not match:
        match = re.fullmatch(r'text\(\(await tools\.mcp__knowledgeradar__' + re.escape(tool) + r'\((\{[^;]*\})\)\)\.content\);?', script.strip())
    if request.get("type") != "custom_tool_call" or request.get("name") != "exec" or not match:
        raise ValueError("only an unambiguous single native host dispatch is supported")
    found = []
    for block in output.get("output", []):
        try:
            raw = json.loads(block.get("text", ""))
            content = raw.get("content") if isinstance(raw, dict) else raw
            if isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        found.append(json.loads(item["text"]))
        except (ValueError, TypeError):
            continue
    if not any(digest(value) == digest(response) for value in found):
        raise ValueError("native response absent from paired host output")
    arguments = json.loads(re.sub(r'([,{]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', match.group(1)))
    if digest(arguments) != host.get("input_sha256"):
        raise ValueError("host input identity mismatch")
    return path


def import_receipt(root, suite, ledger, path, expected_target):
    """只接受受控合同及精确目标绑定；原生层必须有宿主调用及原始输出配对。"""
    path = Path(path).resolve()
    envelope = read_json(path)
    if envelope.get("schema") != "knowledgeradar-evaluation-receipt/v1":
        raise ValueError("unsupported evidence receipt")
    target = envelope["target"]
    if target != expected_target or target.get("kind") not in {"native_mcp", "installed", "live"}:
        raise ValueError("target identity mismatch")
    if not all(target.get(k) for k in ("source_commit", "artifact_sha256", "runtime_identity")):
        raise ValueError("missing product identity")
    if not re.fullmatch(r"[a-f0-9]{40}", target["source_commit"]) or not re.fullmatch(r"[a-f0-9]{64}", target["artifact_sha256"]):
        raise ValueError("invalid source/artifact identity")
    case = next(c for c in suite["cases"] if c["case_id"] == envelope["case_id"])
    if case["executor"] != "manual" or case["layer"] != target["kind"]:
        raise ValueError("receipt layer mismatch")
    contract = case.get("receipt_contract")
    if not contract or envelope.get("contract_hash") != digest(contract):
        raise ValueError("no matching reviewed business receipt contract")
    checked = datetime.fromisoformat(envelope["checked_at"])
    if checked.tzinfo is None or checked > datetime.now(timezone.utc):
        raise ValueError("invalid observation time")
    source = safe_path(path.parent, envelope["response"]["path"])
    if file_hash(source) != envelope["response"]["sha256"]:
        raise ValueError("response hash mismatch")
    response = read_json(source)
    host = None
    if case["layer"] == "native_mcp":
        host_path = safe_path(path.parent, envelope["host_record"]["path"])
        if file_hash(host_path) != envelope["host_record"]["sha256"]:
            raise ValueError("host record hash mismatch")
        host = read_json(host_path)
        if not host.get("call_id") or not host.get("session_id") or host.get("tool") != contract["tool"]:
            raise ValueError("missing native host call binding")
        if host.get("response_sha256") != file_hash(source) or host.get("target") != target or host.get("access_path") != "codex_native_tool":
            raise ValueError("native host evidence mismatch")
        transcript_path = _native_transcript(host, path.parent, response, contract["tool"])
    failed_assertions = []
    for check in contract["assertions"]:
        try:
            actual = _field(response, check["field"])
            if "equals" in check and actual != check["equals"]:
                failed_assertions.append(check["field"])
            if check.get("nonempty") and (not isinstance(actual, (str, list, dict)) or not actual):
                failed_assertions.append(check["field"])
        except (KeyError, IndexError, TypeError, ValueError):
            failed_assertions.append(check["field"])
    # 身份/输入由原始回执字段确认，禁止只在导入表单填一个正确SHA。
    for field, key in contract["identity_fields"].items():
        if _field(response, field) != target[key]:
            raise ValueError("response product identity mismatch")
    if not re.fullmatch(r"[a-f0-9]{64}", str(envelope.get("input_sha256", ""))) or not envelope.get("oracle_version") == case["oracle_version"]:
        raise ValueError("input/oracle identity required")
    if host and host.get("input_sha256") != envelope["input_sha256"]:
        raise ValueError("receipt input mismatch")
    if contract.get("observed_at_field") and _field(response, contract["observed_at_field"]) != envelope["checked_at"]:
        raise ValueError("observation timestamp mismatch")
    if contract.get("input_field") and digest(_field(response, contract["input_field"])) != envelope["input_sha256"]:
        raise ValueError("response input identity mismatch")
    receipt_hash = file_hash(path)
    import_identity = digest({"adapter_version": 2, "case": case["case_id"], "contract": digest(contract),
        "oracle": case["oracle_version"], "target": target, "input": envelope["input_sha256"],
        "checked_at": envelope["checked_at"], "response": envelope["response"]["sha256"]})
    history = ledger.history()
    prior = next((r for r in history if r.get("import_identity") == import_identity), None)
    if prior:
        return prior
    supersedes = next((r["run_id"] for r in history if r.get("import_receipt_sha256") == receipt_hash), None)
    snap = snapshot(root)
    # 导入环境没有测量过的事实必须保持unknown，不能套用导入器的Python包列表。
    env = {"fingerprint": "unknown:" + digest(target), "facts": {"execution_policy": "external_observation", "runtime_packages": "unmeasured"}}
    deps = dependencies(root, suite, case, snap)
    run_id, folder = ledger.begin()
    evidence = []
    for name, original in [("receipt.json", path), ("response.json", source), *(([("host.json", host_path), ("transcript.json", transcript_path)] if host else []))]:
        shutil.copyfile(original, folder / name)
        evidence.append({"path": name, "sha256": file_hash(folder / name)})
    verdict = "FAIL" if failed_assertions else "PASS"
    row = {"case_id": case["case_id"], "version": case["version"], "layer": case["layer"], "verdict": verdict,
           "readiness": "unknown", "evidence_kind": "bound_external_receipt", "dependency_identity": deps,
           "comparison_identity": digest({"contract": contract, "input": envelope["input_sha256"], "oracle": case["oracle_version"]}),
           "evidence": evidence, "failed_assertions": failed_assertions, "cost": "unknown",
           "summary": "Imported observation; not current checkout or whole-product acceptance"}
    return ledger.save({"schema": SCHEMA, "run_id": run_id, "started_at": envelope["checked_at"],
          "finished_at": envelope["checked_at"], "imported_at": now(), "target": target, "snapshot": snap,
          "environment": env, "inventory": inventory(root, suite), "results": [row], "counts": {verdict: 1},
          "import_receipt_sha256": receipt_hash, "import_identity": import_identity,
          "importer_version": 2, "supersedes": supersedes})
