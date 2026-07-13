@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" goto setup
"%VENV_PYTHON%" -c "from importlib.metadata import version; import jmcomic, flask, PIL, webview, PySide6.QtCore; raise SystemExit(0 if version('jmcomic') == '2.7.1' and version('pywebview') == '6.2.1' and version('PySide6-Essentials') == '6.11.1' else 1)" >nul 2>&1
if errorlevel 1 goto setup
goto run

:setup
echo 正在配置首次运行环境...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 环境配置失败，请查看上方信息。
    pause
    exit /b 1
)

:run
"%VENV_PYTHON%" desktop.py
set "APP_RESULT=%ERRORLEVEL%"
if not "%APP_RESULT%"=="0" (
    echo.
    echo [错误] 下载器启动失败，请查看上方信息。
    pause
)
exit /b %APP_RESULT%
