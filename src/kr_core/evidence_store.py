"""Append-only evidence repository for search and detail reuse."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .models import utc_now_iso
from runtime.paths import runtime_log_dir


def default_evidence_store_path() -> str:
    return os.path.join(str(runtime_log_dir()), "knowledgeradar-evidence-store.jsonl")


@dataclass(frozen=True)
class EvidenceRecord:
    record_type: str
    platform: str
    url: str
    payload: Dict[str, Any]
    query: str = ""
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        from kr_core.source_urls import canonical_url, public_evidence_payload
        url = canonical_url(self.url)
        key_source = f"{self.record_type}|{self.platform}|{url}|{self.query}"
        return {
            "schema": "knowledgeradar-evidence-store/v1",
            "id": hashlib.sha256(key_source.encode("utf-8", errors="ignore")).hexdigest()[:24],
            "record_type": self.record_type,
            "platform": self.platform,
            "url": url,
            "query": self.query,
            "timestamp": self.timestamp,
            "payload": public_evidence_payload(self.payload),
        }


class EvidenceStore:
    def __init__(self, path: str | None = None) -> None:
        from .evidence_assets import EvidenceAssets
        self.path = path or default_evidence_store_path()
        self.assets = EvidenceAssets(os.path.join(os.path.dirname(path), 'evidence_assets') if path else None)

    def append(self, record: EvidenceRecord) -> bool:
        from runtime.file_write_lock import file_write_lock
        raw = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with file_write_lock(self.path), open(self.path, "a", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        return True

    def append_search(self, *, platform: str, query: str, response: Dict[str, Any]) -> None:
        trace = _current_trace()
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            # Discovery keeps a compact pointer; full acquired bodies are immutable assets.
            compact = {key:item[key] for key in ('title','url','source_provider','published_at','retrieved_at','score') if key in item}
            compact['snippet'] = str(item.get('snippet') or item.get('description') or '')[:800]
            raw = item.get('raw') or {}
            compact['provenance'] = {key:raw[key] for key in ('provider_provenance','upstream_engine','engines','source_independence') if key in raw}
            if isinstance(item.get('content_entrypoints'), dict):
                compact['content_entrypoints'] = item['content_entrypoints']
            body = raw.get('raw_content') or raw.get('text')
            if isinstance(body, str) and body.strip():
                compact['evidence_assets'] = [self.assets.put(str(item.get('url') or ''), body, kind='content',
                    title=str(item.get('title') or ''), metadata={'extractor':str(item.get('source_provider') or platform),
                    'source_platform':platform, 'coverage':'provider_supplied_unknown', 'original_captured_at':item.get('retrieved_at') or utc_now_iso()})]
            self.append(EvidenceRecord("search_result", platform, str(item.get("url") or ""), _with_trace(compact, trace), query=query))

    def append_detail(self, *, platform: str, url: str, response: Dict[str, Any]) -> None:
        from runtime.execution_context import check_execution
        check_execution()
        from .evidence_coverage import content_coverage
        from .evidence_locators import document_text, transcript_locators
        from .source_urls import canonical_url
        asset_url = canonical_url(str(response.get('evidence_source_url') or url))
        if isinstance(response.get('document_blocks'), list):
            body, locations = document_text(response['document_blocks'])
            if body:
                response['content'] = body
                response['content_locators'] = locations
        response.setdefault('content_coverage', content_coverage(response))
        parsed_coverage = (response.get('metadata') or {}).get('coverage')
        if parsed_coverage in {'complete_selectable_text', 'complete_text_and_ocr', 'partial'}:
            response['content_coverage']['parts']['content']['coverage'] = parsed_coverage
        refs = []
        content_value = response.get("content")
        video_description = response.get("video_description")
        # 视频采集器历史上把简介放进 content。新合同保留兼容读取，
        # 但固定资产只以 video_description 归档，避免 Obsidian 把简介标成正文。
        skip_legacy_video_content = (
            isinstance(video_description, str) and video_description.strip()
            and isinstance(content_value, str)
            and content_value.strip() == video_description.strip()
            and str(platform).lower() in {"b站", "bilibili", "youtube"}
        )
        parts = ("video_description", "transcript", "ocr_text") if skip_legacy_video_content else ("content", "video_description", "transcript", "ocr_text")
        for kind in parts:
            value = response.get(kind)
            if isinstance(value, str) and value.strip():
                if response.get("evidence_storage_allowed") is False:
                    response.setdefault("evidence_storage_gaps", []).append({"part": kind, "reason": "storage_not_permitted"})
                    continue
                try:
                    coverage_parts = (response.get('content_coverage') or {}).get('parts') or {}
                    part = coverage_parts.get('ocr' if kind == 'ocr_text' else kind) or {}
                    metadata = {"extractor": str((response.get("detail_metadata") or {}).get("strategy") or response.get("collector") or platform), "coverage": str(part.get('coverage') or 'unknown'),
                        'source_platform':platform,'content_format':response.get('content_format','text'),
                        'original_captured_at':response.get('fetched_at') or response.get('captured_at') or utc_now_iso(),
                        **{key:response[key] for key in ('display_title_zh','title_zh','display_title','language','source_presentation','site_name','processing_method') if response.get(key)}}
                    if kind == 'video_description':
                        metadata.update(coverage='metadata_description', source_layer='video_metadata')
                    if kind == 'transcript':
                        transcript_meta=response.get('transcript_metadata') or {}
                        metadata.update(transcript_source=transcript_meta.get('source',''),
                            language=transcript_meta.get('language') or metadata.get('language',''))
                    processing = response.get('metadata') or response
                    if isinstance(response.get('provenance'), dict):
                        metadata['provenance'] = dict(response['provenance'])
                        if response['provenance'].get('processing_key'):
                            metadata['evidence_identity'] = response['provenance']['processing_key']
                    if processing.get('ocr_status'):
                        metadata['provenance'] = {**metadata.get('provenance', {}), 'pdf_processing':{k:processing.get(k) for k in
                            ('processing_method','ocr_status','ocr_pages','missing_text_pages','ocr_errors','ocr_model')}}
                        metadata['processing_method'] = processing.get('processing_method','pypdf')
                    if response.get('input_sha256'):
                        metadata['input_sha256'] = response['input_sha256']
                    if kind == 'ocr_text' and isinstance(response.get('ocr_provenance'), dict):
                        metadata['provenance'] = {**metadata.get('provenance', {}), **response['ocr_provenance']}
                    locations = response.get(kind + '_locators')
                    if kind == 'transcript':
                        mapping = response.get('transcript_metadata') or {}
                        if isinstance(mapping.get('locators'), dict):
                            mapping = mapping['locators']
                        locations = mapping.get('segments') if mapping.get('status') == 'aligned' else None
                        if locations is None:
                            mapped = transcript_locators(value, mapping.get('segments'))
                            if mapped['status'] == 'aligned':
                                locations = mapped['segments']
                    if isinstance(locations, list):
                        metadata['locators'] = locations
                    refs.append(self.assets.put(asset_url, value, kind=kind, title=str(response.get("title") or ""), metadata=metadata))
                    if kind == "transcript" and isinstance(response.get("transcript_metadata"), dict):
                        refs.append(self.assets.put(url, json.dumps(response["transcript_metadata"], ensure_ascii=False), kind="transcript_map", metadata={"extractor": platform, "coverage": "segment_mapping_only"}))
                except (OSError, ValueError) as exc:
                    response.setdefault("evidence_storage_gaps", []).append({"part": kind, "reason": str(exc)})
        if response.get('evidence_storage_allowed') is not False:
            if isinstance(response.get('document'), dict) and response['document'].get('page_ledger'):
                try:
                    refs.append(self.assets.put(asset_url, json.dumps(response['document'], ensure_ascii=False),
                        kind='document_receipt', title=str(response.get('title') or ''),
                        metadata={'extractor':'common-pdf-receipt', 'coverage':'processing_facts_not_semantic_quality'}))
                except (OSError, ValueError) as exc:
                    response.setdefault('evidence_storage_gaps', []).append({'part':'document_receipt', 'reason':str(exc)})
            for part in ('comments', 'images', 'tables', 'media_manifest', 'media_ledger', 'comment_media_ledger', 'answer_directory'):
                entries = response.get(part)
                if not isinstance(entries, list) or not entries:
                    continue
                try:
                    # 原始结构另存固定资产；URL 清单绝不冒充媒体二进制。
                    from kr_core.source_urls import public_evidence_payload, public_detail_payload
                    blocks = []
                    for index, entry in enumerate(entries):
                        value = entry if isinstance(entry, dict) else {'value': entry}
                        if part == 'media_ledger':
                            # 原件路径属于本机缓存；固定账本只保存身份、结果和定位。
                            value = {key:val for key,val in value.items() if key != 'path'}
                        safe = public_evidence_payload(value)
                        if part == 'comments':
                            safe = public_detail_payload({**response, 'content': '', 'comments': [value]})['comments'][0]
                        block = {'text': json.dumps(safe, ensure_ascii=False), 'kind': part}
                        if part == 'comments':
                            block['comment_id'] = str(value.get('comment_id') or value.get('id') or index)
                            parent = value.get('parent_comment_id', value.get('in_reply_to_id'))
                            if parent is not None:
                                block['parent_comment_id'] = str(parent)
                        if part == 'images':
                            block['image_index'] = index
                        blocks.append(block)
                    text, locations = document_text(blocks)
                    refs.append(self.assets.put(asset_url, text, kind=part, title=str(response.get('title') or ''), metadata={'extractor': platform, 'source_platform':platform,
                        'provenance':{**(response.get('provenance') or {}), **({'comments_metadata':response['comments_metadata']} if part=='comments' and response.get('comments_metadata') else {})},
                        'original_captured_at':response.get('fetched_at') or response.get('captured_at') or utc_now_iso(),
                        **{key:response[key] for key in ('display_title_zh','title_zh','display_title','language','source_presentation','site_name','processing_method') if response.get(key)},
                        'coverage': 'manifest_only' if part in ('images','media_manifest') else response.get('comments_coverage','partial') if part == 'comments' else 'partial', 'locators': locations}))
                except (OSError, ValueError) as exc:
                    response.setdefault('evidence_storage_gaps', []).append({'part': part, 'reason': str(exc)})
        response["evidence_assets"] = refs
        response.setdefault("content_coverage", content_coverage(response))
        self.append(EvidenceRecord("detail", platform, url, _with_trace(_compact_detail(response), _current_trace())))

    def record_detail(self, *, platform: str, url: str, response: Dict[str, Any]) -> None:
        """用例侧提交：存储失败保留已采集结果和确切缺口，取消仍向上传递。"""
        from runtime.execution_context import ExecutionStopped
        try:
            self.append_detail(platform=platform, url=url, response=response)
        except ExecutionStopped:
            raise
        except Exception as exc:
            response.setdefault('evidence_storage_gaps', []).append({
                'part': 'repository', 'reason': type(exc).__name__})

    def append_academic_search(self, *, query: str, response: Dict[str, Any]) -> None:
        trace = _current_trace()
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            raw = item.get('raw') if isinstance(item.get('raw'), dict) else {}
            resolution = raw.get('fulltext_resolution') if isinstance(raw.get('fulltext_resolution'), dict) else {}
            body = resolution.get('text') or raw.get('fulltext_text')
            if isinstance(body, str) and body.strip():
                try:
                    if item.get('evidence_storage_allowed') is False:
                        raw.pop('fulltext_text', None)
                        resolution.pop('text', None)
                        raw.pop('fulltext_sample', None)
                        resolution.pop('sample', None)
                        item.pop('evidence_excerpts', None)
                        raise ValueError('storage_not_permitted')
                    provenance = resolution.get('provenance') or {}
                    ref = self.assets.put(str(provenance.get('resolved_url') or item.get('url') or ''), body, kind='document_text',
                        title=str(item.get('title') or ''), metadata={
                            'extractor': resolution.get('mode') or 'academic_api_text',
                            'coverage': resolution.get('coverage') or 'partial',
                            'provenance': provenance,
                            'source_platform':'academic',
                            'original_captured_at':resolution.get('fetched_at') or utc_now_iso(),
                            **{key:item[key] for key in ('display_title_zh','title_zh','display_title','language','source_presentation','site_name','processing_method') if item.get(key)},
                            **({'locators': resolution['locators']} if resolution.get('locators') else {}),
                        })
                    owner = str(trace.get('research_task_id') or trace.get('trace_id') or 'academic:' + ref['source_id'])
                    self.assets.reference(ref['version_id'], owner)
                    item.setdefault('evidence_assets', []).append(ref)
                    item['evidence_excerpts'] = _academic_excerpts(body, resolution.get('locators'), query)
                    # 正文保存在可回读资产；默认响应只带短样本，避免全篇灌入上下文。
                    raw.pop('fulltext_text', None)
                    resolution.pop('text', None)
                    raw['fulltext_sample'] = body[:500]
                    from .evidence_coverage import content_coverage
                    item['content_coverage'] = content_coverage({'content': body, 'evidence_assets': [ref]})
                except (OSError, ValueError) as exc:
                    reason = 'storage_not_permitted' if item.get('evidence_storage_allowed') is False else type(exc).__name__
                    item.setdefault('evidence_storage_gaps', []).append({'part': 'document_text', 'reason': reason})
                    response.setdefault('evidence_storage_gaps', []).append({'part': 'document_text', 'reason': reason})
            # 资产写入失败也不绕过资产容量/权限，另把整篇写进 JSONL。
            import copy
            stored = copy.deepcopy(item)
            stored_raw = stored.get('raw') or {}
            stored_raw.pop('fulltext_text', None)
            if isinstance(stored_raw.get('fulltext_resolution'), dict):
                stored_raw['fulltext_resolution'].pop('text', None)
            self.append(EvidenceRecord("academic_search_result", "academic", str(item.get("url") or item.get("doi") or ""), _with_trace(stored, trace), query=query))

    def academic_recent_summary(self, limit: int = 10) -> Dict[str, Any]:
        rows = [row for row in self.recent(max(limit * 4, 20)) if row.get("record_type") == "academic_search_result"]
        rows = rows[-max(1, min(limit, 50)):]
        return {
            "status": "ok",
            "count": len(rows),
            "recent": [
                {
                    "query": row.get("query", ""),
                    "url": row.get("url", ""),
                    "title": ((row.get("payload") or {}).get("title") or ""),
                    "doi": ((row.get("payload") or {}).get("doi") or ""),
                    "source": ((row.get("payload") or {}).get("source") or ""),
                    "retrieved_at": ((row.get("payload") or {}).get("retrieved_at") or row.get("timestamp", "")),
                }
                for row in rows
            ],
        }

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, min(limit, 500)):]
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def health(self) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("")
            return {"status": "ok", "path": self.path, "schema": "knowledgeradar-evidence-store/v1"}
        except Exception as exc:
            return {"status": "degraded", "path": self.path, "detail": str(exc)}


def _compact_detail(response: Dict[str, Any]) -> Dict[str, Any]:
    keep = {
        "platform",
        "url",
        "title",
        "desc",
        "content",
        "transcript",
        "author",
        "routing_reason",
        "recommended_next_action",
        "evidence",
        "detail_metadata",
        "evidence_assets",
        "content_coverage",
        "evidence_storage_gaps",
    }
    data = {key: response.get(key) for key in keep if key in response}
    if isinstance(data.get("content"), str):
        data["content"] = data["content"][:4000]
    if isinstance(data.get("transcript"), str):
        data["transcript"] = data["transcript"][:4000]
    if response.get("evidence_storage_allowed") is False:
        for part in ("content", "transcript"):
            data.pop(part, None)
    return data


def _academic_excerpts(text, locations, query):
    """基础词面选段，保留固定原文偏移；语义取舍仍由 Codex 完成。"""
    from academic_providers.relevance import _query_terms
    from .evidence_locators import paragraph_locators, validate_locators
    locators = validate_locators(text, locations) if locations else paragraph_locators(text)
    terms = _query_terms(query)
    scored = sorted(enumerate(locators), key=lambda row: (
        -sum(term in text[row[1]['start']:row[1]['end']].lower() for term in terms), row[0]))
    excerpts, remaining = [], 1400
    for _, location in scored[:3]:
        end = min(location['end'], location['start'] + min(remaining, 700))
        if end <= location['start']:
            break
        excerpts.append({'text': text[location['start']:end], 'locator': {**location, 'end': end}})
        remaining -= end - location['start']
    return sorted(excerpts, key=lambda row: row['locator']['start'])


def _current_trace() -> Dict[str, Any]:
    try:
        from runtime.tool_trace import current_tool_trace

        trace = current_tool_trace()
        return trace if isinstance(trace, dict) else {}
    except Exception:
        return {}


def _with_trace(payload: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    if not trace:
        return payload
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        **payload,
        "metadata": {
            **metadata,
            "trace_id": trace.get("trace_id", ""),
            "trace_tool": trace.get("tool_name", ""),
            "trace_strategy": trace.get("strategy", ""),
        },
    }
