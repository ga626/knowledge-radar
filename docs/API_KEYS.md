# API Keys

KnowledgeRadar never needs every provider key at once. Configure the providers you actually use.
The single supported product entrypoint is the repository-root `.env` file.
Legacy aliases and secret-file fallbacks are intentionally not loaded.

| Key | Required | Purpose |
| --- | --- | --- |
| `TAVILY_API_KEY` | Optional | General web search |
| `ANYSEARCH_API_KEY` | Optional | General web search fallback |
| `BRAVE_SEARCH_API_KEY` | Optional | General web search fallback |
| `EXA_API_KEY` | Optional | General web search fallback |
| `SEARXNG_BASE_URL` | Optional | Local/private web search endpoint |
| `YOUTUBE_API_KEY` | Optional | YouTube Data API |
| `OPENALEX_API_KEY` | Optional | OpenAlex academic metadata quota |
| `SEMANTIC_SCHOLAR_API_KEY` | Optional | Higher quota academic metadata |
| `BAIDU_QIANFAN_BEARER_TOKEN` | Optional | Baidu Qianfan Scholar API |
| `SERPAPI_API_KEY` | Optional | SerpAPI Google Scholar metadata |
| `KR_CORE_API_KEY` | Optional | CORE academic metadata |
| `DASHSCOPE_API_KEY` | Optional | DashScope/Bailian text and multimodal models |
| `SILICONFLOW_API_KEY` | Optional | Multimodal/LLM processing |
| `LLM_API_KEY` | Optional | OpenAI-compatible keyword expansion helper. Defaults to DeepSeek via `LLM_BASE_URL=https://api.deepseek.com/v1` and `LLM_MODEL=deepseek-chat`; if absent, `expand_keywords` uses rule templates. |
| `MIMO_API_KEY` | Optional | Mimo provider integration |
| `API147_KEY` | Optional | API147 comment-filter model fallback at `https://147ai.com/v1/chat/completions`; only used when `KR_COMMENT_FILTER_MODELS` selects `api147:<model>`. Defaults do not require it. |
| `TIKHUB_API_KEY` | Optional | Explicit Xiaohongshu fallback experiments |

Put secrets only in the repository-root `.env`. Never commit real keys.

The runtime loader ignores empty values. If a provider key is absent or blank,
the related feature reports `not_configured`/`EXPECTED_DEGRADED` instead of
borrowing a value from OpenClaw JSON, `local\secrets`, or `config\runtime.env`.

Validate configuration without printing secrets:

```bat
python scripts\verify_api_keys.py
```
