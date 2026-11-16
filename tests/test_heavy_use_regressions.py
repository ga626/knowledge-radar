"""重度使用发现的真实回归；不依赖外站、账号或模型加载。"""
from types import SimpleNamespace
import zipfile

import pytest
from runtime.optional_media_worker import _detected_transcript, audit_docx_tables, docling_blocks
from academic_providers.models import AcademicWork
from academic_providers.relevance import has_query_evidence


def test_fixed_evidence_keeps_salary_and_asr_observation_provenance(tmp_path):
    from kr_core.evidence_store import EvidenceStore
    store=EvidenceStore(tmp_path/'evidence.sqlite3')
    salary={'method':'rendered_region_ocr','input_sha256':'fixture-input','source_url':'https://example.com/job',
            'ocr_confidence':'not_reported','evidence_assets':[{'version_id':'fixture-ocr'}]}
    response={'title':'职位','content':'职责与要求的完整正文','salary_evidence':salary,
              'quality_warnings':['asr_post_eos_output_discarded'],
              'asr_boundary_diagnostics':[{'discarded_tail_chars':4,'discarded_tail_sha256':'fixture-tail'}]}
    old={'title':response['title'],'content':response['content']}
    store.record_detail(platform='boss',url='https://example.com/job',response=old)
    store.record_detail(platform='boss',url='https://example.com/job',response=response)
    assert response['evidence_assets'][0]['version_id']!=old['evidence_assets'][0]['version_id']
    assert 'salary_ocr' not in store.assets.fetch(old['evidence_assets'][0]['version_id'])['asset']['provenance']
    asset=store.assets.fetch(response['evidence_assets'][0]['version_id'])['asset']
    assert asset['provenance']['salary_ocr']==salary
    assert asset['provenance']['quality_warnings']==response['quality_warnings']
    assert asset['provenance']['asr_boundary_diagnostics']==response['asr_boundary_diagnostics']


@pytest.mark.parametrize('marker', ['<|endoftext|>', '<|im_end|>'])
def test_asr_protocol_terminator_discards_tail(marker):
    result = SimpleNamespace(text='nonempty', tokens=['KRlanguage', ' Chinese', '<asr_text>', '有效内容', marker, '无关尾文'])
    assert _detected_transcript(result) == ('有效内容', 'zh')


def test_asr_none_with_only_eos_is_silence():
    assert _detected_transcript(SimpleNamespace(text='nonempty', tokens=['language None<asr_text><|endoftext|>'])) == ('', '')


def test_asr_does_not_strip_ordinary_words():
    assert _detected_transcript(SimpleNamespace(text='nonempty', tokens=['language English<asr_text>say endoftext now'])) == ('say endoftext now', 'en')


def test_cjk_typesetting_spaces_do_not_hide_query():
    title = '智能体驱动的方志人物 知 识 图 谱 构建与检索增强生成问答研究'
    work = AcademicWork(title=title, url='https://example.org/paper')
    assert has_query_evidence('知识图谱', work)
    assert work.title == title
    assert not has_query_evidence('newyork', AcademicWork(title='new york', url='https://example.org/other'))


def test_docling_preserves_sheet_identity():
    group = SimpleNamespace(label='sheet', name='收入表', parent=None)
    class Ref:
        def resolve(self, doc): return group
    item = SimpleNamespace(label='text', text='123', parent=Ref(), prov=[])
    doc = SimpleNamespace(iterate_items=lambda:iter([(item,1)]))
    blocks = docling_blocks(doc)
    assert blocks[0]['kind'] == 'sheet_heading'
    assert blocks[0]['text'] == '收入表'
    assert blocks[1]['sheet_name'] == '收入表'


def test_docx_single_cell_layout_is_not_claimed_verified(tmp_path):
    source = tmp_path/'layout.docx'
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>保留正文</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    with zipfile.ZipFile(source,'w') as z:z.writestr('word/document.xml',xml)
    result = audit_docx_tables(source,{'text':'保留正文','document_blocks':[]})
    assert result['request_satisfied'] is True  # 文字已保留，不错误报丢失。
    assert 'docx_single_cell_layout_not_verified' in result['quality_warnings']
    assert result['document_audit']['layout_verified'] is False


def test_bilibili_satisfied_root_window_does_not_require_next_cursor(monkeypatch):
    from collectors.platform import bilibili
    class Response:
        status_code=200
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def json(self):return {'code':0,'data':self.data}
    def get(url,**kwargs):
        return Response({'aid':1}) if url.endswith('/view') else Response({'replies':[
            {'rpid':1,'content':{'message':'真实根评论'},'rcount':0}], 'cursor':{'next':0,'is_end':False}})
    monkeypatch.setattr(bilibili.httpx,'get',get)
    result=bilibili.collect_comments('fixture',options={'root_limit':1,'reply_limit':1,'total_limit':10,'max_pages':1})
    assert result['comments_complete'] is True
    assert result['comments_exhausted'] is False
    assert result['comments_metadata']['root_pages']==1
    assert result['comments_metadata']['page_observations'][0]['returned_roots']==1


