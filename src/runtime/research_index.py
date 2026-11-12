"""私密研究工作集：可回放发现、可解释关系和只读增量视图；不决策研究策略。"""
from __future__ import annotations
from runtime.research_storage import guarded_transaction

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from runtime.paths import runtime_state_dir


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provenance_rows(value):
    """保留来源的结构化身份，同时兼容旧的字符串列表。"""
    return value if isinstance(value, list) else [value] if isinstance(value, (dict, str)) and value else []


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


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    role: str = Field(default='source_text', max_length=80)


class ResearchWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["question", "link", "relation", "fold", "repair", "host_request", "scope", "assessment", "decision", "action"]
    task_id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    record_id: str = Field(default="", max_length=128)
    question_id: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=2000)
    candidate_id: str = Field(default="", max_length=128)
    related_candidate_id: str = Field(default="", max_length=128)
    version_id: str = Field(default="", max_length=64)
    relation: Literal["background", "supports", "supports_with_limits", "contradicts", "uncertain", "same_origin", "quotes", "independent"] = "background"
    confidence: Literal["declared", "suspected", "confirmed"] = "declared"
    rationale: str = Field(default="", max_length=2000)
    folded: bool = False
    closed: bool = False
    required: bool = True
    languages: list[str] = Field(default_factory=list, max_length=20)
    populations: list[str] = Field(default_factory=list, max_length=20)
    regions: list[str] = Field(default_factory=list, max_length=20)
    information_roles: list[str] = Field(default_factory=list, max_length=20)
    time_scope: str = Field(default='', max_length=500)
    content_scope: str = Field(default='', max_length=500)
    revision: int = Field(default=1, ge=1)
    claim_id: str = Field(default='', max_length=128)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    counterevidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    applicability: str = Field(default='', max_length=2000)
    basis: str = Field(default='', max_length=2000)
    uncertainty: str = Field(default='', max_length=2000)
    supersedes: str = Field(default='', max_length=128)
    decision: Literal['', 'continue', 'stop', 'defer'] = ''
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    reopen_conditions: str = Field(default='', max_length=2000)
    family_id: str = Field(default='', max_length=200)
    action_id: str = Field(default='', max_length=200)
    attempt_id: str = Field(default='', max_length=200)
    receipt_id: str = Field(default='', max_length=128)
    question_revision: int = Field(default=0, ge=0)


from runtime.research_contracts import (RequirementWrite, AttemptWrite, CoverageWrite,
    ExplorationWrite, ExplorationDisposition)
from runtime.research_reviews import (PurposeWrite, ClaimWrite, AlternativesWrite,
    WithdrawalWrite, ReportReviewWrite, LocalObservationWrite, InvestigationWrite)
