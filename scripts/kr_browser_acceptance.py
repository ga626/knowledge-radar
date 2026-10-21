"""显式运行的真实隔离浏览器验收：受控重定向、JS正文、localhost阻断。"""
from pathlib import Path
import argparse
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def run():
    from generic_web import dynamic
    from generic_web.models import GenericWebRequest
    from generic_web.url_policy import URLPolicyError
    attempts=[]
    def fetch(url, **kwargs):
        attempts.append(url)
        if url=='https://example.com/start':
            return SimpleNamespace(status_code=302,headers={'location':'https://example.org/article'},content=b'')
        if url=='https://example.org/article':
            html='<article><h1>Browser acceptance</h1><p>'+('Controlled browser acceptance sample. '*20)+'</p></article><script>document.querySelector("h1").textContent="JS rendered acceptance";fetch("http://127.0.0.1:9/forbidden").then(()=>document.body.dataset.leak="yes").catch(()=>{});</script>'
            return SimpleNamespace(status_code=200,headers={'content-type':'text/html'},content=html.encode())
        raise URLPolicyError('controlled transport blocks every other target')
    with patch.object(dynamic,'guarded_fetch',fetch):
        response=dynamic._collect_dynamic_url_current_process(GenericWebRequest(url='https://example.com/start',use_jina=False,timeout=15),wait_ms=100)
    ok=not response.error and response.final_url=='https://example.org/article' and 'JS rendered acceptance' in response.content and 'http://127.0.0.1:9/forbidden' in attempts
    return {'schema':'knowledgeradar-browser-acceptance/v1','status':'PASS' if ok else 'FAIL',
            'boundary':'real isolated browser with controlled transport; not public-site acceptance',
            'final_url':response.final_url,'content_chars':len(response.content),'attempts':attempts,'error':response.error}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=run();args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));raise SystemExit(result['status']!='PASS')
