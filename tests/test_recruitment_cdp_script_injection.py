"""猎聘迁出raw CDP后，实际站点脚本仍须识别卡片及登录操作。"""
import json
import shutil
import subprocess

import pytest
from collectors.platform import liepin
from pathlib import Path


def test_liepin_extraction_and_login_action_execute_without_old_template():
    node=shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    program=r'''const {runInNewContext}=require('node:vm');
const source=JSON.parse(process.argv[1]);
let clicked=0;
const node=(innerText)=>({innerText,getBoundingClientRect:()=>({width:100,height:20})});
const card={...node('Python 工程师 20-30K 北京'),querySelector:s=>s==='.job-title'?node('Python 工程师'):s==='.job-salary'?node('20-30K'):null};
const anchor={...node('Python 工程师'),href:'https://www.liepin.com/job/123.shtml',closest:()=>card};
const button={...node('登录/注册'),click:()=>{clicked++;}};
const document={querySelector:()=>null,querySelectorAll:s=>s.startsWith('a[href*="/job/"')?[anchor,anchor]:s==='a,button'?[button]:[],createElement:()=>({innerHTML:'',textContent:''})};
const context={KRSites:{},document,location:{href:'https://www.liepin.com/zhaopin/',pathname:'/zhaopin/',hostname:'www.liepin.com'},URL};
runInNewContext(source,context);
const data=context.KRSites.liepin.extract();context.KRSites.liepin.login();const action={clicked:clicked>0};
console.log(JSON.stringify({data,action,clicked}));'''
    source=(Path(__file__).resolve().parents[1]/'browser_link/extension/sites/liepin.js').read_text(encoding='utf-8')
    output=subprocess.run([node,'-e',program,json.dumps(source)],
        capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    result=json.loads(output.stdout)
    assert len(result['data']['items'])==1
    assert result['data']['items'][0]['salary']=='20-30K'
    assert result['clicked']==1 and result['action']['clicked']
