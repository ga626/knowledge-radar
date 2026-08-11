@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================================
echo KnowledgeRadar 新用户安装向导
echo ============================================================
echo.
echo 本脚本只会在当前项目目录内安装依赖和创建本地运行目录。
echo 不会迁移旧的 WorkBuddy/OpenClaw 配置，也不会归档旧项目目录。
echo.

echo [阶段 1/8] 配置国内下载镜像
echo ------------------------------------------------------------
set "KR_PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
set "KR_PIP_TRUSTED_HOST=mirrors.tuna.tsinghua.edu.cn"
set "KR_NPM_REGISTRY=https://registry.npmmirror.com"
set "KR_PLAYWRIGHT_MIRROR_1=https://cdn.npmmirror.com/binaries/playwright"
set "KR_PLAYWRIGHT_MIRROR_2=https://npmmirror.com/mirrors/playwright"
set "PIP_INDEX_URL=%KR_PIP_INDEX_URL%"
set "PIP_TRUSTED_HOST=%KR_PIP_TRUSTED_HOST%"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "NPM_CONFIG_REGISTRY=%KR_NPM_REGISTRY%"
set "npm_config_registry=%KR_NPM_REGISTRY%"
if not defined PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT set "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000"
if not defined PLAYWRIGHT_BROWSERS_PATH set "PLAYWRIGHT_BROWSERS_PATH=%CD%\runtime\ms-playwright"
set "PLAYWRIGHT_DOWNLOAD_HOST="
set "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST="
set "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST="
set "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST="
echo Python 依赖镜像：%KR_PIP_INDEX_URL%
echo Node.js 依赖镜像：%KR_NPM_REGISTRY%
echo Playwright 浏览器下载：优先使用官方源，失败后尝试国内镜像。
echo Playwright 国内镜像 1：%KR_PLAYWRIGHT_MIRROR_1%
echo Playwright 国内镜像 2：%KR_PLAYWRIGHT_MIRROR_2%
echo Playwright 下载连接超时：%PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT% ms
echo Playwright 浏览器目录：%PLAYWRIGHT_BROWSERS_PATH%
if /I "%KR_INSTALL_KEEP_PROXY%"=="1" (
  echo 已按用户设置保留当前代理环境变量。
) else if defined KR_PLAYWRIGHT_PROXY (
  echo 已检测到显式 Playwright 下载代理，安装阶段保留当前代理环境变量。
) else (
  set "HTTP_PROXY="
  set "HTTPS_PROXY="
  set "ALL_PROXY="
  set "http_proxy="
  set "https_proxy="
  set "all_proxy="
  set "PIP_PROXY="
  set "NO_PROXY=127.0.0.1,localhost"
  set "no_proxy=127.0.0.1,localhost"
  echo 已清理安装阶段代理环境变量，避免沙盒或新电脑误用本机代理。
)
if defined KR_PLAYWRIGHT_PROXY (
  echo 已检测到显式 Playwright 下载代理，将仅在浏览器下载阶段使用。
)
echo.

echo [阶段 2/8] 检查 Python 运行环境
echo ------------------------------------------------------------
if exist ".python312\python.exe" (
  set "PYTHON_EXE=%CD%\.python312\python.exe"
  echo 已发现项目内置 Python：%CD%\.python312\python.exe
) else (
  echo 未发现项目内置 Python，将检查系统 PATH 中的 python。
  where python >nul 2>nul
  if errorlevel 1 (
    echo 错误：未发现项目内置 Python，也未在系统 PATH 中找到 python。
    echo 请先安装 Python 3.12+，或将可用的内置运行时放到 .python312 目录。
    echo 国内下载建议：优先使用 Python 官网国内镜像、公司软件源或可信安装包。
    exit /b 1
  )
  set "PYTHON_EXE=python"
  echo 已发现系统 PATH 中的 python，将继续检查版本。
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" || (
  echo 错误：需要 Python 3.12 或更高版本。
  echo 请先安装 Python 3.12+，或将可用的内置运行时放到 .python312 目录。
  echo 国内下载建议：优先使用 Python 官网国内镜像、公司软件源或可信安装包。
  exit /b 1
)
echo Python 版本检查通过。
echo.

echo [阶段 3/8] 检查 Node.js 和 Chrome
echo ------------------------------------------------------------
set "NPM_AVAILABLE=1"
where node >nul 2>nul
if errorlevel 1 (
  echo 提示：未在 PATH 中找到 Node.js。浏览器桥接和部分平台能力可能降级。
  echo 请按需安装 Node.js，或使用公司/国内可信软件源安装。
  set "NPM_AVAILABLE=0"
) else (
  echo Node.js 检查通过。
)

