"""私密研究工作集：可回放发现、可解释关系和只读增量视图；不决策研究策略。"""
from __future__ import annotations
from runtime.research_storage import guarded_transaction

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from runtime.paths import runtime_state_dir


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(item, ecology=""):
    from kr_core.source_urls import discovery_url as canonical_url
    url = canonical_url(str(item.get("url") or item.get("source_url") or ""))
    # URL定义可访问材料；只在没有URL时才使用平台命名空间下的原生ID。
    native = item.get("content_id") or item.get("note_id") or item.get("id")
    key = url or f"{ecology}|{native or item.get('title') or encoded(item)}"
    reference = (item.get('raw') or {}).get('fulltext_ref') or item.get('academic_reference') or item.get('fulltext_ref')
    if isinstance(reference, dict) and reference.get('edition_id'):
        key += '|academic-edition:' + str(reference['edition_id'])
    return "candidate-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def query_outcome(result):
    """仅按适配器明确结果分类，技术失败永不解释为零命中。"""
    error = result.get("error")
    details = error.get("details", []) if isinstance(error,dict) else []
    if error:
        if details and all(isinstance(x,dict) and x.get("type")=="empty_results" for x in details):
            return "ok"
        if isinstance(error,dict) and error.get("type")=="empty_results":
            return "ok"
        return "failed"
    errors = (result.get("metadata") or {}).get("errors",[])
    if any(not isinstance(x,dict) or x.get("type")!="empty_results" for x in errors):
        return "partial" if (result.get("acquired_items") or result.get("items")) else "failed"
    return "ok"


class ResearchWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["question", "link", "relation", "fold", "repair", "host_request"]
    task_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    record_id: str = Field(default="", max_length=128)
    question_id: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=2000)
    candidate_id: str = Field(default="", max_length=128)
    related_candidate_id: str = Field(default="", max_length=128)
    version_id: str = Field(default="", max_length=64)
    relation: Literal["background", "supports", "contradicts", "uncertain", "same_origin", "quotes", "independent"] = "background"
    confidence: Literal["declared", "suspected", "confirmed"] = "declared"
    rationale: str = Field(default="", max_length=2000)
    folded: bool = False
    closed: bool = False


