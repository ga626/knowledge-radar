/* Shared with the Node runners. No platform names, network calls or page actions. */
(() => {
  function decide(state = {}, previous = {}) {
    const interaction = state.interaction;
    if (interaction?.visible === true && ['login', 'captcha', 'slider', 'qr'].includes(interaction.kind)) {
      return {action: previous.presented ? 'wait' : 'foreground', reason: 'interaction_' + interaction.kind, manual: true};
    }
    const v = state.visibility;
    if (!previous.presented && v?.hidden === true && v.dom_ready === true && v.stalled === true
        && ['zero_viewport', 'explicit_visibility_notice', 'collapsed_layout_after_repair'].includes(v.reason)
        && (v.reason !== 'collapsed_layout_after_repair' || v.repair_attempted === true)) {
      return {action: 'foreground', reason: 'visibility_' + v.reason, manual: false};
    }
    // A timeout, HTTP status, risk flag, empty result or hidden document alone is not evidence.
    return {action: previous.presented ? 'background' : 'none', reason: '', manual: false};
  }

  // Run only in a KR-owned page. Return signal kinds, never page text or credentials.
  function observe() {
    const visible = el => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const dialogs = [...document.querySelectorAll('[role="dialog"],dialog[open],.login-container,.captcha-container,.verify-container,form')].filter(visible);
    const text = dialogs.map(e => e.innerText || '').join(' ').slice(0, 2500);
    let kind = '';
    if (dialogs.length && /滑块|拖动.*验证|slide.*verif/i.test(text)) kind = 'slider';
    else if (dialogs.some(e => e.matches('.captcha-container,.verify-container')
      || e.querySelector('iframe[src*="captcha"],iframe[src*="verify"],input[autocomplete="one-time-code"],input[name*="captcha"]'))
      || (dialogs.length && /验证码|人机验证|captcha|verify you are human/i.test(text))) kind = 'captcha';
    else if (dialogs.length && /扫码|二维码|scan.*(?:code|qr)/i.test(text)
      && dialogs.some(e => e.querySelector('img,canvas,svg'))) kind = 'qr';
    else if (dialogs.length && /登录|sign in|log in/i.test(text)
      && dialogs.some(e => e.querySelector('input,button,[role="button"]'))) kind = 'login';
    // Standalone challenge frames must be displayed; a hidden captcha library is insufficient.
    if (!kind && [...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="recaptcha"],iframe[src*="hcaptcha"]')].some(visible)) kind = 'captcha';
    const hidden = document.hidden === true;
    const ready = document.readyState === 'complete';
    const notice = (document.body?.innerText || '').slice(0, 1500);
    const explicit = /(?:请|需要)(?:先)?(?:切回|激活|保持).*?(?:当前页面|此页面|标签页)|(?:activate|return to|keep).*?(?:this (?:tab|page)).*?(?:load|continue)/i.test(notice);
    return {interaction: {kind, visible: Boolean(kind)}, visibility: {hidden, dom_ready: ready,
      stalled: false, reason: explicit ? 'explicit_visibility_notice' : (innerWidth <= 0 || innerHeight <= 0) ? 'zero_viewport' : ''}};
  }

  globalThis.KRVisibilityPolicy = Object.freeze({decide, observe});
  // OpenCLI derivative uses this same inert content script. It watches only after
  // its background has proven ownership and explicitly addressed this tab.
  if(typeof chrome!=='undefined' && typeof document!=='undefined') {
    let cancelWatch=()=>{};
    chrome.runtime.onMessage.addListener((message,sender)=>{
      if(sender.id!==chrome.runtime.id || message?.kind!=='kr-watch-attention')return;
      cancelWatch();
      let clearAt=0,done=false;
      const stop=()=>{done=true;observer.disconnect();clearTimeout(timer);clearTimeout(checkTimer);};
      const check=()=>{
        if(done)return;
        if(observe().interaction.visible)clearAt=0;
        else if(!clearAt)clearAt=Date.now();
        else if(Date.now()-clearAt>=600){
          stop();void chrome.runtime.sendMessage({kind:'kr-attention-cleared'}).catch(()=>{});return;
        }
        clearTimeout(checkTimer);checkTimer=setTimeout(check,300);
      };
      const observer=new MutationObserver(check);let checkTimer;
      const timer=setTimeout(stop,300000);cancelWatch=stop;
      observer.observe(document.documentElement,{attributes:true,childList:true,subtree:true,characterData:true});
      check();
    });
  }
})();
