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
    {"key": "TAVILY_API_KEY", "label": "Tavily", "group": "网页搜索", "kind": "secret", "provider_id": "tavily"},
    {"key": "ANYSEARCH_API_KEY", "label": "AnySearch", "group": "网页搜索", "kind": "secret", "provider_id": "anysearch"},
    {"key": "BRAVE_SEARCH_API_KEY", "label": "Brave Search", "group": "网页搜索", "kind": "secret", "provider_id": "brave"},
    {"key": "EXA_API_KEY", "label": "Exa", "group": "网页搜索", "kind": "secret", "provider_id": "exa"},
    {"key": "SEARXNG_BASE_URL", "label": "SearXNG 本地地址", "group": "网页搜索", "kind": "url", "provider_id": "searxng"},
    {"key": "YOUTUBE_API_KEY", "label": "YouTube Data API", "group": "内容与学术", "kind": "secret", "provider_id": "youtube"},
    {"key": "OPENALEX_API_KEY", "label": "OpenAlex", "group": "内容与学术", "kind": "secret", "provider_id": "openalex"},
    {"key": "SEMANTIC_SCHOLAR_API_KEY", "label": "Semantic Scholar", "group": "内容与学术", "kind": "secret", "provider_id": "semantic_scholar"},
    {"key": "DASHSCOPE_API_KEY", "label": "DashScope/百炼", "group": "理解与多模态", "kind": "secret", "provider_id": "dashscope"},
    {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow", "group": "理解与多模态", "kind": "secret", "provider_id": "siliconflow"},
    {"key": "LLM_API_KEY", "label": "兼容 LLM 服务", "group": "理解与多模态", "kind": "secret", "provider_id": "compatible_llm"},
)
ALLOWED_KEYS = {field["key"] for field in FIELDS}

# This is product metadata, never configuration.  UI and future packaged guide
# assets read the same registry so a field cannot silently lose its explanation.
PROVIDER_GUIDES = {
    "tavily": {"purpose": "为公开网页研究提供搜索入口。", "official_url": "https://docs.tavily.com/documentation/quickstart", "steps": ("打开官方 Quickstart。", "登录或注册自己的 Tavily 账号。", "在控制台创建 API Key 后复制。", "回到这里粘贴并保存。"), "cost_note": "服务额度和费用以 Tavily 官方页面为准。"},
    "anysearch": {"purpose": "作为可选的网页搜索来源。", "official_url": "https://www.anysearch.ai/", "steps": ("打开 AnySearch 官方网站。", "使用自己的账号进入控制台。", "创建并复制 API Key。", "回到这里粘贴并保存。"), "cost_note": "账号、额度和费用以官方说明为准。"},
    "brave": {"purpose": "为公开网页研究补充 Brave Search API。", "official_url": "https://brave.com/search/api/", "steps": ("打开 Brave Search API 官方页。", "进入自己的开发者控制台。", "创建 Search API Key。", "回到这里粘贴并保存。"), "cost_note": "使用前请核对 Brave 的套餐与额度。"},
    "exa": {"purpose": "为网页发现和语义检索提供 Exa 来源。", "official_url": "https://exa.ai/docs/reference/search", "steps": ("打开 Exa Search API 文档。", "登录自己的 Exa 账号。", "在 API Key 页面创建密钥。", "回到这里粘贴并保存。"), "cost_note": "额度和计费由 Exa 账号决定。"},
    "searxng": {"purpose": "连接你自己部署的 SearXNG 实例。", "official_url": "https://docs.searxng.org/", "steps": ("确认自己的 SearXNG 已启动。", "复制实例的基础地址。", "回到这里填写地址，不要填写搜索关键词。", "保存后按实际研究任务验证。"), "cost_note": "这是你的自建服务；本产品不会替你部署或公开它。"},
    "youtube": {"purpose": "补充 YouTube 公开视频元数据入口。", "official_url": "https://developers.google.com/youtube/v3/getting-started", "steps": ("打开 YouTube Data API 入门页。", "在自己的 Google Cloud 项目启用 API。", "创建受限 API Key。", "回到这里粘贴并保存。"), "cost_note": "Google 项目配额和限制以官方控制台为准。"},
    "openalex": {"purpose": "补充开放学术元数据和文献线索。", "official_url": "https://docs.openalex.org/", "steps": ("打开 OpenAlex 文档。", "按官方说明确认是否需要 Key。", "如果你有 Key，复制后回到这里保存。", "未填写时仍可使用产品支持的其他来源。"), "cost_note": "OpenAlex 的可用性和限制以官方文档为准。"},
    "semantic_scholar": {"purpose": "补充 Semantic Scholar 学术资料线索。", "official_url": "https://www.semanticscholar.org/product/api", "steps": ("打开 Semantic Scholar API 官方页。", "按官方说明申请或创建 Key。", "复制 Key。", "回到这里粘贴并保存。"), "cost_note": "认证与配额规则以官方页面为准。"},
    "dashscope": {"purpose": "连接百炼模型服务处理图文和视频任务。", "official_url": "https://help.aliyun.com/zh/model-studio/getting-started/first-api-call-to-qwen", "steps": ("打开阿里云百炼官方入门页。", "登录自己的阿里云账号。", "创建 API Key 并阅读模型计费说明。", "回到这里粘贴并保存。"), "cost_note": "模型调用可能产生费用；保存不会发起模型调用。"},
    "siliconflow": {"purpose": "连接 SiliconFlow 的可选模型服务。", "official_url": "https://docs.siliconflow.cn/", "steps": ("打开 SiliconFlow 官方文档。", "登录自己的账号。", "在密钥页面创建并复制 API Key。", "回到这里粘贴并保存。"), "cost_note": "模型和额度由你的 SiliconFlow 账号决定。"},
    "compatible_llm": {"purpose": "连接你自行选择的兼容 LLM 服务。", "official_url": "", "steps": ("确认服务商提供兼容 API 与 API Key。", "在服务商控制台创建自己的 Key。", "回到这里粘贴 Key。", "其余地址和模型设置按产品高级文档完成。"), "cost_note": "第三方服务可能计费；本产品不会替你选择或调用服务。"},
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
            "guide_type": "official_steps",
            "asset_status": "text_equivalent_ready",
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
