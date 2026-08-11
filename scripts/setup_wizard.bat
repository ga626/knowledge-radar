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

"%PYTHON_EXE%" scripts\start_setup_wizard.py
