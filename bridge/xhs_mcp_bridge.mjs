// XHS MCP Bridge — Node.js helper for MediaCrawler
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const humanDelay = () => sleep(800 + Math.random() * 1200);

function parseResult(content) {
  if (!content) return null;
  const tc = content.find(c => c.type === 'text');
  if (!tc) return null;
  const raw = tc.text;
  try {
    const jm = raw.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
    let data;
    if (jm) {
      let inner = jm[1].trim();
      if (inner.startsWith('"') && inner.endsWith('"')) inner = JSON.parse(inner);
      data = JSON.parse(inner);
    } else { data = JSON.parse(raw); }
    return data;
  } catch(e) {
    try { return JSON.parse(raw); } catch(e2) { return raw; }
  }
}

async function createClient() {
  const transport = new StdioClientTransport({
    command: 'npx', args: ['-y', 'chrome-devtools-mcp@latest', '--browserUrl=http://127.0.0.1:9222'],
    env: { ...process.env, NODE_OPTIONS: '' }
  });
  const client = new Client({ name: 'xhs-mcp', version: '1.0' });
  await client.connect(transport);
  return client;
}

// 搜索
async function cmdSearch(keyword) {
  const client = await createClient();
  try {
    const encoded = encodeURIComponent(keyword);
    await client.callTool({ name: 'navigate_page', arguments: { type: 'url', url: `https://www.xiaohongshu.com/search_result?keyword=${encoded}&source=web_search_result_notes` } });
    await humanDelay();
    await sleep(4000);

    const r = await client.callTool({
      name: 'evaluate_script',
      arguments: {
        function: `() => {
          const text = document.body.innerText;
          const lines = text.split('\\n').filter(l => l.trim().length > 0);
          const skipWords = new Set(['全部','图文','视频','用户','筛选','综合','发现','直播','通知','发布','我','大家都在搜']);
          const results = [];
          const seen = new Set();
          
          // 小红书搜索结果的文本结构: 标题行 → 作者行 → 日期行 → 互动数行
          // 标题特征: 不是纯数字、不在skipWords中、长度适中
          for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            if (line.length < 4 || line.length > 100) continue;
            if (skipWords.has(line)) continue;
            if (/^\\d+$/.test(line)) continue;  // 纯数字跳过
            if (/^[\\d,.万k]+$/.test(line)) continue;  // 只有数字和单位
            
            // 标题特征: 下一行应该是作者名（通常2-6字）
            const nextLine = lines[i+1]?.trim() || '';
            if (nextLine && nextLine.length < 20 && !/^\\d/.test(nextLine) && !skipWords.has(nextLine)) {
              if (!seen.has(line)) {
                seen.add(line);
                const author = nextLine;
                // 再下一行是日期
                const dateLine = lines[i+2]?.trim() || '';
                results.push({ title: line, author, date: dateLine });
              }
            }
          }

          // 也尝试检测a标签的title
          document.querySelectorAll('a[href*="/explore/"]').forEach(a => {
            const title = a.getAttribute('title') || '';
            const href = a.href;
            const idMatch = href.match(/\\/explore\\/([^?/]+)/);
            if (title && title.length > 3 && !seen.has(title) && title.length < 100) {
              seen.add(title);
              results.push({ title: title, author: '', date: '', noteId: idMatch ? idMatch[1] : '' });
            }
          });

          const hasLogin = text.includes('登录') && text.includes('注册');
          return JSON.stringify({ items: results.slice(0, 20), total: results.length, hasLoginPrompt: hasLogin, bodyStart: text.substring(0, 200) });
        }`
      }
    });

    const data = parseResult(r.content);
    if (data && typeof data === 'object') {
      console.log(JSON.stringify({ status: 'ok', type: 'search', keyword, items: data.items || [], total: data.total || 0 }));
    } else {
      console.log(JSON.stringify({ status: 'error', type: 'search', error: 'parse failed' }));
    }
  } catch(e) {
    console.log(JSON.stringify({ status: 'error', type: 'search', error: e.message }));
  } finally { await client.close(); }
}

