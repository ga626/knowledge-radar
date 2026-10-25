(() => {
  const isDeveloper = document.body?.dataset.developerView === 'true';
  if (!isDeveloper) return;

  const $ = (id) => document.getElementById(id);
  const text = (id, value) => { const node = $(id); if (node) node.textContent = String(value ?? ''); };
  const rows = (value) => Array.isArray(value) ? value : [];
  const display = (value, fallback = '未记录') => value === undefined || value === null || value === '' ? fallback : String(value);
  const compactNumber = (value) => Number.isFinite(Number(value)) ? String(Number(value)) : '—';
  const enabledDetails = () => document.querySelectorAll('[data-developer-only]').forEach((node) => {
    node.hidden = false;
  });

  enabledDetails();
  const nav = document.querySelector('[data-view="developer"]');
  if (nav) nav.hidden = false;
  const subtitle = document.getElementById('topbar-subtitle');
  if (subtitle) subtitle.textContent = '开发者视图 · 详细信息来自本机只读记录';

  const definitionList = (id, pairs) => {
    const target = $(id);
    if (!target) return;
    target.replaceChildren();
    pairs.forEach(([label, value]) => {
      const dt = document.createElement('dt');
      const dd = document.createElement('dd');
      dt.textContent = label;
      dd.textContent = display(value);
      target.append(dt, dd);
    });
  };

  const render = ({dashboard, evaluation, health, status}) => {
    const installation = dashboard.installation || {};
    const tasks = dashboard.activity?.tasks || {};
    const tools = dashboard.activity?.tools || {};
    const evidenceCount = Number(tasks.evidence_versions || tasks.evidence_version_count || 0);
    const active = Number(tasks.active || 0);
    const recent = rows(tasks.recent_tasks);
    const cases = rows(evaluation.cases);
    const gate = display(evaluation.gate, 'INCOMPLETE');
    text('developer-role', display(health.role, '未知角色'));
    text('developer-role-note', `运行代际 ${display(health.generation, '—')} · ${health.role === 'dev' ? '开发源码预览' : `已安装 ${display(installation.version)}`}`);
    text('developer-research', active ? `${active} 项运行中` : `${recent.length} 项后台任务`);
    text('developer-research-note', recent.length
      ? `${compactNumber(tools.successful)} 次成功工具调用 · ${compactNumber(tasks.completed)} 项完成任务`
      : '当前没有产品态后台任务；研究历史请在“研究记录”中按任务查看');
    text('developer-evaluation', gate);
    text('developer-evaluation-note', `${display(evaluation.availability, '无快照')} · ${cases.length} 个已登记用例`);
    text('developer-evidence', evidenceCount ? `${evidenceCount} 份固定证据` : '按任务回读');
    text('developer-evidence-note', '未取得、阻断和处理中状态保留在研究记录');
    text('developer-runtime-state', health.status === 'ready' ? '本机已连通' : '连接待确认');
    text('developer-quality-state', evaluation.installed_acceptance || '未声明正式验收');
    definitionList('developer-runtime-list', [
      ['控制台角色', health.role],
      ['运行代际', health.generation],
      ['当前运行方式', health.role === 'dev' ? '开发源码预览' : '已安装程序'],
      ['运行指纹', health.fingerprint],
      ['已安装稳定版', installation.version || '未安装'],
      ['已安装渠道', installation.channel || '未安装'],
      ['数据根', installation.data_root_present ? '存在且可读' : '未识别'],
      ['能力接入', `${dashboard.control_plane?.connected_count ?? 0}/${(dashboard.control_plane?.capabilities || []).length}`],
    ]);
    const counts = evaluation.counts || {};
    definitionList('developer-quality-list', [
      ['评测门禁', evaluation.gate],
      ['评测快照', evaluation.availability],
      ['案例状态', JSON.stringify(counts.verdict || {})],
      ['适用范围', JSON.stringify(counts.applicability || {})],
      ['当前后台任务', active],
      ['最近刷新', evaluation.generated_at || '未记录'],
    ]);
  };

  let refreshing = false;
  const refresh = async () => {
    if (refreshing) return;
    refreshing = true;
    text('developer-message', '正在读取开发者信息…');
    try {
      const [dashboard, evaluation, health, status] = await Promise.all([
        window.api('/api/dashboard'),
        window.api('/api/evaluation'),
        window.api('/api/health'),
        window.api('/api/status'),
      ]);
      render({dashboard, evaluation, health, status});
      text('developer-message', '已读取当前本机快照；详细过程仍以研究记录中的任务为单位。');
    } catch (error) {
      text('developer-message', error?.message || '开发者信息读取失败，请刷新重试。');
    } finally {
      refreshing = false;
    }
  };

  $('developer-refresh')?.addEventListener('click', () => void refresh());
  // 侧栏用 replaceState 切换页面，不会触发 hashchange。
  nav?.addEventListener('click', () => void refresh());
  window.addEventListener('hashchange', () => {
    if (location.hash === '#developer') void refresh();
  });
  if (location.hash === '#developer') void refresh();
})();
