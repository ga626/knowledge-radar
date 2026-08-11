"""
小红书 MCP 客户端
通过 Chrome MCP Server 操控浏览器完成搜索、详情、评论提取。
替代原有 Playwright/page.expect_response 方案，绕开风控。

用法:
    from mcp_client import McpXhsClient
    client = McpXhsClient()
    results = client.search("关键词")
    detail = client.get_note_detail("note_id", "xsec_token", "xsec_source")
    comments = client.get_comments("note_id", "xsec_token")
"""
import json
import logging
import os
import re
from runtime.process import silent_subprocess_run
import time
from typing import Dict, List, Optional

from runtime.executables import find_node_exe

logger = logging.getLogger("mcp_client")

# Node.js 桥接脚本路径
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BRIDGE_SCRIPT = os.path.join(PROJECT_ROOT, "bridge", "xhs_mcp_bridge.cjs")

NODE_PATH = find_node_exe()
CHROME_DEBUG_PORT = os.environ.get("KR_XHS_CHROME_DEBUG_PORT") or os.environ.get("KR_CHROME_DEBUG_PORT", "12733")
CHROME_PORT_URL = f"http://127.0.0.1:{CHROME_DEBUG_PORT}"


class McpXhsClient:
    """小红书 MCP 客户端"""

    def __init__(self):
        self._check_chrome()

    def _find_bridge(self) -> str:
        """查找桥接脚本路径"""
        env_bridge = os.environ.get("XHS_BRIDGE_PATH", "")
        for p in [env_bridge, BRIDGE_SCRIPT]:
            if os.path.exists(p):
                return p
        # 如果找不到，从项目根目录的相对路径尝试
        for p in [
            os.path.join(os.path.dirname(__file__), "xhs_mcp_bridge.cjs"),
            os.path.join(os.getcwd(), "xhs_mcp_bridge.cjs"),
        ]:
            if os.path.exists(p):
                return p
        raise FileNotFoundError("xhs_mcp_bridge.cjs 未找到。请设置 XHS_BRIDGE_PATH 或放置 repo-local bridge/xhs_mcp_bridge.cjs")

    def _check_chrome(self):
        """检查 Chrome 调试端口是否可用（server.py 已负责自动启动）"""
        import urllib.request
        try:
            req = urllib.request.Request(f"{CHROME_PORT_URL}/json/version",
                                         headers={"User-Agent": "Mozilla/5.0"},
                                         method="GET")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode())
            logger.info(f"Chrome 调试端口可用: {data.get('Browser', '?')}")
        except Exception as e:
            # server.py 会自动启动，这里只记录状态
            logger.debug(f"Chrome 调试端口暂不可用 (server.py 将自动启动): {e}")

    def _run_bridge(self, *args, timeout: int = 90) -> dict:
        """执行桥接脚本并返回 JSON 结果"""
        bridge = self._find_bridge()
        env = os.environ.copy()
        env["NODE_OPTIONS"] = ""
        # Match the per-task Chrome instance managed by src/server.py.
        env["KR_CHROME_DEBUG_PORT"] = CHROME_DEBUG_PORT

        cmd = [NODE_PATH, bridge] + list(args)
        logger.debug(f"执行: {' '.join(cmd)}")

        start = time.time()
        # 使用 PIPE 但合并 stderr 到 stdout 避免死锁
        # 强制 UTF-8 解码，errors='replace' 跳过无法解码的字节
        result = silent_subprocess_run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=os.path.dirname(bridge),
        )
        elapsed = time.time() - start

        # 解析 stdout/stderr 中的 JSON（贪心截取 + 逐行扫描 + 正则）
        stdout_text = (result.stdout or "").strip()
        stderr_text = (result.stderr or "").strip()

        def _parse_json_from(text: str) -> Optional[dict]:
            """从可能含噪音的文本中提取 JSON 对象"""
            if not text:
                return None
            # 策略 1: 贪心截取第一个 { 到最后一个 }
            first = text.find("{")
            last = text.rfind("}")
            if first >= 0 and last > first:
                try:
                    return json.loads(text[first:last + 1])
                except json.JSONDecodeError:
                    pass
            # 策略 2: 逐行扫描
            for line in reversed(text.split("\n")):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        pass
            # 策略 3: 正则匹配含 status 的对象
            for m in re.finditer(r'\{.+\}', text, re.DOTALL):
                if '"status"' in m.group(0):
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        continue
            return None

        data = _parse_json_from(stdout_text) or _parse_json_from(stderr_text)

        if not data:
            logger.error(f"桥接脚本无有效JSON输出. stdout尾: {stdout_text[-200:]}, stderr尾: {stderr_text[-200:]}")
            return {"status": "error", "error": "no valid json output"}

        data["_elapsed"] = round(elapsed, 1)
        logger.info(f"MCP {args[0]} 完成: {elapsed:.1f}s, 状态: {data.get('status')}")
        return data

    def cleanup_tabs(self):
        """清理 Chrome 中多余的标签页，只保留小红书官网和当前搜索页。

        通过 Chrome DevTools Protocol 的 /json/close 接口关闭：
        - about:blank 空页面
        - 非小红书域名的旧标签页（注意保留带有登录态的小红书页面）

        Cookie 保存在 profile 目录中，关标签不影响下次免登录。
        """
        import urllib.request
        try:
            # 获取所有标签页
            req = urllib.request.Request(f"{CHROME_PORT_URL}/json", headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            tabs = json.loads(resp.read().decode())

            kept = 0
            closed = 0
            for tab in tabs:
                url = tab.get("url", "")
                tab_type = tab.get("type", "")
                title = tab.get("title", "")

                # 判断是否应该关闭
                should_close = False

                # 关闭空白页
                if url in ("about:blank", "chrome://new-tab-page/", "chrome://newtab/", ""):
                    should_close = True
                # 关闭 Chrome 内部页面
                elif url.startswith("devtools://") or url.startswith("chrome://"):
                    should_close = True
                # 关闭已完成任务的非小红书页面
                elif tab_type != "page":
                    should_close = True
                # 测试 MCP 留下的临时搜索页
                elif "xhs_mcp" in title.lower() and "error" in title.lower():
                    should_close = True

                if should_close:
                    try:
                        close_req = urllib.request.Request(
                            f"{CHROME_PORT_URL}/json/close/{tab['id']}",
                            method="GET",
                        )
                        urllib.request.urlopen(close_req, timeout=3)
                        closed += 1
                    except Exception:
                        pass
                else:
                    kept += 1

            logger.info(f"Chrome 标签页清理: 关闭 {closed} 个, 保留 {kept} 个")
        except Exception as e:
            logger.warning(f"Chrome 标签页清理失败: {e}")

    def search(self, keyword: str, feed_type: str = "") -> List[Dict]:
        """MCP 搜索小红书

        Args:
            keyword: 搜索关键词
            feed_type: 筛选类型，""=综合, "image"=图文, "video"=视频

        Returns:
            list of dict: [{title, author, date, noteId, noteType?}, ...]
              noteType: "image" 或 "video"，由 xhs_mcp_bridge 返回
        """
        result = self._run_bridge("search", keyword, feed_type, timeout=90)
        if result.get("status") == "ok":
            items = result.get("items", [])
            if result.get("hasLoginPrompt") and not items:
                logger.warning("XHS login prompt detected; persistent profile may need QR login")
                return [{"error": "xhs_login_required", "login_required": True}]
            logger.info(f"MCP 搜索完成: {len(items)} 条 (feed_type={feed_type or 'all'})")
            return items
        else:
            logger.error(f"MCP 搜索失败: {result.get('error')}")
            return []

    def get_note_detail(self, note_id: str,
                        xsec_token: str = "",
                        xsec_source: str = "pc_search") -> Optional[Dict]:
        """MCP 获取笔记详情

        Returns:
            dict: {title, desc, author, likedCount, images, ...}
        """
        result = self._run_bridge("detail", note_id, xsec_token, xsec_source, timeout=90)
        if result.get("status") == "ok":
            return result.get("noteData")
        else:
            logger.error(f"MCP 详情获取失败: {result.get('error')}")
            return None

    def get_comments(self, note_id: str,
                     xsec_token: str = "") -> List[Dict]:
        """MCP 获取笔记评论

        Returns:
            list of dict: [{content, userNick, ...}, ...]
        """
        result = self._run_bridge("comments", note_id, xsec_token, timeout=90)
        if result.get("status") == "ok":
            return result.get("comments", [])
        else:
            logger.error(f"MCP 评论获取失败: {result.get('error')}")
            return []
