#!/usr/bin/env pwsh
# 源码 / 依赖拉取更新入口（Windows）—— 转发到 scripts/fetch.py
# 用法见: .\fetch.ps1 -h
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = $null
foreach ($c in @('python3', 'python', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
}
if (-not $Py) {
    Write-Host "[ERROR] 未找到 python —— depot_tools 也依赖它，请先安装" -ForegroundColor Red
    exit 1
}

# 同上（build.ps1）: 原生命令的 stderr 不该终止脚本，失败看退出码。
$ErrorActionPreference = 'Continue'
& $Py "$Root\scripts\fetch.py" @args
exit $LASTEXITCODE
