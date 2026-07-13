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
$Archive = Join-Path $ReleaseDir "JM-Downloader-Windows-x64.zip"

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

function Remove-BuildFile
{
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf))
    {
        return
    }

    $ResolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $ResolvedPath = (Resolve-Path -LiteralPath $Path).Path
    if (-not $ResolvedPath.StartsWith($ResolvedRoot + [IO.Path]::DirectorySeparatorChar))
    {
        throw "拒绝删除项目目录外的文件：$ResolvedPath"
    }
    Remove-Item -LiteralPath $ResolvedPath -Force
}

function Remove-RuntimeArtifacts
{
    foreach ($Name in @("logs", "Pictures", "PDFs"))
    {
        Remove-BuildDirectory (Join-Path $AppDir $Name)
    }

    foreach ($Name in @("settings.json", "settings.ini"))
    {
        Remove-BuildFile (Join-Path $AppDir $Name)
    }

    $CorruptBackups = Get-ChildItem -LiteralPath $AppDir -File -Force `
        -Filter "settings.json.corrupt-*" -ErrorAction SilentlyContinue
    foreach ($Backup in $CorruptBackups)
    {
        Remove-BuildFile $Backup.FullName
    }
}

function Assert-NoRuntimeArtifacts
{
    $Artifacts = @()
    foreach ($Name in @("logs", "Pictures", "PDFs", "settings.json", "settings.ini"))
    {
        $Path = Join-Path $AppDir $Name
        if (Test-Path -LiteralPath $Path)
        {
            $Artifacts += (Resolve-Path -LiteralPath $Path).Path
        }
    }

    $Artifacts += Get-ChildItem -LiteralPath $AppDir -File -Force `
        -Filter "settings.json.corrupt-*" -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
    if ($Artifacts)
    {
        throw "发行目录包含运行时文件，拒绝打包：$($Artifacts -join ', ')"
    }
}

function Assert-BundledFile
{
    param([Parameter(Mandatory)][string]$Name)

    $Match = Get-ChildItem -LiteralPath $AppDir -Recurse -File -Filter $Name |
        Select-Object -First 1
    if (-not $Match)
    {
        throw "发行目录缺少文件：$Name"
    }
}

function Assert-BundledPath
{
    param([Parameter(Mandatory)][string]$RelativePattern)

    $Pattern = Join-Path $AppDir $RelativePattern
    $Match = Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $Match)
    {
        throw "发行目录缺少文件：$RelativePattern"
    }
}

function Assert-NoLegacyRuntime
{
    $Forbidden = Get-ChildItem -LiteralPath $AppDir -Recurse -Force |
        Where-Object {
            $_.Name -match "^(?i:webview|pywebview|pythonnet|flask|werkzeug|clr_loader)([._-]|$)" -or
            $_.Name -match "^(?i:clr(?:\.py|\.pyd)?|Python\.Runtime.*|WebView2Loader\.dll)$" -or
            $_.Name -match "^(?i:Microsoft\.Web\.WebView2.*|Qt6WebEngine.*|QtWebEngine.*)$" -or
            $_.Name -match "^(?i:JM-Downloader(?:-Debug)?\.exe\.config)$"
        }
    if (Test-Path -LiteralPath (Join-Path $AppDir "static"))
    {
        $Forbidden += Get-Item -LiteralPath (Join-Path $AppDir "static")
    }
    if ($Forbidden)
    {
        $Names = ($Forbidden | Select-Object -ExpandProperty FullName) -join ", "
        throw "发行目录混入旧 UI、.NET 或 WebEngine 依赖：$Names"
    }
}

