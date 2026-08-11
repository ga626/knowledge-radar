# MCP Setup

Start the HTTP server first:

```bat
start.cmd
```

Then register:

```text
http://127.0.0.1:18765/mcp
```

Generate current-path snippets:

```bat
python scripts\generate-mcp-config.py --agent openclaw
python scripts\generate-mcp-config.py --agent workbuddy
python scripts\generate-mcp-config.py --agent claude
python scripts\generate-mcp-config.py --agent cursor
```

Before editing an existing Agent config, create a timestamped backup. The helper `scripts\setup_agent.bat` prints snippets but does not silently overwrite external files.

For stdio clients, the generated snippet launches:

```bat
python -X utf8 src\server.py
```

with `KR_MCP_TRANSPORT=stdio`.
