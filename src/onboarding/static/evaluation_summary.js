// 只读既有摘要。刷新不运行测试、不访问外部平台。
async function refreshEvaluation(){
  const state=document.getElementById('evaluation-state'),detail=document.getElementById('evaluation-detail'),rows=document.getElementById('evaluation-cases');
  if(!state)return;
  state.textContent='读取中';
  try{
    const data=await api('/api/evaluation');
    const available=data.availability==='snapshot',counts=data.counts||{},fresh=(counts.applicability||{}).fresh||0;
    state.textContent=data.availability==='missing'?'尚无评测摘要':data.availability==='invalid'?'摘要校验失败':data.availability==='stale_snapshot'?'上次快照 · 需要更新':data.gate==='BLOCKED'?'有阻断项':data.gate==='PASS'?'开发合同已通过':'验收未完整';
    state.className=data.gate==='BLOCKED'?'eval-danger':available&&data.gate==='PASS'?'eval-success':'eval-warning';
    detail.textContent=data.generated_at?'开发证据 · '+(data.history_runs||0)+' 次记录 · '+fresh+' 条适用 · 更新于 '+new Date(data.generated_at).toLocaleString()+'。另有 '+(data.external_observations||[]).length+' 条外部目标观测，分别保留原身份；不计入本开发版本通过。':'运行受影响评测后会自动产生摘要；此处不以账号已登录代替业务通过。';
    if(data.ci_gate)detail.textContent+=' 最近CI治理批次：'+data.ci_gate.verdict+' / '+(data.ci_gate.applicability==='fresh'?'仍适用':'需重测')+'。';
    const label={PASS:'通过',FAIL:'失败',ERROR:'执行异常',BLOCKED:'被阻断',SKIPPED:'跳过',NOT_RUN:'未实测',fresh:'仍适用',stale:'需重测',unknown:'待核对'};
    rows.innerHTML=(data.cases||[]).map(row=>'<article class="eval-case"><b>'+esc(row.case_id)+'</b><span class="'+(row.verdict==='FAIL'||row.verdict==='ERROR'?'eval-danger':row.verdict==='PASS'&&row.applicability==='fresh'?'eval-success':'eval-warning')+'">'+esc(label[row.verdict]||row.verdict)+' · '+esc(label[row.applicability]||row.applicability)+'</span><small>'+esc(row.layer)+' · '+esc(row.checked_at?new Date(row.checked_at).toLocaleString():'尚无执行记录')+'</small><small>'+esc(row.run_id||'')+'</small></article>').join('');
  }catch(error){state.textContent='读取失败';state.className='eval-warning';detail.textContent='未能读取评测摘要，请重试。'+error.message;rows.replaceChildren();}
}
document.getElementById('evaluation-refresh')?.addEventListener('click',refreshEvaluation);
refreshEvaluation();
