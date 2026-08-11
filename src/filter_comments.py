"""Comment knowledge-value filtering.

P1 defaults to Bailian low-cost text models. MiMo and API147 remain explicit
fallbacks only when configured in KR_COMMENT_FILTER_MODELS.
"""
import json
import logging
import os
import httpx
from typing import Dict, List, Optional

log = logging.getLogger("mcp-server")

from runtime.monitor import get_monitor_tracker
from runtime.degradation import get_degradation_policy
from runtime.usage_tracker import get_usage_tracker
from media_policy import MediaModelPolicy


# === 配置 ===
MIMO_API_URL = os.environ.get("MIMO_API_URL", "https://api.xiaomimimo.com/v1").rstrip("/")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5")
DASHSCOPE_COMPATIBLE_BASE_URL = os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

# 147API - 备用免费模型
API147_URL = "https://147ai.com/v1/chat/completions"
API147_KEY = os.environ.get("API147_KEY", "")
API147_MODEL = "gpt-5.2"  # 147API上的廉价模型

COMMENT_MODEL_PRICES_RMB_PER_M = {
    ("bailian", "qwen-turbo"): {"input": 0.3, "output": 0.6, "cached_input": None},
    ("bailian", "qwen3.5-flash"): {"input": 0.3, "output": 0.6, "cached_input": None},
}


