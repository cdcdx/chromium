# scripts/_find_python.ps1 —— 定位可用的 Python 3（build.ps1 / fetch.ps1 共用）
#
# 坑 1: Windows 的"应用执行别名"会在 %LOCALAPPDATA%\Microsoft\WindowsApps 下
#       放 python.exe / python3.exe 存根（0 字节）。Get-Command 能命中它，
#       但执行只打印 "Python was not found; run without arguments to install
#       from the Microsoft Store"，退出码 9009 —— 所以不能只判断"命令存在"。
# 坑 2: 存根在 PATH 里往往排在真实安装目录前面，Get-Command 取第一个命中就中招。
# 对策: 收集候选 -> 过滤 WindowsApps 存根 -> 逐个真跑一次校验版本。
#
# 返回: @{ Exe = <路径>; Pre = @(前置参数) } 或 $null
function Find-Python3 {
    $cands = New-Object System.Collections.Generic.List[object]

    # py 启动器（最可靠，且能精确选 3.x）
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) { $cands.Add([pscustomobject]@{ Exe = $py.Source; Pre = @('-3') }) }

    # PATH 上的 python3 / python（含全部同名命中，便于跳过存根后取真实那个）
    foreach ($name in @('python3', 'python')) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source -match '\\WindowsApps\\') { continue }
            $cands.Add([pscustomobject]@{ Exe = $cmd.Source; Pre = @() })
        }
    }

    # PATH 之外的常见安装位置（用户级 / 系统级安装默认路径）
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Programs\Python') }
    $roots += 'C:\Program Files'
    $roots += 'C:\'
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $subs = @()
        if ($root -eq 'C:\') {
            $subs += @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Python*' -ErrorAction SilentlyContinue)
        } else {
            $subs += @(Get-ChildItem -LiteralPath $root -Directory -Filter 'Python*' -ErrorAction SilentlyContinue)
        }
        foreach ($sub in $subs) {
            $exe = Join-Path $sub.FullName 'python.exe'
            if (Test-Path -LiteralPath $exe) {
                $cands.Add([pscustomobject]@{ Exe = $exe; Pre = @() })
            }
        }
    }

    foreach ($c in $cands) {
        $pre = @()
        if ($c.Pre) { $pre += $c.Pre }
        $pre += @('-c', 'import sys;print(3 if sys.version_info >= (3, 9) else 0)')
        $out = $null
        try {
            $out = & $c.Exe $pre 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -eq 0 -and ($out | Select-Object -Last 1) -eq '3') {
            return [pscustomobject]@{ Exe = $c.Exe; Pre = $c.Pre }
        }
    }
    return $null
}
