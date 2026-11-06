"""固定版本知识读取与报告登记；索引复用证据库，不依赖任何外部知识产品。"""
from __future__ import annotations
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from kr_core.evidence_assets import EvidenceAssets


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Search(Contract):
    action: Literal["search"]
    query: str = Field(default="", max_length=500)
    match: Literal["terms", "phrase", "any"] = "terms"
    project_id: str = ""
    task_id: str = ""
    kind: str = ""
    history: bool = False
    captured_after: float | None = None
    captured_before: float | None = None
    limit: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=8000, ge=2000, le=24000)
    cursor: str = Field(default="", max_length=2000)


class Fetch(Contract):
    action: Literal["fetch"]
    version_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8000, ge=2000, le=24000)
    line_start: int | None = Field(default=None, ge=1, description='可选原文起始行；与非零start互斥。GitHub固定源码可直接按行读取。')
    line_end: int | None = Field(default=None, ge=1, description='可选原文结束行，包含该行；须同时提供line_start。')


class ResearchRead(Contract):
    action: Literal["research"]
    task_id: str = Field(min_length=1,max_length=96,pattern=r"^[A-Za-z0-9_-]+$")
    view: Literal["candidates","changes","queries","questions","links","relations","feedback","duplicates","host_batches"] = "candidates"
    after_seq: int = Field(default=0,ge=0)
    before_seq: int = Field(default=0,ge=0)
    limit: int = Field(default=20,ge=1,le=100)
    max_chars: int = Field(default=8000,ge=2000,le=24000)
    include_folded: bool = True
    query: str = Field(default="",max_length=500)


ReadRequest = Annotated[Search | Fetch | ResearchRead, Field(discriminator="action")]


class AssetRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    version_id: str
    source_id: str
    title: str
    raw_sha256: str
    captured_at: float
    kind: str


class Hit(Contract):
    asset: AssetRef
    excerpt: str
    start: int
    end: int
    citation: str
    match_reason: str
    passages: list[dict] = Field(default_factory=list)


class SearchResult(Contract):
    kind: Literal["search"] = "search"
    status: Literal["ok", "not_initialized", "upgrade_required"] = "ok"
    items: list[Hit] = Field(default_factory=list)
    unavailable_count: int = 0
    generation: int = 0
    next_cursor: str | None = None
    truncated: bool = False
    read_only: bool = True
    ranking: str = "title_phrase_then_term_hits_then_recency"
    budget_unit: str = "unicode_code_points_entire_response"


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["fetch"] = "fetch"
    status: Literal["ok"] = "ok"
    asset: AssetRef
    text: str
    next_start: int | None
    truncated: bool
    citation: str


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["research"] = "research"
    status: str
    items: list[dict] = Field(default_factory=list)
    read_only: bool = True