def test_xhs_total_budget_does_not_make_incomplete_replies_complete():
    from runtime.xhs_opencli import _normalize_comments
    data={'comments':[{'content':'root'},{'content':'only reply','is_reply':True,'root_index':0}],
          'comments_observation':{'expanders_remaining':1,'skipped_empty':0}, 'comment_count':20}
    _normalize_comments(data, {'comment_limit':1,'comment_budget':2,'comment_reply_limit':10})
    assert data['comments_complete'] is False
    assert data['comments_metadata']['reply_window_complete'] is False


def test_xhs_exact_budget_can_satisfy_finite_window_without_exhausting_source():
    from runtime.xhs_opencli import _normalize_comments
    data={'comments':[{'content':'root'},{'content':'reply','is_reply':True,'root_index':0}],
          'comments_budget_truncated':True,
          'comments_observation':{'expanders_remaining':1,'skipped_empty':0},'comment_count':20}
    _normalize_comments(data,{'comment_limit':1,'comment_budget':2,'comment_reply_limit':1})
    assert data['comments_complete'] is True
    assert data['comments_metadata']['complete_source'] is False
    assert data['comments_metadata']['total_budget_reached'] is True


def test_xhs_clipping_visible_replies_never_claims_reply_window_complete():
    from runtime.xhs_opencli import _normalize_comments
    data={'comments':[{'content':'root'},
        {'content':'reply1','is_reply':True,'root_index':0},
        {'content':'reply2','is_reply':True,'root_index':0}],
        'comments_observation':{'expanders_remaining':0,'skipped_empty':0},'comment_count':3}
    _normalize_comments(data,{'comment_limit':1,'comment_budget':2,'comment_reply_limit':10})
    assert data['comments_complete'] is False


def test_maimai_distinct_native_comments_with_same_text_survive(monkeypatch):
    from collectors.platform import maimai
    url='https://maimai.cn/feed-detail/123'
    response={'ok':True,'data':{'url':url,'comments_scope_found':True,
        'comments_observation':{'stage':'dom_snapshot','scope_found':True},
        'comments':[{'id':'1','content':'赞同','author':'甲'},{'id':'2','content':'赞同','author':'乙'}]}}
    monkeypatch.setattr(maimai,'_site',lambda:SimpleNamespace(invoke=lambda *a,**kw:response))
    result=maimai._comments(url,2)
    assert len(result['comments'])==2
    assert result['comments_metadata']['request_window_complete'] is True
    assert result['comments_metadata']['complete_source'] is False
    assert result['comments_metadata']['observation']['stage']=='dom_snapshot'


def test_coverage_explains_object_complete_without_claiming_source_exhausted():
    from kr_core.evidence_coverage import content_coverage
    result=content_coverage({'content':'source code','body_complete':True,'comments_complete':False}, {'include_comments':True})
    assert result['parts']['content']['coverage']=='requested_object'
    assert result['request_satisfied'] is False and result['complete'] is False
    assert 'comments' in result['summary'] and '整源穷尽未证明' in result['summary']


def test_known_machine_receipt_is_not_unknown_body_type():
    import json
    from runtime.research_archive import readable_asset
    assets=SimpleNamespace(_verified_text=lambda row:'{"pages":1}')
    for kind in ('media_ledger','document_receipt','academic_card'):
        result,reason=readable_asset(assets,{'kind':kind,'metadata':json.dumps({}),'title':'receipt'})
        assert result is None and '未知' not in reason


def test_local_document_title_and_strategy_use_actual_format(monkeypatch,tmp_path):
    from runtime.optional_media import _save_result
    from kr_core.evidence_store import EvidenceStore
    monkeypatch.setattr(EvidenceStore,'record_detail',lambda *a,**kw:None)
    result=_save_result({'component':'docling','text':'123','source_kind':'document_parse'},
        {'input_path':str(tmp_path/'cashflow.xlsx'),'input_sha256':'f'*64})
    assert result['title']=='cashflow.xlsx'
    assert 'xlsx' in result['detail_metadata']['strategy']
    assert 'docx' not in result['detail_metadata']['strategy']


def test_public_fulltext_reports_403_and_uses_declared_oa_alternative(monkeypatch):
    import httpx
    from generic_web import url_policy
    from academic_providers.fulltext import _extract_public_work
    called=[]
    def fetch(url,**kw):
        called.append(url)
        return httpx.Response(403 if len(called)==1 else 200,request=httpx.Request('GET',url),
            headers={'content-type':'text/html'},text='<div class="article-body">'+('真实论文内容。'*100)+'</div>')
    monkeypatch.setattr(url_policy,'guarded_fetch',fetch)
    work=AcademicWork(title='fixture',url='https://oa.example/landing',raw={
        'html_url':'https://oa.example/blocked','fulltext_candidate_urls':['https://mirror.example/full']})
    result=_extract_public_work('europepmc',work.url,work,timeout_s=5)
    assert result.text_extractable
    attempts=result.provenance['attempts']
    assert attempts[0]['http_status']==403 and attempts[0]['stage']=='fulltext_fetch'
    assert attempts[1]['status']=='acquired'
