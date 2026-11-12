"""官方搜索只固定实际可见文字；候选不冒充全文。"""
import json
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.research_index import ResearchIndex
from runtime.host_search_handoff import submit
from test_research_archive import archive


@pytest.fixture
def host(tmp_path,monkeypatch):
    from runtime import research_ledger
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    monkeypatch.setattr(research_ledger,'read_task',lambda **kw:{'project_id':'demo'})
    return ResearchIndex(tmp_path/'index.sqlite3'),EvidenceAssets()


def item(**extra):
    return {'url':'https://example.org/docs','title':'文档','host_reference':'turn1view0',
            'retrieved_at':'2026-11-12T10:00:00Z',**extra}


def test_visible_snapshot_roundtrips_with_provenance_and_no_fulltext_upgrade(host):
    index,assets=host
    rows=[item(visible_text='只看到了这一段\r\n另一行',visible_scope='excerpt',truncated=True)]
    result=submit('t','',rows,host_tool='web.run',query='query',index=index)
    assert len(result['evidence_assets'])==1
    ref=result['evidence_assets'][0]
    fetched=assets.fetch(ref['version_id'])
    assert fetched['text']==rows[0]['visible_text']
    assert ref['kind']=='host_snapshot' and ref['coverage']=='host_visible_excerpt'
    assert ref['provenance']['host']['reference']=='turn1view0'
    assert ref['provenance']['host']['truncated'] is True
    again=submit('t','',rows,host_tool='web.run',query='query',index=index)
    assert again['evidence_assets']==result['evidence_assets']
    projected=index.read('t',view='candidates')['items'][0]
    assert projected['host_reference']=='turn1view0'
    assert ref['version_id'] in projected['visible_version_ids']
    assert projected['verification']=='discovery_only'
    native=assets.put(rows[0]['url'],'真正取得的正文',kind='content')
    assert native['source_id']==ref['source_id'] and native['version_id']!=ref['version_id']


def test_candidates_without_opened_text_create_no_article(host):
    index,assets=host
    result=submit('t','',[item(snippet='搜索摘要')],host_tool='web.run',query='q',index=index)
    assert result['evidence_assets']==[]


@pytest.mark.parametrize('extra',[
    {'visible_text':'文字但范围缺失'},
    {'visible_text':'x','visible_scope':'fulltext'},
    {'visible_text':'x','visible_scope':'excerpt','retrieved_at':''},
    {'visible_text':'x'*262145,'visible_scope':'excerpt'},
])
def test_invalid_snapshot_leaves_no_batch_or_fixed_versions(host,extra):
    index,assets=host
    with pytest.raises(ValueError,match='host_snapshot'):
        submit('t','',[item(**extra)],host_tool='web.run',query='q',index=index)
    assert index.read('t',view='host_batches')['items']==[]
    assert not (assets.root/'index.sqlite3').exists()


def test_host_snapshot_uses_existing_receipt_scope_and_full_archive_path(archive):
    from runtime import research_ledger
    engine,*_=archive
    text='这是宿主实际返回的可见段落。\n' * 80
    result=submit('task-a','',[item(visible_text=text,visible_scope='page_text',truncated=True)],
                  host_tool='web.run',query='公开文档')
    receipt=research_ledger.submit_tool_result(task_id='task-a',trace_id='host-visible-test',
        tool='record_research_candidates_tool',status='ok',result=result)
    assert receipt['status']=='recorded'
    archived=engine.run('task-a')
    assert not archived['error_code']
    folder=engine._task_directory(engine._context('task-a'))
    documents=[p.read_text(encoding='utf-8') for p in folder.glob('*.md')]
    assert any(text in document and '宿主' in document for document in documents)
    version=result['evidence_assets'][0]['version_id']
    assert engine.assets.fetch(version,max_chars=len(text)+10)['text']==text