ReadResult = SearchResult | FetchResult | ResearchResult


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def search(assets: EvidenceAssets, request: Search):
    if not (assets.root / "index.sqlite3").is_file():
        return SearchResult(status="not_initialized").model_dump()
    terms = [request.query.strip()] if request.match == "phrase" else list(dict.fromkeys(request.query.split()))
    terms = [x for x in terms if x]
    if len(terms) > 30:
        raise ValueError("too_many_query_terms")
    fingerprint = hashlib.sha256(encoded(request.model_dump(exclude={"cursor", "max_chars", "limit"})).encode()).hexdigest()
    with assets.connection() as conn:
        if not conn.in_transaction: conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_meta'").fetchone():
            return SearchResult(status="upgrade_required").model_dump()
        generation = conn.execute("SELECT value FROM knowledge_meta WHERE key='generation'").fetchone()[0]
        high = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM assets").fetchone()[0]
        scope_high = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM asset_scopes").fetchone()[0]
        last = None
        if request.cursor:
            try:
                token = json.loads(base64.urlsafe_b64decode(request.cursor.encode()))
                if token["query"] != fingerprint:
                    raise ValueError("cursor_stale_restart_search")
                high, scope_high, last = token["high"], token["scope_high"], token["last"]
                if type(high) is not int or type(scope_high) is not int or high < 0 or scope_high < 0 or not isinstance(last,list) or len(last)!=3:
                    raise ValueError("invalid_cursor")
            except (KeyError, TypeError, json.JSONDecodeError, UnicodeError, binascii.Error) as exc:
                raise ValueError("invalid_cursor") from exc
        where, params = ["s.withdrawn=0", "a.rowid<=?", "a.kind NOT IN ('academic_card','academic_inline','document_processing','document_receipt','github_checkpoint')"], [high]
        if not request.history:
            newer=["newer.rowid<=?", "newer.source_id=a.source_id", "newer.kind=a.kind", "(newer.captured_at>a.captured_at OR (newer.captured_at=a.captured_at AND newer.version_id>a.version_id))"]
            params.append(high)
            if request.project_id or request.task_id:
                scope=["ns.version_id=newer.version_id", "ns.rowid<=?"];params.append(scope_high)
                if request.project_id:scope.append("ns.project_id=?");params.append(request.project_id)
                if request.task_id:scope.append("ns.task_id=?");params.append(request.task_id)
                newer.append("EXISTS(SELECT 1 FROM asset_scopes ns WHERE "+" AND ".join(scope)+")")
            if request.captured_after is not None:newer.append("newer.captured_at>=?");params.append(request.captured_after)
            if request.captured_before is not None:newer.append("newer.captured_at<=?");params.append(request.captured_before)
            where.append("NOT EXISTS(SELECT 1 FROM assets newer WHERE "+" AND ".join(newer)+")")
        if request.project_id or request.task_id:
            scope = ["sc.version_id=a.version_id", "sc.rowid<=?"]; params.append(scope_high)
            if request.project_id: scope.append("sc.project_id=?"); params.append(request.project_id)
            if request.task_id: scope.append("sc.task_id=?"); params.append(request.task_id)
            where.append("EXISTS(SELECT 1 FROM asset_scopes sc WHERE " + " AND ".join(scope) + ")")
        if request.kind: where.append("a.kind=?"); params.append(request.kind)
        if request.captured_after is not None: where.append("a.captured_at>=?"); params.append(request.captured_after)
        if request.captured_before is not None: where.append("a.captured_at<=?"); params.append(request.captured_before)
        term_where = []
        for term in terms:
            if len(term) >= 3:
                term_where.append("(instr(lower(a.title),lower(?))>0 OR a.version_id IN (SELECT version_id FROM asset_fts WHERE asset_fts MATCH ?))")
                params.extend([term, '"' + term.replace('"', '""') + '"'])
            else:
                term_where.append("(instr(lower(a.title),lower(?))>0 OR instr(lower(f.body),lower(?))>0)")
                params.extend([term, term])
        if term_where: where.append("(" + (" OR " if request.match=="any" else " AND ").join(term_where) + ")")
        scores, score_params = [], []
        for term in terms:
            scores.append("CASE WHEN instr(lower(a.title),lower(?))>0 THEN 8 ELSE 0 END")
            score_params.append(term)
        scores.append("CASE WHEN ?<>'' AND instr(lower(f.body),lower(?))>0 THEN 4 ELSE 0 END")
        score_params.extend([request.query, request.query])
        sql = "SELECT a.*, ("+"+".join(scores)+") AS relevance FROM assets a JOIN sources s USING(source_id) JOIN asset_fts f ON f.version_id=a.version_id WHERE " + " AND ".join(where)
        args = [*score_params,*params]
        if last:
            sql = "SELECT * FROM ("+sql+") WHERE relevance<? OR (relevance=? AND captured_at<?) OR (relevance=? AND captured_at=? AND version_id>?)"
            args.extend([last[0],last[0],last[1],last[0],last[1],last[2]])
        sql += " ORDER BY relevance DESC,captured_at DESC,version_id LIMIT ?"
        rows = conn.execute(sql,[*args,request.limit+1]).fetchall()
        result = SearchResult(generation=generation).model_dump()
        def cursor(consumed):
            row = rows[consumed-1] if consumed else None
            key = [row["relevance"],row["captured_at"],row["version_id"]] if row else last
            return base64.urlsafe_b64encode(encoded({"query":fingerprint,"high":high,"scope_high":scope_high,"last":key}).encode()).decode()
        consumed=0
        for row in rows[:request.limit]:
            row = dict(row)
            try:
                assets.assert_available(conn,row)
                text = assets._verified_text(row)
            except ValueError as exc:
                if str(exc) not in {'version_not_found_or_withdrawn','provenance_version_missing','asset_missing','asset_integrity_mismatch'}:raise
                result['unavailable_count']+=1
                consumed+=1
                continue
            # Regex spans refer to original code points, never normalized-index offsets.
            positions = [m.start() for t in terms if (m := re.search(re.escape(t), text, re.IGNORECASE))]
            start = max(0, min(positions, default=0)-80)
            end = min(len(text), start+400)
            hit = {"asset":assets._ref(row),"excerpt":text[start:end],"start":start,"end":end,
                   "citation":f"kr:{row['version_id']}#chars={start}-{end}",
                   "match_reason":"literal_terms_and_title; excerpt_is_not_complete_source"}
            spans = []
            for position in sorted(set(m.start() for t in terms for m in re.finditer(re.escape(t),text,re.IGNORECASE))):
                left,right = max(0,position-80),min(len(text),position+240)
                if spans and left<=spans[-1][1]:
                    continue
                spans.append((left,right))
                if len(spans)>=3:break
            hit["passages"] = [{"start":left,"end":right,"text":text[left:right],"citation":f"kr:{row['version_id']}#chars={left}-{right}"} for left,right in spans]
            result["items"].append(hit)
            result.update(next_cursor=cursor(consumed+1),truncated=True)
            if len(encoded(result)) > request.max_chars:
                result["items"].pop()
                if not result["items"]: raise ValueError("budget_too_small_increase_max_chars")
                break
            consumed+=1
        more = len(rows) > consumed
        result.update(truncated=more,next_cursor=cursor(consumed) if more else None)
        if conn.execute("SELECT value FROM knowledge_meta WHERE key='generation'").fetchone()[0]!=generation:
            raise ValueError('index_changed_restart_search')
        return result


