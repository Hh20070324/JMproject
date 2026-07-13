[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Installer = Join-Path $ProjectRoot "python installer\python-3.14.5-amd64.exe"
$InstalledPython = Join-Path $env:LocalAppData "Programs\Python\Python314\python.exe"

function Test-CompatiblePython
{
    param([Parameter(Mandatory)][string]$Executable)

    try
    {
        & $Executable -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info < (3, 15) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch
    {
        return $false
    }
}

function Find-CompatiblePython
{
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand -and (Test-CompatiblePython $PythonCommand.Source))
    {
        return $PythonCommand.Source
    }

    if ((Test-Path -LiteralPath $InstalledPython) -and (Test-CompatiblePython $InstalledPython))
    {
        return $InstalledPython
    }

    return $null
}

Write-Host "========================================"
Write-Host "  JM 漫画下载器 - 环境配置"
Write-Host "========================================"
Write-Host

if (-not [Environment]::Is64BitOperatingSystem)
{
    throw "当前系统为 32 位 Windows，本项目仅支持 64 位 Windows。"
}

$Python = Find-CompatiblePython
if (-not $Python)
{
    if (-not (Test-Path -LiteralPath $Installer))
    {
        throw "没有找到兼容的 Python 3.10-3.14，也没有找到内置安装包：$Installer"
    }

    Write-Host "未检测到兼容的 Python，正在安装内置 Python 3.14.5..."
    $Arguments = @(
        "/passive"
        "InstallAllUsers=0"
        "PrependPath=1"
        "Include_launcher=1"
        "Include_test=0"
        "TargetDir=$($InstalledPython | Split-Path -Parent)"
    )
    $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru
    if ($Process.ExitCode -notin 0, 3010)
    {
        throw "Python 安装失败，退出代码：$($Process.ExitCode)"
    }
    if (-not (Test-CompatiblePython $InstalledPython))
    {
        throw "Python 安装完成，但没有找到可用的 python.exe。"
    }
    $Python = $InstalledPython
}

Write-Host "使用 Python：$Python"
& $Python --version

$RecreateVenv = -not (Test-Path -LiteralPath $VenvPython)
if (-not $RecreateVenv)
{
    $RecreateVenv = -not (Test-CompatiblePython $VenvPython)
}

if ($RecreateVenv)
{
    Write-Host "正在创建项目虚拟环境..."
    & $Python -m venv --clear (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0)
    {
        throw "创建虚拟环境失败。"
    }
}

Write-Host "正在安装运行依赖..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0)
{
    throw "pip 更新失败，请检查网络连接。"
}

& $VenvPython -m pip install --requirement (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0)
{
    throw "依赖安装失败，请检查网络连接。"
}

& $VenvPython -c "import jmcomic, flask, PIL, PySide6.QtCore; print('依赖检查通过')"
if ($LASTEXITCODE -ne 0)
{
    throw "依赖导入检查失败。"
}

Write-Host
Write-Host "环境配置完成。" -ForegroundColor Green
