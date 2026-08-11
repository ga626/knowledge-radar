# API 与服务商配置

KnowledgeRadar 不需要一次配置所有服务商；只填写你实际要使用的项目即可。唯一受支持的产品配置入口是仓库根目录 `.env`，不会读取旧别名或其他秘密文件作为兜底。

| 配置项 | 是否必填 | 用途 |
| --- | --- | --- |
| `TAVILY_API_KEY` | 可选 | 通用网页搜索 |
| `ANYSEARCH_API_KEY` | 可选 | 通用网页搜索备用服务 |
| `BRAVE_SEARCH_API_KEY` | 可选 | Brave Search |
| `EXA_API_KEY` | 可选 | Exa Search |
| `SEARXNG_BASE_URL` | 可选 | 本地或私有 SearXNG 地址 |
| `YOUTUBE_API_KEY` | 可选 | YouTube Data API |
| `OPENALEX_API_KEY` | 可选 | OpenAlex 学术元数据额度 |
| `SEMANTIC_SCHOLAR_API_KEY` | 可选 | Semantic Scholar 学术元数据额度 |
| `BAIDU_QIANFAN_BEARER_TOKEN` | Optional | Baidu Qianfan Scholar API |
| `SERPAPI_API_KEY` | Optional | SerpAPI Google Scholar metadata |
| `KR_CORE_API_KEY` | Optional | CORE academic metadata |
| `DASHSCOPE_API_KEY` | Optional | DashScope/Bailian text and multimodal models |
| `SILICONFLOW_API_KEY` | Optional | Multimodal/LLM processing |
| `LLM_API_KEY` | Optional | OpenAI-compatible keyword expansion helper. Defaults to DeepSeek via `LLM_BASE_URL=https://api.deepseek.com/v1` and `LLM_MODEL=deepseek-chat`; if absent, `expand_keywords` uses rule templates. |
| `MIMO_API_KEY` | Optional | Mimo provider integration |
| `API147_KEY` | Optional | API147 comment-filter model fallback at `https://147ai.com/v1/chat/completions`; only used when `KR_COMMENT_FILTER_MODELS` selects `api147:<model>`. Defaults do not require it. |
| `TIKHUB_API_KEY` | Optional | Explicit Xiaohongshu fallback experiments |

请只将密钥保存到仓库根目录 `.env`，绝不提交真实 Key。可运行 `scripts\setup_wizard.bat` 使用只监听本机的配置页面；它不会显示或上传密钥。

The runtime loader ignores empty values. If a provider key is absent or blank,
the related feature reports `not_configured`/`EXPECTED_DEGRADED` instead of
borrowing a value from OpenClaw JSON, `local\secrets`, or `config\runtime.env`.

可在不打印密钥的情况下检查配置：

```bat
python scripts\verify_api_keys.py
```
