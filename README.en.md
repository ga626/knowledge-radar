# KnowledgeRadar

[简体中文](README.md) · [Release](https://github.com/ga626/knowledge-radar/releases) · [First success](docs/FIRST_SUCCESS.md) · [Support](SUPPORT.md)

**A Windows-first, local-first MCP service for web research, academic metadata, Chinese content platforms, and recruitment sources.**

KnowledgeRadar lets Codex and other MCP clients use one local tool surface. Credentials, browser profiles, caches, task data, and logs stay on your computer. It is not a hosted service and does not bypass CAPTCHA, paywalls, or third-party account restrictions.

> Alpha: optional providers, platform login, quotas, and anti-bot boundaries are reported honestly as manual-action or degraded states.

## Release user quick start

1. Download the matching `KnowledgeRadar.zip`, checksum file, and `candidate-receipt.json` from [Releases](https://github.com/ga626/knowledge-radar/releases). Verify the ZIP SHA-256.
2. Install Python 3.12 and run this inside the extracted release:

   ```bat
   python scripts\product_install.py apply --channel stable --archive KnowledgeRadar.zip --receipt candidate-receipt.json
   ```

3. Run the generated `console.cmd` (`configure.cmd` remains compatible). It opens the fixed loopback-only console at `http://127.0.0.1:18882/` and writes only the active product data root. It is started again at Windows sign-in by default; see [the local-console guide](docs/LOCAL_CONSOLE.md) for control options.
4. Restart or refresh Codex, then call `health_check` and `get_capabilities`.

Read [First success](docs/FIRST_SUCCESS.md) and [product installation](docs/PRODUCT_INSTALL.md) before upgrading or rolling back.

## Keep the paths separate

| Audience | Start here |
| --- | --- |
| Release user | This quick start and `console.cmd`; do not use `install.bat`, `start.cmd`, or pytest. |
| Source developer | [Developer guide](docs/DEVELOPER.md); source checkout and HTTP debugging remain supported there. |
| Maintainer | [Release candidate guide](docs/RELEASE_CANDIDATE.md); publish only the already-verified immutable artifact. |

The installer maintains one `mcp_servers.knowledgeradar` configuration block, one active version, and a separate user data root. Updates and rollback do not overwrite user configuration. See [privacy and local state](docs/PRIVACY_AND_LOCAL_STATE.md), [support](SUPPORT.md), [contributing](CONTRIBUTING.md), and [security](SECURITY.md).
