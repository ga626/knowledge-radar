// XHS MCP Bridge — 正式版：完整搜索/详情/评论
const { Client } = require("@modelcontextprotocol/sdk/client");
const { StdioClientTransport } = require("@modelcontextprotocol/sdk/client/stdio.js");

const chromeDebugPort = process.env.KR_XHS_CHROME_DEBUG_PORT || process.env.KR_CHROME_DEBUG_PORT || "12733";
const chromeBrowserUrl = "http://127.0.0.1:" + chromeDebugPort;
const selectorBundleVersion = "xhs-selector-bundle-20260613-v1";

const sleep = function(ms) { return new Promise(function(r) { setTimeout(r, ms); }); };
const humanDelay = function() { return sleep(800 + Math.random() * 1200); };

function parseResult(content) {
  if (!content) return null;
  var tc = content.find(function(c) { return c.type === "text"; });
  if (!tc) return null;
  var raw = tc.text;
  if (/Could not connect to Chrome|Failed to fetch browser webSocket URL|Check if Chrome is running/i.test(raw)) {
    return { error: raw.trim() };
  }
  try {
    // chrome-devtools-mcp wraps: Script ran on page and returned:\n```json\n"..."```
    var jm = raw.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
    var str = jm ? jm[1].trim() : raw;
    if (str.startsWith('"') && str.endsWith('"')) {
      str = JSON.parse(str);
    }
    return JSON.parse(str);
  } catch(e) {
    try {
      var objMatch = raw.match(/\{[\s\S]*\}/);
      if (objMatch) return JSON.parse(objMatch[0]);
    } catch(e2) {}
    return null;
  }
}

function createClient() {
  var transport = new StdioClientTransport({
    command: "node",
    args: [
      require("path").join(__dirname, "node_modules", "chrome-devtools-mcp", "build", "src", "bin", "chrome-devtools-mcp.js"),
      "--browserUrl=" + chromeBrowserUrl
    ],
    env: Object.assign({}, process.env, { NODE_OPTIONS: "" })
  });
  var c = new Client({ name: "xhs-bridge", version: "1.0" });
  return c.connect(transport).then(function() { return c; });
}

// ── 搜索 ──────────────────────────────────────
function cmdSearch(keyword, feedType) {
  return createClient().then(function(client) {
    var encoded = encodeURIComponent(keyword);
    var url = "https://www.xiaohongshu.com/search_result?keyword=" + encoded + "&source=web_search_result_notes";
    if (feedType === "image") url += "&type=51";
    if (feedType === "video") url += "&type=video";

    return client.callTool({ name: "navigate_page", arguments: { type: "url", url: url } })
    .then(function() { return humanDelay(); })
    .then(function() { return sleep(5000); })
    .then(function() {
      var feedHint = feedType || "image";
      var searchFn = [
        "() => {",
        "  var text = document.body.innerText || '';",
        "  var lines = text.split('\\n').filter(function(l) { return l.trim().length > 0; });",
        "  var results = [];",
        "  var seen = {};",
        "  var feedHint = '" + feedHint + "';",
        "",
        "  // === 策略 1: a[href*=\"/search_result/\"] 取 textContent（当前小红书版本） ===",
        "  var searchLinks = document.querySelectorAll('a[href*=\"/search_result/\"]');",
        "  for (var j = 0; j < searchLinks.length; j++) {",
        "    var a = searchLinks[j];",
        "    var title = a.textContent.trim();",
        "    var href = a.href;",
        "    var idMatch = href.match(/search_result\\/([^?/]+)/);",
        "    var tokenMatch = href.match(/xsec_token=([^&]+)/);",
        "    if (title && title.length > 3 && title.length < 100 && idMatch && !seen[title]) {",
        "      seen[title] = true;",
        "      results.push({ title: title, author: '', date: '', noteId: idMatch[1], xsecToken: tokenMatch ? tokenMatch[1] : '', noteType: feedHint || 'image' });",
        "    }",
        "  }",
        "",
        "  // === 策略 2: 回退 a[href*=\"/explore/\"] 取 title 属性（兼容旧版） ===",
        "  if (results.length === 0) {",
        "    var anchors = document.querySelectorAll('a[href*=\"/explore/\"]');",
        "    for (var k = 0; k < anchors.length; k++) {",
        "      var ea = anchors[k];",
        "      var et = ea.getAttribute('title') || ea.getAttribute('aria-label') || ea.textContent.trim() || '';",
        "      var eh = ea.href;",
        "      var eid = eh.match(/\\/explore\\/([^?/]+)/);",
        "      if (et && et.length > 3 && et.length < 100 && !seen[et]) {",
        "        seen[et] = true;",
        "        results.push({ title: et, author: '', date: '', noteId: eid ? eid[1] : '', xsecToken: '', noteType: feedHint || 'image' });",
        "      }",
        "    }",
        "  }",
        "",
        "  if (results.length === 0) {",
        "    var cards = document.querySelectorAll('[class*=\"note-item\"], [class*=\"feeds-page\"] section, [class*=\"cover\"], [data-v-]');",
        "    for (var c = 0; c < cards.length; c++) {",
        "      var card = cards[c];",
        "      var link = card.querySelector && (card.querySelector('a[href*=\"/explore/\"]') || card.closest && card.closest('a[href*=\"/explore/\"]'));",
        "      if (!link) continue;",
        "      var href = link.href || '';",
        "      var id = href.match(/\\/explore\\/([^?/]+)/);",
        "      var titleNode = card.querySelector && (card.querySelector('[class*=\"title\"]') || card.querySelector('[class*=\"desc\"]') || card.querySelector('span'));",
        "      var titleText = (titleNode ? titleNode.textContent : card.textContent || '').trim().split('\\n')[0].trim();",
        "      var token = href.match(/xsec_token=([^&]+)/);",
        "      if (id && titleText && titleText.length > 3 && titleText.length < 100 && !seen[id[1]]) {",
        "        seen[id[1]] = true;",
        "        results.push({ title: titleText, author: '', date: '', noteId: id[1], xsecToken: token ? token[1] : '', noteType: feedHint || 'image' });",
        "      }",
        "    }",
        "  }",
        "",
        "  var hasLogin = text.indexOf('登录') >= 0 && text.indexOf('注册') >= 0;",
        "  hasLogin = hasLogin || /登录后查看搜索结果|扫码|登录|注册|login/i.test(text);",
        "  return JSON.stringify({ items: results.slice(0, 20), total: results.length, hasLoginPrompt: hasLogin });",
        "}"
      ].join("\n");

      return client.callTool({ name: "evaluate_script", arguments: { function: searchFn } });
    })
    .then(function(r) {
      var data = parseResult(r.content);
      if (data && data.error) {
        console.log(JSON.stringify({ status: "error", type: "search", error: data.error }));
        return;
      }
      if (data && typeof data === "object") {
        console.log(JSON.stringify({
          status: "ok", type: "search", keyword: keyword,
          feedType: feedType || "all",
          items: data.items || [],
          total: data.total || 0,
          hasLoginPrompt: !!data.hasLoginPrompt
        }));
      } else {
        console.log(JSON.stringify({ status: "error", type: "search", error: "parse failed" }));
      }
    })
    .catch(function(e) {
      console.log(JSON.stringify({ status: "error", type: "search", error: e.message }));
    })
    .then(function() { return client.close(); });
  });
}

