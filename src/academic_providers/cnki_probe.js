const port = process.argv[2];
const options = JSON.parse(process.argv[3] || "{}");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function cdpConnect() {
  const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json());
  let target = targets.find((item) => item.type === "page" && !String(item.url || "").startsWith("devtools://"));
  if (!target) {
    target = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(options.startupUrl || "https://kns.cnki.net/kns8s/search")}`).then((r) => r.json());
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  return { ws, send };
}

async function main() {
  const { ws, send } = await cdpConnect();
  await send("Target.setDiscoverTargets", { discover: true });
  await send("Page.enable");
  await send("Runtime.enable");
  if (options.navigate !== false) {
    await send("Page.navigate", { url: options.startupUrl || "https://kns.cnki.net/kns8s/search" });
    await sleep(4000);
  }
  const expr = `(() => {
    const text = (selector) => Array.from(document.querySelectorAll(selector)).map(el => (el.textContent || '').trim()).filter(Boolean);
    const exists = (selector) => !!document.querySelector(selector);
    const visible = (selector) => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const rowItems = Array.from(document.querySelectorAll('.result-table-list tbody tr')).slice(0, ${Number(options.limit || 10)}).map(row => {
      const pickText = (selector) => {
        const el = row.querySelector(selector);
        return el ? (el.textContent || '').trim().replace(/\\s+/g, ' ') : '';
      };
      const link = row.querySelector('td.name a.fz14, td.name a');
      return {
        title: link ? (link.textContent || '').trim().replace(/\\s+/g, ' ') : pickText('td.name'),
        url: link ? (link.href || '') : '',
        authors: text.call(row, 'td.author a.KnowledgeNetLink, td.author a, td.author').join('; '),
        source: pickText('td.source a, td.source'),
        date: pickText('td.date'),
        quote: pickText('td.quote'),
        download: pickText('td.download'),
        exportId: row.getAttribute('data-id') || row.querySelector('[data-id]')?.getAttribute('data-id') || ''
      };
    }).filter(item => item.title);
    const url = location.href;
    const title = document.title || '';
    const bodyText = (document.body && document.body.innerText || '').slice(0, 1000);
    const selectors = {
      searchInput: exists('input.search-input'),
      searchButton: exists('input.search-btn'),
      resultRows: document.querySelectorAll('.result-table-list tbody tr').length,
      captchaVisible: visible('#tcaptcha_transform_dy') || /verify\\/home|captcha|安全验证/.test(url + ' ' + title + ' ' + bodyText),
      loginLikely: /login|passport|登录|统一认证/.test(url + ' ' + title + ' ' + bodyText),
      authLikely: /无权限|未订购|机构|IP|权限不足|购买|下载受限/.test(bodyText)
    };
    let status = 'SCHEMA_CHANGED';
    let reason = 'expected CNKI search selectors or result rows were not visible';
    if (selectors.captchaVisible) {
      status = 'CAPTCHA_REQUIRED';
      reason = 'CNKI security verification page is visible or URL indicates verification';
    } else if (selectors.loginLikely) {
      status = 'LOGIN_REQUIRED';
      reason = 'CNKI login or institution-auth page is visible';
    } else if (selectors.authLikely) {
      status = 'AUTH_REQUIRED';
      reason = 'CNKI authorization text is visible';
    } else if (rowItems.length > 0) {
      status = 'OK';
      reason = 'CNKI result rows visible';
    } else if (selectors.searchInput && selectors.searchButton) {
      status = 'NEEDS_QUERY';
      reason = 'CNKI search page visible but no result rows were read';
    }
    return { status, reason, url, title, selectors, items: rowItems };
  })()`;
  const result = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  ws.close();
  if (result.result && result.result.exceptionDetails) {
    console.log(JSON.stringify({
      status: "UNKNOWN",
      reason: result.result.exceptionDetails.text || "Runtime.evaluate exception",
      exception: result.result.exceptionDetails
    }));
    return;
  }
  const value = result.result && result.result.result && result.result.result.value;
  console.log(JSON.stringify(value || { status: "UNKNOWN", reason: "Runtime.evaluate returned no value" }));
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
