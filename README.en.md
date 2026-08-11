# KnowledgeRadar

[简体中文](README.md) | [English](README.en.md)

KnowledgeRadar is an MCP server that gives agents a single search and perception layer across general web search, Bilibili, Xiaohongshu, Zhihu, YouTube-via-web, academic metadata, recruitment sites, webpage extraction, research planning, health checks, and runtime task status.

## Quick Start

1. Install Python 3.12 or use the bundled `.python312` runtime.
2. Copy `.env.example` to `.env` and fill only the providers you want to use.
3. Run `scripts\install.bat`.
4. Run `start.cmd`.
5. Register `http://127.0.0.1:18765/mcp` in your agent, or use `config\mcp-config-template.json`.

## Startup Modes

| Mode | Command | Use case |
| --- | --- | --- |
| Foreground HTTP | `start.cmd` | Local debugging; keeps logs visible in the terminal |
| Detached HTTP | `start-knowledgeradar-server.cmd` | Normal Windows startup from Explorer or scripts |
| Hidden HTTP | `powershell -ExecutionPolicy Bypass -File start-knowledgeradar-server-hidden.ps1` | Background startup without a visible console window |
| Stdio | `set KR_MCP_TRANSPORT=stdio && python -X utf8 src\server.py` | Agents that launch MCP servers directly |

All startup modes read only the repository-root `.env` file. It is private local state and is never a source, package, or GitHub input. Logs default to `runtime\logs` when launched through the included scripts.

## User Docs

- `docs\INSTALL.md`: installation and first run.
- `docs\API_KEYS.md`: provider keys and validation.
- `docs\MCP_SETUP.md`: Agent configuration examples.
- `docs\ACCOUNT_SETUP.md`: interactive browser login/profile setup.
- `docs\FAQ.md`: common validation states and troubleshooting.
- `docs\SEARXNG.md`: optional local SearXNG setup.
- `docs\PRIVACY_AND_LOCAL_STATE.md`: what remains local and how public artifacts are checked.

## Important Paths

| Path | Purpose | Commit |
| --- | --- | --- |
| `src/` | MCP server and collectors | Yes |
| `config/*.example` | Publishable templates | Yes |
| `config/profile_registry.json` | Local account/profile state | No |
| `.env` | API keys and private runtime settings | No |
| `local/profiles/` | Browser profile links | No |
| `runtime/` | Logs, task DBs, decision traces | No |
| `.python312/` | Optional bundled Python runtime | No |

## MCP Tools

The source of truth is `src/server.py` `@mcp.tool()` registrations. Current tools include:

- `expand_keywords`
- `plan_research`
- `analyze_decision_logs`
- `get_task_status`
- `kr_web_search`
- `search_academic`
- `extract_web_page`
- `extract_dynamic_page`
- `search_bilibili`
- `search_xiaohongshu`
- `search_zhihu`
- `search_recruitment`
- `get_content_detail`
- `get_capabilities`
- `health_check`

YouTube and GitHub search are exposed through `kr_web_search` provider modes rather than separate public tools.

## Verification

After setup, verify the installed product surface without printing secrets:

```bat
.python312\python.exe scripts\verify_api_keys.py
.python312\python.exe scripts\verify_all_capabilities.py --safe
```

`health_check` separates main production chains from optional candidates and diagnostic paths. A configured environment should have zero required main-chain `FAIL` and zero required main-chain `NEEDS_INTERACTION` in `verify_all_capabilities.py --safe`; declared optional providers, quota exhaustion, and designed fallback paths may be reported as `EXPECTED_DEGRADED` without failing the run. Login, QR-code, CAPTCHA, and similar human steps should be reported as `NEEDS_INTERACTION` until the user completes them.

## Secrets

Never commit real API keys, tokens, cookies, browser profiles, SQLite files, or runtime logs. Put private values only in the repository-root `.env`.
