"""Safe, minimal updates to the supported product .env file."""

from __future__ import annotations

import re
import json
import os
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ASSIGNMENT_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")

FIELDS = (
    {"key": "YOUTUBE_API_KEY", "label": "YouTube Data API", "group": "公开视频", "kind": "secret", "provider_id": "youtube"},
    {"key": "OPENALEX_API_KEY", "label": "OpenAlex", "group": "学术资料", "kind": "secret", "provider_id": "openalex"},
    {"key": "SEMANTIC_SCHOLAR_API_KEY", "label": "Semantic Scholar", "group": "学术资料", "kind": "secret", "provider_id": "semantic_scholar"},
    {"key": "WFDATA_APP_KEY", "label": "万方官方检索（默认停用）", "group": "学术资料（高级）", "kind": "secret", "provider_id": "wanfang"},
    {"key": "SERPAPI_API_KEY", "label": "SerpAPI Scholar", "group": "学术资料（高级）", "kind": "secret", "provider_id": "serpapi_scholar"},
    {"key": "KR_CORE_API_KEY", "label": "CORE", "group": "学术资料（高级）", "kind": "secret", "provider_id": "core"},
    {"key": "DASHSCOPE_API_KEY", "label": "DashScope/百炼", "group": "模型与多模态", "kind": "secret", "provider_id": "dashscope"},
    {"key": "SILICONFLOW_API_KEY", "label": "硅基流动云端 OCR", "group": "模型与多模态", "kind": "secret", "provider_id": "siliconflow"},
    {"key": "TIKHUB_API_KEY", "label": "TikHub 小红书应急兜底", "group": "小红书（高级）", "kind": "secret", "provider_id": "tikhub"},
)
from search_providers.catalog import SEARCH_CATALOG, SEARCH_GUIDES, search_fields
FIELDS = search_fields() + tuple(field for field in FIELDS if field['provider_id'] not in SEARCH_CATALOG)
ALLOWED_KEYS = {field["key"] for field in FIELDS}

