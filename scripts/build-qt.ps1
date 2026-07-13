[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildDir = Join-Path $ProjectRoot "build-qt"
$DistDir = Join-Path $ProjectRoot "dist-qt"
$ReleaseDir = Join-Path $ProjectRoot "release-qt"
$AppDir = Join-Path $DistDir "JM-Downloader-Qt-Prototype"
$Archive = Join-Path $ReleaseDir "JM-Downloader-Qt-Prototype-Windows-x64.zip"

function Remove-BuildDirectory
{
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path))
    {
        return
    }

    $ResolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
    if (-not $ResolvedPath.StartsWith($ResolvedRoot + [IO.Path]::DirectorySeparatorChar))
    {
        throw "拒绝删除项目目录外的路径：$ResolvedPath"
    }
    Remove-Item -LiteralPath $ResolvedPath -Recurse -Force
}

function Assert-BundledFile
{
    param([Parameter(Mandatory)][string]$Name)

    $Match = Get-ChildItem -LiteralPath $AppDir -Recurse -File -Filter $Name |
        Select-Object -First 1
    if (-not $Match)
    {
        throw "Qt 发行目录缺少文件：$Name"
    }
}

function Invoke-SmokeTest
{
    param([Parameter(Mandatory)][string]$Executable)

    $Process = Start-Process -FilePath $Executable -ArgumentList "--smoke-test" -PassThru -WindowStyle Hidden
    if (-not $Process.WaitForExit(30000))
    {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $Process.WaitForExit()
        throw "Qt 启动验证超时：$Executable"
    }
    if ($Process.ExitCode -ne 0)
    {
        throw "Qt 启动验证失败：$Executable，退出代码：$($Process.ExitCode)"
    }
}

if (-not (Test-Path -LiteralPath $Python))
{
    throw "没有找到项目虚拟环境，请先运行 scripts\setup.ps1。"
}

Push-Location $ProjectRoot
try
{
    Write-Host "正在检查 Qt 构建依赖..."
    & $Python -m pip install --requirement requirements-qt-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Qt 构建依赖安装失败。" }

    & $Python -c "from importlib.metadata import version; import PySide6.QtWidgets; raise SystemExit(0 if version('PySide6-Essentials') == '6.11.1' else 1)"
    if ($LASTEXITCODE -ne 0) { throw "PySide6 Essentials 版本检查失败。" }

    if (-not $SkipTests)
    {
        Write-Host "正在运行 Qt 测试..."
        & $Python -m unittest discover -s tests -p test_qt_app.py -v
        if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止 Qt 构建。" }
    }

    Remove-BuildDirectory $BuildDir
    Remove-BuildDirectory $DistDir
    Remove-BuildDirectory $ReleaseDir

    Write-Host "正在构建 Qt 原型发行目录..."
    & $Python -m PyInstaller --noconfirm --clean --workpath $BuildDir --distpath $DistDir JM-Downloader-Qt.spec
    if ($LASTEXITCODE -ne 0) { throw "Qt PyInstaller 构建失败。" }

    Assert-BundledFile "JM-Downloader-Qt.exe"
    Assert-BundledFile "JM-Downloader-Qt-Debug.exe"
    Assert-BundledFile "qwindows.dll"
    Assert-BundledFile "Qt6Core.dll"
    Assert-BundledFile "Qt6Gui.dll"
    Assert-BundledFile "Qt6Widgets.dll"

    $Forbidden = Get-ChildItem -LiteralPath $AppDir -Recurse -Force |
        Where-Object {
            $_.Name -match "^(webview|pythonnet|flask|werkzeug|clr_loader)$" -or
            $_.Name -match "^(Python\.Runtime\.dll|Qt6WebEngineCore\.dll|clr\.pyd)$"
        }
    if ($Forbidden)
    {
        $Names = ($Forbidden | Select-Object -ExpandProperty FullName) -join ", "
        throw "Qt 原型混入旧 UI 或 WebEngine 依赖：$Names"
    }

    Write-Host "正在验证 Qt 正式版..."
    Invoke-SmokeTest (Join-Path $AppDir "JM-Downloader-Qt.exe")

    Write-Host "正在验证 Qt 调试版..."
    Invoke-SmokeTest (Join-Path $AppDir "JM-Downloader-Qt-Debug.exe")

    Remove-BuildDirectory (Join-Path $AppDir "logs")

    New-Item -ItemType Directory -Force $ReleaseDir | Out-Null
    Push-Location $DistDir
    try
    {
        Compress-Archive -LiteralPath "JM-Downloader-Qt-Prototype" -DestinationPath $Archive -CompressionLevel Optimal -Force
    }
    finally
    {
        Pop-Location
    }

    $Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    Write-Host
    Write-Host "Qt 原型构建完成：$Archive" -ForegroundColor Green
    Write-Host "SHA256：$Hash"
}
finally
{
    Pop-Location
}
