(() => {
  'use strict';

  
// navigator.webdriver
(() => {
  if (navigator.webdriver !== false && navigator.webdriver !== undefined) {
    delete Object.getPrototypeOf(navigator).webdriver;
  }
  // Also handle the case where navigator.webdriver is a getter
  Object.defineProperty(navigator, 'webdriver', {
    get: () => false,
    configurable: true
  });
})();


  
// chrome.app
(() => {
  if (!window.chrome) {
    window.chrome = {};
  }
  if (!window.chrome.app) {
    window.chrome.app = {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
    };
  }
})();


  
// chrome.csi
(() => {
  if (!window.chrome) {
    window.chrome = {};
  }
  if (!window.chrome.csi) {
    window.chrome.csi = function() {
      return {
        onloadT: Date.now(),
        pageT: Date.now() - performance.timing.navigationStart,
        startE: performance.timing.navigationStart,
        tran: 15
      };
    };
  }
})();


  
// chrome.loadTimes
(() => {
  if (!window.chrome) {
    window.chrome = {};
  }
  if (!window.chrome.loadTimes) {
    window.chrome.loadTimes = function() {
      return {
        commitLoadTime: performance.timing.responseStart / 1000,
        connectionInfo: 'http/1.1',
        finishDocumentLoadTime: performance.timing.domContentLoadedEventEnd / 1000,
        finishLoadTime: performance.timing.loadEventEnd / 1000,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: performance.timing.domContentLoadedEventEnd / 1000,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'unknown',
        requestTime: performance.timing.navigationStart / 1000,
        startLoadTime: performance.timing.navigationStart / 1000,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: false,
        wasNpnNegotiated: false
      };
    };
  }
})();


  
// chrome.runtime
(() => {
  if (!window.chrome) {
    window.chrome = {};
  }
  // Mock chrome.runtime only if it doesn't exist or is empty
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function() { return { onMessage: { addListener: function(){} }, postMessage: function(){} }; },
      sendMessage: function() {},
      PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
      PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
      PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64', MIPS: 'mips', MIPS64: 'mips64' },
      RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
      OnInstalledReason: { INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' },
      OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }
    };
  }
})();


  
// navigator.hardwareConcurrency
(() => {
  const originalHC = navigator.hardwareConcurrency;
  if (!originalHC || originalHC === 0) {
    Object.defineProperty(navigator, 'hardwareConcurrency', {
      get: () => 8,
      configurable: true
    });
  }
})();


  
// navigator.languages
(() => {
  const languages = ['zh-CN', 'zh', 'en-US', 'en'];
  Object.defineProperty(navigator, 'languages', {
    get: () => languages,
    configurable: true
  });
  if (navigator.language !== 'zh-CN') {
    Object.defineProperty(navigator, 'language', {
      get: () => 'zh-CN',
      configurable: true
    });
  }
})();


  
// navigator.vendor
(() => {
  Object.defineProperty(navigator, 'vendor', {
    get: () => 'Google Inc.',
    configurable: true
  });
})();


  
// webgl.vendor
(() => {
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) {
      return 'Intel Inc.';
    }
    if (parameter === 37446) {
      return 'Intel Iris OpenGL Engine';
    }
    return getParameter.apply(this, arguments);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(parameter) {
      if (parameter === 37445) {
        return 'Intel Inc.';
      }
      if (parameter === 37446) {
        return 'Intel Iris OpenGL Engine';
      }
      return getParameter2.apply(this, arguments);
    };
  }
})();


  
// window.outerdimensions
(() => {
  if (window.outerWidth === 0 || window.outerHeight === 0) {
    Object.defineProperty(window, 'outerWidth', {
      get: () => window.innerWidth,
      configurable: true
    });
    Object.defineProperty(window, 'outerHeight', {
      get: () => window.innerHeight + 85,
      configurable: true
    });
  }
})();


  
// navigator.permissions
(() => {
  const originalQuery = navigator.permissions.query;
  navigator.permissions.query = function(parameters) {
    if (parameters.name === 'notifications') {
      return Promise.resolve({ state: Notification.permission });
    }
    return originalQuery.apply(this, arguments);
  };
})();


  
// sourceurl evasion
(() => {
  const originalToString = Error.prototype.toString;
  Error.prototype.toString = function() {
    const result = originalToString.apply(this, arguments);
    // Remove any puppeteer or stealth related source URLs
    return result.replace(/puppeteer|stealth|__puppeteer/gi, '');
  };
})();


  // Override toString to hide modifications
  const nativeToString = Function.prototype.toString;
  const toStringMap = new WeakMap();
  
  Function.prototype.toString = function() {
    if (toStringMap.has(this)) {
      return toStringMap.get(this);
    }
    return nativeToString.call(this);
  };
  
  // Mark toString itself as native
  toStringMap.set(Function.prototype.toString, 'function toString() { [native code] }');
})();