from runtime.evaluation_maintenance import EvaluationWrite
ResearchRequest = Annotated[ResearchWrite | RequirementWrite | AttemptWrite | CoverageWrite |
    ExplorationWrite | ExplorationDisposition | PurposeWrite | ClaimWrite | AlternativesWrite |
    WithdrawalWrite | ReportReviewWrite | LocalObservationWrite | InvestigationWrite | EvaluationWrite, Field(discriminator='action')]


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
                    ("url", "title", "snippet", "published_at", "source_provider", "provider_provenance", "detail_capability", "parent_id", "host_reference", "retrieved_at", "visible_scope", "truncated", "visible_version_ids") if k in item})
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
                if 'visible_version_ids' in safe:
                    safe['visible_version_ids']=sorted(set(old.get('visible_version_ids',[])+safe['visible_version_ids']))
                provenance = provenance_rows(old.get('provider_provenance')) + provenance_rows(safe.get('provider_provenance'))
                safe['provider_provenance'] = [value for _, value in sorted({encoded(value): value for value in provenance}.items())]
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
        if isinstance(req, EvaluationWrite):
            from runtime.evaluation_maintenance import Maintenance
            return Maintenance().execute(req)
        if not isinstance(req, ResearchWrite):
            from runtime.research_contracts import record
            return record(self, req, assets)
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
        for ref in [*req.evidence_refs, *req.counterevidence_refs]:
            self.validate_ref(ref.model_dump(), assets)
        if req.action == 'assessment' and (not req.claim_id or not req.question_id or not req.evidence_refs
                                          or not req.basis.strip() or not req.applicability.strip()):
            raise ValueError('assessment_requires_claim_question_evidence_basis_applicability')
        if req.action == 'assessment':
            from kr_core.evidence_assets import EvidenceAssets
            repository = assets or EvidenceAssets(read_only=True)
            with repository.connection() as evidence_conn:
                for ref in [*req.evidence_refs, *req.counterevidence_refs]:
                    if not evidence_conn.execute('SELECT 1 FROM asset_scopes WHERE version_id=? AND task_id=?', (ref.version_id, req.task_id)).fetchone():
                        raise ValueError('evidence_not_registered_to_research_task')
        if req.action == 'decision' and (not req.decision or not req.rationale.strip()):
            raise ValueError('decision_and_rationale_required')
        with self.connection(write=True) as conn:
            for cid in (req.candidate_id, req.related_candidate_id):
                if cid and not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='candidate' AND object_id=?", (req.task_id, cid)).fetchone():
                    raise ValueError("unknown_candidate")
            if req.supersedes:
                if not conn.execute('SELECT 1 FROM current WHERE task_id=? AND kind=? AND object_id=?',
                                    (req.task_id, req.action, req.supersedes)).fetchone():
                    raise ValueError('unknown_superseded_record')
            if req.action in {'question', 'scope'}:
                if not req.text.strip():
                    raise ValueError("question_text_required")
                if req.question_id and req.record_id and req.question_id != req.record_id:
                    raise ValueError("question_identity_conflict")
                oid = req.question_id or req.record_id or hashlib.sha256(req.text.encode()).hexdigest()[:20]
                if req.action == 'scope' and not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='question' AND object_id=?",(req.task_id,oid)).fetchone():
                    raise ValueError('unknown_question')
            else:
                if not (req.rationale.strip() or req.action == 'assessment' and req.basis.strip()):
                    raise ValueError("rationale_required")
                if req.action in {"fold", "relation"} and not req.candidate_id:
                    raise ValueError("candidate_required")
                if req.action == "relation" and (not req.related_candidate_id or req.related_candidate_id == req.candidate_id):
                    raise ValueError("distinct_related_candidate_required")
                if req.action in {'link', 'assessment'}:
                    if (req.action == 'link' and not req.version_id) or not req.question_id:
                        raise ValueError("question_and_evidence_version_required")
                    if not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='question' AND object_id=?", (req.task_id, req.question_id)).fetchone():
                        raise ValueError("unknown_question")
                if req.action == 'action':
                    if not req.action_id or not req.receipt_id:
                        raise ValueError('action_and_receipt_required')
                    if not conn.execute("SELECT 1 FROM current WHERE task_id=? AND kind='receipt' AND object_id=?",(req.task_id, req.receipt_id)).fetchone():
                        raise ValueError('unknown_receipt')
                    if req.question_id and not self.question_at(conn, req.task_id, req.question_id):
                        raise ValueError('unknown_question')
                oid = req.candidate_id if req.action == "fold" else (req.record_id or hashlib.sha256(encoded([req.action, req.question_id, req.candidate_id, req.related_candidate_id, req.version_id, req.relation, req.claim_id, req.evidence_refs and [r.model_dump() for r in req.evidence_refs],req.supersedes,req.decision,req.action_id,req.receipt_id]).encode()).hexdigest()[:24])
            value = {**req.model_dump(), "record_id": oid, "author": "agent", "assessment": "agent_declared_not_program_verified"}
            if req.action == 'action':
                value['association_timing'] = 'post_hoc'
            if req.action in {'question', 'scope'}:
                prior = self.question_at(conn, req.task_id, oid)
                fields = ('text','required','languages','populations','regions','information_roles','time_scope','content_scope','closed')
                changed = prior and any(value.get(k) != prior.get(k) for k in fields)
                value['revision'] = max(req.revision, int(prior.get('revision', 0)) + bool(changed))
            if req.action == 'assessment':
                question = self.question_at(conn, req.task_id, req.question_id)
                value['question_revision'] = req.question_revision or question.get('revision',1)
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
                from runtime.research_contracts import feedback
                result.update(feedback(self, conn, task_id, high, assets))
                for key in ("questions_without_valid_links", "unresolved_counterevidence", "questions_without_reviewed_support", "scope_gaps", "assessments_requiring_review", "unplanned_receipts"):
                    result[key + "_count"] = len(result[key])
                    if len(result[key])>limit: result["truncated"] = True
                    result[key] = result[key][:limit]
                # 反馈是摘要；大量问题不能让停止/收尾因为字数预算失败。
                while len(encoded(result)) > max_chars:
                    arrays = [k for k,v in result.items() if isinstance(v,list) and v]
                    if not arrays:
                        raise ValueError("budget_too_small_increase_max_chars")
                    result[max(arrays,key=lambda k:len(encoded(result[k])))].pop()
                    result['truncated'] = True
                if len(encoded(result)) > max_chars:
                    raise ValueError("budget_too_small_increase_max_chars")
                return result
            if view in {'review','withdrawal_scenarios'}:
                from runtime.research_reviews import review_page
                return review_page(self,conn,task_id,base,assets,max_chars,limit,after_seq,high,scenarios=view=='withdrawal_scenarios')
            # 不用OFFSET；续页只跳过已经读过的序号，撤回或折叠不移动其他行。
            clause = "" if view == "changes" else " AND kind=? AND NOT EXISTS(SELECT 1 FROM changes n WHERE n.task_id=c.task_id AND n.kind=c.kind AND n.object_id=c.object_id AND n.seq>c.seq AND n.seq<=?)"
            if view == 'requirements':
                from runtime.research_contracts import requirement_projection, rows_at
                items = requirement_projection(self, conn, task_id, high, assets)
                sequences={r['record_id']:r['_seq'] for r in rows_at(conn,task_id,high,include_seq=True) if r['_kind']=='requirement'}
                pending=sorted((item for item in items if sequences[item['record_id']]>after_seq),key=lambda r:sequences[r['record_id']])
                for item in pending[:limit]:
                    base['items'].append(item)
                    if len(encoded(base))>max_chars:
                        base['items'].pop()
                        if not base['items']:raise ValueError('budget_too_small_increase_max_chars')
                        break
                    base['next_after_seq']=sequences[item['record_id']]
                return {**base,'truncated':len(base['items'])<len(pending)}
            kinds = {"candidates": "candidate", "queries": "query", "questions": "question", "links": "link", "relations": "relation", "host_batches":"host_batch", "scopes":"scope", "assessments":"assessment", "decisions":"decision", "actions":"action", 'attempts':'attempt', 'coverages':'coverage', 'explorations':'exploration', 'exploration_dispositions':'exploration_disposition', 'claims':'claim','purposes':'task_purpose','alternatives_reviews':'alternatives_review','withdrawal_reviews':'withdrawal_review','report_reviews':'report_review','local_observations':'local_observation','investigations':'investigation'}
            if view != "changes" and view not in kinds:
                raise ValueError("invalid_research_view")
            params = [task_id, after_seq, high] + ([] if view == "changes" else [kinds[view], high])
            rows = conn.execute("SELECT c.* FROM changes c WHERE task_id=? AND seq>? AND seq<=?" + clause + " ORDER BY seq", params)
            for row in rows:
                value = json.loads(row["value"])
                if view == 'questions':
                    value = self.question_at(conn,task_id,row['object_id'],high)
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
                if view == 'assessments':
                    value = self.assessment_review(value, self.question_at(conn,task_id,value.get('question_id'),high),assets,task_id)
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
    def validate_ref(ref, assets=None):
        from kr_core.evidence_assets import EvidenceAssets
        if ref['end'] <= ref['start']:
            raise ValueError('invalid_evidence_locator')
        result = (assets or EvidenceAssets(read_only=True)).fetch(ref['version_id'], start=ref['end']-1, max_chars=1)
        if not result.get('text'):
            raise ValueError('evidence_locator_out_of_range')

    @staticmethod
    def question_at(conn, task_id, question_id, high=9223372036854775807):
        row = conn.execute("SELECT value FROM changes WHERE task_id=? AND kind IN ('question','scope') AND object_id=? AND seq<=? ORDER BY seq DESC LIMIT 1",(task_id,question_id,high)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def assessment_review(value, question, assets, task_id):
        if value.get('availability') == 'unavailable':
            return {**value,'review_required':True,'review_reason':'evidence_unavailable'}
        if question.get('revision',1) != value.get('question_revision',1):
            return {**value,'review_required':True,'review_reason':'question_scope_changed'}
        from kr_core.evidence_assets import EvidenceAssets
        repository = assets or EvidenceAssets(read_only=True)
        newer = []
        with repository.connection() as conn:
            for ref in [*value.get('evidence_refs',[]),*value.get('counterevidence_refs',[])]:
                newer.extend(row[0] for row in conn.execute('''SELECT n.version_id FROM assets n
                    JOIN assets old ON n.source_id=old.source_id AND n.kind=old.kind AND n.rowid>old.rowid
                    JOIN asset_scopes s ON s.version_id=n.version_id AND s.task_id=?
                    WHERE old.version_id=? ORDER BY n.rowid''',(task_id,ref['version_id'])))
        return {**value,'review_required':True,'review_reason':'source_version_changed','newer_versions':sorted(set(newer))} if newer else value

    @staticmethod
    def available(value, assets=None):
        for ref in [*value.get('evidence_refs', []), *value.get('counterevidence_refs', [])]:
            try:
                ResearchIndex.validate_ref(ref, assets)
            except (OSError, ValueError):
                return {**value, 'availability':'unavailable', 'assessment':'excluded_from_support', 'review_required':True}
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
        superseded = {x.get('supersedes') for x in groups.get('assessment', []) if x.get('supersedes')}
        questions = [self.question_at(conn,task_id,q['record_id'],high) for q in groups.get('question',[])]
        assessments = [self.assessment_review(x,next((q for q in questions if q['record_id']==x.get('question_id')),{}),assets,task_id)
                       for x in groups.get('assessment',[]) if x.get('record_id') not in superseded and not x.get('closed')]
        from runtime.research_reviews import question_assessments
        assessments += [x for x in question_assessments(self,conn,task_id,assets,high) if x['relation']!='background']
        review_required = [{'record_id':x['record_id'],'question_id':x.get('question_id'),
                            'reason':x.get('review_reason'),'newer_versions':x.get('newer_versions',[])}
                           for x in assessments if x.get('review_required')]
        assessments = [x for x in assessments if not x.get('review_required')]
        support = [x for x in assessments if x.get('relation') in {'supports', 'supports_with_limits'}]
        scope_gaps = []
        for q in questions:
            if q.get('closed') or not q.get('required', True):
                continue
            # 反证也覆盖其实际读过的语言/人群；覆盖与支持是两个问题。
            reviewed = [x for x in assessments if x.get('question_id') == q['record_id']]
            gaps = {name: sorted(set(q.get(name, [])) - {v for x in reviewed for v in x.get(name, [])})
                    for name in ('languages', 'populations', 'regions', 'information_roles')}
            scalar_gaps = {name: q.get(name,'') for name in ('time_scope','content_scope')
                           if q.get(name) and not any(x.get(name)==q[name] for x in reviewed)}
            if not reviewed or any(gaps.values()) or scalar_gaps:
                scope_gaps.append({'question_id':q['record_id'], **{'missing_'+k:v for k,v in gaps.items()},
                                   **{'missing_'+k:v for k,v in scalar_gaps.items()},
                                   'basis':'agent_declared_scope_only', 'time_scope':q.get('time_scope',''),
                                   'content_scope':q.get('content_scope','')})
        queries = groups.get("query", [])
        parents = {x.get('parent_family_id') for x in queries if x.get('parent_family_id')}
        queries = [x for x in queries if not (x.get('status') == 'planned' and x.get('family_id') in parents)]
        relation_counts = {v: sum(x.get("confidence") == v for x in groups.get("relation", [])) for v in ("declared", "suspected", "confirmed")}
        counts = {kind: len(values) for kind, values in groups.items()}
        delta = conn.execute("SELECT kind,COUNT(*) FROM changes WHERE task_id=? AND seq>? AND seq<=? GROUP BY kind", (task_id, after, high)).fetchall()
        new_materials = conn.execute("SELECT COUNT(*) FROM (SELECT object_id,MIN(seq) first_seq FROM changes WHERE task_id=? AND kind='candidate' AND seq<=? GROUP BY object_id HAVING first_seq>?)",(task_id,high,after)).fetchone()[0]
        stages = groups.get("stage",[])
        progress = {name:sum(x.get("stage")==name for x in stages) for name in ("discovered_candidate","selected","detail_extracted","identity_checked","cross_checked","counterevidence","deferred","degraded_or_blocked")}
        return {**base, "next_after_seq": high, "counts": counts, "change_counts": dict(delta),
                "new_materials":new_materials,"material_progress":progress,
                "scope_registration": "recorded" if questions else "missing",
                "unregistered_coverage": "unknown",
                "questions_without_valid_links": [q["record_id"] for q in questions if not q.get("closed") and not any(l.get("question_id") == q["record_id"] for l in [*links, *assessments])],
                "questions_without_reviewed_support": [q['record_id'] for q in questions if not q.get('closed') and not any(x.get('question_id') == q['record_id'] for x in support)],
                "scope_gaps":scope_gaps,
                "assessments_requiring_review":review_required,
                "assessment_basis":"agent_declared_not_program_verified",
                "assessment_origins":{kind:sum(x.get('origin','assessment')==kind for x in assessments) for kind in ('assessment','claim')},
                "investigation_summary":[{'question_id':x['question_id'],'revision':x['revision'],'state':x['state'],
                    'decision_use':x['decision_use'],'next_action':x['next_action'],
                    'scope_changed':x.get('question_revision')!=self.question_at(conn,task_id,x['question_id'],high).get('revision',1),
                    'judgment':'agent_declared_not_program_verified'} for x in groups.get('investigation',[])],
                "questions_without_investigation":[q['record_id'] for q in questions if not q.get('closed') and not any(x['question_id']==q['record_id'] for x in groups.get('investigation',[]))],
                "unplanned_receipts": [x['receipt_id'] for x in groups.get('receipt', [])
                    if x.get('tool') != 'kr_research' and not (x.get('research_action') or {}).get('action_id') and not (x.get('research_action') or {}).get('exploration_id') and not any(a.get('receipt_id') == x['receipt_id'] for a in groups.get('action', []))
                    and not any(q.get('receipt_id') == x['receipt_id'] for q in queries)],
                "unresolved_counterevidence": [l["record_id"] for l in [*links, *assessments] if l.get("relation") == "contradicts" or l.get('counterevidence_refs')],
                "query_outcomes": {s: sum(q.get("status") == s for q in queries) for s in ("planned", "running", "ok", "partial", "failed", "skipped")},
                "zero_result_queries": sum(q.get("status") == "ok" and q.get("returned_count") == 0 for q in queries),
                "origin_relations": relation_counts, "origin_independence": "not_inferred_from_domain_or_receipt_count",
                "stop_decision": "agent_owned", "semantic_saturation": "not_computed"}
