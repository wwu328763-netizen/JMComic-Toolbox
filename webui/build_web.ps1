# 打包 WebView2 版界面（PyInstaller + pywebview）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File webui\build_web.ps1
$ErrorActionPreference = 'Continue'
$pkg = Split-Path -Parent $PSScriptRoot
$out = Join-Path $pkg 'JMComicWeb.exe'

Write-Host '[1/3] 检查依赖 ...'
python -c "import webview, jmcomic, PIL; print('依赖 OK')"
if ($LASTEXITCODE -ne 0) { Write-Host '缺少依赖：pip install jmcomic pillow pyyaml pywebview'; exit 1 }

Write-Host '[2/3] 打包中（约 1-3 分钟）...'
Get-Process -Name 'JMComicWeb' -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item (Join-Path $env:TEMP 'jmcomic_web_build') -Recurse -Force -ErrorAction SilentlyContinue

python -m PyInstaller --noconfirm --onefile --windowed `
  --name JMComicWeb `
  --icon "$pkg\icon.ico" `
  --add-data "$pkg\webui;webui" `
  --paths "$pkg\src" `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import webview.platforms.winforms `
  --hidden-import clr `
  --hidden-import psutil `
  --collect-all psutil `
  --collect-all webview `
  --collect-all pythonnet `
  --collect-all clr_loader `
  --distpath "$pkg" `
  --workpath (Join-Path $env:TEMP 'jmcomic_web_build') `
  --specpath (Join-Path $env:TEMP 'jmcomic_web_build') `
  "$pkg\webui\host.py" 2>&1 |
  Select-String -Pattern 'ERROR|Build complete|completed successfully|Traceback' |
  Select-Object -Last 6

if (-not (Test-Path $out)) { Write-Host '打包失败：没有生成 exe'; exit 1 }
$mb = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "[3/3] 完成：$out ($mb MB)"
