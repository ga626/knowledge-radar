import base64
from copy import deepcopy
import hashlib
import json

import pytest
from pydantic import ValidationError

from collectors.platform import github_content as content
from collectors.platform.github_api import GitHubError
from kr_core.models import DetailRequest, DetailResponse
from kr_core.evidence_assets import EvidenceAssets
from kr_core.evidence_store import EvidenceStore
from kr_core.evidence_coverage import content_coverage
from runtime.knowledge import Fetch, read

SHA='a'*40
TREE='b'*40
TEXT='  # 中文 source\r\nurl = "https://example.org/a?token=public-example#section"\r\nprint("末尾")\r\n'


def blob(raw):
    return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


class Client:
    transport='rest'
    identity='public-test'
    def __init__(self, text=TEXT):
        self.calls=[];self.attempts=[];self.fail_page=None
        raw=text.encode()
        self.repo={'id':1,'full_name':'o/r','private':False,'visibility':'public','default_branch':'main'}
        self.commit={'sha':SHA,'commit':{'tree':{'sha':TREE}}}
        self.file={'type':'file','path':'README.md','sha':blob(raw),'encoding':'base64','size':len(raw),'content':base64.b64encode(raw).decode()}
        self.tree={'tree':[{'path':'README.md','mode':'100644','type':'blob','sha':blob(raw),'size':len(raw)}], 'truncated':False}
    def json(self, endpoint, *, params=None):
        self.calls.append((endpoint,params))
        if endpoint=='repos/o/r':return deepcopy(self.repo),{}
        if '/commits/' in endpoint:return deepcopy(self.commit),{}
        if '/contents/' in endpoint or endpoint.endswith('/readme'):return deepcopy(self.file),{}
        if '/git/trees/' in endpoint:return deepcopy(self.tree),{}
        if endpoint.endswith('/issues/7') or endpoint.endswith('/pulls/7'):
            return {'id':7,'title':'Issue title','body':'Actual body with code.','updated_at':'2026-11-05T00:00:00Z','html_url':'https://github.com/o/r/issues/7','comments':2},{}
        if endpoint.endswith('/comments') or endpoint.endswith('/reviews'):
            page=params['page']
            if page==self.fail_page:raise GitHubError('NETWORK_ERROR','page interrupted',retryable=True)
            row={'id':100+page,'body':'comment '+str(page),'user':{'login':'person'},'created_at':'2026-11-05T00:00:00Z'}
            if '/pulls/' in endpoint and endpoint.endswith('/comments'):
                row.update(in_reply_to_id=100,path='source.py',line=8,commit_id=SHA)
            return [row], {'link':'<https://api.github.com/repos/o/r/issues/7/comments?page=2>; rel="next"'} if page==1 else {}
        raise AssertionError(endpoint)


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    monkeypatch.setenv('KR_LOG_DIR',str(tmp_path/'logs'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))


def request(url='https://github.com/o/r', *, github=None, comments=False, checkpoint='one'):
    return DetailRequest(url=url,platform='GitHub',options={'github':github or {},'include_comments':comments,'github_checkpoint_id':checkpoint})


def test_readme_fixed_commit_keeps_original_bytes_through_store_and_line_read(tmp_path):
    client=Client()
    raw=content.collect(request(github={'kind':'readme'}),client=client)
    assert raw['status']=='ok' and raw['content']==TEXT
    assert '/blob/'+SHA+'/README.md' in raw['url']
    assert all((params or {}).get('ref')==SHA for ep,params in client.calls if ep.endswith('/readme'))
    result=DetailResponse.from_legacy('GitHub',request().url,raw).to_legacy_dict()
    assert result['content']==TEXT
    store=EvidenceStore(str(tmp_path/'detail.jsonl'))
    store.append_detail(platform='GitHub',url=request().url,response=result)
    asset=next(x for x in result['evidence_assets'] if x['kind']=='content')
    assert asset['raw_sha256']==hashlib.sha256(TEXT.encode()).hexdigest()
    assert asset['source_url']==raw['url']
    fetched=read(Fetch(action='fetch',version_id=asset['version_id'],line_start=2,line_end=2,max_chars=8000),store.assets)
    assert fetched['text']==TEXT.splitlines(keepends=True)[1]
    assert fetched['asset']['provenance']['github']['commit']==SHA
    assert fetched['locators'][0]['line_start']==2
    assert '#chars='+str(fetched['locator']['start']) in fetched['citation']