// ── 详情 ──────────────────────────────────────
function cmdDetail(noteId, xsecToken, xsecSource) {
  return createClient().then(function(client) {
    var url = "https://www.xiaohongshu.com/explore/" + noteId + "?xsec_token=" + (xsecToken || "") + "&xsec_source=" + (xsecSource || "pc_search");
    return client.callTool({ name: "navigate_page", arguments: { type: "url", url: url } })
    .then(function() { return humanDelay(); })
    .then(function() { return sleep(3000); })
    .then(function() {
      var detailFn = [
        "() => {",
        "  var info = {};",
        "  var bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';",
        "  var selectorHitsByField = { title: 0, body: 0, author: 0, interaction: 0 };",
        "  function clean(v) { return (v || '').replace(/\\s+/g, ' ').trim(); }",
        "  function firstText(field, selectors, attr) {",
        "    for (var s = 0; s < selectors.length; s++) {",
        "      var node = document.querySelector(selectors[s]);",
        "      var value = node ? clean(attr ? (node.getAttribute(attr) || node[attr] || '') : node.textContent) : '';",
        "      if (value) { selectorHitsByField[field] += 1; return value; }",
        "    }",
        "    return '';",
        "  }",
        "  info.selector_bundle_version = '" + selectorBundleVersion + "';",
        "  var mt = document.querySelector('meta[property=\"og:title\"]');",
        "  info.title = mt ? mt.content : document.title;",
        "  var md = document.querySelector('meta[name=\"description\"]');",
        "  info.desc = md ? md.content : '';",
        "  info.noteId = '" + noteId + "';",
        "  info.textSample = bodyText.slice(0, 800);",
        "  info.platformState = /登录|扫码|验证|安全验证|页面不见了|访问的页面不见了/.test(bodyText) ? 'needs_review' : 'content_candidate';",
        "",
        "  // 提取正文内容",
        "  info.content = firstText('body', ['#detail-desc', '[class*=\"note-content\"]', '[class*=\"note-text\"]', '[class*=\"desc\"]', '[class*=\"content\"]']);",
        "",
        "  // 提取作者",
        "  info.author = firstText('author', ['[class*=\"author\"] span', '[class*=\"user\"] span', '.username', '[class*=\"author\"]', '[class*=\"user\"]']);",
        "",
        "  // 提取图片 URL",
        "  var imgs = [], seen = {};",
        "  function addImage(src) {",
        "    src = (src || '').trim();",
        "    if (!src || src.indexOf('data:') === 0) return;",
        "    if (src.indexOf('//') === 0) src = 'https:' + src;",
        "    if (!/^https?:\\/\\//.test(src)) return;",
        "    if (!/(xhscdn|sns-img|xiaohongshu|ci\\.xiaohongshu|xhs)/i.test(src)) return;",
        "    if (!seen[src]) { seen[src] = true; imgs.push(src); }",
        "  }",
        "  var imgEls = document.querySelectorAll('img');",
        "  for (var i = 0; i < imgEls.length; i++) {",
        "    var el = imgEls[i];",
        "    addImage(el.currentSrc || el.src || el.getAttribute('data-src') || el.getAttribute('data-original') || '');",
        "    var srcset = el.getAttribute('srcset') || '';",
        "    if (srcset) srcset.split(',').forEach(function(part) { addImage(part.trim().split(/\\s+/)[0]); });",
        "  }",
        "  var styled = document.querySelectorAll('[style*=\"background-image\"]');",
        "  for (var j = 0; j < styled.length; j++) {",
        "    var bg = styled[j].style && styled[j].style.backgroundImage || '';",
        "    var m = bg.match(/url\\([\"']?([^\"')]+)[\"']?\\)/);",
        "    if (m) addImage(m[1]);",
        "  }",
        "  info.images = imgs;",
        "  selectorHitsByField.title += info.title ? 1 : 0;",
        "  info.selector_hits_by_field = selectorHitsByField;",
        "  info.selector_hit_count = Object.keys(selectorHitsByField).reduce(function(sum, key) { return sum + Number(selectorHitsByField[key] || 0); }, 0);",
        "  info.text_len = bodyText.length;",
        "  info.image_count = imgs.length;",
        "",
        "  return JSON.stringify(info);",
        "}"
      ].join("\n");
      return client.callTool({ name: "evaluate_script", arguments: { function: detailFn } });
    })
    .then(function(r) {
      var data = parseResult(r.content);
      console.log(JSON.stringify({ status: "ok", type: "detail", noteId: noteId, noteData: (data && typeof data === "object") ? data : {} }));
    })
    .catch(function(e) {
      console.log(JSON.stringify({ status: "error", type: "detail", error: e.message }));
    })
    .then(function() { return client.close(); });
  });
}