where npm >nul 2>nul
if errorlevel 1 (
  echo 提示：未在 PATH 中找到 npm。Node.js 桥接依赖将跳过安装。
  set "NPM_AVAILABLE=0"
) else (
  echo npm 检查通过。
)

where chrome >nul 2>nul
if errorlevel 1 (
  echo 提示：未在 PATH 中找到 Chrome。需要浏览器登录态的平台可能需要手动配置 Chrome 路径。
) else (
  echo Chrome 检查通过。
)
echo.

echo [阶段 4/8] 初始化本地配置和运行目录
echo ------------------------------------------------------------
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo 已从 .env.example 创建 .env。
) else (
  echo 已存在 .env，保持不覆盖。
)
if not exist "local\profiles" mkdir "local\profiles"
if not exist "runtime\logs" mkdir "runtime\logs"
if not exist "runtime\reports" mkdir "runtime\reports"
echo 本地目录初始化完成。
echo.

echo [阶段 5/8] 安装 Python 依赖
echo ------------------------------------------------------------
set "KR_INSTALL_LOG_DIR=%CD%\runtime\logs"
if not exist "%KR_INSTALL_LOG_DIR%" mkdir "%KR_INSTALL_LOG_DIR%"
if /I "%KR_INSTALL_SKIP_DEP_DOWNLOAD%"=="1" (
  echo 当前处于仿真验证模式，已跳过 Python 依赖下载。
  echo 将使用的安装命令：
  echo   "%PYTHON_EXE%" -m pip install -e . -i "%KR_PIP_INDEX_URL%" --trusted-host "%KR_PIP_TRUSTED_HOST%" --timeout 300 --retries 10 --prefer-binary --no-build-isolation
) else (
  echo 正在确认 Python 构建工具，请稍候...
  "%PYTHON_EXE%" -m pip install setuptools^>=68 wheel -i "%KR_PIP_INDEX_URL%" --trusted-host "%KR_PIP_TRUSTED_HOST%" --timeout 300 --retries 10 --prefer-binary > "%KR_INSTALL_LOG_DIR%\pip-build-tools.log" 2>&1
  if errorlevel 1 (
    echo 错误：Python 构建工具安装失败。
    echo 日志文件：%KR_INSTALL_LOG_DIR%\pip-build-tools.log
    findstr /R /C:"^Looking in indexes" /C:"^ERROR:" /C:"^WARNING:" "%KR_INSTALL_LOG_DIR%\pip-build-tools.log"
    exit /b 1
  )
  echo 正在使用清华 PyPI 镜像安装当前项目，请稍候...
  set "PIP_OK=0"
  for /L %%I in (1,1,3) do (
    if "!PIP_OK!"=="0" (
      echo Python 依赖安装尝试 %%I/3...
      echo 日志文件：%KR_INSTALL_LOG_DIR%\pip-install-attempt-%%I.log
      "%PYTHON_EXE%" -m pip install -e . -i "%KR_PIP_INDEX_URL%" --trusted-host "%KR_PIP_TRUSTED_HOST%" --timeout 300 --retries 10 --prefer-binary --no-build-isolation > "%KR_INSTALL_LOG_DIR%\pip-install-attempt-%%I.log" 2>&1
      if errorlevel 1 (
        echo 提示：本次 Python 依赖安装未成功，将按需重试。
        findstr /R /C:"^Looking in indexes" /C:"^ERROR:" /C:"^WARNING:" "%KR_INSTALL_LOG_DIR%\pip-install-attempt-%%I.log"
      ) else (
        set "PIP_OK=1"
        findstr /R /C:"^Looking in indexes" /C:"^Successfully" "%KR_INSTALL_LOG_DIR%\pip-install-attempt-%%I.log"
      )
    )
  )
  if not "!PIP_OK!"=="1" (
    echo 错误：Python 依赖安装失败。
    echo 建议：检查网络、代理、防火墙，或稍后重试。
    exit /b 1
  )
)
echo Python 依赖安装完成。
echo.

