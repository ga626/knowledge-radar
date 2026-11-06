(() => {
  const visible = (el) => !!el && !!el.getBoundingClientRect && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
  const txt = (root, selectors) => {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      if (visible(el) && (el.innerText || '').trim()) return el.innerText.trim();
    }
    return '';
  };
  const bodyText = () => (document.body && document.body.innerText || '').trim();
  const riskText = () => [...document.querySelectorAll('body,[role="dialog"]')].map(e => e.innerText || '').join(' ').slice(0, 30000);
  const pageState = () => {
    const text = [...document.querySelectorAll('.sign-dialog,.sign-form,.login-dialog,[role="dialog"],.geetest_panel,.verify-wrap,.captcha-container')].filter(visible).map(e => e.innerText || '').join(' ');
    const path = location.pathname;
    const risk = /安全验证|访问过于频繁|操作太频繁|滑动验证|访问受限|点击验证/.test(text) || /captcha|\/verify\b|\/safe\/|^\/web\/passport\/zp\/security\.html$/.test(path);
    const authenticated_ui = [...document.querySelectorAll('.nav-figure img,.user-name')].some(visible);
    const login = !authenticated_ui && (/扫码登录|微信登录|密码登录|验证码登录|登录.*注册/.test(text) || /^\/web\/user\//.test(path));
    return {risk, login, authenticated_ui, url: location.href, title: document.title};
  };
  const extractSearch = () => {
    const state = pageState();
    const seen = new Set();
    const items = [];
    for (const a of [...document.querySelectorAll('a[href*="/job_detail/"]')]) {
      if (!visible(a)) continue;
      const href = new URL(a.getAttribute('href'), location.href);
      if (!/^\/job_detail\/[^/?]+\.html$/.test(href.pathname)) continue;
      if (!['www.zhipin.com', 'zhipin.com'].includes(href.hostname) || seen.has(href.href)) continue;
      seen.add(href.href);
      const card = a.closest('.job-card-wrapper,.job-card-box,.job-primary,li') || a;
      const take = (selectors) => txt(card, selectors);
      items.push({
        id: (href.pathname.match(/\/job_detail\/([^/.]+)/) || [])[1] || '',
        title: take(['.job-name','.job-title']) || (a.innerText || '').trim().split('\n')[0],
        url: href.href,
        company: take(['.company-name','.company-text a','.company-title','a[href*="/gongsi/"]','a[href*="/gongsir/"]','.boss-name .company-name']),
        salary: take(['.salary','.job-salary']),
        city: take(['.job-area','.company-location']),
        experience: take(['.tag-list li:nth-child(1)','.job-limit .tag-list li']),
        education: take(['.tag-list li:nth-child(2)']),
        desc: (card.innerText || '').trim(),
        salary_status: /[\uE000-\uF8FF]/u.test((card.innerText || '')) ? 'obfuscated_dom' : 'readable_dom',
        platform: 'BOSS直聘', content_type: 'recruitment_job_candidate'
      });
    }
    const empty = !!document.querySelector('.job-empty-wrapper,.search-job-result-empty') || /暂无职位|没有找到/.test(bodyText());
    return { ...state, items, empty, query: new URL(location.href).searchParams.get('query') || '' };
  };
  const extractDetail = () => {
    const state = pageState();
    const salary = txt(document, ['.job-detail-box .job-salary','.salary','.job-salary']);
    const content = txt(document, ['.job-sec-text','.job-detail-box .job-detail-body .desc','.job-detail-section','.job-detail .text','.detail-content']);
    const primary = txt(document, ['.job-sec-text','.job-detail-box .job-detail-body .desc']);
    const title = txt(document, ['.job-detail-box .job-name','h1','.job-name','.name']);
    const company = txt(document, ['.company-name','.company-info .name','.company-info a','.sider-company .company-info','.company-title','a[href*="/gongsi/"]']);
    const privateUse = /[\uE000-\uF8FF]/u.test(salary);
    const detailGate = txt(document, ['.zp-more-info-layer-wrapper','.zp-more-info-layer-text']);
    const detailLogin = /登录.*(?:查看|更多|完整)|(?:查看|更多|完整).*登录/.test(detailGate);
    return {
      ...state, title, company, salary, content, url: location.href,
      content_source: primary && primary === content ? 'job_intro_primary' : 'content_script_dom',
      detail_login_required: detailLogin,
      salary_status: privateUse ? 'obfuscated_dom' : (salary ? 'readable_dom' : 'missing'),
      salary_raw: salary,
      content_type: 'recruitment_job_detail', platform: 'BOSS直聘'
    };
  };
return {
 extract: async (operation) => operation === 'search' ? extractSearch() : operation === 'probe' || operation === 'auth_verify' ? pageState() : extractDetail(),
 ready: (d, op) => op === 'search' ? !!(d.items?.length || d.empty) : op === 'probe' || op === 'auth_verify' ? !!d.authenticated_ui : (d.content || '').length >= 40,
};
})();
