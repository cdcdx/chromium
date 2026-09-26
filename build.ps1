# 编译 / 测试 / 打包入口（Windows）—— 转发到 scripts/build.py
# 用法: .\build.ps1 kernel gen build --arch x64 --link static
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$py = $null
foreach ($c in @('python3', 'python', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { Write-Host "[ERROR] 未找到 python3" -ForegroundColor Red; exit 1 }

& $py "$Root\scripts\build.py" @args
exit $LASTEXITCODE
