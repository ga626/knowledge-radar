# API 与服务商配置

KnowledgeRadar 不需要一次配置所有服务商；只填写你实际要使用的项目即可。Release 产品的唯一配置文件是 data 根 `config\runtime.env`，请通过 `configure.cmd` 写入；源码 checkout 兼容使用仓库根 `.env`。两者都不会读取 OpenClaw JSON、`local\secrets` 或其他秘密文件作为兜底。

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

绝不提交真实 Key。Release 用户运行 `configure.cmd`；源码开发者可运行 `scripts\setup_wizard.bat`。两种页面都只监听本机，均不会显示或上传密钥。

The runtime loader ignores empty values. If a provider key is absent or blank,
the related feature reports `not_configured`/`EXPECTED_DEGRADED` instead of
borrowing a value from OpenClaw JSON, `local\secrets`, or `config\runtime.env`.

可在不打印密钥的情况下检查配置：

```bat
python scripts\verify_api_keys.py
```
