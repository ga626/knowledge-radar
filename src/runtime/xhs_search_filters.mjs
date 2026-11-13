// Search can unmount its popover after a successful chip click. Reopen only the
// popover; never repeat the filter click or replace a filtered query with "all".
export async function reopenFilterPanels(panels, openPanel) {
  const current = panels();
  if (current.length) return current;
  const opened = await openPanel();
  return opened.status === 'ok' ? [opened.panel] : [];
}

export function repairFilterEvaluation(expression) {
  if (typeof expression !== 'string' || !expression.includes('const requestedFilters =')) return expression;
  const active = "const isActive = (option) => option.classList.contains('active');";
  const polling = 'const currentPanels = panels();\n            if (currentPanels.length === 1) {';
  const final = 'const finalPanels = panels();';
  for (const marker of [active, polling, final]) {
    if (expression.split(marker).length !== 2) {
      throw Object.assign(new Error('opencli_search_filter_contract_mismatch'), {code:'opencli_search_filter_contract_mismatch'});
    }
  }
  return expression
    .replace(active, `${active}\nconst reopenFilterPanels = ${reopenFilterPanels.toString()};`)
    .replace(polling, 'const currentPanels = await reopenFilterPanels(panels, openPanel);\n            if (currentPanels.length === 1) {')
    .replace(final, 'const finalPanels = await reopenFilterPanels(panels, openPanel);');
}

// Pinned OpenCLI 2.0.2 submits once, then observes the same SPA navigation.
// Classic search is encoded once; the observed AI route encodes the query twice.
// Match the exact submitted query/its encoding instead of blindly decoding %.
export function searchSubmissionButtons(input, matches, visible) {
  if (!['search-input-in-feeds', 'search-input-ai'].includes(input.id)) {
    return matches('.search-icon, .single-line-search-btn');
  }
  // The feed AI input has an add/attachment button on the left. Submit only
  // through the unique search button in this input's own row.
  const row = input.closest('.single-line-row');
  if (!row || !row.contains(input)) return [];
  return [...row.querySelectorAll('button.single-line-search-btn')].filter(visible);
}

const searchFill = `const prototype = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        input.focus();
        setter.call(input, query);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));`;

export async function insertSearchQuery(page, text) {
  // 扩展的Unicode插入可作用于已绑定的后台输入框；End是无关的可见页
  // 键盘操作，会在最小化窗口抛错。提交仍由后续同页校验/唯一按钮完成。
  await page.insertText(text);
}

export async function evaluateSearchNavigation(evaluate, insertText, expression, query) {
  if (typeof expression !== 'string' || !expression.includes('ambiguous_or_missing_search_button')) return evaluate(expression);
  if (expression.split(searchFill).length !== 2) {
    throw Object.assign(new Error('opencli_search_navigation_contract_mismatch'), {code:'opencli_search_navigation_contract_mismatch'});
  }
  const ready = await evaluate(expression.slice(0,expression.indexOf(searchFill)) +
    "input.focus(); input.select(); return {status:'input_ready'}; })()");
  if (ready?.status !== 'input_ready') return ready;
  // A DOM value setter can leave the site's controlled input empty while the
  // DOM reports the requested value. Use the bridge's native Unicode input.
  // Never submit the AI chat button or replay an uncertain input/submit.
  await insertText(query);
  return evaluate(repairSearchNavigationEvaluation(expression.replace(searchFill,'')));
}

export function repairSearchNavigationEvaluation(expression) {
  if (typeof expression !== 'string' || !expression.includes('ambiguous_or_missing_search_button')) return expression;
  const deadline = 'const deadline = Date.now() + 10000;';
  const decode = "try { keyword = decodeURIComponent(keyword || ''); } catch {}";
  const success = "return { status: 'ok', detail: location.pathname };";
  const timeout = "return { status: 'timeout', detail: location.pathname };";
  for (const marker of ['button.click();', deadline, decode, success, timeout]) {
    if (expression.split(marker).length !== 2) {
      throw Object.assign(new Error('opencli_search_navigation_contract_mismatch'), {code:'opencli_search_navigation_contract_mismatch'});
    }
  }
  const diagnostic = `navigation: {path: location.pathname, query_match: keyword === query,
    rendered: Boolean(searchRendered || document.querySelector('.search-empty-wrapper')),
    elapsed_ms: Date.now() - navigationStarted, budget_ms: 20000}`;
  const normalize = "if (location.pathname === '/search_result_ai' && keyword === encodeURIComponent(query)) keyword = query;";
  const buttons = "const buttons = matches('.search-icon, .single-line-search-btn');";
  if (expression.includes(buttons)) {
    if (expression.split(buttons).length !== 2) {
      throw Object.assign(new Error('opencli_search_navigation_contract_mismatch'), {code:'opencli_search_navigation_contract_mismatch'});
    }
    expression = expression.replace(buttons,
      `const searchSubmissionButtons = ${searchSubmissionButtons.toString()};
       const inputReadyUntil = Date.now() + 5000;
       let buttons = searchSubmissionButtons(input, matches, visible);
       while (!buttons.length && Date.now() < inputReadyUntil) {
         const wall = blocked(); if (wall) return wall;
         if (!input.isConnected || input.value !== query) return {status:'layout',detail:'search_input_not_filled'};
         await sleep(100);
         buttons = searchSubmissionButtons(input, matches, visible);
       }`);
  }
  return expression.replace(deadline, 'const navigationStarted = Date.now(); const deadline = navigationStarted + 20000;')
    .replace(decode, normalize)
    .replace(success, `return { status: 'ok', detail: location.pathname, ${diagnostic} };`)
    .replace(timeout, `let keyword = new URLSearchParams(location.search).get('keyword'); ${normalize}
      const searchRendered = document.querySelector('.search-layout__top > .filter, .filter.ai-chat-filter, section.note-item, section:has(a[href*="/search_result/"])');
      return { status: 'timeout', detail: location.pathname, ${diagnostic} };`);
}
