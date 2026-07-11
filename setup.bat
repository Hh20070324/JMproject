@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ========================================
echo   JM 漫画下载器 - 环境安装
echo ========================================
echo.

set "PYTHON_EXE="
set "INSTALLER=python installer\python-3.14.5-amd64.exe"
set "INSTALLED_PYTHON=%LocalAppData%\Programs\Python\Python314\python.exe"
set "VENV_PYTHON=.venv\Scripts\python.exe"

rem 内置安装器和已锁定依赖面向 64 位 Windows。
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if "%PROCESSOR_ARCHITEW6432%"=="" (
    echo [错误] 当前系统为 32 位 Windows，本项目仅支持 64 位 Windows。
    pause
    exit /b 1
)

rem Pillow 12.2.0 至少需要 Python 3.10；Python 3.15 尚未验证。
python -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 15) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_EXE=python"

if not defined PYTHON_EXE (
    python --version >nul 2>&1
    if not errorlevel 1 (
        echo 当前 PATH 中的 Python 不在已验证的 3.10 至 3.14 范围内。
        echo 将改用内置 Python 3.14.5。
        echo.
    ) else (
        echo 未检测到已加入 PATH 的兼容 Python，将安装内置 Python。
        echo.
    )

    if not exist "!INSTALLER!" (
        echo [错误] 找不到内置安装包：
        echo "!INSTALLER!"
        echo.
        pause
        exit /b 1
    )

    echo 正在安装内置 Python 3.14.5...
    echo 安装程序将自动启用 Add python.exe to PATH。
    echo.

    start "" /wait "!INSTALLER!" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 TargetDir="%LocalAppData%\Programs\Python\Python314"
    set "INSTALL_RESULT=!ERRORLEVEL!"

    if not "!INSTALL_RESULT!"=="0" if not "!INSTALL_RESULT!"=="3010" (
        echo.
        echo [错误] Python 安装失败，安装程序退出代码：!INSTALL_RESULT!
        pause
        exit /b 1
    )

    if not exist "!INSTALLED_PYTHON!" (
        echo.
        echo [错误] Python 安装结束，但未找到 python.exe。
        echo 请重新运行本脚本，或手动运行 python installer 文件夹中的安装包。
        pause
        exit /b 1
    )

    rem 当前窗口不会自动获得安装器写入的新 PATH，因此本次使用绝对路径。
    set "PYTHON_EXE=!INSTALLED_PYTHON!"
    echo Python 安装完成。
    echo.
)

"!PYTHON_EXE!" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 15) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] Python 必须在 3.10 至 3.14 范围内。
    pause
    exit /b 1
)

echo Python 已检测到：
"!PYTHON_EXE!" --version
echo.

set "CREATE_VENV=0"
if not exist "!VENV_PYTHON!" set "CREATE_VENV=1"
if exist "!VENV_PYTHON!" (
    "!VENV_PYTHON!" -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 15) else 1)" >nul 2>&1
    if errorlevel 1 set "CREATE_VENV=1"
)

if "!CREATE_VENV!"=="1" (
    echo 正在创建项目独立运行环境 .venv ...
    if exist ".venv" (
        "!PYTHON_EXE!" -m venv --clear ".venv"
    ) else (
        "!PYTHON_EXE!" -m venv ".venv"
    )
    if errorlevel 1 (
        echo.
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo 已检测到项目虚拟环境 .venv。
)

echo.
echo 正在更新虚拟环境中的 pip...
"!VENV_PYTHON!" -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo [错误] pip 更新失败，请检查网络连接后重试。
    pause
    exit /b 1
)

echo.
echo 正在安装固定版本的爬虫及运行依赖...
echo.
"!VENV_PYTHON!" -m pip install --requirement requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败，请检查网络连接后重试。
    pause
    exit /b 1
)

echo.
echo 正在检查运行环境...
"!VENV_PYTHON!" -c "import jmcomic, flask, PIL; print('依赖检查通过')"
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装完成，但导入检查失败。
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安装完成！现在可以双击“一键下载.bat”开始使用。
echo ========================================
pause
exit /b 0
