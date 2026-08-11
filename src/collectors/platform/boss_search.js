/**
 * BOSS直聘 CDP 搜索脚本（独立文件）
 *
 * 用法: node boss_search.js <port> <keyword> [city] [limit]
 * 输出: JSON 到 stdout
 *
 * 设计原则：不在 Python 中嵌入 JS，彻底避免字符转义问题。
 */

const http = require('http');
const ws = require('ws');

const port = process.argv[2] || '9337';
const keyword = process.argv[3] || '';
const city = process.argv[4] || '';
const limit = Number(process.argv[5] || 15);

const CITY_CODES = {
    '北京': '101010100',
    '上海': '101020100',
    '广州': '101280100',
    '深圳': '101280600',
    '杭州': '101210100',
    '成都': '101270100',
    '南京': '101190100',
    '武汉': '101200100',
    '西安': '101110100',
    '苏州': '101190400',
};

function buildSearchUrl(keywordValue, cityValue) {
    const params = new URLSearchParams();
    params.set('query', keywordValue || '');
    const normalizedCity = (cityValue || '').trim();
    if (normalizedCity) params.set('city', CITY_CODES[normalizedCity] || normalizedCity);
    return `https://www.zhipin.com/web/geek/jobs?${params.toString()}`;
}

async function fetchJson(url) {
    return new Promise((resolve, reject) => {
        http.get(url, res => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try { resolve(JSON.parse(data)); }
                catch (e) { reject(new Error('JSON 解析失败: ' + data.slice(0, 200))); }
            });
        }).on('error', reject);
    });
}

function cdpSend(wsClient, method, params, sessionId) {
    return new Promise((resolve, reject) => {
        const id = cdpSend._seq = (cdpSend._seq || 0) + 1;
        const timeout = setTimeout(() => reject(new Error('CDP 超时: ' + method)), 15000);

        const handler = (raw) => {
            const msg = JSON.parse(raw.toString());
            if (msg.id === id) {
                clearTimeout(timeout);
                wsClient.removeListener('message', handler);
                if (msg.error) reject(new Error('CDP 错误: ' + JSON.stringify(msg.error)));
                else resolve(msg);
            }
        };
        wsClient.on('message', handler);

        const message = { id, method, params };
        if (sessionId) message.sessionId = sessionId;
        wsClient.send(JSON.stringify(message));
    });
}

async function main() {
    // 1. 获取页面列表
    const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
    const page = targets.find(t => t.type === 'page' && (t.url || '').includes('zhipin.com'))
        || targets.find(t => t.type === 'page');

    if (!page) {
        console.log(JSON.stringify({ items: [], error: 'no_page_target' }));
        return;
    }

    // 2. 连接 CDP
    const version = await fetchJson(`http://127.0.0.1:${port}/json/version`);
    const client = new ws.WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { client.on('open', resolve); client.on('error', reject); });

    try {
        // 3. 附加到页面
        const attached = await cdpSend(client, 'Target.attachToTarget', { targetId: page.id, flatten: true });
        const sessionId = attached.result.sessionId;
        await cdpSend(client, 'Runtime.enable', {}, sessionId);

        // 4. 按需导航
        const searchUrl = buildSearchUrl(keyword, city);
        await cdpSend(client, 'Page.navigate', { url: searchUrl }, sessionId);
        await new Promise(r => setTimeout(r, 12000));

        // 5. 提取职位卡片（带重试）
        const extractExpr = `
            (() => {
                const items = [];
                const text = document.body ? document.body.innerText || '' : '';
                const isBlocked = /安全验证|滑动验证|captcha|verify/i.test(text);
                if (isBlocked) return JSON.stringify({ items: [], blocked: true, textSample: text.slice(0, 500) });

                const cards = document.querySelectorAll('.job-card-box');
                for (const card of cards) {
                    try {
                        const title = card.querySelector('.job-name') ? card.querySelector('.job-name').textContent.trim() : '';
                        const salary = card.querySelector('.job-salary') ? card.querySelector('.job-salary').textContent.trim() : '';
                        const company = card.querySelector('.boss-name') ? card.querySelector('.boss-name').textContent.trim() : '';
                        const area = card.querySelector('.company-location') ? card.querySelector('.company-location').textContent.trim() : '';
                        const tags = Array.from(card.querySelectorAll('.tag-list li')).map(t => t.textContent.trim()).filter(Boolean);
                        const href = card.querySelector('.job-name') ? (card.querySelector('.job-name').href || '') : '';
                        if (title) items.push({ title, salary, company, area, tags, url: href, platform: 'BOSS直聘' });
                    } catch (e) { /* 跳过异常卡片 */ }
                    if (items.length >= ${limit}) break;
                }
                return JSON.stringify({ items, cardCount: document.querySelectorAll('.job-card-box').length, url: location.href, title: document.title });
            })()
        `;

        let result = { items: [] };
        for (let retry = 0; retry < 3; retry++) {
            const evaluated = await cdpSend(client, 'Runtime.evaluate', {
                expression: extractExpr,
                returnByValue: true,
                awaitPromise: true,
            }, sessionId);

            const raw = evaluated.result && evaluated.result.result ? evaluated.result.result.value : '{"items":[]}';
            try {
                const parsed = JSON.parse(raw);
                if (parsed.cardCount > 0 || parsed.blocked) {
                    result = parsed;
                    break;
                }
            } catch (e) { /* 解析失败，重试 */ }

            if (retry < 2) await new Promise(r => setTimeout(r, 5000));
        }

        console.log(JSON.stringify(result));
    } finally {
        client.close();
    }
}

main().catch(error => {
    console.error(error.message || String(error));
    console.log(JSON.stringify({ items: [], error: error.message || String(error) }));
    process.exit(0);  // 用 exit(0) 让 Python 能读取 stdout
});