def read(request: ReadRequest, assets=None):
    assets = assets or EvidenceAssets(read_only=True)
    if isinstance(request, ResearchRead):
        from runtime.research_index import ResearchIndex
        return ResearchIndex().read(**request.model_dump(exclude={"action"}),assets=assets)
    if isinstance(request, Search):
        return search(assets, request)
    budget = request.max_chars
    size = budget
    while size > 0:
        value = assets.fetch(request.version_id, start=request.start, max_chars=size,
                             line_start=request.line_start, line_end=request.line_end)
        value.update(kind="fetch", status="ok", citation=f"kr:{request.version_id}#chars={value['locator']['start']}-{value['locator']['end']}")
        excess = len(encoded(value)) - budget
        if excess <= 0: return value
        # 一段文字可能带很多行定位；直接减整个超额会越过可行的小片段。
        size -= min(max(1, size // 2), max(1, excess))
    raise ValueError("budget_too_small_increase_max_chars")


def guarded_path(root, path, *, require_file=False):
    root, path = Path(root), Path(path)
    if not root.is_absolute() or not path.is_absolute() or not root.is_dir():
        raise ValueError("existing_absolute_authorized_root_required")
    for part in (root, *root.parents, path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("links_not_allowed")
    root, path = root.resolve(), path.resolve()
    if path == root or not path.is_relative_to(root): raise ValueError("outside_authorized_root")
    if require_file and not path.is_file(): raise ValueError("input_missing")
    if str(path).startswith("\\\\"): raise ValueError("network_path_not_supported")
    return path


def register_report(*, assets, input_path, approved_input_root, input_sha256, project_id, task_id, report_id, title, derived_from):
    if not project_id or not task_id or not report_id: raise ValueError("project_task_report_identity_required")
    path = guarded_path(approved_input_root, input_path, require_file=True)
    if path.suffix.lower() not in {".md", ".txt"}: raise ValueError("report_text_required")
    with path.open("rb") as handle:
        raw = handle.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: raise ValueError("report_too_large")
    if hashlib.sha256(raw).hexdigest()!=input_sha256: raise ValueError("input_changed")
    for version in derived_from: assets.fetch(version, max_chars=1)
    identity = hashlib.sha256(encoded([project_id, task_id, report_id]).encode()).hexdigest()
    # Provenance changes create a new report version even when prose is unchanged.
    provenance = hashlib.sha256(encoded(sorted(set(derived_from))).encode()).hexdigest()
    ref = assets.put("kr-report:"+identity,raw.decode("utf-8"),kind="report",title=title,
        metadata={"project_id":project_id,"task_id":task_id,"report_id":report_id,"input_sha256":input_sha256,"derived_from":sorted(set(derived_from)),"coverage":"registered_report_text_only","extractor":"report-registration/v1:"+provenance})
    assets.reference_many([ref["version_id"],*derived_from],task_id,project_id=project_id,task_id=task_id)
    return {"status":"registered","asset":ref,"input_sha256":input_sha256,"evidence_assets":[ref]}
