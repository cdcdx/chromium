# 编译 / 测试 / 打包入口（Windows）—— 转发到 scripts/build.py
# 用法: .\build.ps1 kernel gen build --arch x64 --link static
# 坑: PS 5.1 会把原生命令写进 stderr 的每一行（npm warn / dotnet warning / git hint ...）
# 包装成 ErrorRecord；$ErrorActionPreference='Stop' 下它会**直接终止脚本** ——
# 几小时的构建被一行 warning 掐断，且只看到"命令失败"看不到真正原因。
# 真实失败靠退出码判定（build.py 自己会 err 到 stderr 并 exit 1），故这里降级为 Continue。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $Root 'scripts\_find_python.ps1')
$py = Find-Python3
if (-not $py) {
    Write-Host "[ERROR] 未找到可用的 Python 3" -ForegroundColor Red
    Write-Host "        （若已安装：多半是 PATH 里只有 WindowsApps 的 python 存根，" -ForegroundColor DarkGray
    Write-Host "         在 设置 > 应用 > 高级应用设置 > 应用执行别名 里关掉 python.exe/python3.exe）" -ForegroundColor DarkGray
    exit 1
}

$ErrorActionPreference = 'Continue'
$pyArgs = @()
if ($py.Pre) { $pyArgs += $py.Pre }
$pyArgs += (Join-Path $Root 'scripts\build.py')
if ($args) { $pyArgs += $args }
& $py.Exe @pyArgs
exit $LASTEXITCODE
