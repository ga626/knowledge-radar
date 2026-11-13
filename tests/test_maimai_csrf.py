"""实际执行浏览器内请求脚本，覆盖 CSRF 握手及失败边界。"""
import json
import subprocess

import pytest
from playwright._impl._driver import compute_driver_executable
from collectors.platform import maimai


@pytest.mark.parametrize('responses,expected_status,expected_calls', [
    ([{'status': 204, 'token': 'test-only-csrf'}, {'status': 200, 'data': {'result':'ok','user':{'id':'test'}}}], 200, 2),
    ([{'status': 200, 'data': {'result':'ok'}}], 200, 1),
    ([{'status': 204}], 204, 1),
    ([{'status': 204, 'token': 'test-only-csrf'}, {'status': 403, 'data': {'error_type':'CSRF_TOKEN_MISMATCH'}}], 403, 2),
    ([{'status': 429, 'retry': '7200'}], 429, 1),
])
def test_maimai_same_origin_csrf_handshake(responses, expected_status, expected_calls):
    from pathlib import Path
    node, _ = compute_driver_executable()
    harness = r"""
const fs=require('fs'),vm=require('vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));const calls=[];
const sandbox={KRSites:{},AbortController,location:{pathname:'/community/home/recommended',href:'https://maimai.cn/community/home/recommended'},
 document:{querySelectorAll:()=>[]},URLSearchParams,
 fetch:async(url,options)=>{calls.push({url,headers:options.headers,credentials:options.credentials});
 const item=input.responses.shift();if(!item)throw Error('unexpected extra request');
 return {status:item.status,headers:{get:name=>name==='Retry-After'?(item.retry||null):(item.token||null)},json:async()=>item.data||null};}};
vm.createContext(sandbox);vm.runInContext(input.script,sandbox);
sandbox.KRSites.maimai.extract('probe',{},new AbortController().signal).then(value=>process.stdout.write(JSON.stringify({result:value.probe,calls})));
"""
    script=(Path(__file__).resolve().parents[1]/'browser_link/extension/sites/maimai.js').read_text(encoding='utf-8')
    process=subprocess.run([str(node),'-e',harness],input=json.dumps({'script':script,'responses':responses}),capture_output=True,text=True,encoding='utf-8',check=True,timeout=10)
    observed=json.loads(process.stdout);result=observed['result']
    assert result['http_status'] == expected_status
    calls = observed['calls']
    assert len(calls) == expected_calls
    assert all(c['credentials'] == 'same-origin' for c in calls)
    assert not calls[0]['headers'].get('x-csrf-retry')
    if expected_calls == 2:
        assert calls[1]['headers']['x-csrf-retry'] == '1'
        assert calls[1]['headers']['x-csrf-token'] == 'test-only-csrf'
    assert 'test-only-csrf' not in json.dumps(result)
    if expected_status == 429:
        assert result['headers'] == {'retry-after': '7200'}

@pytest.mark.parametrize('probe,expected_failure', [
    ({'http_status':204,'data':None}, 'auth_probe_inconclusive'),
    ({'http_status':403,'data':{'error_type':'CSRF_TOKEN_MISMATCH'}}, 'csrf_handshake_failed'),
])
def test_auth_protocol_failure_is_not_a_login_prompt(probe, expected_failure):
    result=maimai._auth_from_probe(probe, maimai.COMMUNITY_HOME, '')
    assert result['failure_type'] == expected_failure
    assert result['manual_action_required'] is False
    assert not result['error'].get('login_required')
    assert 'login_required' not in result['error']['failure_tags']


@pytest.mark.parametrize('key,kind', [('data','gossip'),('gossipData','gossip'),('feedData','feed')])
def test_detail_snapshot_is_ready_only_for_matching_body(key, kind):
    from pathlib import Path
    node,_=compute_driver_executable()
    harness=r"""
const fs=require('fs'),vm=require('vm'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const url='https://maimai.cn/community/'+input.kind+'-detail/123?egid=fixture';
const script='self.__next_f.push('+JSON.stringify([1,'0:'+JSON.stringify({[input.key]:{id:'123',text:'目标正文 \"quoted\" \\ path'}})])+')';
const context=vm.createContext({KRSites:{},URL,URLSearchParams,AbortController,location:{href:url,pathname:new URL(url).pathname},
 document:{querySelectorAll:s=>s==='script'?[{textContent:script}]:[],querySelector:()=>null}});
vm.runInContext(input.code,context);
context.KRSites.maimai.extract('detail',{},new AbortController().signal).then(d=>process.stdout.write(JSON.stringify({ready:context.KRSites.maimai.ready(d,'detail'),snapshot:d.body_snapshot})));
"""
    code=(Path(__file__).resolve().parents[1]/'browser_link/extension/sites/maimai.js').read_text(encoding='utf-8')
    proc=subprocess.run([str(node),'-e',harness],input=json.dumps(dict(code=code,key=key,kind=kind)),capture_output=True,text=True,encoding='utf-8',check=True)
    value=json.loads(proc.stdout)
    assert value['ready'] and value['snapshot']['text']=='目标正文 \"quoted\" \\ path'
    assert value['snapshot']['target_id']=='123'