// 笔记详情（从页面提取）
async function cmdDetail(noteId, xsecToken, xsecSource) {
  const client = await createClient();
  try {
    const url = `https://www.xiaohongshu.com/explore/${noteId}?xsec_token=${xsecToken || ''}&xsec_source=${xsecSource || 'pc_search'}`;
    await client.callTool({ name: 'navigate_page', arguments: { type: 'url', url } });
    await humanDelay();
    await sleep(3000);

    const r = await client.callTool({
      name: 'evaluate_script',
      arguments: {
        function: `() => {
          const info = {};
          const metaTitle = document.querySelector('meta[property="og:title"]');
          info.title = metaTitle ? metaTitle.content : document.title;
          const metaDesc = document.querySelector('meta[name="description"]');
          info.desc = metaDesc ? metaDesc.content : '';
          
          // 提取正文
          const descEl = document.querySelector('[class*="desc"], [class*="content"], [class*="note-text"]');
          if (descEl) info.content = descEl.textContent.trim();
          
          // 提取作者
          const authorEl = document.querySelector('[class*="username"], [class*="author"], [class*="user-name"]');
          info.author = authorEl ? authorEl.textContent.trim() : '';
          
          // 提取互动数据
          const likeEl = document.querySelector('[class*="like"] [class*="count"], [class*="liked"] [class*="count"]');
          info.likedCount = likeEl ? likeEl.textContent.trim() : '';
          
          // 提取图片
          const imgs = [];
          document.querySelectorAll('img[class*="note-image"], [class*="swiper"] img').forEach(img => {
            if (img.src) imgs.push(img.src);
          });
          info.images = imgs.slice(0, 9);

          info.noteId = '${noteId}';
          return JSON.stringify(info);
        }`
      }
    });

    const data = parseResult(r.content);
    if (data && typeof data === 'object') {
      console.log(JSON.stringify({ status: 'ok', type: 'detail', noteId, noteData: data }));
    } else {
      console.log(JSON.stringify({ status: 'error', type: 'detail', error: 'parse failed' }));
    }
  } catch(e) {
    console.log(JSON.stringify({ status: 'error', type: 'detail', error: e.message }));
  } finally { await client.close(); }
}

// 评论提取
async function cmdComments(noteId, xsecToken) {
  const client = await createClient();
  try {
    const url = `https://www.xiaohongshu.com/explore/${noteId}?xsec_token=${xsecToken || ''}`;
    await client.callTool({ name: 'navigate_page', arguments: { type: 'url', url } });
    await sleep(2000);
    
    // 滚动加载评论
    await client.callTool({ name: 'evaluate_script', arguments: { function: `() => { window.scrollTo(0, document.body.scrollHeight); }` } });
    await sleep(3000);

    const r = await client.callTool({
      name: 'evaluate_script',
      arguments: {
        function: `() => {
          const comments = [];
          const seen = new Set();
          
          // 从页面文本提取评论
          const text = document.body.innerText;
          const lines = text.split('\\n').filter(l => l.trim());
          
          let inComment = false;
          let currentUser = '';
          let currentContent = '';
          
          for (const line of lines) {
            const t = line.trim();
            // 评论特征: 用户昵称后接评论内容
            if (t.includes('：') && t.length > 4 && t.length < 200) {
              const parts = t.split('：');
              if (parts[0].length < 20) {
                comments.push({ userNick: parts[0].trim(), content: parts.slice(1).join('：').trim() });
                seen.add(t);
              }
            }
          }
          
          // 尝试DOM提取
          if (comments.length === 0) {
            document.querySelectorAll('[class*="comment"], [class*="reply"]').forEach(el => {
              const txt = el.textContent.trim();
              if (txt.length > 3 && !seen.has(txt)) {
                seen.add(txt);
                comments.push({ content: txt.substring(0, 200), userNick: '' });
              }
            });
          }

          return JSON.stringify({ comments: comments.slice(0, 30), total: comments.length });
        }`
      }
    });

    const data = parseResult(r.content);
    if (data && typeof data === 'object') {
      console.log(JSON.stringify({ status: 'ok', type: 'comments', noteId, comments: data.comments || [], total: data.total || 0 }));
    } else {
      console.log(JSON.stringify({ status: 'error', type: 'comments', error: 'parse failed' }));
    }
  } catch(e) {
    console.log(JSON.stringify({ status: 'error', type: 'comments', error: e.message }));
  } finally { await client.close(); }
}

// 主入口
const [,, cmd, ...args] = process.argv;
switch (cmd) {
  case 'search': await cmdSearch(args[0] || ''); break;
  case 'detail': await cmdDetail(args[0] || '', args[1] || '', args[2] || ''); break;
  case 'comments': await cmdComments(args[0] || '', args[1] || ''); break;
  default: console.log(JSON.stringify({ status: 'error', error: `Unknown: ${cmd}` }));
}
