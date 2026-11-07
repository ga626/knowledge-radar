"""筛选已生效但popover卸载的最小反馈；真实站点另以同关键字验收。"""
import json
from pathlib import Path
import shutil

import pytest


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