# This is product metadata, never configuration.  UI and future packaged guide
# assets read the same registry so a field cannot silently lose its explanation.
PROVIDER_GUIDES = {
    "siliconflow": {"purpose": "使用 PaddleOCR-VL 1.5 识别图片和扫描 PDF；处理时上传对应页面。", "official_url": "https://cloud.siliconflow.cn/account/ak", "official_label": "硅基流动 API Key", "prerequisite": "硅基流动账号与模型权限", "tier": "云端 OCR", "steps": ("进入自己的硅基流动控制台。", "创建 API Key，核对 PaddleOCR-VL 1.5 权限与额度。", "在这里保存密钥；保存不会上传文件。", "通过 Codex 发起授权文件识别；图片和扫描件需要云端连接。"), "cost_note": "费用和免费额度以账号当前规则为准；云端失败不会自动转为慢速本地 CPU。", "screenshot": "", "screenshot_note": "密钥仅在账号控制台创建，不保存账户截图。"},
    "youtube": {"purpose": "补充 YouTube 公开视频元数据入口。", "official_url": "https://developers.google.com/youtube/v3/getting-started", "official_label": "YouTube Data API 入门", "prerequisite": "Google Cloud 项目", "tier": "按需", "steps": ("打开官方入门页，在自己的 Google Cloud 项目中启用 YouTube Data API v3。", "进入 APIs & Services 的 Credentials，创建 API key。", "按官方建议对 Key 施加 API 与应用限制，再复制。", "回到这里保存；配额、区域限制与公开视频可访问性仍会影响结果。"), "cost_note": "Google Cloud 配额、启用状态和限制以官方控制台为准。", "screenshot": "", "screenshot_note": "凭据页面需要登录 Google Cloud；不在产品内保留或伪造账户截图。"},
    "openalex": {"purpose": "补充开放学术元数据、作者、机构和文献线索。", "official_url": "https://docs.openalex.org/how-to-use-the-api/get-api-key", "official_label": "OpenAlex API key 文档", "prerequisite": "OpenAlex 账号（需要 API key 时）", "tier": "推荐补充", "steps": ("打开 OpenAlex 的 API key 官方文档，确认当前申请和使用规则。", "在自己的 OpenAlex 账号中按页面指引创建或取得 API key。", "复制 Key，并确认项目的限额或访问策略。", "回到这里保存；缺少 Key 时可用来源会减少，但不会突破服务端限制。"), "cost_note": "访问规则和限额会更新，以 OpenAlex 当前官方文档为准。", "screenshot": "", "screenshot_note": "官方规则页可直接打开；账户内的 key 页面不会被控制台截图。"},
    "semantic_scholar": {"purpose": "补充论文元数据和摘要；当前接口不包含引用网络遍历。", "official_url": "https://www.semanticscholar.org/product/api#api-key-form", "official_label": "Semantic Scholar API Key 申请表", "prerequisite": "本人身份、所属机构或项目资料与可收信邮箱；需站方审核", "tier": "推荐补充", "steps": ("打开官方申请表，按真实情况填写姓名、邮箱、所属机构、网站、地区和用途。", "说明实际接口、字段、预计每日用量、限速与退避；不编造机构或匿名调用成功记录。", "本人确认 API 许可与表单声明后提交，等待邮件审核；获批后从邮件取得 Key。", "将自己的 Key 保存到本地；不随软件分发。未配置不影响其他公开来源。"), "cost_note": "官方起始速率为每秒 1 次；学术、科研、非营利与政府申请优先，其他逐案审核，不保证批准。", "screenshot": "", "screenshot_note": "申请表与 Key 邮件涉及个人资料，不保存账号截图。"},
    "dashscope": {"purpose": "连接阿里云百炼，用于按需的图文、视频或文本理解模型。", "official_url": "https://help.aliyun.com/zh/model-studio/get-api-key", "official_label": "阿里云百炼 API Key", "prerequisite": "阿里云账号与已授权的百炼业务空间", "tier": "模型按需", "steps": ("打开百炼 API Key 官方说明，并进入自己的业务空间。", "由业务空间管理员创建 API Key；如有需要，配置模型范围或 IP 白名单。", "复制 Key，并核对它所属业务空间与模型权限。", "回到这里保存；保存不会发起模型调用或扣费。"), "cost_note": "模型调用可能产生费用；Key 权限由业务空间、模型授权和账号策略决定。", "screenshot": "", "screenshot_note": "官方说明包含创建路径；实际 Key 页面需要登录阿里云。"},
    "serpapi_scholar": {"purpose": "按需补充 Google Scholar 元数据来源。", "official_url": "https://serpapi.com/google-scholar-api", "official_label": "SerpAPI Google Scholar API", "prerequisite": "SerpAPI 账号与 API Key", "tier": "高级按需", "steps": ("打开官方 API 页面并进入自己的控制台。", "创建或复制 API Key。", "确认套餐与额度。", "回到这里保存；它不会替代开放学术链路。"), "cost_note": "查询可能消耗套餐额度。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
    "wanfang": {"purpose": "付费增强来源，当前默认停用；保存 Key 不会启用请求。", "official_url": "https://www.wfdata.com/skill-detail?id=wfdata-search", "official_label": "万方文献搜索订阅", "prerequisite": "本人核清收费并决定恢复后，再准备账号、接口订阅与 APP KEY", "tier": "默认停用", "steps": ("目前无需充值或继续开通；已有 Key 可以保留。", "如将来决定恢复，先核对当前 APP Key 的接口单价、额度和套餐适用范围。", "确认后才显式设置 WFDATA_ENABLED=true；仅保存 Key 不会开启。", "自动补查还需 authorized_sources 包含 wanfang；资源范围仍以账号权限为准。"), "cost_note": "当前默认禁止万方检索和详情请求；不将 MCP 套餐当作已确认的开放 API 价格。", "screenshot": "", "screenshot_note": "不保存账号或 Key 截图。"},
    "core": {"purpose": "按需补充 CORE 的开放获取元数据与链接发现。", "official_url": "https://api.core.ac.uk/docs/v3", "official_label": "CORE API 文档", "prerequisite": "CORE API 访问密钥", "tier": "高级按需", "steps": ("阅读官方 API 文档并确认账号权限。", "在自己的 CORE 账号中获取 Key。", "核对日限额。", "回到这里保存。"), "cost_note": "仅用于检索与链接发现，遵守 CORE 的访问政策。", "screenshot": "", "screenshot_note": "本轮仅搭建能力框架，教程截图后续再补。"},
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
    values = {field["key"]: os.environ.get(field["key"], "") for field in FIELDS}
    values.update(_read_values(path))
    fields = [
        {**field, "configured": bool(values.get(field["key"], "").strip())}
        for field in FIELDS
    ]
    from runtime.search_configuration import current_configuration
    from importlib.util import find_spec
    config = current_configuration(fresh=True, path=path)
    routes = [name for name, row in SEARCH_CATALOG.items() if config.allowed(name)
              and (bool(values.get(row['key'], '').strip()) if row['key'] else find_spec('ddgs') is not None)]
    retired = bool(values.get('SEARXNG_BASE_URL') or config.policy.get('connections') or
                   'searxng' in config.policy.get('providers', {}))
    return {"env_exists": path.is_file(), "fields": fields, 'search_ready_sources': routes,
            'retired_sources': [{'id':'searxng','state':'ignored','message':'SearXNG 来源已退役；旧配置保留但不会发送请求。'}] if retired else [],
            'configuration_generation':config.generation, 'search_health':'not_probed'}


def public_provider_guides(path: Path | None = None) -> list[dict[str, Any]]:
    """Return user-facing guide metadata and configured flags, never values."""
    snapshot = public_snapshot(path)
    by_key = {str(field["key"]): field for field in snapshot["fields"]}
    guides: list[dict[str, Any]] = []
    for field in FIELDS:
        guide = (SEARCH_GUIDES if field["provider_id"] in SEARCH_CATALOG else PROVIDER_GUIDES)[str(field["provider_id"])]
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
    from runtime.search_configuration import current_configuration
    config = current_configuration(fresh=True, path=path)
    for guide in guides:
        descriptor = SEARCH_CATALOG.get(guide['id'])
        if descriptor:
            guide.update(purpose=descriptor['purpose'], tier={'preferred': '默认优先',
                         'paid_fallback': '授权兜底', 'optional': '按需选择'}[descriptor['role']],
                         enabled=config.allowed(guide['id']), configuration_generation=config.generation,
                         runtime_state='configured_unverified' if guide['configured'] else 'not_configured',
                         cost=descriptor['cost'], capabilities=descriptor['capabilities'])
    guides.append({'id': 'ddgs', **SEARCH_CATALOG['ddgs'], 'group': '网页发现',
        'configured': True, 'enabled': config.allowed('ddgs'), 'tier': '轻量可选',
        'configuration_generation': config.generation, 'runtime_state': 'library_not_probed',
        'official_url': 'https://github.com/deedy5/ddgs', 'official_label': 'DDGS 官方项目',
        'prerequisite': '随 KR 管理的搜索库', 'steps': ['启用后按任务需要调用；无需 Key、Docker 或独立服务。',
        '上游可能限制自动请求；失败会说明原因，不自动轮换绕过限制。'],
        'cost_note': '无 API Key；上游覆盖与可用性不作保证。', 'screenshot': '',
        'screenshot_note': '仅使用搜索能力，正文仍由 KR 公共链处理。'})
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


def apply_updates(payload: Any, path: Path | None = None, *, delete_keys=(), provider_updates=None,
                  connections=None, expected_generation=None) -> list[str]:
    """Merge intentionally supplied nonempty values while preserving all other lines."""
    path = path or runtime_env_path()
    updates = _validated_updates(payload)
    if not isinstance(delete_keys, (tuple, list)) or any(key not in ALLOWED_KEYS for key in delete_keys):
        raise ValueError('不支持的凭据删除项。')
    updates.update({key: '' for key in delete_keys})
    from runtime.file_write_lock import file_write_lock
    with file_write_lock(path):
        from runtime.search_configuration import read_env, validate_policy
        values = read_env(path)
        if expected_generation:
            from runtime.search_configuration import current_configuration
            if current_configuration(fresh=True, path=path).generation != expected_generation:
                raise ValueError('配置已被其他窗口修改，请刷新后重试。')
        if provider_updates is not None or connections is not None:
            try:
                policy = validate_policy(json.loads(values.get('KR_SEARCH_POLICY') or '{}'))
            except (ValueError, TypeError):
                raise ValueError('现有搜索策略损坏，请先修复配置，不能恢复为默认授权。') from None
            if not isinstance(provider_updates or {}, dict):
                raise ValueError('来源启用设置必须是对象。')
            for name, flags in (provider_updates or {}).items():
                if name not in SEARCH_CATALOG or not isinstance(flags, dict) or set(flags) - {'enabled', 'authorized'}:
                    raise ValueError('不支持的来源设置。')
                if any(type(v) is not bool for v in flags.values()):
                    raise ValueError('来源授权必须是布尔值。')
                policy.setdefault('providers', {}).setdefault(name, {}).update(flags)
            if connections is not None:
                if connections:
                    raise ValueError('SearXNG 来源已退役，不能新增实例。')
                policy['connections'] = []
            validate_policy(policy)
            updates['KR_SEARCH_POLICY'] = json.dumps(policy, ensure_ascii=False, separators=(',', ':'))
        return _write_updates(path, updates)


def _write_updates(path, updates):
    if not updates:
        return []
    original = path.read_text(encoding='utf-8-sig') if path.is_file() else ''
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.kr-config-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(newline.join(output).rstrip() + newline)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return sorted(updates)
