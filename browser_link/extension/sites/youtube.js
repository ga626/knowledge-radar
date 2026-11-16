/* YouTube 页面文字记录；不调用隐藏API、不读取Cookie、不把首屏当全量。 */
(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = e => !!e && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
  const target = value => {
    try {
      const url = new URL(value, location.href);
      if (url.protocol !== 'https:' || !['youtube.com', 'www.youtube.com', 'youtu.be'].includes(url.hostname)) return null;
      const id = url.hostname === 'youtu.be' ? url.pathname.slice(1) : url.searchParams.get('v');
      return id ? {id, url: `https://www.youtube.com/watch?v=${id}`} : null;
    } catch { return null; }
  };
  const state = () => ({url: location.href, title: clean(document.querySelector('h1.ytd-watch-metadata, h1.title')?.innerText) || document.title,
    login: /登录|sign in/i.test(clean(document.body?.innerText).slice(0, 600)) && !!document.querySelector('tp-yt-paper-dialog,[role="dialog"]'),
    authenticated_ui: !!document.querySelector('ytd-masthead #avatar-btn,yt-img-shadow#avatar')});
  const transcriptButton = () => [...document.querySelectorAll('button,[role="button"],ytd-menu-service-item-renderer')]
    .find(e => visible(e) && /^(Show transcript|显示文字记录|显示转录|显示转录文稿|Transcript)$/i.test(clean(e.innerText || e.getAttribute('aria-label'))));
  async function transcript(payload, signal, progress) {
    signal.throwIfAborted();
    const current = target(location.href), wanted = target(payload.url);
    if (!current || !wanted || current.id !== wanted.id) return {...state(), failure_type: 'page_identity_changed'};
    let button = transcriptButton();
    if (!button) {
      const expand = document.querySelector('#description-inline-expander #expand, ytd-text-inline-expander #expand');
      if (visible(expand)) { expand.click(); progress('description_expanded'); await globalThis.KRWaitForChange(signal, 250); }
      button = transcriptButton();
    }
    if (button) { button.click(); progress('transcript_panel_requested'); }
    const deadline = Date.now() + 5000;
    let rows = [], previous = '', quiet = 0;
    while (Date.now() < deadline) {
      signal.throwIfAborted();
      if (target(location.href)?.id !== wanted.id) return {...state(), failure_type: 'page_identity_changed'};
      rows = [...document.querySelectorAll('ytd-transcript-segment-renderer,[role="dialog"] ytd-transcript-segment-renderer')]
        .filter(visible).map(row => clean(row.innerText)).filter(Boolean);
      const signature = JSON.stringify(rows);
      quiet = signature === previous ? quiet + 1 : 0;
      previous = signature;
      if (rows.length && quiet >= 2) break;
      await globalThis.KRWaitForChange(signal, 250);
    }
    const diagnostics = {panel_present: !!document.querySelector('ytd-transcript-renderer,[role="dialog"] ytd-transcript-segment-renderer'),
      button_present: !!button, document_state: document.readyState, panel_rows: rows.length,
      coverage: 'visible_dom_rows_only', stable_observations: quiet};
    if (!rows.length) return {...state(), failure_type: 'transcript_not_exposed', transcript_diagnostics: diagnostics};
    const languageNode = document.querySelector('ytd-transcript-renderer [data-language-code], ytd-transcript-renderer [lang]');
    const language = languageNode?.getAttribute('data-language-code') || languageNode?.getAttribute('lang') || '';
    progress('transcript_rows_read');
    return {...state(), transcript: rows.join('\n'), segments: rows.map(text => ({text})),
      language, requested_language: payload.language || '', completeness: 'unverified',
      source: 'visible_transcript_panel', transcript_diagnostics: diagnostics};
  }
  globalThis.KRSites.youtube = {
    async extract(op, payload, signal, progress = () => {}) {
      const current = state();
      if (current.login) return {...current, failure_type: 'login_required'};
      if (op === 'transcript') return transcript(payload, signal, progress);
      if (['probe', 'auth_verify'].includes(op)) return current;
      return {...current, failure_type: 'unsupported_operation'};
    },
    singleShotOps: ['transcript', 'probe', 'auth_verify'],
    ready: (data, op) => !!data.failure_type || (op === 'transcript' ? !!data.transcript : true),
  };
})();
