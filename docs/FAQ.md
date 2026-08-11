# FAQ

## `/mcp` returns 406 in a browser

That is normal for streamable HTTP MCP. Use an MCP client or Agent, not a normal browser page.

## `health_check` is degraded

First check whether the degraded item is a main production chain or an optional candidate/diagnostic path. Missing optional providers, SearXNG, Chrome, or login profiles are configuration work, not a final pass state. Configure the missing item, then run `scripts\verify_all_capabilities.py --safe`; configured main-chain tools should pass.

## Xiaohongshu, BOSS, Liepin, or Maimai do not pass on first install

Run `scripts\setup_accounts.bat` and complete interactive login. Platform verification, CAPTCHA, account risk, and expired sessions require user action. After the user resolves them, rerun verification; remaining failures should be investigated.

## SearXNG is not running

Configure another web provider key or see `docs\SEARXNG.md`.

## Node.js or Chrome is missing

Core low-risk tools can still run, but bridge/browser-assisted tools require Node.js and Chrome. Install or configure them, then rerun verification.
