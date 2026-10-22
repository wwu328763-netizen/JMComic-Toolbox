[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'

# 命令行版：位于 项目包\cli\，配置和保存位置都在上一级目录
$root = Split-Path -Parent $PSScriptRoot
$optionFile = Join-Path $root 'option.yml'
if (Test-Path $optionFile) { $env:JM_OPTION_PATH = $optionFile }

# 从 option.yml 里读保存位置；读不到就用 项目包\downloads
$downloadDir = Join-Path $root 'downloads'
try {
    $text = Get-Content $optionFile -Raw -Encoding UTF8
    $m = [regex]::Match($text, "(?m)^\s*base_dir\s*:\s*'?([^'\r\n]+)'?")
    if ($m.Success) { $downloadDir = $m.Groups[1].Value.Trim() }
} catch { }

$downloadExe = (Get-Command jmcomic -CommandType Application -ErrorAction SilentlyContinue).Source
$infoExe = (Get-Command jmv -CommandType Application -ErrorAction SilentlyContinue).Source

if (-not $downloadExe) {
    Write-Host ''
    Write-Host '没找到 jmcomic 命令。命令行版需要本机装有 Python 和 jmcomic：'
    Write-Host '    pip install jmcomic'
    Write-Host ''
    Write-Host '如果只是想下载/阅读，直接用上一级的 JMComic工具箱.exe，不需要 Python。'
    Read-Host '按回车退出'
    return
}

function Show-Menu {
    Write-Host ''
    Write-Host '=================================================='
    Write-Host '  JMComic 工具箱 · 命令行版'
    Write-Host '--------------------------------------------------'
    Write-Host "  保存位置 : $downloadDir"
    Write-Host '=================================================='
    Write-Host ''
    Write-Host '   [1] 下载本子'
    Write-Host '   [2] 查询车号详情（不下载）'
    Write-Host '   [3] 打开下载文件夹'
    Write-Host '   [0] 退出'
    Write-Host ''
}

while ($true) {
    Show-Menu
    $choice = Read-Host '请选择'

    if ([string]::IsNullOrWhiteSpace($choice)) { return }

    switch ($choice.Trim()) {

        '1' {
            Write-Host ''
            $text = Read-Host '车号（多个用空格隔开，章节前面加 p，例如 123 p456）'
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $list = @($text -split '\s+' | Where-Object { $_ -ne '' })
                Write-Host ''
                Write-Host '开始下载 ...'
                Write-Host ''
                & $downloadExe @list
                Write-Host ''
                Write-Host "--- 处理结束，文件在 $downloadDir ---"
                Write-Host "（如果没下到东西，报错详情见 $root\jmcomic-download.log）"
            }
            Read-Host '按回车返回菜单'
        }

        '2' {
            Write-Host ''
            $text = Read-Host '车号（可直接粘贴一段含车号的文字，自动提取数字）'
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                Write-Host ''
                if ($infoExe) { & $infoExe $text }
                else { Write-Host '没找到 jmv 命令，请先 pip install jmcomic' }
            }
            Read-Host '按回车返回菜单'
        }

        '3' {
            Invoke-Item $downloadDir
        }

        '0' { return }
        'q' { return }

        default {
            Write-Host ''
            Write-Host '请输入 1、2、3 或 0'
            Start-Sleep -Milliseconds 800
        }
    }
}
