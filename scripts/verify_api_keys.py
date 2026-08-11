"""检查 KnowledgeRadar API Key 配置，不打印任何密钥内容。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.env_loader import load_runtime_env  # noqa: E402

PROVIDERS = [
    ("TAVILY_API_KEY", "Tavily 网页搜索", False),
    ("ANYSEARCH_API_KEY", "AnySearch 网页搜索", False),
    ("BRAVE_SEARCH_API_KEY", "Brave Search", False),
    ("EXA_API_KEY", "Exa Search", False),
    ("YOUTUBE_API_KEY", "YouTube Data API", False),
    ("OPENALEX_API_KEY", "OpenAlex 学术元数据", False),
    ("SEMANTIC_SCHOLAR_API_KEY", "Semantic Scholar", False),
    ("BAIDU_QIANFAN_BEARER_TOKEN", "百度千帆百度学术", False),
    ("SERPAPI_API_KEY", "SerpAPI Google Scholar", False),
    ("KR_CORE_API_KEY", "CORE 学术 API", False),
    ("DASHSCOPE_API_KEY", "DashScope/百炼", False),
    ("SILICONFLOW_API_KEY", "SiliconFlow 多模态/LLM", False),
    ("LLM_API_KEY", "通用 LLM 服务商", False),
    ("MIMO_API_KEY", "Mimo 服务商", False),
    ("API147_KEY", "API147 备用模型服务", False),
    ("TIKHUB_API_KEY", "TikHub 兜底能力", False),
]


def configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

def masked_status(value: str) -> str:
    if not value:
        return "未配置"
    return "已配置"


def main() -> int:
    configure_console_encoding()
    load_runtime_env()

    print("KnowledgeRadar API Key/服务商配置检查")
    print("不会打印任何密钥内容。")
    print()
    configured = 0
    for key, label, required in PROVIDERS:
        value = os.environ.get(key, "").strip()
        if value:
            configured += 1
        requirement = "必填" if required else "可选"
        print(f"{key:28} {masked_status(value):20} {requirement:8} {label}")

    searxng = os.environ.get("SEARXNG_BASE_URL", "").strip()
    print(f"{'SEARXNG_BASE_URL':28} {('已配置' if searxng else '未配置'):20} 可选       本地 SearXNG 地址")
    print()
    if configured == 0 and not searxng:
        print("尚未配置 Web 搜索服务商。基础服务器仍可启动，但 Web 搜索能力需要补齐配置后重新验证。")
    else:
        print("服务商配置检查完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
