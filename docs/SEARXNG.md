# SearXNG

SearXNG is optional. KnowledgeRadar can use provider keys such as Tavily/AnySearch/Brave/Exa instead.

Default local URL:

```text
http://127.0.0.1:18888
```

Set in `.env`:

```env
SEARXNG_BASE_URL=http://127.0.0.1:18888
```

If SearXNG is missing, verification may report web search provider degradation. That is acceptable when another provider is configured, or when you only need non-web-search tools.