echo [阶段 5.5/8] 安装 Playwright 浏览器运行时
echo ------------------------------------------------------------
if /I "%KR_INSTALL_SKIP_DEP_DOWNLOAD%"=="1" (
  echo 当前处于仿真验证模式，已跳过 Playwright 浏览器下载。
) else if /I "%KR_INSTALL_SKIP_PLAYWRIGHT_BROWSER%"=="1" (
  echo 已按用户设置跳过 Playwright 浏览器下载。
) else if exist "%PLAYWRIGHT_BROWSERS_PATH%\chromium-1223\chrome-win64\chrome.exe" (
  echo 已发现项目内 Playwright Chromium 缓存，无需重新下载。
  set "PW_STATUS_FILE=%CD%\runtime\reports\playwright-browser-status.txt"
  echo status=pass> "!PW_STATUS_FILE!"
  echo source=project-cache>> "!PW_STATUS_FILE!"
) else (
  echo 正在下载 Playwright Chromium，请稍候...
  set "PW_OK=0"
  set "PW_STATUS_FILE=%CD%\runtime\reports\playwright-browser-status.txt"
  if exist "!PW_STATUS_FILE!" del /f /q "!PW_STATUS_FILE!" >nul 2>nul
  call :install_playwright_chromium "official" "" "1"
  if not "!PW_OK!"=="1" call :install_playwright_chromium "npmmirror-cdn" "%KR_PLAYWRIGHT_MIRROR_1%" "2"
  if not "!PW_OK!"=="1" call :install_playwright_chromium "npmmirror-web" "%KR_PLAYWRIGHT_MIRROR_2%" "3"
  if not "!PW_OK!"=="1" (
    echo 警告：Playwright Chromium 下载失败，安装流程将继续。
    echo 这不是最终修复状态。动态网页抓取和部分浏览器能力需要 Chromium 才能完整通过。
    echo 解决方式：
    echo   1. 如果你有代理，请重新运行前设置 KR_PLAYWRIGHT_PROXY=http://代理地址:端口
    echo   2. 如果你下载的是完整包，请确认 runtime\ms-playwright 已随包保留
    echo   3. 如果你手动下载 Playwright 浏览器，请放入 runtime\ms-playwright 后重新运行安装
    echo 日志文件位于：%KR_INSTALL_LOG_DIR%\playwright-install-chromium-*.log
    echo status=needs_configuration> "!PW_STATUS_FILE!"
    echo reason=playwright_chromium_download_failed>> "!PW_STATUS_FILE!"
    echo action=provide_proxy_or_project_browser_cache>> "!PW_STATUS_FILE!"
  ) else (
    echo status=pass> "!PW_STATUS_FILE!"
    echo source=!PW_SOURCE!>> "!PW_STATUS_FILE!"
  )
)
echo.

echo [阶段 6/8] 安装 Node.js 桥接依赖
echo ------------------------------------------------------------
if exist "bridge\package.json" (
  if "%NPM_AVAILABLE%"=="1" (
    if /I "%KR_INSTALL_SKIP_DEP_DOWNLOAD%"=="1" (
      echo 当前处于仿真验证模式，已跳过 Node.js 依赖下载。
      echo 将使用的安装命令：
      echo   npm install --registry "%KR_NPM_REGISTRY%"
    ) else (
      echo 正在使用 npmmirror 镜像安装 bridge 依赖，请稍候...
      pushd bridge
      call npm install --registry "%KR_NPM_REGISTRY%" --fetch-retries 5 --fetch-retry-mintimeout 20000 --fetch-retry-maxtimeout 120000 --fetch-timeout 300000
      if errorlevel 1 (
        popd
        echo 错误：Node.js 依赖安装失败。
        echo 建议：确认 Node.js/npm 可用，并检查 npm 镜像网络连接。
        exit /b 1
      )
      popd
    )
    echo Node.js 桥接依赖安装完成。
  ) else (
    echo 未检测到可用的 npm，已跳过 Node.js 桥接依赖安装。
    echo 需要浏览器桥接能力时，请安装 Node.js/npm 后重新运行本脚本。
  )
) else (
  echo 未发现 bridge\package.json，跳过 Node.js 桥接依赖安装。
)
echo.

echo [阶段 7/8] API Key 配置检查
echo ------------------------------------------------------------
echo API Key 配置说明：docs\API_KEYS.md
echo 推荐运行本地配置向导：scripts\setup_wizard.bat
echo 向导只监听 127.0.0.1，只会写入当前项目的 .env，不会上传或回显 Key。
echo 你也可以继续手动编辑 .env。缺少某项配置不会阻止服务器启动，但对应能力需要补齐配置后重新验证。
if /I not "%KR_INSTALL_NO_OPEN%"=="1" (
  start "" "%CD%\docs\API_KEYS.md"
  if exist "%CD%\.env" start "" notepad "%CD%\.env"
) else (
  echo 当前处于仿真/无人值守模式，已跳过自动打开文档。
)
if /I "%KR_INSTALL_NO_PAUSE%"=="1" (
  echo 当前处于无人值守模式，已跳过等待输入。
) else (
  echo 编辑完成后请回到此窗口继续。
  set /p _CONTINUE=请编辑完成后按 Enter 继续：
)

"%PYTHON_EXE%" scripts\verify_api_keys.py
echo.

echo [阶段 8/8] 可选配置
echo ------------------------------------------------------------
if defined KR_INSTALL_SETUP_AGENT (
  set "SETUP_AGENT=%KR_INSTALL_SETUP_AGENT%"
) else (
  set /p SETUP_AGENT=是否现在打印 AI Agent 的 MCP 配置片段？[输入 Y 或“是”，默认否]：
)
if /I "%SETUP_AGENT%"=="Y" call scripts\setup_agent.bat
if "%SETUP_AGENT%"=="是" call scripts\setup_agent.bat