function Invoke-ExecutableTest
{
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$Argument,
        [Parameter(Mandatory)][string]$Description
    )

    $Process = Start-Process -FilePath $Executable -ArgumentList $Argument `
        -PassThru -WindowStyle Hidden
    if (-not $Process.WaitForExit(30000))
    {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $Process.WaitForExit()
        throw "$Description 超时：$Executable"
    }
    if ($Process.ExitCode -ne 0)
    {
        throw "$Description 失败：$Executable，退出代码：$($Process.ExitCode)"
    }
}

if (-not (Test-Path -LiteralPath $Python))
{
    throw "没有找到项目虚拟环境，请先运行 scripts\setup.ps1。"
}

Push-Location $ProjectRoot
try
{
    Write-Host "正在检查构建依赖..."
    & $Python -m pip install --requirement requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }

    & $Python -c "from importlib.metadata import version; import PySide6.QtWidgets; raise SystemExit(0 if version('PySide6-Essentials') == '6.11.1' else 1)"
    if ($LASTEXITCODE -ne 0) { throw "PySide6 Essentials 版本检查失败。" }

    if (-not $SkipTests)
    {
        Write-Host "正在运行完整测试..."
        & $Python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止构建。" }
    }

    Remove-BuildDirectory $BuildDir
    Remove-BuildDirectory $DistDir
    Remove-BuildFile $Archive

    Write-Host "正在构建 Windows 发行目录..."
    & $Python -m PyInstaller --noconfirm --clean --workpath $BuildDir `
        --distpath $DistDir JM-Downloader.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

    Copy-Item -LiteralPath "option.yml" -Destination $AppDir
    Copy-Item -LiteralPath "README.md" -Destination $AppDir
    Copy-Item -LiteralPath "用户指南.md" -Destination $AppDir
    Copy-Item -LiteralPath "LICENSE" -Destination $AppDir
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination $AppDir

    Assert-BundledFile "JM-Downloader.exe"
    Assert-BundledFile "JM-Downloader-Debug.exe"
    Assert-BundledFile "option.yml"
    Assert-BundledFile "README.md"
    Assert-BundledFile "用户指南.md"
    Assert-BundledFile "LICENSE"
    Assert-BundledFile "THIRD_PARTY_NOTICES.md"
    Assert-BundledFile "qwindows.dll"
    Assert-BundledFile "Qt6Core.dll"
    Assert-BundledFile "Qt6Gui.dll"
    Assert-BundledFile "Qt6Widgets.dll"
    Assert-BundledFile "styles_light.qss"
    Assert-BundledFile "styles_dark.qss"
    Assert-BundledPath "_internal\curl_cffi\_wrapper.pyd"
    Assert-BundledPath "_internal\certifi\cacert.pem"
    Assert-BundledPath "_internal\yaml\_yaml*.pyd"
    Assert-BundledPath "_internal\Crypto\Cipher\_raw_aes*.pyd"
    Assert-BundledPath "_internal\PIL\_imaging*.pyd"
    Assert-NoLegacyRuntime

    try
    {
        Write-Host "正在验证正式版..."
        Invoke-ExecutableTest (Join-Path $AppDir "JM-Downloader.exe") `
            "--smoke-test" "启动验证"

        Write-Host "正在验证调试版..."
        Invoke-ExecutableTest (Join-Path $AppDir "JM-Downloader-Debug.exe") `
            "--smoke-test" "启动验证"

        Write-Host "正在验证正式版离线下载后端..."
        Invoke-ExecutableTest (Join-Path $AppDir "JM-Downloader.exe") `
            "--backend-smoke-test" "离线下载后端验证"

        Write-Host "正在验证调试版离线下载后端..."
        Invoke-ExecutableTest (Join-Path $AppDir "JM-Downloader-Debug.exe") `
            "--backend-smoke-test" "离线下载后端验证"
    }
    finally
    {
        Remove-RuntimeArtifacts
    }

    Assert-NoRuntimeArtifacts
    Assert-NoLegacyRuntime

    New-Item -ItemType Directory -Force $ReleaseDir | Out-Null
    Push-Location $DistDir
    try
    {
        Compress-Archive -LiteralPath "JM-Downloader" `
            -DestinationPath $Archive -CompressionLevel Optimal -Force
    }
    finally
    {
        Pop-Location
    }

    $Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
    Write-Host
    Write-Host "构建完成：$Archive" -ForegroundColor Green
    Write-Host "SHA256：$Hash"
}
finally
{
    Pop-Location
}