def filter_valuable_comments(
    comments: List[Dict[str, str]],
    api_url: str = "",
    api_key: str = "",
    model: str = "",
    verbose: bool = True,
) -> Dict:
    """
    使用免费 LLM 过滤有知识价值的评论。
    
    Args:
        comments: 评论列表,每项包含 user, content, likes 等字段
        api_url: LLM API 地址
        api_key: API Key
        model: 模型名称
        verbose: 是否打印详细信息
    
    Returns:
        {
            "total": 总评论数,
            "kept": 保留数,
            "discarded": 丢弃数,
            "retention_rate": 保留率,
            "results": [
                {
                    "content": 原始评论,
                    "verdict": "keep" | "discard",
                    "reason": 判断理由
                },
                ...
            ],
            "kept_comments": 仅保留的评论列表
        }
    """
    if not comments:
        return {"total": 0, "kept": 0, "discarded": 0, "results": [], "kept_comments": []}

    provider = _resolve_comment_provider(api_url=api_url, api_key=api_key, model=model)
    api_url = provider["api_url"]
    api_key = provider["api_key"]
    model = provider["model"]
    provider_name = provider["provider"]
    if not api_key:
        log.info("[comment_filter] %s API key missing, using fallback filter", provider_name)
        return _fallback_filter(comments, verbose, provider=provider_name, model=model, reason="api_key_missing")

    policy = get_degradation_policy()
    breaker_key = f"llm:filter_comments:{provider_name}:{model}"
    if policy.is_open(breaker_key).get("open"):
        policy.record_degradation("llm", breaker_key, "circuit breaker open", {"model": model, "comment_count": len(comments)})
        return _fallback_filter(comments, verbose, provider=provider_name, model=model, reason="circuit_breaker_open")
    chat_completions_url = _chat_completions_url(api_url)

    # 构建评论列表文本
    comments_text = ""
    for i, c in enumerate(comments, 1):
        user = c.get("user", "匿名")
        content = c.get("content", "")
        likes = c.get("likes", 0)
        comments_text += f"{i}. [{user}] (👍{likes}): {content}\n"

    # 系统提示词 - 定义知识价值的判断标准
    system_prompt = """你是一个专业的信息质量评估专家。你的任务是对B站视频的评论进行分类，判断其是否具有"知识价值"。

### 知识价值判断标准

**有价值（保留）** 的类型包括：
- 补充信息：提供视频未涵盖的额外背景、数据、资料链接
- 不同观点/修正：提出与视频内容不同的合理观点，或对视频中错误/遗漏的修正
- 个人实践经验：分享自己的真实使用经验、操作心得、遇到的问题及解决方法
- 提问并回答：既有好的提问，也有其他用户有价值的回答
- 技术分析：对视频涉及的技术方案进行深度分析、对比、优缺点讨论
- 资源推荐：推荐相关的工具、模型、论文、教程等

**无价值（丢弃）** 的类型包括：
- 纯情绪表达：如"太厉害了！""牛逼！""666"等无信息量的赞叹
- 灌水打卡：如"第一""来了""打卡""沙发"等
- 纯转发语：无个人观点的转发
- 表情包/颜文字：纯表情无文字内容的评论
- 广告/垃圾信息
- 与视频内容完全无关的闲聊

**注意**：
- 要严格区分"有信息量的讨论"和"纯情绪表达"。即使评论有少量情绪，只要包含实质信息也应保留
- 优先保留包含技术讨论、实践经验、问题反馈的评论
- 对于模棱两可的情况，优先保留（宁多勿漏）

请按以下JSON格式输出结果（只输出JSON，不要其他文字）：
{
  "results": [
    {
      "index": 1,
      "verdict": "keep" 或 "discard",
      "reason": "简要的判断理由（中文，10-30字）"
    },
    ...
  ]
}"""

    # 构建用户消息
    user_prompt = f"""请评估以下评论的知识价值：\n\n{comments_text}\n\n请逐条判断哪些评论有知识价值（保留），哪些没有（丢弃）。"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider_name == "mimo":
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,  # 低温度以提高一致性
        "max_tokens": 4096,
    }
    if provider_name == "mimo":
        payload["max_completion_tokens"] = payload.pop("max_tokens")

    try:
        resp = httpx.post(
            chat_completions_url,
            json=payload,
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()
        llm_output = result["choices"][0]["message"]["content"]
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        usage_summary = _usage_summary(usage, provider=provider_name, model=model)
        completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        get_usage_tracker().record(
            model=model,
            capability="text",
            prompt_tokens=usage_summary["input_tokens"],
            completion_tokens=usage_summary["output_tokens"],
            total_tokens=usage_summary["total_tokens"] or None,
            metadata={
                "provider": provider_name,
                "purpose": "comment_filter",
                "cached_tokens": usage_summary["cached_tokens"],
                "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
                "cache_hit_ratio": usage_summary["cache_hit_ratio"],
                "estimated_cost_rmb": usage_summary["estimated_cost_rmb"],
            },
        )

        # 解析JSON
        # 提取 ```json ... ``` 或直接JSON
        if "```json" in llm_output:
            json_str = llm_output.split("```json")[1].split("```")[0].strip()
        elif "```" in llm_output:
            json_str = llm_output.split("```")[1].split("```")[0].strip()
        else:
            # 尝试从文本中提取JSON
            start = llm_output.find("{")
            end = llm_output.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = llm_output[start:end]
            else:
                json_str = llm_output

        parsed = json.loads(json_str)
        policy.mark_success(breaker_key, "llm", {"model": model, "comment_count": len(comments), "provider": provider_name})
        
    except Exception as e:
        if verbose:
            log.info(f"[LLM] API Error: {e}")
        policy.mark_failure(breaker_key, "llm", str(e), metadata={"model": model, "comment_count": len(comments), "provider": provider_name}, retryable=True)
        get_monitor_tracker().record(
            scope="fallback",
            name="filter_comments",
            success=False,
            fallback_count=1,
            metadata={"provider": provider_name, "error": str(e)},
        )
        # LLM不可用时，用启发式规则降级
        return _fallback_filter(comments, verbose, provider=provider_name, model=model, reason=str(e)[:120])

    # 计算结果汇总
    total = len(comments)
    kept = 0
    discarded = 0
    detailed_results = []

    for item in parsed.get("results", []):
        idx = item.get("index", 0) - 1
        if 0 <= idx < len(comments):
            verdict = item.get("verdict", "discard")
            reason = item.get("reason", "")
            if verdict == "keep":
                kept += 1
            else:
                discarded += 1
            detailed_results.append({
                "index": idx + 1,
                "user": comments[idx].get("user", ""),
                "content": comments[idx].get("content", ""),
                "likes": comments[idx].get("likes", 0),
                "verdict": verdict,
                "reason": reason,
            })

    # 余额补齐
    if len(detailed_results) < total:
        for i in range(len(detailed_results), total):
            detailed_results.append({
                "index": i + 1,
                "user": comments[i].get("user", ""),
                "content": comments[i].get("content", ""),
                "likes": comments[i].get("likes", 0),
                "verdict": "unknown",
                "reason": "LLM未处理",
            })

    kept_comments = [
        r for r in detailed_results if r["verdict"] == "keep"
    ]

    return {
        "total": total,
        "kept": kept,
        "discarded": discarded if discarded > 0 else total - kept,
        "retention_rate": round(kept / total * 100, 1) if total > 0 else 0,
        "results": detailed_results,
        "kept_comments": kept_comments,
        "provider": provider_name,
        "model": model,
        "usage": usage_summary,
        "input_tokens": usage_summary["input_tokens"],
        "output_tokens": usage_summary["output_tokens"],
        "cached_tokens": usage_summary["cached_tokens"],
        "cache_hit_ratio": usage_summary["cache_hit_ratio"],
        "estimated_cost_rmb": usage_summary["estimated_cost_rmb"],
    }


def _resolve_comment_provider(*, api_url: str = "", api_key: str = "", model: str = "") -> Dict[str, str]:
    if api_url and model:
        return {
            "provider": _provider_name_from_url(api_url),
            "api_url": api_url.rstrip("/"),
            "api_key": api_key,
            "model": model,
        }
    policy = MediaModelPolicy.from_env()
    configured = policy.ordered_models("comment_filter")
    selected = configured[0] if configured else "bailian:qwen3.5-flash"
    provider, provider_model = _split_provider_model(selected)
    if provider == "bailian":
        return {
            "provider": "bailian",
            "api_url": DASHSCOPE_COMPATIBLE_BASE_URL,
            "api_key": os.environ.get("DASHSCOPE_API_KEY") or "",
            "model": provider_model or "qwen3.5-flash",
        }
    if provider == "mimo":
        return {
            "provider": "mimo",
            "api_url": MIMO_API_URL,
            "api_key": os.environ.get("MIMO_API_KEY", ""),
            "model": provider_model or MIMO_MODEL,
        }
    if provider == "api147":
        return {
            "provider": "api147",
            "api_url": API147_URL,
            "api_key": os.environ.get("API147_KEY", ""),
            "model": provider_model or API147_MODEL,
        }
    return {
        "provider": "openai_compatible",
        "api_url": api_url or DASHSCOPE_COMPATIBLE_BASE_URL,
        "api_key": api_key,
        "model": provider_model or model or selected,
    }


def _split_provider_model(value: str) -> tuple[str, str]:
    if ":" not in value:
        return "", value
    provider, model = value.split(":", 1)
    return provider.strip().lower(), model.strip()


def _provider_name_from_url(api_url: str) -> str:
    normalized = api_url.lower()
    if "dashscope.aliyuncs.com" in normalized:
        return "bailian"
    if "xiaomimimo.com" in normalized:
        return "mimo"
    if "147ai.com" in normalized:
        return "api147"
    return "openai_compatible"


def _cache_hit_ratio(cached_tokens: int, prompt_tokens: int) -> float:
    if prompt_tokens <= 0:
        return 0.0
    return round(cached_tokens / prompt_tokens, 4)


def _usage_summary(usage: Dict, *, provider: str, model: str) -> Dict:
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached_tokens = int(
        usage.get("cached_tokens")
        or prompt_details.get("cached_tokens")
        or input_details.get("cached_tokens")
        or 0
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_ratio": _cache_hit_ratio(cached_tokens, input_tokens),
        "estimated_cost_rmb": _estimate_comment_cost_rmb(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        ),
        "raw": usage,
    }


def _estimate_comment_cost_rmb(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
) -> Optional[float]:
    price = COMMENT_MODEL_PRICES_RMB_PER_M.get((provider, model))
    if not price:
        return None
    cached_price = price.get("cached_input")
    uncached_tokens = input_tokens if cached_price is None else max(input_tokens - cached_tokens, 0)
    cost = uncached_tokens * price["input"] / 1_000_000
    if cached_price is not None:
        cost += cached_tokens * cached_price / 1_000_000
    cost += output_tokens * price["output"] / 1_000_000
    return round(cost, 8)


def _chat_completions_url(api_url: str) -> str:
    """Build the OpenAI-compatible chat completions URL from a provider base URL."""
    normalized = api_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _fallback_filter(
    comments: List[Dict[str, str]],
    verbose: bool = True,
    *,
    provider: str = "rule",
    model: str = "",
    reason: str = "",
) -> Dict:
    """
    基于规则的降级过滤器，当LLM不可用时使用。
    
    使用简单的关键词和长度规则来判断评论价值。
    """
    VALUABLE_KEYWORDS = [
        "但", "不过", "然而", "可是", "缺点", "问题", "bug", "报错",
        "教程", "方法", "步骤", "配置", "安装", "经验", "实测",
        "对比", "推荐", "替代", "方案", "工具", "链接", "地址",
        "代码", "github", "api", "为什么", "怎么办", "不行",
        "升级", "版本", "更新", "修复", "解决", "注意", "建议",
    ]

    DISCARD_KEYWORDS = [
        "沙发", "板凳", "地板", "第一", "打卡", "来了",
        "666", "nb", "牛逼", "太强", "太棒", "厉害",
    ]

    total = len(comments)
    results = []
    kept = 0
    discarded = 0

    for i, c in enumerate(comments, 1):
        content = c.get("content", "")
        user = c.get("user", "")
        likes = c.get("likes", 0)
        content_lower = content.lower()

        # 检查是否包含有价值关键词
        has_value = any(kw in content for kw in VALUABLE_KEYWORDS)
        has_discard = any(kw in content_lower for kw in DISCARD_KEYWORDS)

        if has_value and not has_discard:
            verdict = "keep"
            kept += 1
            verdict_reason = "包含技术/经验关键词"
        elif len(content) < 5:
            verdict = "discard"
            discarded += 1
            verdict_reason = "评论过短，缺乏信息量"
        elif has_discard:
            verdict = "discard"
            discarded += 1
            verdict_reason = "灌水/打卡类评论"
        elif likes >= 100:
            verdict = "keep"
            kept += 1
            verdict_reason = "高赞评论，具有一定讨论价值"
        else:
            verdict = "discard"
            discarded += 1
            verdict_reason = "无明显知识价值"

        results.append({
            "index": i,
            "user": user,
            "content": content,
            "likes": likes,
            "verdict": verdict,
            "reason": verdict_reason,
        })

    kept_comments = [r for r in results if r["verdict"] == "keep"]

    return {
        "total": total,
        "kept": kept,
        "discarded": discarded,
        "retention_rate": round(kept / total * 100, 1) if total > 0 else 0,
        "results": results,
        "kept_comments": kept_comments,
        "fallback": True,
        "provider": provider,
        "model": model,
        "fallback_reason": reason,
    }


def print_result(result: Dict):
    """打印过滤结果"""
    print("=" * 60)
    print("评论过滤结果")
    print("=" * 60)
    print(f"总评论数: {result['total']}")
    print(f"保留: {result['kept']} | 丢弃: {result['discarded']}")
    print(f"保留率: {result['retention_rate']}%")
    if result.get("fallback"):
        print("⚠️ 使用降级规则过滤（LLM不可用）")
    print()
    print("--- 逐条判断 ---")
    for r in result["results"]:
        icon = "✅" if r["verdict"] == "keep" else "❌"
        print(f'{icon} #{r["index"]} [{r.get("likes",0)}赞] {r["user"]}: {r["content"][:60]}')
        print(f'   原因: {r["reason"]}')
    print()
    print("--- 保留的评论 ---")
    for r in result["kept_comments"]:
        print(f'✅ #{r["index"]}: {r["content"][:80]}')
        print(f'   原因: {r["reason"]}')
    print()


if __name__ == "__main__":
    # 测试: 加载前面爬取的评论
    import os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.environ.get("KR_DEMO_DATA_DIR") or os.path.join(project_root, "data")
    comments_file = os.environ.get("KR_DEMO_COMMENTS_FILE") or os.path.join(data_dir, "bilibili_comments_raw.json")
    
    if os.path.exists(comments_file):
        with open(comments_file, "r", encoding="utf-8") as f:
            comments = json.load(f)
        print(f"加载 {len(comments)} 条评论进行过滤测试\n")
        
        # 先尝试 LLM 过滤
        result = filter_valuable_comments(comments, verbose=True)
        print_result(result)
        
        # 保存过滤结果
        output_file = os.environ.get("KR_DEMO_COMMENT_FILTER_OUTPUT") or os.path.join(data_dir, "comment_filter_result.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已保存到: {output_file}")
    else:
        print(f"评论文件不存在: {comments_file}")
