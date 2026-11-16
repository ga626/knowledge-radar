/* YouTube transcript adapter: visible reader UI only, no direct API calls or cookies. */
(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = element => !!element && element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0;
  const target = value => {
    try {
      const url = new URL(value, location.href);
      if (url.protocol !== 'https:' || !['youtube.com', 'www.youtube.com', 'youtu.be'].includes(url.hostname)) return null;
      const id = url.hostname === 'youtu.be' ? url.pathname.slice(1) : url.searchParams.get('v');
      return id ? {id, url: `https://www.youtube.com/watch?v=${id}`} : null;
    } catch { return null; }
  };
  const state = () => ({url: location.href, title: clean(document.querySelector('h1.ytd-watch-metadata, h1.title')) || document.title,
    login: /登录|sign in/i.test(clean(document.body).slice(0, 600)) && !!document.querySelector('tp-yt-paper-dialog,[role="dialog"]'),
    authenticated_ui: !!document.querySelector('ytd-masthead #avatar-btn,yt-img-shadow#avatar')});
  async function transcript(payload, signal, progress) {
    signal.throwIfAborted();
    const current = target(location.href), wanted = target(payload.url);
    if (!current || !wanted || current.id !== wanted.id) return {...state(), failure_type: 'page_identity_changed'};
    const button = [...document.querySelectorAll('button,[role="button"],ytd-menu-service-item-renderer')]
      .find(e => visible(e) && /^(Show transcript|显示文字记录|显示转录|Transcript)$/i.test(clean(e.innerText || e.getAttribute('aria-label'))));
    if (button) { button.click(); progress('transcript_panel_requested'); }
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      signal.throwIfAborted();
      const rows = [...document.querySelectorAll('ytd-transcript-segment-renderer,[role="dialog"] ytd-transcript-segment-renderer')]
        .filter(visible).map(row => clean(row.innerText));
      if (rows.length) return {...state(), transcript: rows.join('\n'), segments: rows.map(text => ({text})),
        language: payload.language || '', source: 'visible_transcript_panel', panel_rows: rows.length};
      await globalThis.KRWaitForChange(signal, 250);
    }
    return {...state(), failure_type: 'transcript_not_exposed', transcript_diagnostics: {
      panel_present: !!document.querySelector('ytd-transcript-renderer,[role="dialog"] ytd-transcript-segment-renderer'),
      button_present: !!button, document_state: document.readyState}};
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
