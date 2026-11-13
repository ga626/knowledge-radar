"""筛选已生效但popover卸载的最小反馈；真实站点另以同关键字验收。"""
import json
from pathlib import Path
import shutil

import pytest


def test_background_search_uses_unicode_insert_without_visible_keypress():
    from runtime.process import silent_subprocess_run
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime unavailable')
    module = (Path(__file__).resolve().parents[1] / 'src/runtime/xhs_search_filters.mjs').as_uri()
    script = r'''
import assert from 'node:assert/strict';
import {insertSearchQuery} from MODULE;
const inserted=[];
const page={
  insertText:async text=>inserted.push(text),
  pressKey:async()=>assert.fail('minimized page cannot receive native keypress'),
  cdp:async()=>assert.fail('no visibility guard bypass'),
};
await insertSearchQuery(page,'选品工具 100%');
assert.deepEqual(inserted,['选品工具 100%']);
let attempts=0;
await assert.rejects(insertSearchQuery({insertText:async()=>{attempts++;throw Error('unknown outcome');}},'x'),/unknown outcome/);
assert.equal(attempts,1);
'''.replace('MODULE', json.dumps(module))
    result = silent_subprocess_run([node, '--input-type=module'], input=script,
        capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr


def test_filter_panel_reopens_without_replaying_filter_or_accepting_inactive_chip():
    from runtime.process import silent_subprocess_run
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime unavailable')
    module = (Path(__file__).resolve().parents[1] / 'src/runtime/xhs_search_filters.mjs').as_uri()
    script = r'''
import assert from 'node:assert/strict';
import {repairFilterEvaluation, reopenFilterPanels} from MODULE;
let opens = 0;
const panel = {selected:true};
assert.deepEqual(await reopenFilterPanels(()=>[], async()=>{opens++;return {status:'ok',panel};}),[panel]);
assert.equal(opens,1);
assert.deepEqual(await reopenFilterPanels(()=>[panel,panel], ()=>assert.fail('ambiguous panel reopened')),[panel,panel]);
assert.deepEqual(await reopenFilterPanels(()=>[], async()=>({status:'layout'})),[]);
const expression = `(async()=>{
  const requestedFilters = [];
  let visible=false;
  const panel={classList:{contains:()=>globalThis.testSelected}};
  const panels=()=>visible?[panel]:[];
  const openPanel=async()=>{visible=true;globalThis.testOpens++;return {status:'ok',panel};};
  const isActive = (option) => option.classList.contains('active');
  const currentPanels = panels();
            if (currentPanels.length === 1) {if(!isActive(currentPanels[0]))return 'inactive';}
  else return 'inactive';
  visible=false;
  const finalPanels = panels();
  return finalPanels.length===1&&isActive(finalPanels[0])?'ok':'inactive';
})()`;
globalThis.testSelected=true;globalThis.testOpens=0;
assert.equal(await eval(expression),'inactive');
assert.equal(await eval(repairFilterEvaluation(expression)),'ok');
assert.equal(globalThis.testOpens,2);
globalThis.testSelected=false;
assert.equal(await eval(repairFilterEvaluation(expression)),'inactive');
assert.equal(repairFilterEvaluation('ordinary extraction'),'ordinary extraction');
assert.throws(()=>repairFilterEvaluation('const requestedFilters = []'),/contract_mismatch/);
'''.replace('MODULE', json.dumps(module))
    result = silent_subprocess_run([node, '--input-type=module'], input=script,
        capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr


def test_search_navigation_waits_same_submission_and_preserves_literal_percent():
    from runtime.process import silent_subprocess_run
    node = shutil.which('node')
    if not node:
        pytest.skip('Node runtime unavailable')
    module = (Path(__file__).resolve().parents[1] / 'src/runtime/xhs_search_filters.mjs').as_uri()
    script = r'''
import assert from 'node:assert/strict';
import {repairSearchNavigationEvaluation as repair, searchSubmissionButtons, evaluateSearchNavigation} from MODULE;
const attachment={name:'add'}, submitButton={name:'search'}, hidden={hidden:true};
let buttons=[submitButton,hidden];
const input={id:'search-input-in-feeds',closest:()=>({contains:e=>e===input,querySelectorAll:selector=>{
  assert.equal(selector.includes('ai-input-action-btn'),false);return buttons;
}})};
const visible=e=>!e.hidden;
assert.deepEqual(searchSubmissionButtons(input,()=>assert.fail('unscoped search'),visible),[submitButton]);
buttons=[submitButton,submitButton];
assert.equal(searchSubmissionButtons(input,()=>[],visible).length,2);
assert.deepEqual(searchSubmissionButtons({...input,closest:()=>null},()=>[],visible),[]);
assert.deepEqual(searchSubmissionButtons({id:'search-input'},()=>[submitButton],visible),[submitButton]);
let now=0, clicks=0;
Date.now=()=>now;
globalThis.location={pathname:'/explore',search:''};
globalThis.document={querySelector:()=>now>=12000?{}:null};
globalThis.submit=()=>{clicks++;};
const expression=`(async()=>{
  const query='100%25 学习';
  const button={click:submit};
  const guard='ambiguous_or_missing_search_button';
  const sleep=async ms=>{now+=ms;if(now>=12000){location.pathname=globalThis.ai?'/search_result_ai':'/search_result';location.search='?keyword='+encodeURIComponent(globalThis.ai?encodeURIComponent(query):query);}};
  button.click();
  const deadline = Date.now() + 10000;
  while(Date.now()<deadline){
    const searchRendered = document.querySelector('section.note-item');
    let keyword = new URLSearchParams(location.search).get('keyword');
    try { keyword = decodeURIComponent(keyword || ''); } catch {}
    if(['/search_result','/search_result_ai'].includes(location.pathname) && keyword===query && searchRendered){
      return { status: 'ok', detail: location.pathname };
    }
    await sleep(100);
  }
  return { status: 'timeout', detail: location.pathname };
})()`;
globalThis.now=0;
const original=await eval(expression);
assert.equal(original.status,'timeout');assert.equal(clicks,1);
now=0;clicks=0;location.pathname='/explore';location.search='';
const result=await eval(repair(expression));
assert.equal(result.status,'ok');assert.equal(clicks,1);
assert.equal(result.navigation.query_match,true);
assert.equal(result.navigation.elapsed_ms,12000);
assert.equal(JSON.stringify(result).includes('100%'),false);
now=0;clicks=0;globalThis.ai=true;location.pathname='/explore';location.search='';
const ai=await eval(repair(expression));
assert.equal(ai.status,'ok');assert.equal(ai.navigation.query_match,true);assert.equal(clicks,1);
globalThis.ai=false;
now=0;clicks=0;location.pathname='/explore';location.search='';
globalThis.document={querySelector:()=>null};
const timeout=await eval(repair(expression));
assert.equal(timeout.status,'timeout');assert.equal(now,20000);assert.equal(clicks,1);
assert.equal(timeout.navigation.rendered,false);
assert.equal(repair('ordinary extraction'),'ordinary extraction');
assert.throws(()=>repair(expression.replace('10000','15000')),/contract_mismatch/);
const fill=`const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        input.focus();
        setter.call(input, query);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));`;
const controlled=expression.replace('button.click();',fill+'\nbutton.click();');
const calls=[];
const evaluate=async code=>{
  calls.push(code.includes('input_ready')?'prepare':'submit');
  assert.equal(code.includes('setter.call'),false);
  return code.includes('input_ready')?{status:'input_ready'}:{status:'ok'};
};
assert.equal((await evaluateSearchNavigation(evaluate,async text=>calls.push('native:'+text),controlled,'测试')).status,'ok');
assert.deepEqual(calls,['prepare','native:测试','submit']);
calls.length=0;
assert.equal((await evaluateSearchNavigation(async()=>({status:'auth'}),async()=>assert.fail('typing through login wall'),controlled,'测试')).status,'auth');
await assert.rejects(()=>evaluateSearchNavigation(evaluate,async()=>{throw Error('input result unknown');},controlled,'测试'),/input result unknown/);
assert.deepEqual(calls,['prepare']);
await assert.rejects(()=>evaluateSearchNavigation(evaluate,async()=>{},expression,'测试'),/contract_mismatch/);
'''.replace('MODULE', json.dumps(module))
    # Make the fake DOM clock accessible to the evaluated expression.
    result = silent_subprocess_run([node, '--input-type=module'], input=script,
        capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
