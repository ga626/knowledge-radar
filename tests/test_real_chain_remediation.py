from __future__ import annotations

from detail_strategies.recruitment import _normalize_recruitment_detail
from generic_web.collector import collect_rendered_html
from generic_web.models import GenericWebRequest
from runtime.tool_trace import ToolTraceRecorder, record_trace_child, set_current_tool_trace


def test_recruitment_login_shell_is_never_normalized_as_success() -> None:
    result = _normalize_recruitment_detail(
        "猎聘",
        "https://www.liepin.com/job/example.shtml",
        {
            "status": "ok",
            "title": "Python",
            "content": "首页 职位 校园 海归 简历优化 猎聘APP 我是猎头 我是招聘方 NEW 登录/注册",
        },
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "login_required"
    assert result["manual_action_required"] is True


def test_recruitment_short_non_login_text_is_not_usable_detail() -> None:
    result = _normalize_recruitment_detail(
        "BOSS直聘",
        "https://www.zhipin.com/job_detail/example.html",
        {"status": "ok", "title": "Python", "content": "页面暂时没有可用职位正文"},
    )

    assert result["status"] == "empty"
    assert result["failure_type"] == "empty_detail"


def test_rendered_html_prefers_long_semantic_content_container() -> None:
    long_text = "这是动态加载后的正文。" * 40
    html = f"""
    <html><head><title>动态文章</title></head><body>
      <main><p>页面壳很短。</p></main>
      <div class='article-body'><p>{long_text}</p></div>
    </body></html>
    """

    result = collect_rendered_html(
        GenericWebRequest(url="https://example.com/article", timeout=5),
        html=html,
        render_metadata={"networkidle": "not_reached:TimeoutError", "html_chars": len(html)},
    )

    assert result.error is None
    assert long_text[:30] in result.content
    assert result.metadata["content_selector"] == "semantic_class_or_id"
    assert result.metadata["render"]["networkidle"] == "not_reached:TimeoutError"


def test_trace_child_receipt_links_to_active_execution_without_sensitive_payload(monkeypatch, tmp_path) -> None:
    import runtime.tool_trace as tool_trace

    recorder = ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)
    set_current_tool_trace({"trace_id": "root-123", "tool_name": "kr_web_search", "strategy": "provider_fallback"})

    child = record_trace_child(
        "provider_attempt",
        metadata={
            "status": "ok",
            "wave_index": 0,
            "provider_id": "anysearch",
            "result_count": 3,
            "outcome": "usable_results",
            "query": "must_not_be_written",
        },
    )

    assert child["event_type"] == "provider_attempt"
    assert child["parent_trace_id"] == "root-123"
    assert child["metadata"]["provider_id"] == "anysearch"
    assert "query" not in child["metadata"]


def test_provider_receipt_uses_wave_and_provider_without_query(monkeypatch) -> None:
    import search_providers.service as service

    calls = []
    monkeypatch.setattr(service, "record_trace_child", lambda event_type, **kwargs: calls.append((event_type, kwargs)))

    service._record_provider_attempt_receipts(
        wave_index=2,
        attempted=["anysearch", "codex_web_search"],
        results={"anysearch": [object()]},
        errors=[{"provider": "codex_web_search", "type": "not_callable", "expected_degraded": True}],
    )

    assert [(event, item["metadata"]["provider_id"], item["metadata"]["wave_index"]) for event, item in calls] == [
        ("provider_attempt", "anysearch", 2),
        ("provider_attempt", "codex_web_search", 2),
    ]
    assert all("query" not in item["metadata"] for _, item in calls)



def _docx_xml(tmp_path, rows, grid=''):
    import zipfile
    source=tmp_path/'table.docx'
    xml='<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>'+grid+rows+'</w:tbl></w:body></w:document>'
    with zipfile.ZipFile(source,'w') as archive: archive.writestr('word/document.xml',xml)
    return source


