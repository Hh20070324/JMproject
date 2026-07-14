[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ReleaseVersion = "2.4.0"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
$ReleaseDir = Join-Path $ProjectRoot "release"
$AppDir = Join-Path $DistDir "JM-Downloader"
$ArchiveName = "JM-Downloader-v$ReleaseVersion-Windows-x64.zip"
$Archive = Join-Path $ReleaseDir $ArchiveName
$ChecksumFile = "$Archive.sha256"
$HistoricalArchives = @(
    (Join-Path $ReleaseDir "JM-Downloader-v2.1.0-Windows-x64.zip"),
    (Join-Path $ReleaseDir "JM-Downloader-v2.2.0-Windows-x64.zip"),
    (Join-Path $ReleaseDir "JM-Downloader-v2.3.0-Windows-x64.zip")
)
$LicensesDir = Join-Path $ProjectRoot "LICENSES"
$RequiredLicenseFiles = @(
    "README.md",
    "GPL-3.0-only.txt",
    "LGPL-3.0-only.txt",
    "Game-Icon-Pack-CC0-1.0.txt",
    "Python-3.14.txt",
    "JMComic-Crawler-Python-2.7.1.txt",
    "commonX-0.6.40.txt",
    "curl_cffi-0.15.0.txt",
    "curl_cffi-0.15.0-native.txt",
    "certifi-2026.6.17.txt",
    "cffi-2.0.0.txt",
    "Pillow-12.2.0.txt",
    "pycparser-3.0.txt",
    "PyCryptodome-3.23.0.txt",
    "PyInstaller-6.21.0.txt",
    "PyYAML-6.0.3.txt",
    "typing_extensions-4.16.0.txt"
)

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

    foreach ($Name in @("settings.json", "settings.ini", "tasks.json"))
    {
        Remove-BuildFile (Join-Path $AppDir $Name)
    }

    $RuntimeFiles = Get-ChildItem -LiteralPath $AppDir -Recurse -File -Force `
        -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -like "settings.json.corrupt-*" -or
            $_.Name -like "tasks.json.corrupt-*" -or
            $_.Name -like ".tasks.json.*.tmp" -or
            $_.Name -like "*.jm-part-*"
        }
    foreach ($RuntimeFile in $RuntimeFiles)
    {
        Remove-BuildFile $RuntimeFile.FullName
    }
}

function Assert-NoRuntimeArtifacts
{
    $Artifacts = @()
    foreach ($Name in @(
        "logs",
        "Pictures",
        "PDFs",
        "settings.json",
        "settings.ini",
        "tasks.json"
    ))
    {
        $Path = Join-Path $AppDir $Name
        if (Test-Path -LiteralPath $Path)
        {
            $Artifacts += (Resolve-Path -LiteralPath $Path).Path
        }
    }

    $Artifacts += Get-ChildItem -LiteralPath $AppDir -Recurse -File -Force `
        -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -like "settings.json.corrupt-*" -or
            $_.Name -like "tasks.json.corrupt-*" -or
            $_.Name -like ".tasks.json.*.tmp" -or
            $_.Name -like "*.jm-part-*"
        } | Select-Object -ExpandProperty FullName
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

function Assert-BundledRelativeFile
{
    param([Parameter(Mandatory)][string]$RelativePath)

    $Path = Join-Path $AppDir $RelativePath
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf))
    {
        throw "发行目录缺少文件：$RelativePath"
    }
}

