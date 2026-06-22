<#
.SYNOPSIS
  一键构建 zhz_tool 发布产物:PyInstaller 打包 → 绿色版 zip → Inno Setup 安装包。

.DESCRIPTION
  版本号单一来源 = app/version.py 的 __version__。本脚本读它,贯穿命名与安装包。
  产物落在 dist/:
    dist\zhz_tool\                  (onedir 目录产物,运行时用)
    dist\zhz_tool_v<ver>.zip        (绿色版)
    dist\zhz_tool_setup_v<ver>.exe  (安装版)

.PARAMETER Publish
  额外把两个产物上传到 GitHub 上「已存在」的 v<ver> Release(用 gh,--clobber 覆盖同名附件)。
  不会自动创建 Release、不会改发布说明 —— 发布是敏感动作,Release 需你先手动建好。
  Tag 不存在则报错退出,绝不擅自发布。

.PARAMETER SkipBuild
  跳过 PyInstaller(复用已有 dist\zhz_tool\),只重打 zip + 安装包。调试打包脚本时省时间。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build.ps1
  powershell -ExecutionPolicy Bypass -File build.ps1 -Publish
#>
param(
    [switch]$Publish,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ── 1. 读版本号(单一来源:app/version.py)──────────────────────────────────
Step "读取版本号 (app/version.py)"
$verLine = Select-String -Path "app\version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $verLine) { Fail "在 app/version.py 找不到 __version__" }
$ver = $verLine.Matches[0].Groups[1].Value
Write-Host "    版本 = $ver"

$distApp   = "dist\zhz_tool"
$zipPath   = "dist\zhz_tool_v$ver.zip"
$setupName = "zhz_tool_setup_v$ver.exe"
$setupPath = "dist\$setupName"

# ── 2. PyInstaller 打包(onedir)──────────────────────────────────────────────
if ($SkipBuild) {
    Step "跳过 PyInstaller (-SkipBuild),复用 $distApp"
    if (-not (Test-Path $distApp)) { Fail "$distApp 不存在,不能 -SkipBuild。先跑一次完整构建。" }
} else {
    Step "PyInstaller 打包 (OCRTool.spec, onedir)"
    if (Test-Path "build")   { Remove-Item "build"   -Recurse -Force }
    if (Test-Path $distApp)  { Remove-Item $distApp  -Recurse -Force }
    python -m PyInstaller OCRTool.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller 打包失败" }
    if (-not (Test-Path "$distApp\zhz_tool.exe")) { Fail "未生成 $distApp\zhz_tool.exe" }
}

# ── 3. 绿色版 zip(打包文件夹「内容」,使 exe 在 zip 根层)──────────────────────
Step "打包绿色版 zip -> $zipPath"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path "$distApp\*" -DestinationPath $zipPath -CompressionLevel Optimal
if (-not (Test-Path $zipPath)) { Fail "zip 生成失败" }

# ── 4. Inno Setup 安装包(版本号经 /D 传入,与 version.py 一致)─────────────────
Step "编译安装包 -> $setupPath"
$iscc = $null
foreach ($p in @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe")) {
    if ($p -and (Test-Path $p)) { $iscc = $p; break }
}
if (-not $iscc) { Fail "找不到 Inno Setup 6 的 ISCC.exe。请先安装 Inno Setup 6。" }
& $iscc "/DMyAppVersion=$ver" "installer\zhz_tool.iss"
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup 编译失败" }
if (-not (Test-Path $setupPath)) { Fail "未生成 $setupPath" }

# ── 5. 汇总 ────────────────────────────────────────────────────────────────────
Step "构建完成"
Get-Item $distApp\zhz_tool.exe, $zipPath, $setupPath |
    Select-Object @{N='产物';E={$_.Name}}, @{N='MB';E={[math]::Round($_.Length/1MB,1)}} |
    Format-Table -AutoSize

# ── 6. 可选发布(只上传到「已存在」的 Release,绝不擅自创建/改说明)──────────────
if ($Publish) {
    Step "发布到 GitHub Release v$ver"
    & gh release view "v$ver" --json tagName 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Fail "Release v$ver 不存在。请先手动建好该 Release(含发布说明),再用 -Publish 上传附件。"
    }
    & gh release upload "v$ver" $zipPath $setupPath --clobber
    if ($LASTEXITCODE -ne 0) { Fail "上传附件失败" }
    Write-Host "    已上传:$($setupName) + $(Split-Path $zipPath -Leaf)" -ForegroundColor Green
} else {
    Write-Host "`n(未发布。如需上传到已存在的 v$ver Release,加 -Publish 重跑。)" -ForegroundColor DarkGray
}

