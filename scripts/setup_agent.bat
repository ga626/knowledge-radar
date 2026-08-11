@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist ".python312\python.exe" (
  set "PYTHON_EXE=%CD%\.python312\python.exe"
) else (
  set "PYTHON_EXE=python"
)

echo KnowledgeRadar Agent 配置助手
echo.
echo 本助手只打印适配当前路径的 MCP 配置片段。
echo 不会静默覆盖任何外部 Agent 配置文件。
echo.
echo [1] OpenClaw HTTP
echo [2] Codex stdio
echo [3] WorkBuddy stdio
echo [4] Claude Desktop stdio
echo [5] Cursor stdio
echo [6] CodeX 产品化检查/安装
echo [7] 打印全部配置片段
echo.
if defined KR_AGENT_CHOICE (
  set "CHOICE=%KR_AGENT_CHOICE%"
) else (
  set /p CHOICE=请选择目标 [1-7，默认 1]：
)
if "%CHOICE%"=="" set "CHOICE=1"

if "%CHOICE%"=="7" (
  echo.
  echo === OpenClaw HTTP ===
  "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent openclaw
  echo.
  echo === Codex stdio ===
  "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent codex
  echo.
  echo === WorkBuddy stdio ===
  "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent workbuddy
  echo.
  echo === Claude Desktop stdio ===
  "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent claude
  echo.
  echo === Cursor stdio ===
  "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent cursor
  goto after_choice
)
if "%CHOICE%"=="1" "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent openclaw & goto after_choice
if "%CHOICE%"=="2" "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent codex & goto after_choice
if "%CHOICE%"=="3" "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent workbuddy & goto after_choice
if "%CHOICE%"=="4" "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent claude & goto after_choice
if "%CHOICE%"=="5" "%PYTHON_EXE%" scripts\generate-mcp-config.py --agent cursor & goto after_choice
if "%CHOICE%"=="6" (
  "%PYTHON_EXE%" scripts\verify_codex_productization.py --json
  echo.
  echo 如需写入 CodeX 全局配置，请运行：
  echo "%PYTHON_EXE%" scripts\setup_codex_product.py --apply --json
  goto after_choice
)

echo 错误：无效选择。请重新运行本助手，并输入 1 到 7。
exit /b 1

:after_choice
echo.
echo 修改 Agent 配置前，请先备份目标文件。
echo 文件位置和回滚说明见 docs\MCP_SETUP.md。
exit /b 0
