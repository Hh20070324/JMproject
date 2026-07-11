@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo [错误] 尚未安装项目运行环境。
    echo 请先双击“一键安装.bat”，安装完成后再启动。
    echo.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -c "import jmcomic, flask, PIL" >nul 2>&1
if errorlevel 1 (
    echo [错误] 项目运行环境不完整。
    echo 请重新双击“一键安装.bat”修复依赖。
    echo.
    pause
    exit /b 1
)

"%VENV_PYTHON%" server.py
set "SERVER_RESULT=%ERRORLEVEL%"
if not "%SERVER_RESULT%"=="0" (
    echo.
    echo [错误] 下载器启动失败，请查看上方错误信息。
)
pause
exit /b %SERVER_RESULT%
