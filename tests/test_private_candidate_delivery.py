"""私有交付权限与制品身份；全部HTTP替身，不向GitHub写入。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import httpx
import pytest

spec=importlib.util.spec_from_file_location('private_candidate_delivery',Path(__file__).parents[1]/'scripts/private_candidate_delivery.py')
delivery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)
SHA='a'*40

def candidate(root):
    archive=root/'KnowledgeRadar.zip';archive.write_bytes(b'fixture immutable archive')
    (root/'candidate-receipt.json').write_text(json.dumps({'source_commit':SHA,'source_dirty':False,
        'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}),encoding='utf-8')
    return root

def transport(root,*,public=False,other=False,token_owner='owner',existing=False,wrong_asset=False,wrong_digest=False):
    calls=[]
    def handle(request):
        calls.append((request.method,request.url.path))
        path=request.url.path
        release={'id':7,'tag_name':'experiment-'+SHA,'prerelease':True,'draft':False,
                 'upload_url':'https://uploads.github.com/repos/owner/private/releases/7/assets{?name,label}'}
        if path=='/user': data={'login':token_owner}
        elif path.endswith('/collaborators'): data=[{'login':'other' if other else 'owner'}]
        elif path.endswith('/releases/7/assets') and request.method=='GET':
            data=[{'name':p.name,'digest':'sha256:'+('bad' if wrong_asset else hashlib.sha256(p.read_bytes()).hexdigest()),'size':p.stat().st_size}
                  for p in root.iterdir()] if existing else []
        elif path.endswith('/releases/7/assets'):
            content=request.read();data={'digest':'sha256:'+('bad' if wrong_digest else hashlib.sha256(content).hexdigest()),'size':len(content)}
        elif '/releases/tags/' in path:
            return httpx.Response(200 if existing else 404,json=release if existing else {})
        elif path.endswith('/releases'):data=release
        elif path=='/repos/owner/private':data={'private':not public,'owner':{'login':'owner','type':'User'}}
        else:raise AssertionError(path)
        return httpx.Response(200,json=data)
    return httpx.MockTransport(handle),calls

@pytest.mark.parametrize('problem',['public','other','token'])
def test_permissions_block_before_writes(tmp_path,problem):
    root=candidate(tmp_path)
    mock,calls=transport(root,public=problem=='public',other=problem=='other',token_owner='stranger' if problem=='token' else 'owner')
    with httpx.Client(transport=mock) as client,pytest.raises(ValueError):
        delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit=SHA)
    assert not any(method!='GET' for method,_ in calls)

def test_wrong_source_or_archive_blocks_before_network(tmp_path):
    root=candidate(tmp_path);mock,calls=transport(root)
    with httpx.Client(transport=mock) as client:
        with pytest.raises(ValueError):delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit='b'*40)
        (root/'KnowledgeRadar.zip').write_bytes(b'changed')
        with pytest.raises(ValueError):delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit=SHA)
    assert not calls

def test_private_uploads_verify_digest_and_access(tmp_path):
    root=candidate(tmp_path);mock,calls=transport(root)
    with httpx.Client(transport=mock) as client:
        result=delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit=SHA)
    assert result['public_release'] is False
    assert sum(method=='POST' for method,_ in calls)==3
    assert calls[-1]==('GET','/repos/owner/private/collaborators')

def test_identical_rerun_reuses_assets_without_writing(tmp_path):
    root=candidate(tmp_path);mock,calls=transport(root,existing=True)
    with httpx.Client(transport=mock) as client:
        delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit=SHA)
    assert all(method=='GET' for method,_ in calls)

@pytest.mark.parametrize('existing',[False,True])
def test_wrong_remote_digest_never_overwritten(tmp_path,existing):
    root=candidate(tmp_path);mock,calls=transport(root,existing=existing,wrong_asset=existing,wrong_digest=not existing)
    with httpx.Client(transport=mock) as client,pytest.raises(ValueError):
        delivery.deliver(root,client=client,repository='owner/private',owner='owner',source_commit=SHA)
    assert not any(method in {'DELETE','PATCH'} for method,_ in calls)

def test_main_requires_private_configuration(monkeypatch,tmp_path):
    monkeypatch.setenv('GITHUB_EVENT_NAME','push');monkeypatch.setenv('GITHUB_REF','refs/heads/main')
    for key in ('KR_DELIVERY_OWNER','KR_PRIVATE_DELIVERY_REPOSITORY','KR_PRIVATE_DELIVERY_TOKEN'):monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr('sys.argv',['delivery','--candidate',str(tmp_path)])
    with pytest.raises(ValueError,match='not configured'):delivery.main()
