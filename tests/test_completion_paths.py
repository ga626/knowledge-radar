"""新功能的完整数据链与失败边界；模型质量不由替身数据赋予。"""
import base64
import io
import json
import zipfile
from pathlib import Path
import pytest
from kr_core.evidence_assets import EvidenceAssets
from kr_core.evidence_store import EvidenceStore
from onboarding.research_api import workspace_api
from runtime.research_workspace import ResearchRecords
from runtime.research_ledger import open_task, record_tool_receipt, read_task


def test_structured_document_and_comments_are_fixed_and_localizable(tmp_path):
    store = EvidenceStore(str(tmp_path / 'summary.jsonl'))
    result = {'document_blocks':[{'page':1,'kind':'paragraph','text':'开头🙂'}, {'page':3,'kind':'table','row':2,'column':1,'bbox':[1,2,30,40],'text':'收入：125.50'}], 'comments':[{'comment_id':'reply-7','text':'原始评论末尾'}]}
    store.append_detail(platform='pdf', url='https://example.org/document.pdf', response=result)
    refs = {row['kind']:row for row in result['evidence_assets']}
    read = store.assets.fetch(refs['content']['version_id'], start=4)
    assert '125.50' in read['text']
    assert any(loc.get('page') == 3 and loc.get('row') == 2 and loc['bbox'] == [1,2,30,40] for loc in read['locators'])
    assert store.assets.fetch(refs['comments']['version_id'])['locators'][0]['comment_id'] == 'reply-7'


def test_physical_admission_counts_index_and_does_not_publish_original(tmp_path):
    assets = EvidenceAssets(tmp_path, max_physical_bytes=1)
    with pytest.raises(ValueError, match='physical_capacity'):
        assets.put('https://example.org', '正文末尾')
    assert not list(tmp_path.glob('*.utf8'))
    assert not assets.search()


def test_legacy_apply_is_hash_bound_idempotent_and_remains_partial(tmp_path):
    from kr_core.evidence_migration import migration_plan, apply_migration
    source = tmp_path / 'old.jsonl'
    source.write_text(json.dumps({'record_type':'detail','url':'https://example.org/old','payload':{'content':'只剩历史摘要'}}, ensure_ascii=False)+'\n', encoding='utf-8')
    raw = source.read_bytes()
    plan = migration_plan(source)
    assets = EvidenceAssets(tmp_path / 'assets')
    first = apply_migration(source, assets, expected_source_sha256=plan['source_sha256'], acknowledged=True)
    second = apply_migration(source, assets, expected_source_sha256=plan['source_sha256'], acknowledged=True)
    assert first['migrated'] == second['migrated']
    assert source.read_bytes() == raw
    assert assets.fetch(first['migrated'][0]['version_id'])['asset']['coverage'] == 'partial'
    source.write_text(source.read_text(encoding='utf-8')+'\n',encoding='utf-8')
    with pytest.raises(ValueError, match='source_changed'):
        apply_migration(source, assets, expected_source_sha256=plan['source_sha256'], acknowledged=True)


def test_task_export_download_obsidian_and_audience_share_same_sources(tmp_path, monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path / 'state'))
    assets = EvidenceAssets(tmp_path / 'assets')
    ref = assets.put('https://example.org/source', '完整原文🙂末尾')
    task = open_task(objective='核对证据导出',budget='fast',considered=[])
    record_tool_receipt(task_id=task['research_task_id'],trace_id='trace',tool='extract_web_page',status='ok', result={'evidence_assets':[ref]})
    path = tmp_path / 'state/research_tasks' / (task['research_task_id']+'.json')
    task = json.loads(path.read_text(encoding='utf-8'))
    report = tmp_path / 'report.md';report.write_text('# 研究报告\n结论末尾',encoding='utf-8')
    task['closeout']={'report_path':str(report)}
    path.write_text(json.dumps(task,ensure_ascii=False),encoding='utf-8')
    before = path.read_bytes(), (assets.root / 'index.sqlite3').read_bytes()
    records = ResearchRecords(tmp_path / 'state', assets=EvidenceAssets(assets.root,read_only=True))
    user = workspace_api(records,{'action':'inspect','task_id':task['research_task_id']},developer=False)
    assert 'tool_receipts' not in user and 'events' not in user
    assert all(row['type'] in {'report','fixed_evidence'} and 'path' not in row for row in user['files'])
    report_ref = next(row for row in user['files'] if row['type']=='report')
    assert '结论末尾' in workspace_api(records,{'action':'file_fetch','task_id':task['research_task_id'],'file_id':report_ref['file_id']},developer=False)['text']
    payload={'task_id':task['research_task_id'],'version_ids':[ref['version_id']],'export_acknowledged':True,'include_report':True}
    package=workspace_api(records,{**payload,'action':'export_task'},developer=False)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(package['data_base64']))) as archive:
        assert archive.read('report-1.md').decode() == report.read_text(encoding='utf-8')
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['sources'][0]['asset']['version_id'] == ref['version_id']
    vault=tmp_path/'vault';vault.mkdir();note=vault/'人工笔记.md';note.write_text('保留',encoding='utf-8')
    delivered=workspace_api(records,{**payload,'action':'export_obsidian','vault_path':str(vault)},developer=False)
    assert Path(delivered['inbox']).is_relative_to(vault)
    assert Path(delivered['inbox'],'report-1.md').is_file()
    assert delivered['open_uri'].startswith('obsidian://open?path=')
    repeated=workspace_api(records,{**payload,'action':'export_obsidian','vault_path':str(vault)},developer=False)
    assert repeated['inbox'] == delivered['inbox'] and repeated['already_exported'] is True
    assert note.read_text(encoding='utf-8')=='保留'
    assert (path.read_bytes(),(assets.root/'index.sqlite3').read_bytes()) == before


