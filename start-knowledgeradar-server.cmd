@echo off
setlocal
set "KR_ROOT=%~dp0"
cd /d "%KR_ROOT%"

if not exist "%KR_ROOT%runtime\logs" mkdir "%KR_ROOT%runtime\logs"
if not defined KR_LOG_DIR set "KR_LOG_DIR=%KR_ROOT%runtime\logs"
if not defined KR_STATE_DIR set "KR_STATE_DIR=%KR_ROOT%runtime"
if not defined KR_MCP_TRANSPORT set "KR_MCP_TRANSPORT=streamable-http"
if not defined KR_MCP_HOST set "KR_MCP_HOST=127.0.0.1"
if not defined KR_MCP_PORT set "KR_MCP_PORT=18765"

if exist "%KR_ROOT%.python312\python.exe" (
  set "PYTHON_EXE=%KR_ROOT%.python312\python.exe"
) else (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" -X utf8 "%KR_ROOT%start-knowledgeradar-server-detached.py"
