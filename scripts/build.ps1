[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$AppDir = Join-Path $DistDir "JM-Downloader"
$Archive = Join-Path $ReleaseDir "JM-Downloader-v2.1.0-Windows-x64.zip"

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

if (-not (Test-Path -LiteralPath $Python))
{
    throw "没有找到项目虚拟环境，请先运行 start.bat。"
}
Push-Location $ProjectRoot
try
{
    Write-Host "正在检查构建依赖..."
    & $Python -m pip install --requirement requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }

    if (-not $SkipTests)
    {
        Write-Host "正在运行测试..."
        & $Python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止构建。" }
    }

    Remove-BuildDirectory $BuildDir
    Remove-BuildDirectory $DistDir
    Remove-BuildDirectory $ReleaseDir

    Write-Host "正在构建 Windows 发行目录..."
    & $Python -m PyInstaller --noconfirm JM-Downloader.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

    Copy-Item -LiteralPath "option.yml" -Destination $AppDir
    Copy-Item -LiteralPath "README.md" -Destination $AppDir
    Copy-Item -LiteralPath "用户指南.md" -Destination $AppDir
    Copy-Item -LiteralPath "LICENSE" -Destination $AppDir
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination $AppDir
    Copy-Item -LiteralPath "scripts\windows-dotnet.config" -Destination (Join-Path $AppDir "JM-Downloader.exe.config")
    Copy-Item -LiteralPath "scripts\windows-dotnet.config" -Destination (Join-Path $AppDir "JM-Downloader-Debug.exe.config")

    New-Item -ItemType Directory -Force $ReleaseDir | Out-Null
    Push-Location $DistDir
    try
    {
        Compress-Archive -LiteralPath "JM-Downloader" -DestinationPath $Archive -CompressionLevel Optimal -Force
    }
    finally
    {
        Pop-Location
    }

    Write-Host
    Write-Host "构建完成：$Archive" -ForegroundColor Green
}
finally
{
    Pop-Location
}
