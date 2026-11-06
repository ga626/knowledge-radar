"""从固定书目卡接单；MCP、CLI和搜索兼容快捷方式共享同一实现。"""
import math
from dataclasses import fields


def request_academic_fulltext(reference, *, research_task_id='', timeout_s=600, include_ocr=None, url='', refresh=False, document=None):
    from academic_providers.identity import load_work
    from runtime.task_service import TaskService
    from runtime.execution_context import check_execution
    check_execution()
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 1 <= timeout_s <= 3600:
        raise ValueError('academic_deadline_must_be_1_to_3600_seconds')
    from runtime.read_options import DocumentReadOptions, normalized_options
    document=normalized_options(DocumentReadOptions,document)
    if document.get('include_ocr') is not None:
        include_ocr=document['include_ocr']
    work = load_work(reference)
    if work.raw.get('read_allowed') is False or work.raw.get('download_allowed') is False:
        raise ValueError('academic_fulltext_permission_denied; metadata remains readable')
    if work.raw.get('evidence_storage_allowed') is False:
        raise ValueError('academic_persistent_fulltext_not_authorized; metadata remains readable')
    if work.raw.get('cloud_processing_allowed') is False:
        include_ocr = False
    if url and url != work.url:
        raise ValueError('academic_reference_url_mismatch')
    if include_ocr is not None and not isinstance(include_ocr, bool):
        raise ValueError('include_ocr_must_be_boolean')
    version = reference['version_id'] if isinstance(reference, dict) else reference
    import time
    import uuid
    from runtime.application import current_request_id
    refresh_request = (current_request_id() or uuid.uuid4().hex) if refresh else ''
    return TaskService().submit('academic_fulltext', {'work_reference': version,
        'timeout_s': timeout_s, 'include_ocr': include_ocr, 'document':document, 'refresh':refresh,
        'verification_epoch':int(time.time()//86400), 'refresh_request_id':refresh_request}, timeout_s=timeout_s,
        billing_scope_id='academic-'+version,
        scope={'research_task_id': research_task_id, 'source_ecology': 'academic_literature_ecology'})


def execute(payload):
    """同篇跨研究处理互斥，先复用公共固定证据，研究引用仍各自记录。"""
    import os
    import time
    import uuid
    from runtime.task_service import content_hash, source_identity
    from runtime.pdf_text import PDF_PROCESSING_CONTRACT
    from runtime.model_roles import CLOUD_OCR_MODEL
    from runtime.execution_context import check_execution
    from runtime.leases import get_runtime_lease_coordinator, default_owner
    # 缓存复用和排队恢复都必须重新核固定卡片；已撤回来源不能凭旧正文绕过。
    from academic_providers.identity import load_work
    if payload.get('work_reference'):
        work = load_work(payload['work_reference'])
        if work.raw.get('read_allowed') is False or work.raw.get('download_allowed') is False:
            raise ValueError('academic_fulltext_permission_denied; metadata remains readable')
        if work.raw.get('evidence_storage_allowed') is False:
            raise ValueError('academic_persistent_fulltext_not_authorized; metadata remains readable')
        if work.raw.get('cloud_processing_allowed') is False:
            payload = {**payload, 'include_ocr': False,
                       'document': {**payload.get('document', {}), 'include_ocr': False}}
    key = content_hash({'reference':payload.get('work_reference') or payload.get('work'),
        'source':source_identity(), 'pdf_contract':PDF_PROCESSING_CONTRACT,
        'document':payload.get('document',{}), 'ocr':payload.get('include_ocr'), 'ocr_model':CLOUD_OCR_MODEL,
        'ocr_configured':bool(os.environ.get('SILICONFLOW_API_KEY','').strip())})
    coordinator = get_runtime_lease_coordinator()
    owner = default_owner('academic_fulltext', request_id=uuid.uuid4().hex)
    deadline = time.monotonic()+float(payload.get('timeout_s',600))
    while True:
        check_execution()
        if time.monotonic() >= deadline:
            raise TimeoutError('academic_processing_wait_deadline')
        lease = coordinator.acquire_exclusive('academic_processing', key, owner=owner,
                                              ttl_s=int(payload.get('timeout_s',600))+15)
        if lease.acquired:
            break
        time.sleep(.1)
    try:
        if not payload.get('refresh'):
            reused = _reuse(key)
            if reused:
                from kr_core.evidence_store import EvidenceStore
                EvidenceStore().record_detail(platform='学术',url=reused['url'],response=reused)
                return reused
        return _execute({**payload, 'timeout_s':max(.1, deadline-time.monotonic())}, key)
    finally:
        coordinator.release(lease.lease_id)


def _reuse(key):
    import json
    import time
    from kr_core.evidence_assets import EvidenceAssets
    assets = EvidenceAssets()
    with assets.connection() as conn:
        row = conn.execute("SELECT a.* FROM assets a JOIN sources s USING(source_id) WHERE s.withdrawn=0 AND a.kind='content' AND json_extract(a.metadata,'$.provenance.processing_key')=? ORDER BY a.captured_at DESC LIMIT 1", (key,)).fetchone()
    if not row:
        return None
    row = dict(row)
    # 已知版本也保留一天的核验周期；缓存不是永久真实性保证。
    if time.time()-row['captured_at'] > 86400:
        return None
    meta = json.loads(row['metadata'])
    processing = meta['provenance'].get('pdf_processing') or {}
    if processing.get('ocr_status') in {'blocked','partial'}:
        return None
    if processing.get('missing_text_pages'):
        return None
    part = assets.fetch(row['version_id'],max_chars=24000)
    text = part['text']
    while part['next_start'] is not None:
        part = assets.fetch(row['version_id'],start=part['next_start'],max_chars=24000)
        text += part['text']
    return {'url':row['source_url'], 'title':row['title'], 'content':text,
        'content_format':meta.get('content_format','text'), 'collector':meta['extractor'],
        'content_locators':meta['locators'], 'provenance':meta['provenance'],
        'source_presentation':meta.get('source_presentation',{'category':'学术'}),
        'input_sha256':meta.get('input_sha256',''), 'metadata':processing, 'document':processing,
        'content_coverage':{'parts':{'content':{'coverage':meta['coverage']}}},
        'fulltext_status':'REUSED', 'fetched_at':meta.get('original_captured_at'),
        'reuse':{'version_id':row['version_id'], 'captured_at':row['captured_at'], 'network_reverified':False}}


def _execute(payload, processing_key):
    from runtime.execution_context import check_execution
    from academic_providers.fulltext import extract_academic_fulltext
    from academic_providers.models import AcademicWork
    from academic_providers.identity import load_work, work_identity
    from kr_core.evidence_store import EvidenceStore
    check_execution()
    if payload.get('work_reference'):
        work = load_work(payload['work_reference'])
    else:  # 已接受的旧任务允许恢复；新接单只接受固定卡片。
        work = AcademicWork(**{k:v for k,v in payload['work'].items() if k in {f.name for f in fields(AcademicWork)}})
    if work.raw.get('evidence_storage_allowed') is False:
        raise ValueError('academic_persistent_fulltext_not_authorized; metadata remains readable')
    work.raw['document_options'] = payload.get('document') or {}
    if (payload.get('document') or {}).get('page_numbers'):
        work.raw['requested_pdf_pages']=payload['document']['page_numbers']
    provider = work.source_database or payload.get('provider') or work.source
    result = extract_academic_fulltext(provider, work.url, work=work,
        timeout_s=float(payload.get('timeout_s', 600)), include_ocr=payload.get('include_ocr'))
    check_execution()
    academic = {**work_identity(work), 'doi':work.doi, 'version':work.version,
                'license':work.license, 'license_scope':work.license_scope,
                'document_type':work.document_type, 'provider_id':provider,
                'bibliographic_source_url':work.url, 'verification_status':work.verification_status,
                'semantic_completeness':'not_asserted', 'retraction':work.raw.get('is_retracted', 'unknown')}
    provenance = {**result.provenance, 'academic':academic, 'processing_key':processing_key}
    detail = {'url':work.url, 'title':work.title, 'content':result.text,
              'content_format':'text', 'collector':result.mode, 'content_locators':result.locators,
              'content_coverage':{'parts':{'content':{'coverage':result.coverage}}},
              'source_presentation':{'category':'学术','site_name':provider},
              'processing_method':result.mode, 'fulltext_status':result.status,
              'provenance':provenance, 'academic':academic,
              'input_sha256':provenance.get('input_sha256', ''),
              'evidence_storage_allowed':work.raw.get('evidence_storage_allowed', True),
              'metadata':provenance.get('pdf_processing') or {},
              'document':provenance.get('pdf_processing') or {},
              'work_reference':payload.get('work_reference', '')}
    if not result.text:
        detail['error'] = {'type':'fulltext_unavailable','message':result.degraded_reason,
                           'next_action':'Choose another authorized fulltext link; search metadata remains usable.'}
    else:
        missing_pages = (provenance.get('pdf_processing') or {}).get('missing_text_pages') or []
        if missing_pages:
            detail['content_coverage']['required_gaps'] = [{'part':'content', 'reason':'pdf_pages_unread', 'pages':missing_pages}]
        EvidenceStore().record_detail(platform='学术', url=work.url, response=detail)
    return detail
