"""
关键词拆解工具：将用户输入的主题拆成 5-8 个不同搜索角度的关键词
支持 LLM API 模式（OpenAI 兼容接口）和规则兜底模式
"""
import json
import os
import sys
import urllib.parse
from typing import List

from runtime.monitor import get_monitor_tracker
from runtime.degradation import get_degradation_policy
from runtime.usage_tracker import get_usage_tracker

# ==================== 配置 ====================
# 优先从环境变量读取
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")


def expand_via_llm(topic: str) -> List[str]:
    """通过 LLM API 将主题拆解成多个搜索关键词"""
    policy = get_degradation_policy()
    breaker_key = "llm:expand_keywords"
    if policy.is_open(breaker_key).get("open"):
        policy.record_degradation("llm", breaker_key, "circuit breaker open", {"topic": topic})
        return []
    
    prompt = f"""你是一个搜索策略专家。用户有一个搜索主题，请从不同角度拆解成 5-8 个独立的搜索关键词。

要求：
- 每个关键词是一个独立的搜索词，可直接用于搜索引擎
- 必须从不同视角覆盖该主题
- 不要重复同一个意思的不同说法
- 输出格式：纯 JSON 列表，如 ["关键词1", "关键词2", ...]
- 不要输出任何其他内容

主题：{topic}
"""
    
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 500,
    }

    try:
        import httpx
        resp = httpx.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise Exception(f"API returned {resp.status_code}: {resp.text[:200]}")

        result = resp.json()
        content = result["choices"][0]["message"]["content"].strip()
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        get_usage_tracker().record(
            model=LLM_MODEL,
            capability="text",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0) or None,
            metadata={"provider": "deepseek", "purpose": "expand_keywords"},
        )

        # 尝试解析 JSON
        # 有些 LLM 可能会输出 markdown 代码块
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        keywords = json.loads(content)
        if not isinstance(keywords, list) or len(keywords) < 3:
            raise ValueError(f"Invalid LLM response: {content}")
        policy.mark_success(breaker_key, "llm", {"topic": topic})
        return keywords[:8]
    except Exception as e:
        print(f"[expand_keywords] LLM 模式失败: {e}", file=sys.stderr)
        policy.mark_failure(breaker_key, "llm", str(e), metadata={"topic": topic}, retryable=True)
        get_monitor_tracker().record(
            scope="fallback",
            name="expand_keywords",
            success=False,
            fallback_count=1,
            metadata={"provider": "deepseek", "error": str(e)},
        )
        return []


# 搜索视角模板
_ANGLE_TEMPLATES = [
    "{} 教程",
    "{} 入门",
    "{} 踩坑",
    "{} 经验",
    "{} 对比",
    "{} 推荐",
    "{} 评测",
    "{} 实战",
    "{} 案例",
    "{} 技巧",
    "{} 常见问题",
    "{} 方案",
    "{} 搭建指南",
    "{} 最佳实践",
    "{} 避坑",
    "{} 优缺点",
]


def expand_via_rules(topic: str) -> List[str]:
    """基于规则的关键词拆解（无需 LLM）"""
    seen = set()
    results = []

    # 1. 原主题和精简版
    candidates = [topic, topic.replace(" ", "")]
    # 提取核心词（取最后一部分）
    parts = topic.replace("的", " ").split()
    if len(parts) > 3:
        core = " ".join(parts[-3:])
        candidates.append(core)
        candidates.append(core.replace(" ", ""))

    # 2. 用角度模板组合
    for base in candidates:
        for tmpl in _ANGLE_TEMPLATES:
            kw = tmpl.format(base)
            if kw not in seen and len(kw) >= 4:
                seen.add(kw)
                results.append(kw)
            if len(results) >= 12:
                break
        if len(results) >= 12:
            break

    return results[:8]


def expand_keywords(topic: str) -> List[str]:
    """主入口：先尝试 LLM，失败则用规则兜底"""
    topic = topic.strip()
    if not topic:
        return []

    # 尝试 LLM 模式
    if LLM_API_KEY:
        keywords = expand_via_llm(topic)
        if keywords:
            return keywords

    # 规则兜底
    print("[expand_keywords] 使用规则兜底模式", file=sys.stderr)
    return expand_via_rules(topic)


# ==================== 主入口 ====================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        topic = "OpenClaw 知识库搭建"
    else:
        topic = " ".join(sys.argv[1:])

    print(f"输入主题: {topic}", file=sys.stderr)
    print("---", file=sys.stderr)

    keywords = expand_keywords(topic)

    if not keywords:
        print("[]")
        sys.exit(1)

    print(f"生成 {len(keywords)} 个关键词:", file=sys.stderr)
    for i, kw in enumerate(keywords, 1):
        print(f"  {i}. {kw}", file=sys.stderr)

    # 输出 JSON 供管道使用
    if "--batch" in sys.argv:
        # 批处理模式：每行一个，最后一行是逗号合并版
        for kw in keywords:
            print(kw)
        print("|".join(keywords))
    else:
        print(json.dumps(keywords, ensure_ascii=False))
