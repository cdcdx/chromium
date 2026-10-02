#!/usr/bin/env pwsh
# 源码 / 依赖拉取更新入口（Windows）—— 转发到 scripts/fetch.py
# 用法见: .\fetch.ps1 -h
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'scripts\_find_python.ps1')
$Py = Find-Python3
if (-not $Py) {
    Write-Host "[ERROR] 未找到可用的 Python 3 —— depot_tools 也依赖它，请先安装" -ForegroundColor Red
    Write-Host "        （若已安装：多半是 PATH 里只有 WindowsApps 的 python 存根，" -ForegroundColor DarkGray
    Write-Host "         在 设置 > 应用 > 高级应用设置 > 应用执行别名 里关掉 python.exe/python3.exe）" -ForegroundColor DarkGray
    exit 1
}

# 同上（build.ps1）: 原生命令的 stderr 不该终止脚本，失败看退出码。
$ErrorActionPreference = 'Continue'
$PyArgs = @()
if ($Py.Pre) { $PyArgs += $Py.Pre }
$PyArgs += (Join-Path $Root 'scripts\fetch.py')
if ($args) { $PyArgs += $args }
& $Py.Exe @PyArgs
exit $LASTEXITCODE
