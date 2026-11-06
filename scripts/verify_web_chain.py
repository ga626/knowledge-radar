"""候选源码的有限真实网页验收；不安装、不调用新增付费账号、不回显凭据。"""
from pathlib import Path
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runtime-env',type=Path,required=True)
    parser.add_argument('--live',action='store_true',help='显式运行公开站点及已授权 AnySearch/DDGS 小样本')
    parser.add_argument('--group', choices=['all','ddgs','pages','academic','host-register','host-submit','searx-audit','post','pdf','transport'], default='all')
    parser.add_argument('--host-candidates',type=Path)
    parser.add_argument('--host-outcome',choices=['results','no_results','unavailable'], default='results')
    parser.add_argument('--pdf-url')
    parser.add_argument('--academic-source', default='arxiv', choices=['arxiv','openalex','crossref','europepmc','chinaxiv','nssd','napstic_oa','sciengine','pubscholar'])
    parser.add_argument('--academic-query', default='1706.03762v7')
    parser.add_argument('--academic-discovery-only',action='store_true')
    parser.add_argument('--academic-open-access',action='store_true')
    parser.add_argument('--academic-index',type=int,default=0)
    parser.add_argument('--acquired-pdf',type=Path,help='Process bytes already obtained by the validated acquisition probe; report this composed path explicitly.')
    parser.add_argument('--transport-url',default='https://www.w3.org/WAI/WCAG22/working-examples/pdf-ocr/ocr-example.pdf')
    args=parser.parse_args()
    if not args.live:
        parser.error('this receipt requires explicit --live')
    args.output.mkdir(parents=True,exist_ok=True)
    for key, path in {'KR_STATE_DIR':'state','KR_LOG_DIR':'logs','KR_EVIDENCE_ROOT':'assets',
                       'KR_RUNTIME_LEASE_DB_PATH':'leases.sqlite3','KR_OWNER_ROOT':'owner'}.items():
        os.environ[key]=str(args.output/path)
    os.environ.update(KR_RUNTIME_ENV_PATH=str(args.runtime_env),KR_DISABLE_MODEL_PREWARM='1',
                      KR_DESKTOP_NOTIFICATIONS_DISABLED='1', KR_PROFILE_REGISTRY_PATH=str(args.output/'absent-profiles.json'),
                      KR_SEARCH_POLICY=json.dumps({'providers':{'ddgs':{'enabled':True,'authorized':True},
                          'tavily':{'enabled':False,'authorized':False}, 'brave':{'enabled':False,'authorized':False},
                          'exa':{'enabled':False,'authorized':False}}}))
    from runtime import composition  # noqa: F401
    from runtime.application import invoke, catalog
    from runtime.execution_owner import source_identity
    from kr_core.evidence_assets import EvidenceAssets
    from generic_web.browser_runtime import close
    cases=[]
    def call(label,tool,arguments):
        started=time.perf_counter()
        result=invoke(tool,arguments)
        (args.output/(label+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        error=result.get('error')
        refs=result.get('evidence_assets') or []
        readback=[]
        for ref in refs:
            saved=EvidenceAssets(read_only=True).fetch(ref['version_id'],max_chars=2000)
            readback.append({'version_id':ref['version_id'],'text_chars':len(saved.get('text',''))})
        row={'case':label,'tool':tool,'elapsed_s':round(time.perf_counter()-started,3),
             'error':error,'total':result.get('total'),'content_chars':len(result.get('content') or ''),
             'readback':readback,'provider':result.get('provider'),'metadata':result.get('metadata',{})}
        cases.append(row)
        print(json.dumps({k:row[k] for k in ('case','elapsed_s','error','total','content_chars')},ensure_ascii=False),flush=True)
        return result
    try:
        if args.group == 'transport':
            import httpx
            from generic_web.url_policy import guarded_fetch, public_addresses
            from runtime.proxy_config import get_httpx_proxy
            url=args.transport_url
            for route in ('validated_ip','original_hostname'):
                try:
                    if route == 'validated_ip':
                        response=guarded_fetch(url,timeout=30,max_bytes=4*1024*1024)
                    else:
                        with httpx.Client(proxy=get_httpx_proxy(),trust_env=False,follow_redirects=False,timeout=30) as client:
                            response=client.get(url)
                    (args.output/(route+'.pdf')).write_bytes(response.content)
                    cases.append({'case':route,'url':url,'error':None,'status':response.status_code,'bytes':len(response.content)})
                except Exception as exc:
                    cases.append({'case':route,'error':type(exc).__name__})
            print(json.dumps(cases,ensure_ascii=False),flush=True)
        if args.group == 'post':
            from academic_providers.chinaxiv import ChinaXivProvider
            from academic_providers.models import AcademicSearchRequest
            from generic_web.url_policy import guarded_fetch
            homepage_url=ChinaXivProvider.config.homepage
            documentation = guarded_fetch(homepage_url, timeout=25, max_bytes=2*1024*1024)
            (args.output/'official-search-form.html').write_bytes(documentation.content)
            from bs4 import BeautifulSoup
            form = BeautifulSoup(documentation.text,'html.parser').find('input',attrs={'name':'keywordbanner'})
            declared_form = form.find_parent('form') if form else None
            (args.output/'form-observation.json').write_text(json.dumps({'homepage_status':documentation.status_code,
                'form_method':declared_form.get('method') if declared_form else None,
                'form_action':declared_form.get('action') if declared_form else None},ensure_ascii=False),encoding='utf-8')
            if declared_form is None or declared_form.get('method','get').lower() != 'post':
                raise ValueError('official_read_only_search_post_form_not_confirmed')
            provider = ChinaXivProvider()
            provider.timeout_s = 25
            try:
                rows=provider.search(AcademicSearchRequest(query='人工智能',limit=2))
            except Exception as exc:
                (args.output/'post-failure.json').write_text(json.dumps({'error':type(exc).__name__,
                    'diagnostics':getattr(exc,'diagnostics',[])},ensure_ascii=False,indent=2),encoding='utf-8')
                raise
            (args.output/'post-search-results.json').write_text(json.dumps([x.to_dict() for x in rows],ensure_ascii=False,indent=2),encoding='utf-8')
            cases.append({'case':'declared-read-only-post','method':'POST', 'provider':'chinaxiv',
                'endpoint':ChinaXivProvider.search_post.endpoint,'count':len(rows),
                'documentation_status':documentation.status_code,
                'error':None if rows else 'no_results'})
            print(json.dumps(cases[-1],ensure_ascii=False),flush=True)
        if args.group == 'pdf':
            from generic_web.url_policy import guarded_fetch
            if not args.pdf_url:
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                doc_url='https://www.w3.org/WAI/WCAG22/Techniques/pdf/PDF7'
                document=guarded_fetch(doc_url,timeout=30,max_bytes=512*1024)
                soup=BeautifulSoup(document.text,'html.parser')
                links=[{'text':a.get_text(' ',strip=True),'url':urljoin(doc_url,a['href'])}
                       for a in soup.select('a[href]') if '.pdf' in a['href'] or 'working-example' in a['href']]
                (args.output/'scan-sample-links.json').write_text(json.dumps(links,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(links,ensure_ascii=False),flush=True)
            else:
                if args.acquired_pdf:
                    from runtime.pdf_text import process_pdf
                    from kr_core.evidence_store import EvidenceStore
                    acquisition=json.loads((args.acquired_pdf.parent/'receipt.json').read_text(encoding='utf-8'))
                    matched=[row for row in acquisition.get('cases',[]) if row.get('case')==args.acquired_pdf.stem
                             and row.get('url')==args.pdf_url and row.get('status')==200
                             and row.get('bytes')==args.acquired_pdf.stat().st_size]
                    if not matched:
                        raise ValueError('acquired_pdf_origin_receipt_mismatch')
                    parsed=process_pdf(args.acquired_pdf.read_bytes(),timeout_s=600)
                    result={'content':parsed['text'],'metadata':{k:v for k,v in parsed.items() if k!='text'},
                        'url':args.pdf_url,'collector':'pypdf','document_blocks':parsed['document_blocks']}
                    EvidenceStore().record_detail(platform='web',url=args.pdf_url,response=result)
                    refs=result.get('evidence_assets') or []
                    readbacks=[EvidenceAssets(read_only=True).fetch(ref['version_id'],max_chars=20000) for ref in refs]
                    (args.output/'public-pdf-ocr.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    cases.append({'case':'public-pdf-ocr-composed','error':None if parsed['text'] else 'empty_document',
                        'input_sha256':__import__('hashlib').sha256(args.acquired_pdf.read_bytes()).hexdigest(),
                        'acquisition_receipt':str(args.acquired_pdf.parent/'receipt.json'),
                        'content_chars':len(parsed['text']),'readback_chars':[len(r.get('text','')) for r in readbacks]})
                    print(json.dumps(cases[-1],ensure_ascii=False),flush=True)
                else:
                    result=call('public-pdf-ocr','extract_web_page',{'url':args.pdf_url,'use_jina':False,'timeout':60})
                parsed=result.get('metadata') or {}
                cases[-1].update(ocr_pages=parsed.get('ocr_pages'),missing_text_pages=parsed.get('missing_text_pages'),
                    ocr_status=parsed.get('ocr_status'),ocr_errors=parsed.get('ocr_errors'),coverage=parsed.get('coverage'))
        if args.group == 'searx-audit':
            from generic_web.url_policy import guarded_fetch
            import hashlib
            directory_url = 'https://searx.space/data/instances.json'
            directory = guarded_fetch(directory_url, timeout=30, max_bytes=8*1024*1024)
            directory.raise_for_status()
            (args.output/'instances.json').write_bytes(directory.content)
            instances = directory.json().get('instances', {})
            # Directory presence is not authorization. Inspect a small fixed
            # sample's robots and operator pages before any search request.
            chosen = ['https://searx.tiekoetter.com/', 'https://search.sapti.me/', 'https://search.inetol.net/']
            for i, base in enumerate(chosen):
                row = {'url':base, 'directory_entry':instances.get(base), 'pages':[]}
                for suffix in ('robots.txt', 'info/en/about', 'config'):
                    target = base + suffix
                    try:
                        resp = guarded_fetch(target, timeout=15, max_bytes=512*1024)
                        (args.output/f'instance-{i}-{suffix.replace("/","-")}.txt').write_text(resp.text,encoding='utf-8')
                        row['pages'].append({'url':target,'status':resp.status_code,
                            'content_type':resp.headers.get('content-type'), 'chars':len(resp.text),
                            'sha256':hashlib.sha256(resp.content).hexdigest()})
                    except Exception as exc:
                        row['pages'].append({'url':target,'error':type(exc).__name__})
                cases.append({'case':'public-instance-admission','error':None,**row})
                print(json.dumps({'instance':base,'pages':row['pages']},ensure_ascii=False),flush=True)
        for language,query in [('zh','国务院 人工智能 政策'),('en','Python asyncio TaskGroup official documentation')]:
            if args.group == 'all':
                call('anysearch-'+language,'kr_web_search',{'query':query,'language':language,'limit':3,'provider':'anysearch','timeout_s':20})
            if args.group in ('all','ddgs'):
                call('ddgs-'+language,'kr_web_search',{'query':query,'language':language,'limit':3,'provider':'ddgs','timeout_s':15})
        for label,url in ([] if args.group not in ('all','pages') else [('official-html','https://docs.python.org/3/library/asyncio-task.html'),
                          ('official-pdf','https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf')]):
            call(label,'extract_web_page',{'url':url,'use_jina':False,'timeout':30})
        for label in (() if args.group not in ('all','pages') else ('dynamic-first','dynamic-reuse')):
            call(label,'extract_dynamic_page',{'url':'https://docs.python.org/3/library/asyncio-task.html','timeout':25})
        if args.group == 'academic':
            from runtime.task_service import TaskService
            service=TaskService()
            try:
                discovery=call('academic-discovery','search_academic',{'query':args.academic_query,
                    'provider':args.academic_source,'limit':2,'options':{'deadline_s':90,'open_access_only':args.academic_open_access}})
                pending=discovery.get('metadata',{}).get('pending_discoveries') or []
                if pending:
                    status=call('academic-pending','get_task_status',{'task_id':pending[0]['task_id'],
                        'wait':True,'max_wait_s':200})
                    discovery=(status.get('durable_result') or {}).get('result') or {}
                items=discovery.get('items') or []
                cases.append({'case':'academic-nonempty-discovery','count':len(items),
                    'error':discovery.get('error') or (None if items else 'no_matching_candidates')})
                if items and not args.academic_discovery_only:
                    chosen=items[args.academic_index]
                    reference=(chosen.get('raw',{}).get('fulltext_ref') or {})['version_id']
                    task=call('academic-request','get_content_detail',{'url':chosen['url'],
                        'academic_reference':reference,'include_ocr':False,'deadline_s':600})
                    status=call('academic-result','get_task_status',{'task_id':task['task_id'],
                        'wait':True,'max_wait_s':600})
                    durable=status.get('durable_result') or {}
                    detail=durable.get('result') or {}
                    refs=detail.get('evidence_assets') or []
                    readbacks=[call('academic-readback-'+str(n),'read_knowledge',{'request':{
                        'action':'fetch','version_id':ref['version_id'],'max_chars':8000}}) for n,ref in enumerate(refs)]
                    cases.append({'case':'academic-fixed-card-fulltext-readback','status':durable.get('status'),
                        'reference':reference, 'fulltext_status':detail.get('fulltext_status'),
                        'provenance':detail.get('provenance'),
                        'error':detail.get('error') or (None if durable.get('status')=='completed' and refs and all(x.get('text') for x in readbacks) else 'fulltext_or_readback_incomplete'),
                        'asset_count':len(refs),'readback_chars':[len(x.get('text','')) for x in readbacks]})
            finally:
                service.stop_maintenance()
        if args.group == 'host-register':
            research=call('host-research','kr_research',{'task':'核对 SearXNG 官方 Search API 的接口边界','mode':'plan_only','project_name':'KnowledgeRadar'})
            task_id=research['research_task']['research_task_id']
            batch=call('host-register','update_research',{'request':{'task_id':task_id,'action':'host_request',
                'text':'site:docs.searxng.org Search API JSON public instances'}})
            (args.output/'host-context.json').write_text(json.dumps({'research_task_id':task_id,'batch_id':batch['batch_id']}),encoding='utf-8')
        if args.group == 'host-submit':
            if not args.host_candidates:
                parser.error('--host-candidates is required')
            context=json.loads((args.output/'host-context.json').read_text(encoding='utf-8'))
            candidates=json.loads(args.host_candidates.read_text(encoding='utf-8'))
            call('host-submit','record_research_candidates_tool',{'research_task_id':context['research_task_id'],
                'host_batch_id':context['batch_id'],'source_ecology':'generic_web_ecology','tool':'web.run',
                'host_outcome':args.host_outcome,'candidates':candidates})
            call('host-readback','read_knowledge',{'request':{'action':'research','task_id':context['research_task_id'],
                'view':'host_batches'}})
            for i,item in enumerate(candidates[:2]):
                call(f'host-detail-{i}','extract_web_page',{'url':item['url'],'use_jina':False,'timeout':30})
    finally:
        close()
    receipt={'schema':'kr-web-real-candidate-validation/v1','source_root':str(ROOT),
             'source_identity':source_identity(),'contract_hash':catalog()['contract_hash'],
             'access_path':'candidate_application_same_kernel_contract_not_installed_native', 'group':args.group,
             'cases':cases,'no_installation_change':True,
             'no_new_paid_provider_accounts_used':True,
             'existing_ocr_provider_may_be_called':args.group in {'all','pdf'}}
    (args.output/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if all(not row['error'] for row in cases) else 1


if __name__=='__main__':
    raise SystemExit(main())