def test_fixed_file_reuses_public_asset_without_downloading_again(tmp_path):
    store=EvidenceStore(str(tmp_path/'detail.jsonl'));client=Client()
    result=content.collect(request(github={'kind':'readme'}),client=client,assets=store.assets)
    store.append_detail(platform='GitHub',url=request().url,response=result)
    client.calls=[]
    second=content.collect(request('https://github.com/o/r/blob/'+SHA+'/README.md',checkpoint='two'),client=client,assets=store.assets)
    assert second['content']==TEXT
    assert second['provenance']['github']['file_network_reverified'] is False
    assert not any('/contents/' in ep for ep,_ in client.calls)


def test_scope_covered_is_not_all_comments_exhausted():
    result=content.collect(request('https://github.com/o/r/issues/7',comments=True),client=Client())
    assert result['comments_complete'] is True
    assert result['comments_pagination']['issue_comments']['all_comments_exhausted'] is False
    assert result['comments_pagination']['issue_comments']['next_page']==2
    assert len(result['comments'])==1
    assert 'parent_comment_id' not in result['comments'][0] and 'in_reply_to_id' not in result['comments'][0]
    assert content_coverage(result,{'include_comments':True})['request_satisfied'] is True


def test_resume_preserves_body_completed_pages_and_fixed_versions():
    client=Client();client.fail_page=2
    req=request('https://github.com/o/r/issues/7',comments=True,github={'max_pages':2})
    first=content.collect(req,client=client)
    assert first['status']=='partial' and first['comments_complete'] is False
    assert len(first['comments'])==1
    assert content_coverage(first,{'include_comments':True})['required_gaps']==['comments']
    client.fail_page=None;client.calls=[]
    second=content.collect(req,client=client)
    assert second['comments_complete'] and len(second['comments'])==2
    assert second['comments_pagination']['issue_comments']['all_comments_exhausted'] is True
    assert client.calls==[('repos/o/r/issues/7/comments',{'page':2,'per_page':30})]


def test_pull_review_relations_and_comment_evidence_are_preserved(tmp_path):
    result=content.collect(request('https://github.com/o/r/pull/7',comments=True),client=Client())
    assert {row['discussion_kind'] for row in result['comments']}=={'issue_comments','reviews','review_comments'}
    review=next(x for x in result['comments'] if x['discussion_kind']=='review_comments')
    assert review['in_reply_to_id']==100 and review['line']==8 and review['commit_id']==SHA
    store=EvidenceStore(str(tmp_path/'review.jsonl'))
    store.append_detail(platform='GitHub',url=result['url'],response=result)
    ref=next(r for r in result['evidence_assets'] if r['kind']=='comments')
    saved=store.assets.fetch(ref['version_id'])
    assert any(x.get('parent_comment_id')=='100' for x in saved['locators'])
    assert 'review_comments' in saved['text']


@pytest.mark.parametrize('url', ['https://github.com/o/r/blob/release/v1/README.md','https://github.com/o/r/blob/tag/README.md'])
def test_ambiguous_nondefault_ref_requires_explicit_input(url):
    result=content.collect(request(url),client=Client())
    assert result['error']['type']=='AMBIGUOUS_REF_PATH'


def test_slash_ref_can_be_explicit_and_resume_does_not_follow_new_head():
    client=Client()
    req=request('https://github.com/o/r/blob/release/v1/README.md',github={'ref':'release/v1'})
    first=content.collect(req,client=client)
    assert first['provenance']['github']['commit']==SHA
    client.commit['sha']='c'*40;client.calls=[]
    second=content.collect(req,client=client)
    assert second['provenance']['github']['commit']==SHA and not client.calls


