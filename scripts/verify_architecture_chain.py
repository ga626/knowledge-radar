"""隔离候选的评论/PDF验收；真实来源、单owner、固定证据回读，不安装或导出账号。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def document_request_satisfied(result):
    """按各公开入口的实际合同验收，不把顶层 coverage 缺键误作缺页。"""
    document = result.get('document') or result
    return (
        not result.get('error')
        and document.get('request_satisfied') is True
        and bool(document.get('page_ledger'))
        and not (document.get('resume') or {}).get('pending_pages')
        and not (document.get('resume') or {}).get('unknown_pages')
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', default='')
    parser.add_argument('--document-url', default='')
    parser.add_argument('--academic-document-url', default='', help='给定真实公开论文URL，经手工导入文献卡和独立学术全文入口验证；不冒充学术发现验收')
    parser.add_argument('--input-pdf', type=Path)
    parser.add_argument('--ocr', action='store_true', help='explicitly use the already authorized cloud OCR configuration')
    parser.add_argument('--order',choices=['time','relevance'],default='time')
    args = parser.parse_args()
    if sum(map(bool, (args.url, args.document_url, args.academic_document_url, args.input_pdf))) != 1:
        parser.error('choose exactly one --url, --document-url, --academic-document-url or --input-pdf')
    if args.ocr and args.url:
        parser.error('--ocr is only for document verification')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for key, part in {'KR_DATA_ROOT':'data', 'KR_STATE_DIR':'state', 'KR_LOG_DIR':'logs',
        'KR_EVIDENCE_ROOT':'assets', 'KR_MEDIA_CACHE_DIR':'media', 'KR_RUNTIME_LEASE_DB_PATH':'leases.sqlite3',
        'KR_TASK_DB_PATH':'tasks.sqlite3', 'KR_OWNER_ROOT':'owner', 'KR_PROFILE_REGISTRY_PATH':'absent-profile.json',
        'KR_RUNTIME_ENV_PATH':'absent.env'}.items():
        os.environ[key] = str(output / part)
    os.environ.update(KR_DESKTOP_NOTIFICATIONS_DISABLED='1', KR_DISABLE_MODEL_PREWARM='1')
    os.environ.pop('KR_ACTIVE_INSTALL_ROOT',None)
    os.environ.pop('KR_RUNTIME_ROLE',None)
    from runtime.execution_owner import OwnerClient, source_identity
    from kr_core.evidence_assets import EvidenceAssets
    from generic_web.url_policy import public_evidence_payload
    owner = OwnerClient(timeout=200)
    receipt = {'access_path':'candidate_owner_not_native_or_installed', 'source_identity':source_identity(),
               'readbacks':[], 'installation_changed':False}
    passed = False
    try:
        started = owner.start()
        if started.get('status') != 'ready':
            raise RuntimeError('candidate_owner_not_ready')
        if args.input_pdf:
            path = args.input_pdf.resolve(strict=True)
            arguments = {'input_path':str(path), 'approved_input_root':str(path.parent),
                'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'component':'pypdf',
                'document':{'include_ocr':args.ocr}, 'timeout_s':120}
            tool = 'process_local_evidence'
        elif args.document_url or args.academic_document_url:
            target=args.document_url or args.academic_document_url
            arguments = {'url':target, 'include_comments':False, 'include_transcript':False,
                'include_ocr':args.ocr, 'document':{'include_ocr':args.ocr}, 'deadline_s':180}
            if args.academic_document_url:
                from academic_providers.models import AcademicWork
                from academic_providers.identity import save_work
                card=save_work(AcademicWork('Provided public paper URL', target,
                    source_database='citation_import', source='explicit_pipeline_sample',
                    verification_status='provided_url_not_discovery_result',
                    raw={'fulltext_candidate_urls':[target]}))
                arguments['academic_reference']=card['version_id']
                receipt['metadata_origin']='provided_public_paper_url; not_a_discovery_test'
            tool = 'get_content_detail'
        else:
            arguments = {'url':args.url, 'include_comments':True, 'include_transcript':False,
                'include_ocr':False, 'comments':{'root_limit':3,'reply_limit':2,'total_limit':12,'max_pages':5,
                    'include_images':True,'media_limit':3,'order':args.order}, 'deadline_s':180}
            tool = 'get_content_detail'
        result = owner.call(tool, arguments)
        receipt.update(tool=tool, arguments=public_evidence_payload(arguments))
        task_id = result.get('task_id') or (result.get('task') or {}).get('task_id')
        if task_id:
            deadline = time.monotonic()+210
            while time.monotonic() < deadline:
                status = owner.call('get_task_status', {'task_id':task_id})
                if status.get('status') not in {'queued','running'}:
                    result = status.get('result') or status
                    receipt.update(task_id=task_id, task_status=status.get('status'))
                    break
                time.sleep(.25)
        safe = public_evidence_payload(result)
        (output/'result.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2),encoding='utf-8')
        for ref in result.get('evidence_assets') or []:
            part = EvidenceAssets(read_only=True).fetch(ref['version_id'],max_chars=24000)
            text = part.get('text','')
            while part.get('next_start') is not None:
                part = EvidenceAssets(read_only=True).fetch(ref['version_id'],start=part['next_start'],max_chars=24000)
                text += part.get('text','')
            receipt['readbacks'].append({'kind':ref['kind'],'version_id':ref['version_id'],
                'chars':len(text),'sha256_matches':hashlib.sha256(text.encode()).hexdigest()==ref['raw_sha256']})
        receipt.update(status=safe.get('status'),error=safe.get('error'),failure_type=safe.get('failure_type'),
            comment_count=len(result.get('comments') or []),comments_metadata=safe.get('comments_metadata'),
            comment_media_ledger=safe.get('comment_media_ledger'),content_coverage=safe.get('content_coverage'),
            document=safe.get('document') or {key:safe[key] for key in ('page_ledger','input_sha256','coverage') if key in safe})
        passed = bool(receipt['readbacks']) and all(row['sha256_matches'] for row in receipt['readbacks'])
        if args.url:
            passed = passed and any(row['kind']=='comments' for row in receipt['readbacks']) and result.get('comments_complete') is True
            passed = passed and not (result.get('comments_metadata') or {}).get('media_gaps')
        else:
            passed = passed and document_request_satisfied(result)
    except Exception as exc:
        receipt['verification_failure'] = type(exc).__name__
    finally:
        receipt['owner_stop'] = owner.stop()
        receipt['verdict'] = 'PASS' if passed else 'INCOMPLETE'
        (output/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
