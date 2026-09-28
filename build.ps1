# 编译 / 测试 / 打包入口（Windows）—— 转发到 scripts/build.py
# 用法: .\build.ps1 kernel gen build --arch x64 --link static
# 坑: PS 5.1 会把原生命令写进 stderr 的每一行（npm warn / dotnet warning / git hint ...）
# 包装成 ErrorRecord；$ErrorActionPreference='Stop' 下它会**直接终止脚本** ——
# 几小时的构建被一行 warning 掐断，且只看到"命令失败"看不到真正原因。
# 真实失败靠退出码判定（build.py 自己会 err 到 stderr 并 exit 1），故这里降级为 Continue。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$py = $null
foreach ($c in @('python3', 'python', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { Write-Host "[ERROR] 未找到 python3" -ForegroundColor Red; exit 1 }

$ErrorActionPreference = 'Continue'
& $py "$Root\scripts\build.py" @args
exit $LASTEXITCODE