@pytest.mark.parametrize('mode', ['120000','160000'])
def test_symlink_and_submodule_are_not_followed(mode):
    client=Client();client.tree['tree'][0]['mode']=mode
    result=content.collect(request(github={'kind':'readme'}),client=client)
    assert result['error']['type']=='NONREGULAR_FILE'


def test_wrong_blob_binary_lfs_and_private_are_explicit():
    client=Client();client.file['sha']='f'*40;client.tree['tree'][0]['sha']='f'*40
    result=content.collect(request(github={'kind':'readme'}),client=client)
    assert result['error']['type']=='CONTENT_INTEGRITY_FAILED'
    client=Client('\x00binary')
    assert content.collect(request(github={'kind':'readme'},checkpoint='binary'),client=client)['error']['type']=='BINARY_CONTENT'
    client=Client('version https://git-lfs.github.com/spec/v1\noid sha256:test\n')
    assert content.collect(request(github={'kind':'readme'},checkpoint='lfs'),client=client)['error']['type']=='LFS_POINTER'
    client=Client();client.repo['private']=True
    assert content.collect(request(checkpoint='private'),client=client)['error']['type']=='PRIVATE_SCOPE_NOT_AUTHORIZED'
    assert len(client.calls)==1


def test_tree_truncation_and_internal_snapshots_never_look_like_full_text_assets():
    client=Client();client.tree['truncated']=True
    result=content.collect(request(),client=client)
    assert result['status']=='partial' and result['body_complete'] is False
    assert not EvidenceAssets().search('')


@pytest.mark.parametrize('value',[{'path':'../x'},{'ref':'../../main'},{'path':'C:\\secret'},{'kind':'command'},{'max_pages':0}])
def test_invalid_scope_is_rejected(value):
    with pytest.raises(ValidationError):content.GitHubReadOptions.model_validate(value)


def test_github_scope_flows_through_public_request_to_durable_task(monkeypatch):
    from use_cases import content_detail as usecase
    calls=[]
    def submit(req,**kwargs):
        calls.append(req)
        return {'task_id':'test'}, {'status':'queued'}
    monkeypatch.setattr(usecase,'submit_detail_task',submit)
    usecase.request_content_detail('https://github.com/o/r',infer_platform=lambda u:'GitHub',execute_sync=lambda r:pytest.fail('must be durable'),
        github=content.GitHubReadOptions(kind='readme'))
    assert calls[0].options['github']['kind']=='readme'
    with pytest.raises(ValueError):
        usecase.request_content_detail('https://example.org',infer_platform=lambda u:'web',execute_sync=lambda r:{},github={})


def test_registry_routes_github_without_web_or_browser():
    from platform_adapters.factory import register_default_adapters
    from kr_core.registry import PlatformRegistry
    registry=register_default_adapters(target_registry=PlatformRegistry())
    assert registry.platform_for_url('https://github.com/o/r')=='GitHub'
    assert registry.detail_strategy('GitHub').__class__.__name__=='GitHubDetailStrategy'


def test_resume_rejects_changed_identity_and_private_metadata_is_not_saved():
    client=Client()
    content.collect(request(),client=client)
    client.identity='different-auth-context';client.calls=[]
    assert content.collect(request(),client=client)['error']['type']=='AUTH_CONTEXT_CHANGED'
    assert not client.calls
    client=Client();client.repo['private']=True
    result=content.collect(request(checkpoint='private-empty'),client=client)
    assert result['error']['type']=='PRIVATE_SCOPE_NOT_AUTHORIZED'
    with EvidenceAssets().connection() as conn:
        assert not any('"private": true' in EvidenceAssets()._verified_text(dict(row)) for row in conn.execute('SELECT * FROM assets'))