def test_real_tool_receipts_automatically_link_unique_candidate(tmp_path, monkeypatch):
    from runtime.tool_trace import traced_tool
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    task=open_task(objective='自动记录同一研究',budget='balanced',considered=[])
    @traced_tool('search_academic')
    def search(research_task_id=''):
        return {'items':[{'url':'https://example.org/paper','title':'论文'}]}
    @traced_tool('extract_web_page')
    def detail(research_task_id=''):
        return {'url':'https://example.org/paper','evidence_assets':[{'version_id':'a'*64}]}
    search(research_task_id=task['research_task_id'])
    extracted=detail(research_task_id=task['research_task_id'])
    current=read_task(task_id=task['research_task_id'])
    assert len(current['tool_receipts']) == 2
    assert current['candidate_pool'][0]['stage']=='detail_extracted'
    assert extracted['research_receipt']['candidate_association']['association']=='unique_source_identity'
    assert 'query' not in current['candidate_pool'][0]


def test_transcript_nested_map_and_source_ecology(tmp_path):
    from runtime.research_workspace import task_metrics
    store=EvidenceStore(str(tmp_path/'index.jsonl'))
    result={'transcript':'原文🙂', 'transcript_metadata':{'locators':{'status':'aligned','segments':[{'start':0,'end':3,'start_s':1,'end_s':2}]}}}
    store.append_detail(platform='video',url='https://example.org/video',response=result)
    version=next(row['version_id'] for row in result['evidence_assets'] if row['kind']=='transcript')
    assert store.assets.fetch(version)['locators'][0]['start_s']==1
    assert task_metrics({'tool_receipts':[{'tool':'search_academic','status':'ok'}]})['source_ecologies']==['academic_literature_ecology']


def test_model_main_path_has_no_old_qwen_engine_and_blocks_unapproved(tmp_path, monkeypatch):
    from runtime.optional_media import COMPONENTS, install_component
    monkeypatch.setenv('KR_OPTIONAL_COMPONENT_RECEIPTS_DIR',str(tmp_path/'components'))
    assert COMPONENTS['qwen3-asr-0.6b']['package']=='transformers'
    with pytest.raises(ValueError,match='reviewed_installation'):
        install_component('qwen3-asr-0.6b',{})
    assert not (tmp_path/'components').exists()


def test_real_two_page_pdf_parse_store_and_fetch(tmp_path, monkeypatch):
    import httpx
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from generic_web import collector
    from generic_web.models import GenericWebRequest
    writer=PdfWriter()
    for text in ('First page 125.50', 'Second page final evidence'):
        page=writer.add_blank_page(width=612,height=792)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    binary=io.BytesIO();writer.write(binary)
    url='https://example.org/test.pdf'
    response=httpx.Response(200,content=binary.getvalue(),headers={'content-type':'application/pdf'},request=httpx.Request('GET',url))
    monkeypatch.setattr(collector,'guarded_fetch',lambda *args,**kwargs:response)
    parsed=collector._collect_via_trafilatura(GenericWebRequest(url=url)).to_mcp_dict()
    store=EvidenceStore(str(tmp_path/'summary.jsonl'))
    store.append_detail(platform='web',url=url,response=parsed)
    ref=next(row for row in parsed['evidence_assets'] if row['kind']=='content')
    saved=store.assets.fetch(ref['version_id'])
    assert '125.50' in saved['text'] and 'final evidence' in saved['text']
    assert {row['page'] for row in saved['locators']} == {1,2}


def test_same_call_chain_reuses_one_gpu_lease(tmp_path,monkeypatch):
    from runtime.resource_concurrency import acquire_resource
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    with acquire_resource('asr_gpu') as outer:
        with acquire_resource('asr_gpu') as inner:
            assert inner['lease_id'] == outer['lease_id'] and inner['reused_outer_lease']


def test_optional_install_failure_does_not_block_retry(tmp_path,monkeypatch):
    from runtime import optional_media
    root=tmp_path/'components'
    monkeypatch.setenv('KR_OPTIONAL_COMPONENT_RECEIPTS_DIR',str(root))
    def fail(component,spec):
        (root/'environments'/component).mkdir(parents=True)
        raise RuntimeError('offline_install_failed')
    monkeypatch.setattr(optional_media,'_install_component',fail)
    for _ in range(2):
        with pytest.raises(RuntimeError,match='offline_install_failed'):
            optional_media.install_component('qwen3-asr-0.6b',{})
    assert not (root/'environments/qwen3-asr-0.6b').exists()
