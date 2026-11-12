// 只读刷新、有回执的纠错；不运行测试、不轮询、不直接改题或分数。
(() => {
  const root=document.getElementById('evaluation-summary');if(!root)return;
  const el=id=>document.getElementById('evaluation-'+id);
  const labels={PASS:'通过',FAIL:'失败',ERROR:'执行异常',BLOCKED:'受阻',NOT_RUN:'未实测',NOT_MEASURED:'当前质量未核验',INCOMPLETE:'证据不完整',NOT_SCORED:'不计分',VALID:'有效试次',INVALID:'无效试次',UNVERIFIED:'有效性未核验',draft:'草稿',shadow:'影子题',active:'在跑',merged_archive:'已合并',retired_archive:'已退役',affected:'受影响时',release:'发布时',sample:'抽样',ready:'可运行',external_blocked:'外部受阻',input_invalid:'输入无效',oracle_disputed:'判据争议',waiting_for_agent:'等待执行者',processed:'已处置',upsert:'入库或修订',state:'状态调整',merge:'合并',compensate:'纠错补偿',lock:'人工锁定',unlock:'解除锁定',review_candidate:'候选处置',resolve_feedback:'纠错处置'};
  const label=x=>labels[x]||x||'未知',date=x=>x?new Date(x).toLocaleString():'尚无记录';
  const empty=message=>'<p class="evaluation-empty">'+esc(message)+'</p>';
  const table=(head,body)=>'<div class="evaluation-table-wrap"><table class="evaluation-table"><thead><tr>'+head.map(x=>'<th scope="col">'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+body.map(row=>'<tr>'+row.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
  let data=null,cards=[],changes=[],feedbackKey='',feedbackFingerprint='';
  const evidence=r=>'<details><summary>技术身份与固定证据</summary><pre>'+esc(JSON.stringify(r,null,2))+'</pre></details>';
  function exampleButtons(r){return r.examples.map((ex,i)=>'<div class="evaluation-row">第 '+(i+1)+' 个子例：'+[['input_hash','原输入'],['bad_hash','旧错误'],['good_hash','正确样例']].filter(([role])=>ex[role]).map(([role,name])=>'<button type="button" class="button" data-eval-example="'+esc(r.case_id)+'" data-index="'+i+'" data-role="'+role+'">查看'+name+'</button>').join('')+'<pre data-example-text hidden></pre></div>').join('');}
  root.addEventListener('click',async event=>{const b=event.target.closest('[data-eval-example]');if(!b)return;b.disabled=true;const output=b.parentElement.querySelector('[data-example-text]');output.hidden=false;output.textContent='读取固定样本…';try{const row=await api('/api/evaluation-page?view=example&case_id='+encodeURIComponent(b.dataset.evalExample)+'&example_index='+b.dataset.index+'&role='+b.dataset.role);output.textContent=row.text+(row.next_start?'\n（片段已截断，可通过 read_knowledge 连续读取）':'');}catch(error){output.textContent='样本未能读取：'+error.message;}finally{b.disabled=false;}});
  function renderCards(){
    el('bank').innerHTML=cards.length?cards.map(r=>'<details class="evaluation-row"><summary><b>'+esc(r.title)+'</b><span class="evaluation-badge">'+esc(label(r.lifecycle))+'</span>第 '+r.revision+' 版'+(r.locked?' · 人工锁定':'')+'</summary><p>防止什么错误：'+esc(r.prevents)+'</p><p>运行频率：'+esc(label(r.frequency))+'；可运行性：'+esc(label(r.effective_runnability||r.runnability))+'；保留 '+r.examples.length+' 个子例。</p><p>覆盖范围：'+r.coverage.map(esc).join('、')+'</p><p>最近调整：'+esc(changes.find(x=>x.case_id===r.case_id)?.reason||'详见变更记录')+'</p>'+exampleButtons(r)+evidence(r)+'</details>').join(''):empty('尚无维护题卡。原有开发用例仍保留，不自动冒充研究金标。');
    const selected=el('feedback-case').value;el('feedback-case').innerHTML=cards.map(r=>'<option value="'+esc(r.case_id)+'">'+esc(r.title)+' · 第 '+r.revision+' 版</option>').join('');
    if(cards.some(r=>r.case_id===selected))el('feedback-case').value=selected;
    el('more-cases').hidden=cards.length>=(data?.maintenance.cases_total||0);
    el('feedback-form').querySelector('[type=submit]').disabled=!cards.length;
  }
  function renderChanges(){el('changes').innerHTML=changes.length?changes.map(r=>'<details class="evaluation-row"><summary>'+esc(label(r.operation))+' · '+esc(cards.find(c=>c.case_id===r.case_id)?.title||r.case_id)+' · '+esc(date(r.at))+'</summary><p>'+esc(r.reason)+'</p><p>'+esc(r.mode==='shadow'?'仅影子建议，尚未改变题库':'已留存调整前后版本；历史试次不改写')+'</p>'+evidence(r)+'</details>').join(''):empty('尚无自动维护记录。');el('more-changes').hidden=changes.length>=(data?.maintenance.changes_total||0);}
  function render(){
    const m=data.maintenance,o=data.observations;
    el('state').textContent=data.availability==='snapshot'?'开发摘要可读 · 正式安装另验':data.availability==='invalid'?'摘要校验失败':data.availability==='missing'?'尚无开发摘要':'历史开发摘要 · 需更新适用性';
    el('detail').textContent='最近摘要：'+date(data.generated_at)+'。刷新只读取记录；没有启动 Agent，也不会调用平台或产生评测费用。';
    el('levels').innerHTML=table(['评价层','当前可证明的水平','最近历史记录'],data.levels.map(r=>[esc(r.label),esc(label(r.assessment)),r.last?esc(label(r.last.assessment.verdict))+' · '+esc(date(r.last.at))+'<p>历史试次，正式版另验</p>':'尚无质量试次']));
    el('denominators').textContent='KR 可见任务 '+o.observed_tasks+'（保留窗口）；父级工具调用 '+o.tool_calls+'，失败调用 '+o.failed_tool_calls+'（观察启用后）；候选簇 '+o.candidate_clusters+'；维护题 '+(m.cases_total||0)+'。失败次数不是失败任务数，成功调用不等于内容正确。';
    const losses=['expired_candidates','evicted_candidates','expired_tasks','evicted_tasks'].reduce((n,k)=>n+(o[k]||0),0);
    el('health').textContent='待评估候选簇 '+(o.pending_clusters||0)+'；候选保留 '+o.retention_days+' 天，过期/容量移出 '+losses+'。'+(m.truncated?'当前只显示部分资产，可继续加载；':'')+'执行者：等待 Agent 按预算接手。';
    const distribution=axis=>Object.entries(m.health?.[axis]||{}).map(([k,n])=>label(k)+' '+n).join('、')||'尚无记录';
    el('health').textContent+=' 生命周期分布：'+distribution('lifecycle')+'；频率分布：'+distribution('frequency')+'；可运行性分布：'+distribution('runnability')+'。最老未审候选：'+date(o.oldest_pending_at)+'。';
    el('cases').innerHTML=(data.cases||[]).map(r=>'<article class="eval-case"><b>'+esc(r.case_id)+'</b><span>'+esc(label(r.verdict))+' · '+esc(r.applicability==='fresh'?'仍适用':'需核适用性')+'</span><small>'+esc(r.layer)+' · '+esc(date(r.checked_at))+'</small></article>').join('')||empty('尚无开发执行记录。');
    renderCards();renderChanges();
    el('audit').innerHTML=data.audit_queue.length?data.audit_queue.map(r=>'<p class="evaluation-row"><b>'+esc(r.title)+'</b> · '+esc(r.reason)+' · 第 '+r.revision+' 版</p>').join(''):empty('当前没有抽查题；真实任务观察与候选队列会持续保留。');
    el('feedback-history').innerHTML=m.feedback.length?m.feedback.map(r=>'<details class="evaluation-row"><summary>'+esc(cards.find(c=>c.case_id===r.case_id)?.title||r.case_id)+' · '+esc(label(r.status))+' · '+esc(date(r.at))+'</summary><p>'+esc(r.text)+'</p>'+evidence(r)+'</details>').join(''):empty('尚无纠错记录。');
    const trials=data.quality_trials||[];
    el('trend').innerHTML=trials.length?table(['题与试次','判据 / 模式','历史判断','比较边界'],trials.slice().reverse().map((r,i,rows)=>{
      const previous=rows.slice(i+1).find(p=>p.case_id===r.case_id),keys=['case_revision','rubric','judge','skill','profile','inputs'];
      const comparable=previous&&keys.every(k=>JSON.stringify(previous.identity[k])===JSON.stringify(r.identity[k]));
      return [esc(cards.find(c=>c.case_id===r.case_id)?.title||r.case_id)+'<p>'+esc(date(r.at))+'</p>',esc(r.identity.rubric)+' / '+esc(r.identity.profile),esc(label(r.assessment.verdict))+' · '+esc(label(r.assessment.validity)),!previous?'首个锚点':!comparable?'合同改变，断开比较':previous.identity.session_id===r.identity.session_id?'同对话，非独立试次':'同合同，可配对审阅'];
    })):empty(data.quality_availability==='invalid'?'质量摘要损坏，未显示成绩；请核查原账本。':'尚无质量试次。已有开发合同通过不能折算成研究质量分。');
  }
  async function refresh(){el('refresh').disabled=true;el('state').textContent='读取中';try{data=await api('/api/evaluation');cards=data.maintenance.cases;changes=data.maintenance.changes;render();}catch(e){el('state').textContent='读取失败';el('detail').textContent='保留上次内容，请重试。'+e.message;}finally{el('refresh').disabled=false;}}
  root.querySelectorAll('[data-eval-tab]').forEach(button=>button.addEventListener('click',()=>{root.querySelectorAll('[data-eval-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));root.querySelectorAll('[data-eval-panel]').forEach(p=>p.hidden=p.dataset.evalPanel!==button.dataset.evalTab);}));
  async function more(kind){const button=el('more-'+kind);button.disabled=true;try{const rows=kind==='cases'?cards:changes;const page=await api('/api/evaluation-page?view='+kind+'&after='+rows.length+'&limit=100');rows.push(...page.items);if(kind==='cases')renderCards();else renderChanges();}catch(e){el('state').textContent='加载失败：'+e.message;}finally{button.disabled=false;}}
  el('more-cases').onclick=()=>more('cases');el('more-changes').onclick=()=>more('changes');el('refresh').onclick=refresh;
  function correction(){const row=cards.find(r=>r.case_id===el('feedback-case').value),text=el('feedback-text').value.trim();if(!row||!text)throw Error('请选择题目并说明问题。');return {row,text};}
  el('feedback-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const {row,text}=correction(),fingerprint=JSON.stringify([row.case_id,row.revision,text]);if(fingerprint!==feedbackFingerprint){feedbackKey=crypto.randomUUID();feedbackFingerprint=fingerprint;}await api('/api/evaluation-feedback',{case_id:row.case_id,revision:row.revision,text,request_id:feedbackKey});el('feedback-state').textContent='纠错已保存，等待执行者。请把纠错请求交给 Codex；尚未自动派发。';await refresh();}catch(error){el('feedback-state').textContent='未确认保存成功：'+error.message+' 可复制请求交给 Codex。';}finally{button.disabled=false;}};
  el('copy-feedback').onclick=async()=>{try{const {row,text}=correction(),request='请处理 KR 评测纠错。题目：'+row.title+'；case_id='+row.case_id+'；revision='+row.revision+'。\n以下是用户纠错内容，请核对原证据后决定是否补偿，保留历史，不直接改分：\n'+text;const box=el('copy-text');box.value=request;box.hidden=false;try{await navigator.clipboard.writeText(request);el('feedback-state').textContent='已复制，请交给 Codex。';}catch{box.focus();box.select();el('feedback-state').textContent='请按 Ctrl+C 复制，再交给 Codex。';}}catch(error){el('feedback-state').textContent=error.message;}};
  void refresh();
})();