function Assert-ArchiveContents
{
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try
    {
        $Entries = @($Zip.Entries | ForEach-Object {
            $_.FullName.Replace("\", "/")
        })
        $Required = @(
            "JM-Downloader/LICENSE",
            "JM-Downloader/THIRD_PARTY_NOTICES.md",
            "JM-Downloader/QT_SOURCE_AND_RELINKING.md",
            "JM-Downloader/QT_THIRD_PARTY_NOTICES.txt"
        )
        $Required += $RequiredLicenseFiles | ForEach-Object {
            "JM-Downloader/LICENSES/$($_.Replace('\', '/'))"
        }
        foreach ($Entry in $Required)
        {
            if ($Entries -notcontains $Entry)
            {
                throw "发行 ZIP 缺少文件：$Entry"
            }
        }

        $InvalidRoot = $Entries | Where-Object {
            $_ -and -not $_.StartsWith("JM-Downloader/")
        }
        if ($InvalidRoot)
        {
            throw "发行 ZIP 顶层结构无效：$($InvalidRoot -join ', ')"
        }

        $RuntimeArtifacts = $Entries | Where-Object {
            $_ -match "^JM-Downloader/(?:Pictures|PDFs|logs)(?:/|$)" -or
            $_ -match "^JM-Downloader/settings\.(?:json|ini)$" -or
            $_ -match "^JM-Downloader/settings\.json\.corrupt-" -or
            $_ -match "^JM-Downloader/tasks\.json$" -or
            $_ -match "^JM-Downloader/tasks\.json\.corrupt-" -or
            $_ -match "^JM-Downloader/\.tasks\.json\..*\.tmp$" -or
            $_ -match "\.jm-part-[^/]*$"
        }
        if ($RuntimeArtifacts)
        {
            throw "发行 ZIP 包含运行时文件：$($RuntimeArtifacts -join ', ')"
        }
    }
    finally
    {
        $Zip.Dispose()
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

function Assert-NoUnusedRuntime
{
    $ForbiddenNames = @(
        "_tcl_data",
        "_tk_data",
        "_tkinter.pyd",
        "opengl32sw.dll",
        "tcl8",
        "tcl86t.dll",
        "tk86t.dll"
    )
    $Forbidden = Get-ChildItem -LiteralPath $AppDir -Recurse -Force |
        Where-Object { $_.Name -in $ForbiddenNames }
    if ($Forbidden)
    {
        $Names = ($Forbidden | Select-Object -ExpandProperty FullName) -join ", "
        throw "发行目录混入未使用的 Tcl/Tk 或软件 OpenGL 运行时：$Names"
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
    Remove-BuildFile $ChecksumFile
    $HistoricalHashes = @{}
    foreach ($HistoricalArchive in $HistoricalArchives)
    {
        if (Test-Path -LiteralPath $HistoricalArchive -PathType Leaf)
        {
            $HistoricalHashes[$HistoricalArchive] = (
                Get-FileHash -LiteralPath $HistoricalArchive -Algorithm SHA256
            ).Hash
        }
    }

    Write-Host "正在构建 Windows 发行目录..."
    & $Python -m PyInstaller --noconfirm --clean --workpath $BuildDir `
        --distpath $DistDir JM-Downloader.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

    Remove-BuildFile (Join-Path $AppDir "_internal\PySide6\opengl32sw.dll")

    Copy-Item -LiteralPath "option.yml" -Destination $AppDir
    Copy-Item -LiteralPath "README.md" -Destination $AppDir
    Copy-Item -LiteralPath "用户指南.md" -Destination $AppDir
    Copy-Item -LiteralPath "LICENSE" -Destination $AppDir
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination $AppDir
    Copy-Item -LiteralPath "QT_SOURCE_AND_RELINKING.md" -Destination $AppDir
    Copy-Item -LiteralPath "QT_THIRD_PARTY_NOTICES.txt" -Destination $AppDir
    Copy-Item -LiteralPath $LicensesDir -Destination $AppDir -Recurse

    Assert-BundledFile "JM-Downloader.exe"
    Assert-BundledFile "JM-Downloader-Debug.exe"
    Assert-BundledFile "option.yml"
    Assert-BundledFile "README.md"
    Assert-BundledFile "用户指南.md"
    Assert-BundledFile "LICENSE"
    Assert-BundledFile "THIRD_PARTY_NOTICES.md"
    Assert-BundledRelativeFile "QT_SOURCE_AND_RELINKING.md"
    Assert-BundledRelativeFile "QT_THIRD_PARTY_NOTICES.txt"
    foreach ($LicenseFile in $RequiredLicenseFiles)
    {
        Assert-BundledRelativeFile (Join-Path "LICENSES" $LicenseFile)
    }
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
    Assert-NoUnusedRuntime

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
    Assert-NoUnusedRuntime

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
    $Utf8NoBom = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        $ChecksumFile,
        "$Hash  $ArchiveName`r`n",
        $Utf8NoBom
    )
    Assert-ArchiveContents

    foreach ($HistoricalArchive in $HistoricalHashes.Keys)
    {
        $CurrentHistoricalHash = (
            Get-FileHash -LiteralPath $HistoricalArchive -Algorithm SHA256
        ).Hash
        if ($CurrentHistoricalHash -ne $HistoricalHashes[$HistoricalArchive])
        {
            throw "构建过程修改了历史发行包：$HistoricalArchive"
        }
    }

    Write-Host
    Write-Host "构建完成：$Archive" -ForegroundColor Green
    Write-Host "SHA256：$Hash"
    Write-Host "校验文件：$ChecksumFile"
}
finally
{
    Pop-Location
}
