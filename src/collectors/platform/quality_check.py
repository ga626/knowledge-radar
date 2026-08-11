"""搜索结果质量检查模块。

用于监控 CSS 选择器是否失效，防止前端改版导致采集失败。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List

log = logging.getLogger("mcp-server")


def validate_search_results(items: List[Dict], platform: str) -> Dict:
    """验证搜索结果质量。

    Args:
        items: 搜索结果列表
        platform: 平台名称

    Returns:
        质量检查结果，包含 status、alert、rate 等字段
    """
    if not items:
        return {
            "status": "empty",
            "alert": True,
            "platform": platform,
            "message": f"{platform} 搜索结果为空",
            "timestamp": time.time(),
        }

    # 检查必要字段完整性
    valid_count = 0
    for item in items:
        if item.get("title"):
            valid_count += 1

    validity_rate = valid_count / len(items) if items else 0

    if validity_rate < 0.3:
        return {
            "status": "critical",
            "alert": True,
            "platform": platform,
            "rate": validity_rate,
            "total": len(items),
            "valid": valid_count,
            "message": f"{platform} 数据质量严重下降：有效率 {validity_rate:.1%}",
            "timestamp": time.time(),
        }

    if validity_rate < 0.5:
        return {
            "status": "degraded",
            "alert": True,
            "platform": platform,
            "rate": validity_rate,
            "total": len(items),
            "valid": valid_count,
            "message": f"{platform} 数据质量下降：有效率 {validity_rate:.1%}",
            "timestamp": time.time(),
        }

    # 检查是否包含薪资信息
    salary_count = sum(1 for item in items if item.get("salary"))
    salary_rate = salary_count / len(items) if items else 0

    if salary_rate < 0.3:
        return {
            "status": "warning",
            "alert": False,
            "platform": platform,
            "rate": validity_rate,
            "salary_rate": salary_rate,
            "total": len(items),
            "valid": valid_count,
            "message": f"{platform} 薪资信息缺失较多：{salary_rate:.1%}",
            "timestamp": time.time(),
        }

    return {
        "status": "ok",
        "alert": False,
        "platform": platform,
        "rate": validity_rate,
        "salary_rate": salary_rate,
        "total": len(items),
        "valid": valid_count,
        "message": f"{platform} 数据质量正常",
        "timestamp": time.time(),
    }


def log_quality_check(result: Dict) -> None:
    """记录质量检查结果到日志。"""
    status = result.get("status", "unknown")
    platform = result.get("platform", "unknown")
    message = result.get("message", "")

    if status == "critical":
        log.error(f"[质量检查] {message}")
    elif status == "degraded":
        log.warning(f"[质量检查] {message}")
    elif status == "warning":
        log.info(f"[质量检查] {message}")
    else:
        log.debug(f"[质量检查] {message}")
