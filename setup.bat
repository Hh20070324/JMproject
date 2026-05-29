@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   JM 漫画下载器 - 环境安装
echo ========================================
echo.

:: 检测 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python！
    echo.
    echo 请先安装 Python，安装时务必勾选 "Add python.exe to PATH"
    echo 下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo Python 已检测到:
python --version
echo.

:: 安装依赖
echo 正在安装依赖包 (jmcomic + Pillow) ...
echo.
python -m pip install jmcomic Pillow -U

if errorlevel 1 (
    echo.
    echo [错误] 安装失败，请检查网络连接后重试
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安装完成！现在可以双击 "一键下载.bat" 开始使用了喵~
echo ========================================
pause
