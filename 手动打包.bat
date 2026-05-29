@echo off
chcp 65001 >nul
cd /d "%~dp0"
python jpg2pdf.py
echo.
echo 按任意键退出...
pause >nul
