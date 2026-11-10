/* 运行控制独立于业务内核；只在进入页面、点击或一次有界等待时读取。 */
(() => {
  const byId = id => document.getElementById(id);
  const card = byId('runtime-card');
  if (!card) return;
  const key = 'kr-runtime-recovery';
  const token = __TOKEN_JSON__;
  const labels = {ready:'内核已就绪', recovering:'内核正在恢复', circuit_open:'自动恢复已停止',
    stopped:'内核已停止', blocked:'启动条件受阻', maintenance:'正在安装或迁移',
    unresponsive:'进程仍在但未响应', unknown:'暂不能确认内核状态'};
  const reasons = {required_file_missing:'必要文件缺失',required_dependency_missing:'必要依赖无法导入',
    access_denied:'操作被拒绝',data_open_failed:'数据打开失败',configuration_invalid:'配置格式无效',
    storage_unavailable:'存储写入失败',address_in_use:'地址绑定失败',startup_timeout:'启动超时',
    unexpected_exit:'内核意外退出',startup_failed:'启动失败',owner_exit_unobserved:'上次退出未被管理器观测'};
  const terminal = new Set(['succeeded','failed','rejected','no_op']);
  let snapshot = null, intent = null, outcome = null, busy = false, storageFailed = false;
  try { intent = JSON.parse(sessionStorage.getItem(key) || 'null'); }
  catch { storageFailed = true; }
  const text = (id, value) => { byId(id).textContent = value; };
  async function api(path, payload) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 20000);
    try {
      const response = await fetch('/api/runtime'+path, {method:payload?'POST':'GET',
        headers:{'X-KR-Setup-Token':token,'Content-Type':'application/json'},
        ...(payload?{body:JSON.stringify(payload)}:{}), signal:abort.signal});
      const value = await response.json();
      if (!response.ok && !(path === '/recover' && value.status === 'rejected')) throw new Error('runtime_request_unconfirmed');
      return value;
    } finally { clearTimeout(timer); }
  }
  function render() {
    const pending = intent && !terminal.has(outcome?.status);
    byId('runtime-recover').disabled = busy || storageFailed || pending || card.dataset.readOnly === 'true' || !snapshot?.manual_recovery_allowed;
    byId('runtime-refresh').disabled = busy;
    if (!snapshot) return;
    text('runtime-state', labels[snapshot.state] || labels.unknown);
    const r = snapshot.recovery || {};
    text('runtime-progress', `自动额外重启最多 ${r.maximum_restarts ?? '—'} 次，剩余 ${r.remaining_restarts ?? '—'} 次。${snapshot.supervisor?.status === 'unavailable' ? '管理器当前未连接。' : ''}`);
    const f = snapshot.last_failure;
    text('runtime-failure', f ? `最近故障：${reasons[f.code] || '未分类故障'}${f.exit_code != null ? `；退出码 ${f.exit_code}` : ''}。根本原因尚未调查。` : '尚无可用的最近故障记录。');
    text('runtime-impact', snapshot.state === 'ready' ? '可以继续提交任务；旧请求结果仍按原请求号核对。' : '研究调用可能无法执行。不会重发任务、自动调查或修复环境。');
    text('runtime-detail', `安装身份 ${snapshot.source_identity || '未知'}；事件 ${snapshot.incident_id || '无'}；错误码 ${f?.code || '无'}。${card.dataset.readOnly === 'true' ? '开发预览只读，请在正式控制台恢复。' : ''}`);
  }
  async function refresh(wait = false) {
    if (busy) return;
    busy = true; render();
    try {
      const value = await api(wait?'/wait':'', wait?{wait_seconds:15}:null);
      snapshot = value.runtime;
      if (!snapshot) throw new Error('missing_runtime_snapshot');
      if (!intent && snapshot.recovery?.request_id) {
        // 标签页关闭会丢sessionStorage；服务端的已消费意图仍是权威。
        intent = {request_id:snapshot.recovery.request_id,expected_source:snapshot.source_identity,
          expected_incident:snapshot.incident_id};
        sessionStorage.setItem(key, JSON.stringify(intent));
      }
      if (intent) {
        try { outcome = intent.local_outcome || await api('/receipt', {request_id:intent.request_id}); }
        catch { outcome = {status:'unknown'}; }
        text('runtime-message', outcome.status === 'succeeded' ? '这次恢复已完成；当前运行状态请见上方。' :
          outcome.status === 'no_op' ? '内核已就绪，这次请求未启动新进程。' :
          outcome.status === 'failed' ? '本次恢复失败，已停止本次尝试。是否调查原因由你决定。' :
          outcome.status === 'rejected' ? '恢复请求已被拒绝，请刷新核对安装与事件。' :
          outcome.status === 'unknown' ? '原恢复请求结果未知，保留请求号，不自动重发。' : '恢复请求已接收，等待管理器确认；可手动刷新。');
      }
    } catch {
      snapshot = null;
      text('runtime-state', '无法联系运行管理器');
      text('runtime-impact', '尚不能确认内核状态，请使用本机启动入口或同源 CLI 核查。');
      text('runtime-message', intent?'原请求结果尚未确认；保留请求号，刷新只查结果。':'连接未确认，未发送恢复请求。');
    } finally { busy = false; render(); }
  }
  async function recover() {
    if (byId('runtime-recover').disabled || busy || !snapshot) return;
    busy = true; render();
    try {
      intent = {request_id:crypto.randomUUID(),expected_source:snapshot.source_identity,expected_incident:snapshot.incident_id};
      sessionStorage.setItem(key, JSON.stringify(intent));
      outcome = await api('/recover', intent);
      if (outcome.recovery_request_id) {
        intent.request_id = outcome.recovery_request_id;
        sessionStorage.setItem(key, JSON.stringify(intent));
      }
      if (terminal.has(outcome.status)) {
        // 明确未受理的意图没有服务端收据；保留拒绝事实，不能查空后改写成未知。
        intent.local_outcome = {status:outcome.status};
        sessionStorage.setItem(key, JSON.stringify(intent));
      }
      text('runtime-message', '恢复请求已接收，等待真实就绪；关闭页面不取消这次尝试。');
    } catch {
      outcome = {status:'unknown'};
      text('runtime-message', '恢复结果未知，保留原请求号；请刷新核对，不重复启动。');
    } finally { busy = false; render(); }
    await refresh(['accepted','pending','running'].includes(outcome?.status));
  }
  byId('runtime-refresh').addEventListener('click', () => refresh());
  byId('runtime-recover').addEventListener('click', recover);
  render();
  void refresh();
})();
