@echo off
setlocal
cd /d "%~dp0.."

if exist ".python312\python.exe" (
  set "PYTHON_EXE=%CD%\.python312\python.exe"
) else (
  set "PYTHON_EXE=python"
)

if "%KR_PROFILE_ROOT%"=="" (
  set "PROFILE_ROOT=%CD%\local\profiles"
) else (
  set "PROFILE_ROOT=%KR_PROFILE_ROOT%"
)

if not exist "%PROFILE_ROOT%" mkdir "%PROFILE_ROOT%"
if not exist "config\profile_registry.json" copy "config\profile_registry.example.json" "config\profile_registry.json" >nul

echo KnowledgeRadar 账号/Profile 配置
echo.
echo 这个流程需要人工交互。它会打开登录页面，并等待你完成登录或扫码。
echo 脚本不会、也不能绕过平台安全机制。
echo 脚本不会复制 cookies, secrets, browser locks, or existing local profiles.
echo Profile 根目录：%PROFILE_ROOT%
echo.
echo [1] Xiaohongshu
echo [2] Zhihu
echo [3] BOSS
echo [4] Liepin
echo [5] Maimai
echo [6] 跳过
echo.
set /p CHOICE=请选择平台 [1-6，默认 6]：
if "%CHOICE%"=="" set "CHOICE=6"
if "%CHOICE%"=="6" goto done

if "%CHOICE%"=="1" (
  set "PLATFORM=xiaohongshu"
  set "URL=https://www.xiaohongshu.com/explore"
)
if "%CHOICE%"=="2" (
  set "PLATFORM=zhihu"
  set "URL=https://www.zhihu.com"
)
if "%CHOICE%"=="3" (
  set "PLATFORM=boss"
  set "URL=https://www.zhipin.com"
)
if "%CHOICE%"=="4" (
  set "PLATFORM=liepin"
  set "URL=https://www.liepin.com"
)
if "%CHOICE%"=="5" (
  set "PLATFORM=maimai"
  set "URL=https://maimai.cn"
)

if not defined PLATFORM (
  echo 错误：无效选择。
  exit /b 1
)

set "PROFILE_DIR=%PROFILE_ROOT%\%PLATFORM%\account_a"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

echo.
echo Profile 目录：
echo   %PROFILE_DIR%
echo.
echo 正在打开登录页面：
echo   %URL%
echo.
start "" "%URL%"
echo 请在浏览器中完成登录，然后回到此窗口继续。
set /p _CONTINUE=完成后请按 Enter 继续：

echo.
echo 如需让 KnowledgeRadar 显式使用该 Profile，请更新 config\profile_registry.json。
echo 各平台说明见 docs\ACCOUNT_SETUP.md。
echo.
if "%PLATFORM%"=="xiaohongshu" (
  echo 可选的小红书连通性探测：
  echo   "%PYTHON_EXE%" tools\xhs_camoufox_login_probe.py
)

:done
echo 账号/Profile 配置流程结束。未配置的平台应提示 NEEDS_INTERACTION；完成登录后请重新运行能力验证。
exit /b 0
