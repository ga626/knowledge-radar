"""兼容关键词规则模板；实际研究规划由当前 Agent 完成，不调用额外模型。"""

from typing import List

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
    return expand_via_rules(topic.strip()) if topic.strip() else []