class ResearchIndex:
    def __init__(self, root=None):
        self.root = Path(root or runtime_state_dir())
        self.path = self.root / "research_index.sqlite3"

    @guarded_transaction
    @contextmanager
    def connection(self, *, write=False):
        if not write and not self.path.is_file():
            yield None
            return
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=15)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS changes(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                        kind TEXT NOT NULL, object_id TEXT NOT NULL, value TEXT NOT NULL,
                        dedup_key TEXT NOT NULL UNIQUE, observed_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS changes_task ON changes(task_id,kind,object_id,seq);
                    CREATE TABLE IF NOT EXISTS current(
                        task_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,value TEXT NOT NULL,
                        PRIMARY KEY(task_id,kind,object_id));
                """)
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def put(conn, task_id, kind, object_id, value, key):
        previous = conn.execute("SELECT seq,value FROM current WHERE task_id=? AND kind=? AND object_id=?", (task_id,kind,object_id)).fetchone()
        if previous and previous["value"] == encoded(value):
            return False
        dedup = hashlib.sha256(encoded([task_id, kind, object_id, key, previous["seq"] if previous else 0]).encode()).hexdigest()
        cur = conn.execute("INSERT OR IGNORE INTO changes(task_id,kind,object_id,value,dedup_key,observed_at) VALUES(?,?,?,?,?,?)",
                           (task_id, kind, object_id, encoded(value), dedup, time.time()))
        if not cur.rowcount:
            return False
        conn.execute("INSERT INTO current VALUES(?,?,?,?,?) ON CONFLICT(task_id,kind,object_id) DO UPDATE SET seq=excluded.seq,value=excluded.value",
                     (task_id, kind, object_id, cur.lastrowid, encoded(value)))
        return True

    def candidates(self, task_id, items, ecology, receipt_id, *, query="", aliases=None):
        from kr_core.source_urls import discovery_url as canonical_url, public_evidence_payload
        if aliases is None:
            # 首个provider可早于本轮账本收口；复用旧材料ID，不产生升级孤岛。
            from runtime.research_ledger import _read
            from kr_core.source_urls import canonical_url as evidence_url
            legacy = (_read(task_id) or {}).get("candidate_pool",[])
            aliases = {}
            for item in items:
                if not isinstance(item,dict) or not item.get("url"): continue
                if (item.get('raw') or {}).get('fulltext_ref') or item.get('academic_reference'):
                    continue  # 旧URL别名没有版本信息，不能吞并不同学术版本。
                key=hashlib.sha256(evidence_url(item["url"]).encode()).hexdigest()
                matches=[x for x in legacy if x.get("source_key")==key]
                if len(matches)==1: aliases[identity(item,ecology)] = matches[0]["candidate_id"]
        with self.connection(write=True) as conn:
            for item in items:
                if not isinstance(item, dict):
                    continue
                item = {**item, "provider_provenance": item.get("provider_provenance") or (item.get("raw") or {}).get("provider_provenance",[])}
                cid = (aliases or {}).get(identity(item, ecology), identity(item, ecology))
                safe = public_evidence_payload({k: item[k] for k in
                    ("url", "title", "snippet", "published_at", "source_provider", "provider_provenance", "detail_capability", "parent_id") if k in item})
                safe = {k: str(v)[:4000] if isinstance(v, str) else v for k, v in safe.items()}
                fulltext_ref = (item.get('raw') or {}).get('fulltext_ref') or item.get('fulltext_ref')
                if isinstance(fulltext_ref, dict) and fulltext_ref.get('version_id'):
                    safe['academic_reference'] = public_evidence_payload(fulltext_ref)
                    safe['doi'] = str(item.get('doi') or '')
                    safe['version'] = str(item.get('version') or 'unknown')
                if safe.get("url"):
                    safe["url"] = canonical_url(safe["url"])
                prior = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='candidate' AND object_id=?", (task_id, cid)).fetchone()
                old = json.loads(prior[0]) if prior else {}
                safe["provider_provenance"] = sorted(set(old.get("provider_provenance",[]) + safe.get("provider_provenance",[])))
                value = {**old, **safe, "candidate_id": cid,
                         "source_ecologies": sorted(set(old.get("source_ecologies", []) + [ecology])),
                         "verification": "discovery_only", "independence": "unknown"}
                self.put(conn, task_id, "candidate", cid, value, receipt_id or encoded(safe))
                discovery = {"candidate_id": cid, "receipt_id": receipt_id, "source_ecology": ecology,
                             "query": query, "provider": safe.get("source_provider", "unknown")}
                self.put(conn, task_id, "discovery", cid + ":" + (receipt_id or "declared"), discovery, receipt_id or encoded(discovery))

    def ledger_projection(self, payload):
        """仅由账本成功写入后调用；可幂等重放，不由读取触发修复。"""
        tid = payload["research_task_id"]
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='ledger'", (tid,)).fetchone()
            revision = int(payload.get("revision", 0))
            if old and json.loads(old[0]).get("revision", -1) >= revision:
                return
            for row in payload.get("candidate_pool", []):
                value = {k: row[k] for k in ("candidate_id", "stage", "receipt_ids", "verification", "last_outcome", "source_key") if k in row}
                self.put(conn, tid, "stage", row["candidate_id"], value, encoded(value))
            for row in payload.get("tool_receipts", []):
                self.put(conn, tid, "receipt", row["receipt_id"], row, row["receipt_id"])
            self.put(conn, tid, "ledger", tid, {"revision": revision, "considered": payload.get("candidate_ecologies", []),
                                               "pending_submission_count": len(payload.get("pending_submissions", []))}, str(revision))

    def query(self, task_id, family, *, status="planned", result=None):
        value = {**family, "status": status}
        if result is not None:
            value.update(returned_count=len(result.get("acquired_items") or result.get("items") or []), failure=result.get("error") or None,
                         receipt_id=(result.get("research_receipt") or {}).get("receipt_id", ""))
        with self.connection(write=True) as conn:
            prior = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='query' AND object_id=?",(task_id,family["family_id"])).fetchone()
            if prior and status=="planned":
                old=json.loads(prior[0])
                if all(old.get(k)==v for k,v in family.items()):
                    return
            self.put(conn, task_id, "query", family["family_id"], value, encoded(value))

    def annotate(self, req: ResearchWrite, *, assets=None):
        from runtime.research_ledger import read_task
        if read_task(task_id=req.task_id).get('status') == 'unknown_task':
            raise ValueError("unknown_research_task")
        if req.action == 'host_request':
            from runtime.host_search_handoff import register
            return register(req.task_id, req.text, batch_id=req.record_id, index=self)
        if req.action == "repair":
            self.ledger_projection(read_task(task_id=req.task_id))
            return {"status": "recorded", "kind": "projection_repair"}
        if req.version_id:
            from kr_core.evidence_assets import EvidenceAssets
            (assets or EvidenceAssets(read_only=True)).fetch(req.version_id, max_chars=1)
        with self.connection(write=True) as conn:
            for cid in (req.candidate_id, req.related_candidate_id):
                if cid and not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='candidate' AND object_id=?", (req.task_id, cid)).fetchone():
                    raise ValueError("unknown_candidate")
            if req.action == "question":
                if not req.text.strip():
                    raise ValueError("question_text_required")
                oid = req.question_id or hashlib.sha256(req.text.encode()).hexdigest()[:20]
            else:
                if not req.rationale.strip():
                    raise ValueError("rationale_required")
                if req.action in {"fold", "relation"} and not req.candidate_id:
                    raise ValueError("candidate_required")
                if req.action == "relation" and (not req.related_candidate_id or req.related_candidate_id == req.candidate_id):
                    raise ValueError("distinct_related_candidate_required")
                if req.action == "link":
                    if not req.version_id or not req.question_id:
                        raise ValueError("question_and_evidence_version_required")
                    if not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='question' AND object_id=?", (req.task_id, req.question_id)).fetchone():
                        raise ValueError("unknown_question")
                oid = req.candidate_id if req.action == "fold" else (req.record_id or hashlib.sha256(encoded([req.question_id, req.candidate_id, req.related_candidate_id, req.version_id, req.relation]).encode()).hexdigest()[:24])
            value = {**req.model_dump(), "record_id": oid, "author": "agent", "assessment": "agent_declared_not_program_verified"}
            self.put(conn, req.task_id, req.action, oid, value, encoded(value))
        return {"status": "recorded", "record_id": oid, "author": "agent"}

    def read(self, task_id, *, view="candidates", after_seq=0, before_seq=0, limit=20, include_folded=True, query="", max_chars=8000, assets=None):
        with self.connection() as conn:
            if conn is None:
                return {"kind": "research", "status": "not_initialized", "items": [], "read_only": True}
            upper = conn.execute("SELECT COALESCE(MAX(seq),0) FROM changes WHERE task_id=?", (task_id,)).fetchone()[0]
            high = min(before_seq or upper, upper)
            base = {"kind": "research", "status": "ok", "read_only": True, "task_id": task_id,
                    "watermark": high, "latest_seq": upper, "items": [], "next_after_seq": after_seq,
                    "coverage_scope": "registered_items_only", "truncated": False}
            if view == "duplicates":
                from runtime.evidence_views import suggestions
                page = self.read(task_id,view="candidates",after_seq=after_seq,before_seq=high,limit=min(limit,100),max_chars=max_chars,assets=assets)
                findings = suggestions(page["items"])
                result = {**base,"items":[],"next_after_seq":page["next_after_seq"],"truncated":page["truncated"],"coverage_scope":"current_candidate_page_snippets_only"}
                for finding in findings:
                    result["items"].append(finding)
                    if len(encoded(result))>max_chars:
                        result["items"].pop()
                        result["truncated"]=True
                        result["suggestions_omitted"]=True
                        break
                return result
            if view == "feedback":
                result = self.feedback(conn, task_id, after_seq, high, base, assets)
                for key in ("questions_without_valid_links", "unresolved_counterevidence"):
                    result[key + "_count"] = len(result[key])
                    if len(result[key])>limit: result["truncated"] = True
                    result[key] = result[key][:limit]
                if len(encoded(result)) > max_chars:
                    raise ValueError("budget_too_small_increase_max_chars")
                return result
            # 不用OFFSET；续页只跳过已经读过的序号，撤回或折叠不移动其他行。
            clause = "" if view == "changes" else " AND kind=? AND NOT EXISTS(SELECT 1 FROM changes n WHERE n.task_id=c.task_id AND n.kind=c.kind AND n.object_id=c.object_id AND n.seq>c.seq AND n.seq<=?)"
            kinds = {"candidates": "candidate", "queries": "query", "questions": "question", "links": "link", "relations": "relation", "host_batches":"host_batch"}
            if view != "changes" and view not in kinds:
                raise ValueError("invalid_research_view")
            params = [task_id, after_seq, high] + ([] if view == "changes" else [kinds[view], high])
            rows = conn.execute("SELECT c.* FROM changes c WHERE task_id=? AND seq>? AND seq<=?" + clause + " ORDER BY seq", params)
            for row in rows:
                value = json.loads(row["value"])
                if query and query.casefold() not in encoded(value).casefold():
                    base["next_after_seq"] = row["seq"]
                    continue
                if view == "candidates":
                    fold = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='fold' AND object_id=?", (task_id, row["object_id"])).fetchone()
                    value["fold"] = json.loads(fold[0]) if fold else {"folded": False}
                    stage = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='stage' AND object_id=?", (task_id,row["object_id"])).fetchone()
                    if stage:
                        value["progress"] = json.loads(stage[0])
                    if not include_folded and value["fold"].get("folded"):
                        base["next_after_seq"] = row["seq"]
                        continue
                value = self.available(value, assets)
                record = {"seq": row["seq"], "record_kind": row["kind"], **value}
                base["items"].append(record)
                if len(base["items"]) > limit or len(encoded(base)) > max_chars:
                    base["items"].pop()
                    if not base["items"]:
                        raise ValueError("budget_too_small_increase_max_chars")
                    base["truncated"] = True
                    break
                base["next_after_seq"] = row["seq"]
            if not base["truncated"]:
                base["next_after_seq"] = high
            return base

    @staticmethod
    def available(value, assets=None):
        if value.get("version_id"):
            from kr_core.evidence_assets import EvidenceAssets
            try:
                (assets or EvidenceAssets(read_only=True)).fetch(value["version_id"], max_chars=1)
            except (OSError, ValueError):
                return {"record_id": value.get("record_id"), "question_id": value.get("question_id"), "availability": "unavailable", "assessment": "excluded_from_support"}
        return value

    def feedback(self, conn, task_id, after, high, base, assets):
        rows = conn.execute("SELECT c.* FROM changes c WHERE task_id=? AND seq<=? AND NOT EXISTS(SELECT 1 FROM changes n WHERE n.task_id=c.task_id AND n.kind=c.kind AND n.object_id=c.object_id AND n.seq>c.seq AND n.seq<=?)", (task_id, high, high)).fetchall()
        groups = {}
        for row in rows:
            groups.setdefault(row["kind"], []).append(self.available(json.loads(row["value"]), assets))
        links = [x for x in groups.get("link", []) if x.get("version_id") and not x.get("closed")]
        questions = groups.get("question", [])
        queries = groups.get("query", [])
        relation_counts = {v: sum(x.get("confidence") == v for x in groups.get("relation", [])) for v in ("declared", "suspected", "confirmed")}
        counts = {kind: len(values) for kind, values in groups.items()}
        delta = conn.execute("SELECT kind,COUNT(*) FROM changes WHERE task_id=? AND seq>? AND seq<=? GROUP BY kind", (task_id, after, high)).fetchall()
        new_materials = conn.execute("SELECT COUNT(*) FROM (SELECT object_id,MIN(seq) first_seq FROM changes WHERE task_id=? AND kind='candidate' AND seq<=? GROUP BY object_id HAVING first_seq>?)",(task_id,high,after)).fetchone()[0]
        stages = groups.get("stage",[])
        progress = {name:sum(x.get("stage")==name for x in stages) for name in ("discovered_candidate","selected","detail_extracted","identity_checked","cross_checked","counterevidence","deferred","degraded_or_blocked")}
        return {**base, "next_after_seq": high, "counts": counts, "change_counts": dict(delta),
                "new_materials":new_materials,"material_progress":progress,
                "questions_without_valid_links": [q["record_id"] for q in questions if not q.get("closed") and not any(l.get("question_id") == q["record_id"] for l in links)],
                "unresolved_counterevidence": [l["record_id"] for l in links if l.get("relation") == "contradicts"],
                "query_outcomes": {s: sum(q.get("status") == s for q in queries) for s in ("planned", "running", "ok", "partial", "failed", "skipped")},
                "zero_result_queries": sum(q.get("status") == "ok" and q.get("returned_count") == 0 for q in queries),
                "origin_relations": relation_counts, "origin_independence": "not_inferred_from_domain_or_receipt_count",
                "stop_decision": "agent_owned", "semantic_saturation": "not_computed"}