def _cell(text, properties=''):
    return '<w:tc><w:tcPr>'+properties+'</w:tcPr><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:tc>'


def test_docx_missing_grid_salvages_text_but_does_not_claim_layout(tmp_path):
    from runtime.optional_media_worker import audit_docx_tables
    source=_docx_xml(tmp_path,'<w:tr>'+_cell('甲方')+_cell('金额9800')+'</w:tr>')
    result=audit_docx_tables(source,{'text':'前言','document_blocks':[]})
    assert not result['request_satisfied'] and result['coverage']=='partial'
    assert '金额9800' in result['text']
    assert 'missing_grid' in result['document_audit']['tables'][0]['structural_risks']
    assert result['document_blocks'][-1]['kind']=='raw_table_cell'
    assert result['document_blocks'][-1]['locator']['cell']==2
    assert result['document_blocks'][-1]['column']==2


def test_docx_short_grid_detects_unretained_last_column(tmp_path):
    from runtime.optional_media_worker import audit_docx_tables
    source=_docx_xml(tmp_path,'<w:tr>'+_cell('首列')+_cell('末列')+'</w:tr>','<w:tblGrid><w:gridCol/></w:tblGrid>')
    result=audit_docx_tables(source,{'text':'首列','document_blocks':[]})
    assert 'short_grid' in result['document_audit']['tables'][0]['structural_risks']
    assert result['document_audit']['unretained_cells']==1 and '末列' in result['text']


def test_docx_flattened_table_and_merged_grid_do_not_false_warn(tmp_path):
    from runtime.optional_media_worker import audit_docx_tables
    source=_docx_xml(tmp_path,'<w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>'+_cell('合并 单元格','<w:gridSpan w:val="2"/>')+'</w:tr>',
                     '<w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>')
    result=audit_docx_tables(source,{'text':'合并 单元格','document_blocks':[]})
    assert result['request_satisfied'] and not result['quality_warnings']
    assert result['document_audit']['tables'][0]['inferred_columns']==3


def test_docx_audit_failure_is_visible(tmp_path):
    from runtime.optional_media_worker import audit_docx_tables
    source=tmp_path/'bad.docx';source.write_bytes(b'not zip')
    result=audit_docx_tables(source,{'text':'已取得正文','document_blocks':[]})
    assert result['text']=='已取得正文' and not result['request_satisfied']
    assert result['document_audit']['status']=='failed'


def test_docx_wrapped_rows_and_nested_tables_have_distinct_locators(tmp_path):
    from runtime.optional_media_worker import audit_docx_tables
    nested='<w:tbl><w:tr>'+_cell('内表')+'</w:tr></w:tbl>'
    source=_docx_xml(tmp_path,'<w:sdt><w:sdtContent><w:tr>'+_cell('外表')[:-7]+nested+'</w:tc></w:tr></w:sdtContent></w:sdt>')
    result=audit_docx_tables(source,{'text':'','document_blocks':[]})
    assert len(result['document_audit']['tables'])==2
    assert {x['locator']['table'] for x in result['document_blocks']}=={1,2}



def test_docx_audit_survives_fixed_asset_archival(monkeypatch,tmp_path):
    from runtime import optional_media
    from kr_core.evidence_store import EvidenceStore
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path/'data'))
    result=optional_media._save_result({'component':'docling','status':'partial','text':'原文与挽救文字',
        'request_satisfied':False,'coverage':'partial','quality_warnings':['docx_table_text_unverified_in_primary_conversion'],
        'document_audit':{'schema':'docx-table-retention/v1','status':'checked','unretained_cells':1},
        'document_blocks':[{'text':'挽救文字','kind':'raw_table_cell'}]}, {'input_sha256':'synthetic'})
    assert result['status']=='partial' and not result['request_satisfied']
    assert not result['content_coverage']['request_satisfied']
    refs=result['evidence_assets']
    assert any(r['kind']=='document_receipt' for r in refs)
