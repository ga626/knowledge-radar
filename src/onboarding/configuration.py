"""Safe, minimal updates to the supported product .env file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ASSIGNMENT_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")

FIELDS = (
    {"key": "TAVILY_API_KEY", "label": "Tavily", "group": "网页发现", "kind": "secret", "provider_id": "tavily"},
    {"key": "ANYSEARCH_API_KEY", "label": "AnySearch", "group": "网页发现", "kind": "secret", "provider_id": "anysearch"},
    {"key": "BRAVE_SEARCH_API_KEY", "label": "Brave Search", "group": "网页发现", "kind": "secret", "provider_id": "brave"},
    {"key": "EXA_API_KEY", "label": "Exa", "group": "网页发现", "kind": "secret", "provider_id": "exa"},
    {"key": "SEARXNG_BASE_URL", "label": "SearXNG 本地地址", "group": "自建搜索", "kind": "url", "provider_id": "searxng"},
    {"key": "YOUTUBE_API_KEY", "label": "YouTube Data API", "group": "公开视频", "kind": "secret", "provider_id": "youtube"},
    {"key": "OPENALEX_API_KEY", "label": "OpenAlex", "group": "学术资料", "kind": "secret", "provider_id": "openalex"},
    {"key": "SEMANTIC_SCHOLAR_API_KEY", "label": "Semantic Scholar", "group": "学术资料", "kind": "secret", "provider_id": "semantic_scholar"},
    {"key": "BAIDU_QIANFAN_BEARER_TOKEN", "label": "百度千帆 Scholar", "group": "学术资料（高级）", "kind": "secret", "provider_id": "baidu_scholar"},
    {"key": "SERPAPI_API_KEY", "label": "SerpAPI Scholar", "group": "学术资料（高级）", "kind": "secret", "provider_id": "serpapi_scholar"},
    {"key": "KR_CORE_API_KEY", "label": "CORE", "group": "学术资料（高级）", "kind": "secret", "provider_id": "core"},
    {"key": "DASHSCOPE_API_KEY", "label": "DashScope/百炼", "group": "模型与多模态", "kind": "secret", "provider_id": "dashscope"},
    {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow", "group": "模型与多模态", "kind": "secret", "provider_id": "siliconflow"},
    {"key": "MIMO_API_KEY", "label": "Mimo", "group": "模型与多模态（高级）", "kind": "secret", "provider_id": "mimo"},
    {"key": "API147_KEY", "label": "API147 评论筛选", "group": "模型与多模态（高级）", "kind": "secret", "provider_id": "api147"},
    {"key": "TIKHUB_API_KEY", "label": "TikHub 小红书应急兜底", "group": "小红书（高级）", "kind": "secret", "provider_id": "tikhub"},
    {"key": "LLM_API_KEY", "label": "兼容 LLM 服务", "group": "模型与多模态", "kind": "secret", "provider_id": "compatible_llm"},
)
ALLOWED_KEYS = {field["key"] for field in FIELDS}

# This is product metadata, never configuration.  UI and future packaged guide
# assets read the same registry so a field cannot silently lose its explanation.
PROVIDER_GUIDES = {
    "tavily": {"purpose": "面向研究任务优化的网页发现入口。", "official_url": "https://docs.tavily.com/documentation/quickstart", "official_label": "Tavily Quickstart", "prerequisite": "Tavily 账号", "tier": "建议先配", "steps": ("打开官方 Quickstart，并选择 Get an API key。", "登录或注册自己的 Tavily 账号。", "在 Dashboard 的 API keys 区域创建或复制一条密钥。", "回到这里粘贴并保存；页面只确认已填写，不会回显密钥。"), "cost_note": "Tavily 官方 Quickstart 写明每月免费额度；套餐、速率和余额以你的账号页面为准。", "screenshot": "tavily.jpg", "screenshot_note": "公开官方 Quickstart 页面预览；登录后的密钥页不会被截图。"},
    "anysearch": {"purpose": "可选的网页搜索来源，适合需要补充来源时启用。", "official_url": "https://www.anysearch.ai/", "official_label": "AnySearch 官网", "prerequisite": "AnySearch 账号", "tier": "按需", "steps": ("打开 AnySearch 官方网站并进入自己的控制台。", "在账号的 API 或开发者设置中创建 API Key。", "核对套餐、余额或额度提示后复制该 Key。", "回到这里保存；保存后仍要在真实研究任务中验证可用性。"), "cost_note": "账号、额度与计费由 AnySearch 官方控制台决定。", "screenshot": "anysearch.jpg", "screenshot_note": "公开官网预览；具体菜单会因账号状态而变化。"},
    "brave": {"purpose": "为公开网页研究补充 Brave Search API。", "official_url": "https://brave.com/search/api/", "official_label": "Brave Search API", "prerequisite": "Brave Search API 账号与套餐", "tier": "按需", "steps": ("打开 Brave Search API 官方页并进入 API Dashboard。", "使用自己的账号创建 Search API 订阅或选择套餐。", "在 Keys 页面创建一条 Search API Key。", "复制后回到这里保存；不要把 Key 放进查询文本或截图。"), "cost_note": "套餐、免费额度、地域和速率限制以 Brave Dashboard 为准。", "screenshot": "", "screenshot_note": "该服务的密钥菜单在登录后显示；本页提供官方入口与逐步文字，不伪造登录态截图。"},
    "exa": {"purpose": "为网页发现和语义检索提供 Exa 来源。", "official_url": "https://docs.exa.ai/reference/getting-started", "official_label": "Exa Getting Started", "prerequisite": "Exa 账号", "tier": "按需", "steps": ("打开官方 Getting Started 文档并进入自己的 Dashboard。", "在 Dashboard 的 API Keys 页面新建或复制密钥。", "先核对项目的额度或账单状态。", "回到这里粘贴并保存；保存动作不会请求 Exa。"), "cost_note": "额度、计费和模型/搜索能力由 Exa 账号决定。", "screenshot": "exa.jpg", "screenshot_note": "公开官方 Getting Started 页面预览；Key 仅在你的 Dashboard 内创建。"},
    "searxng": {"purpose": "连接你自己部署的 SearXNG 实例，而不是购买第三方 Key。", "official_url": "https://docs.searxng.org/admin/settings/index.html", "official_label": "SearXNG 管理文档", "prerequisite": "已可访问的自建 SearXNG 实例", "tier": "自建可选", "steps": ("先在浏览器中打开自己的 SearXNG 实例，确认它可以正常返回搜索页。", "复制实例基础地址，例如 https://search.example.com；不要包含查询参数。", "打开官方设置文档，核对你的实例是否启用了可用格式与访问策略。", "回到这里填写基础地址并保存；之后用真实研究任务验证连通性。"), "cost_note": "这是自建服务；服务器、搜索后端和网络成本由你的部署承担。", "screenshot": "searxng.jpg", "screenshot_note": "公开官方管理文档预览；不要在截图或说明中暴露你的实例地址。"},
    "youtube": {"purpose": "补充 YouTube 公开视频元数据入口。", "official_url": "https://developers.google.com/youtube/v3/getting-started", "official_label": "YouTube Data API 入门", "prerequisite": "Google Cloud 项目", "tier": "按需", "steps": ("打开官方入门页，在自己的 Google Cloud 项目中启用 YouTube Data API v3。", "进入 APIs & Services 的 Credentials，创建 API key。", "按官方建议对 Key 施加 API 与应用限制，再复制。", "回到这里保存；配额、区域限制与公开视频可访问性仍会影响结果。"), "cost_note": "Google Cloud 配额、启用状态和限制以官方控制台为准。", "screenshot": "", "screenshot_note": "凭据页面需要登录 Google Cloud；不在产品内保留或伪造账户截图。"},
    "openalex": {"purpose": "补充开放学术元数据、作者、机构和文献线索。", "official_url": "https://docs.openalex.org/how-to-use-the-api/get-api-key", "official_label": "OpenAlex API key 文档", "prerequisite": "OpenAlex 账号（需要 API key 时）", "tier": "推荐补充", "steps": ("打开 OpenAlex 的 API key 官方文档，确认当前申请和使用规则。", "在自己的 OpenAlex 账号中按页面指引创建或取得 API key。", "复制 Key，并确认项目的限额或访问策略。", "回到这里保存；缺少 Key 时可用来源会减少，但不会突破服务端限制。"), "cost_note": "访问规则和限额会更新，以 OpenAlex 当前官方文档为准。", "screenshot": "", "screenshot_note": "官方规则页可直接打开；账户内的 key 页面不会被控制台截图。"},
    "semantic_scholar": {"purpose": "补充论文、引用网络和学术资料线索。", "official_url": "https://www.semanticscholar.org/product/api/tutorial", "official_label": "Semantic Scholar API Tutorial", "prerequisite": "Semantic Scholar API 访问资格", "tier": "推荐补充", "steps": ("打开官方 API Tutorial，了解当前公开访问与认证规则。", "需要更高配额时，按官方入口申请或创建 API Key。", "复制 Key 前先核对使用限制和速率说明。", "回到这里保存；未配置时仍可使用不依赖它的其他公开来源。"), "cost_note": "公开访问与带 Key 的额度不同，具体以官方 API 文档为准。", "screenshot": "", "screenshot_note": "教程页是公开文档；申请或密钥管理页面依账号资格显示。"},
    "dashscope": {"purpose": "连接阿里云百炼，用于按需的图文、视频或文本理解模型。", "official_url": "https://help.aliyun.com/zh/model-studio/get-api-key", "official_label": "阿里云百炼 API Key", "prerequisite": "阿里云账号与已授权的百炼业务空间", "tier": "模型按需", "steps": ("打开百炼 API Key 官方说明，并进入自己的业务空间。", "由业务空间管理员创建 API Key；如有需要，配置模型范围或 IP 白名单。", "复制 Key，并核对它所属业务空间与模型权限。", "回到这里保存；保存不会发起模型调用或扣费。"), "cost_note": "模型调用可能产生费用；Key 权限由业务空间、模型授权和账号策略决定。", "screenshot": "", "screenshot_note": "官方说明包含创建路径；实际 Key 页面需要登录阿里云。"},
    "siliconflow": {"purpose": "连接 SiliconFlow 的可选模型服务。", "official_url": "https://docs.siliconflow.cn/cn/userguide/quickstart", "official_label": "SiliconFlow 快速上手", "prerequisite": "SiliconFlow 账号", "tier": "模型按需", "steps": ("打开 SiliconFlow 快速上手，先查看可用模型、价格和速率上限。", "登录自己的账号并进入 API 密钥页面。", "选择新建 API 密钥并复制。", "回到这里保存；如需调用兼容接口，仍须按官方文档选择模型和地址。"), "cost_note": "模型价格、额度和速率限制由 SiliconFlow 账号与模型决定。", "screenshot": "", "screenshot_note": "公开官方 Quickstart 页面说明了登录、模型与密钥路径。"},
    "compatible_llm": {"purpose": "连接你自行选择且确实支持兼容接口的 LLM 服务。", "official_url": "", "official_label": "服务商官方文档", "prerequisite": "你选择的服务商、Base URL、模型名和 API Key", "tier": "高级按需", "steps": ("先确认服务商的官方文档明确支持你要使用的兼容接口。", "在该服务商控制台创建自己的 API Key，并记录 Base URL 与模型名。", "只把 Key 粘贴到本机字段；不要把 Key 放在 URL、截图、任务描述或提交记录中。", "其余地址和模型选择按高级配置完成，再用真实调用单独验证。"), "cost_note": "第三方服务可能计费，也可能与兼容接口存在差异；本产品不会替你选择或调用服务。", "screenshot": "", "screenshot_note": "没有通用的官方截图，因为该项由你选择的服务商决定。"},
    "baidu_scholar": {"purpose": "按需接入百度千帆 Scholar 官方 API；它不是默认学术链路的前置条件。", "official_url": "https://cloud.baidu.com/doc/WENXINWORKSHOP/s/qlqlk0ftr", "official_label": "百度智能云官方文档", "prerequisite": "百度智能云账号与可用 Bearer Token", "tier": "高级按需", "steps": ("先确认当前账号已具备官方 API 的访问资格。", "按官方控制台流程获取自己的 Bearer Token。", "核对试用、额度与调用边界。", "回到这里保存；未保存不会影响开放学术资料入口。"), "cost_note": "账号资格、试用额度与速率以百度智能云控制台为准。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "serpapi_scholar": {"purpose": "按需补充 Google Scholar 元数据来源。", "official_url": "https://serpapi.com/google-scholar-api", "official_label": "SerpAPI Google Scholar API", "prerequisite": "SerpAPI 账号与 API Key", "tier": "高级按需", "steps": ("打开官方 API 页面并进入自己的控制台。", "创建或复制 API Key。", "确认套餐与额度。", "回到这里保存；它不会替代开放学术链路。"), "cost_note": "查询可能消耗套餐额度。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "core": {"purpose": "按需补充 CORE 的开放获取元数据与链接发现。", "official_url": "https://api.core.ac.uk/docs/v3", "official_label": "CORE API 文档", "prerequisite": "CORE API 访问密钥", "tier": "高级按需", "steps": ("阅读官方 API 文档并确认账号权限。", "在自己的 CORE 账号中获取 Key。", "核对日限额。", "回到这里保存。"), "cost_note": "仅用于检索与链接发现，遵守 CORE 的访问政策。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "mimo": {"purpose": "为特定多模态兼容路径提供显式备用模型服务。", "official_url": "", "official_label": "服务商官方文档", "prerequisite": "Mimo 账号、Key 与明确的成本理由", "tier": "高级按需", "steps": ("先确认当前任务确实需要这个备用服务。", "从服务商官方控制台创建自己的 Key。", "核对模型、价格与速率。", "回到这里保存，再用单项任务验证。"), "cost_note": "默认链路不会自动使用该服务。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "api147": {"purpose": "仅在评论筛选模型明确选择 API147 时才使用。", "official_url": "https://147ai.com/", "official_label": "API147 官网", "prerequisite": "API147 账号与 Key", "tier": "高级按需", "steps": ("确认评论筛选模型已明确选择 API147。", "在服务商控制台创建自己的 Key。", "核对费用和模型名称。", "回到这里保存。"), "cost_note": "默认评论筛选不会要求该 Key。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "tikhub": {"purpose": "小红书浏览器路径不可用时的受限付费应急兜底，不用于普通配置。", "official_url": "https://www.tikhub.io/", "official_label": "TikHub 官网", "prerequisite": "TikHub 账号、Key 与明确的应急场景", "tier": "应急高级", "steps": ("先检查小红书登录态、浏览器会话与平台验证。", "仅在常规路径不可用且已明确需要应急兜底时，再准备自己的 Key。", "核对每日预算和服务费用。", "回到这里保存；不会自动开启或自动调用。"), "cost_note": "失败的付费调用也可能计入额度；默认不启用。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
}


def _read_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if KEY_PATTERN.fullmatch(key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def runtime_env_path() -> Path:
    return Path(__import__("os").environ.get("KR_RUNTIME_ENV_PATH", str(ENV_PATH))).expanduser()


def public_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Return configuration presence only; values never leave the process."""
    path = path or runtime_env_path()
    values = _read_values(path)
    fields = [
        {**field, "configured": bool(values.get(field["key"], "").strip())}
        for field in FIELDS
    ]
    return {"env_exists": path.is_file(), "fields": fields}