// ── 评论 ──────────────────────────────────────
function cmdComments(noteId, xsecToken) {
  return createClient().then(function(client) {
    var url = "https://www.xiaohongshu.com/explore/" + noteId + "?xsec_token=" + (xsecToken || "");
    return client.callTool({ name: "navigate_page", arguments: { type: "url", url: url } })
    .then(function() { return sleep(2000); })
    .then(function() {
      return client.callTool({ name: "evaluate_script", arguments: { function: "() => { window.scrollTo(0, document.body.scrollHeight); }" } });
    })
    .then(function() { return sleep(3000); })
    .then(function() {
      var cmtFn = [
        "() => {",
        "  var cmts = [];",
        "  var seen = {};",
        "  var els = document.querySelectorAll('[class*=\"comment\"]');",
        "  for (var i = 0; i < els.length; i++) {",
        "    var t = els[i].textContent.trim();",
        "    if (t.length > 3 && !seen[t]) {",
        "      seen[t] = true;",
        "      cmts.push({ content: t.substring(0, 200), userNick: '' });",
        "    }",
        "  }",
        "  return JSON.stringify({ comments: cmts.slice(0, 30), total: cmts.length });",
        "}"
      ].join("\n");
      return client.callTool({ name: "evaluate_script", arguments: { function: cmtFn } });
    })
    .then(function(r) {
      var data = parseResult(r.content);
      console.log(JSON.stringify({ status: "ok", type: "comments", noteId: noteId, comments: (data && data.comments) || [], total: (data && data.total) || 0 }));
    })
    .catch(function(e) {
      console.log(JSON.stringify({ status: "error", type: "comments", error: e.message }));
    })
    .then(function() { return client.close(); });
  });
}

// ── 入口 ──────────────────────────────────────
var cmd  = process.argv[2];
var args = process.argv.slice(3);
var typeArg = (args[1] || "").toLowerCase();

// StdioClientTransport owns stdout; mirror JSON results to stderr so Python can read them.
console.log = function() {
  var msg = Array.prototype.join.call(arguments, ' ');
  process.stderr.write(msg + '\n');
};

var p;
switch (cmd) {
  case "search":    p = cmdSearch(   args[0] || "", typeArg); break;
  case "detail":    p = cmdDetail(   args[0] || "", args[1] || "", args[2] || ""); break;
  case "comments":  p = cmdComments( args[0] || "", args[1] || ""); break;
  default: process.stderr.write(JSON.stringify({ status: "error", error: "unknown cmd" }) + '\n'); process.exit(1);
}
if (p) p.then(function() { process.exit(0); });
