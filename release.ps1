# release.ps1 —— zhz_tool 发版一条龙
# 用法:在项目根目录运行  ./release.ps1
# 作用:读版本号 → 打包 → 算 SHA256 → 打印 GitHub 发版指引(上传仍由你手动到 GitHub 完成)。
# 不联网、不自动上传:只把本地这几步串好,避免漏步骤。

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 1) 从 app/version.py 读 __version__(单一来源,脚本不另存版本号)
$verLine = Select-String -Path "app/version.py" -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { Write-Error "未能在 app/version.py 读到 __version__"; exit 1 }
$version = $verLine.Matches[0].Groups[1].Value
Write-Host "当前版本:v$version" -ForegroundColor Cyan
Write-Host "提示:若要发新版,先改 app/version.py 的 __version__,再跑本脚本。" -ForegroundColor DarkGray

# 2) 打包
Write-Host "`n[1/3] 正在打包(PyInstaller)..." -ForegroundColor Cyan
python -m PyInstaller OCRTool.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "打包失败"; exit 1 }

$exe = "dist/zhz_tool.exe"
if (-not (Test-Path $exe)) { Write-Error "未找到产物 $exe"; exit 1 }
$sizeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)

# 3) 算 SHA256(供你校验上传完整性,可选)
Write-Host "`n[2/3] 计算 SHA256..." -ForegroundColor Cyan
$sha = (Get-FileHash $exe -Algorithm SHA256).Hash

# 4) 打印发版指引
Write-Host "`n[3/3] 打包完成,接下来手动到 GitHub 发版:" -ForegroundColor Green
Write-Host "  产物    : $exe ($sizeMB MB)"
Write-Host "  SHA256  : $sha"
Write-Host ""
Write-Host "  GitHub 发版步骤:" -ForegroundColor Yellow
Write-Host "   1. 打开 https://github.com/zhoujiuzun/zhz_tool/releases/new"
Write-Host "   2. Tag 填: v$version   (必须和版本号对应,软件靠它比对)"
Write-Host "   3. 标题填: v$version"
Write-Host "   4. 正文写本次更新内容(会显示在用户的更新提示详情里)"
Write-Host "   5. 把 $exe 拖进附件区上传"
Write-Host "   6. Publish release"
Write-Host ""
Write-Host "  发布后,旧版本用户下次启动(或点检查更新)即会收到 v$version 提示。" -ForegroundColor DarkGray