def public_provider_guides(path: Path | None = None) -> list[dict[str, Any]]:
    """Return user-facing guide metadata and configured flags, never values."""
    snapshot = public_snapshot(path)
    by_key = {str(field["key"]): field for field in snapshot["fields"]}
    guides: list[dict[str, Any]] = []
    for field in FIELDS:
        guide = PROVIDER_GUIDES[str(field["provider_id"])]
        current = by_key[field["key"]]
        guides.append({
            "id": field["provider_id"],
            "label": field["label"],
            "group": field["group"],
            "key": field["key"],
            "kind": field["kind"],
            "configured": bool(current["configured"]),
            "purpose": guide["purpose"],
            "official_url": guide["official_url"],
            "steps": list(guide["steps"]),
            "cost_note": guide["cost_note"],
            "official_label": guide["official_label"],
            "prerequisite": guide["prerequisite"],
            "tier": guide["tier"],
            "screenshot": f"/assets/{guide['screenshot']}" if guide["screenshot"] else "",
            "screenshot_note": guide["screenshot_note"],
            "guide_type": "official_steps",
            "asset_status": "official_screenshot_ready" if guide["screenshot"] else "official_text_ready",
        })
    return guides


def _validated_updates(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("配置内容必须是对象。")
    updates: dict[str, str] = {}
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            raise ValueError("包含不支持的配置项。")
        if not isinstance(value, str):
            raise ValueError("配置值必须是文本。")
        value = value.strip()
        if not value:
            continue
        if len(value) > 4096 or any(character in value for character in "\r\n\x00"):
            raise ValueError("配置值格式无效。")
        updates[key] = value
    return updates


def apply_updates(payload: Any, path: Path | None = None) -> list[str]:
    """Merge intentionally supplied nonempty values while preserving all other lines."""
    path = path or runtime_env_path()
    updates = _validated_updates(payload)
    if not updates:
        return []

    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    remaining = set(updates)
    output: list[str] = []
    for line in lines:
        match = ASSIGNMENT_PATTERN.match(line)
        key = match.group(1) if match else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            remaining.discard(key)
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    if remaining:
        output.extend(["# Added by the local KnowledgeRadar setup wizard"])
        output.extend(f"{key}={updates[key]}" for key in sorted(remaining))
    path.write_text(newline.join(output).rstrip() + newline, encoding="utf-8")
    return sorted(updates)