echo.
if defined KR_INSTALL_SETUP_ACCOUNTS (
  set "SETUP_ACCOUNTS=%KR_INSTALL_SETUP_ACCOUNTS%"
) else (
  set /p SETUP_ACCOUNTS=是否现在配置需要登录的平台账号/Profile？[输入 Y 或“是”，默认否]：
)
if /I "%SETUP_ACCOUNTS%"=="Y" call scripts\setup_accounts.bat
if "%SETUP_ACCOUNTS%"=="是" call scripts\setup_accounts.bat

echo.
if defined KR_INSTALL_SETUP_TASK (
  set "SETUP_TASK=%KR_INSTALL_SETUP_TASK%"
) else (
  set /p SETUP_TASK=是否为当前项目创建 Windows 登录自启动任务？[输入 Y 或“是”，默认否]：
)
if /I "%SETUP_TASK%"=="Y" (
  schtasks /Create /TN "KnowledgeRadar MCP Server" /TR "\"%CD%\start-knowledgeradar-server.cmd\"" /SC ONLOGON /F
)
if "%SETUP_TASK%"=="是" (
  schtasks /Create /TN "KnowledgeRadar MCP Server" /TR "\"%CD%\start-knowledgeradar-server.cmd\"" /SC ONLOGON /F
)

echo.
echo ============================================================
echo 安装完成
echo ============================================================
echo 启动前台服务：
echo   start.cmd
echo.
echo 执行安全能力验证：
echo   "%PYTHON_EXE%" scripts\verify_all_capabilities.py --safe
echo.
echo MCP 地址：
echo   http://127.0.0.1:18765/mcp
echo.
exit /b 0

:install_playwright_chromium
set "PW_SOURCE=%~1"
set "PW_HOST=%~2"
set "PW_ATTEMPT=%~3"
set "PLAYWRIGHT_DOWNLOAD_HOST="
set "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST="
set "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST="
set "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST="
set "PW_PREV_HTTPS_PROXY=%HTTPS_PROXY%"
set "PW_PREV_HTTP_PROXY=%HTTP_PROXY%"
set "PW_PREV_ALL_PROXY=%ALL_PROXY%"
if defined KR_PLAYWRIGHT_PROXY (
  set "HTTPS_PROXY=%KR_PLAYWRIGHT_PROXY%"
  set "HTTP_PROXY=%KR_PLAYWRIGHT_PROXY%"
  set "ALL_PROXY=%KR_PLAYWRIGHT_PROXY%"
)
if not "%PW_HOST%"=="" (
  set "PLAYWRIGHT_DOWNLOAD_HOST=%PW_HOST%"
  set "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST=%PW_HOST%"
  set "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST=%PW_HOST%"
  set "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST=%PW_HOST%"
  echo Playwright 浏览器下载尝试 %PW_ATTEMPT%/3：%PW_SOURCE%（%PW_HOST%）
) else (
  echo Playwright 浏览器下载尝试 %PW_ATTEMPT%/3：%PW_SOURCE%（官方源）
)
echo 日志文件：%KR_INSTALL_LOG_DIR%\playwright-install-chromium-%PW_ATTEMPT%-%PW_SOURCE%.log
"%PYTHON_EXE%" -m playwright install chromium > "%KR_INSTALL_LOG_DIR%\playwright-install-chromium-%PW_ATTEMPT%-%PW_SOURCE%.log" 2>&1
if errorlevel 1 (
  if defined KR_PLAYWRIGHT_PROXY (
    set "HTTPS_PROXY=%PW_PREV_HTTPS_PROXY%"
    set "HTTP_PROXY=%PW_PREV_HTTP_PROXY%"
    set "ALL_PROXY=%PW_PREV_ALL_PROXY%"
  )
  echo 提示：本次 Playwright 浏览器下载未成功，将尝试下一个来源。
  findstr /R /C:"^Downloading" /C:"^Error" /C:"^Failed" "%KR_INSTALL_LOG_DIR%\playwright-install-chromium-%PW_ATTEMPT%-%PW_SOURCE%.log"
  exit /b 1
)
if defined KR_PLAYWRIGHT_PROXY (
  set "HTTPS_PROXY=%PW_PREV_HTTPS_PROXY%"
  set "HTTP_PROXY=%PW_PREV_HTTP_PROXY%"
  set "ALL_PROXY=%PW_PREV_ALL_PROXY%"
)
set "PW_OK=1"
echo Playwright Chromium 下载完成，来源：%PW_SOURCE%。
exit /b 0
