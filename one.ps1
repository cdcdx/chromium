#!/usr/bin/env pwsh
# 统一入口（Windows）：fetch / build / backup —— 行为与 one.sh 一一对应
#
# 统一命令与选项层：共享选项可以写在命令之前，由本脚本解析、校验并按子命令
# 能力转发；子命令其余参数（位置参数、专属选项）原样透传，退出码原样返回。
# 等价关系：
#   .\one.ps1 fetch  [...]  =  .\fetch.ps1  [...]
#   .\one.ps1 build  [...]  =  .\build.ps1  [...]
#   .\one.ps1 backup [...]  =  .\backup.ps1 [...]
#
# 不用 param() 声明参数：与 start.ps1 一致，-h / --help / --os 这类以 '-'
# 开头的词直接落在 $args 里，不会被当成参数名而绑定失败。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$UsageText = @'
用法: .\one.ps1 [全局选项] <命令> [命令选项/参数...]

命令:
  fetch                  拉取 / 更新 depot_tools、Chromium、DEPS 及业务仓库
  build                  编译 / 打包内核与浏览器
  backup                 将 src 的净修改备份为可恢复的二进制 Git 补丁
  help [命令]            显示帮助；带命令时等价于 [命令] -h

全局选项（可写在命令之前；按子命令能力校验）:
  --os <系统>            目标系统（fetch / build / backup）
  --arch <架构>          目标架构（fetch / build）
  --ver <X.Y.Z.W>        Chromium 版本（fetch / build / backup）
  --jobs, -j <N>         并发任务数（fetch / build）
  --dry-run              仅打印计划，不写文件、不联网（三个命令）

backup 专属（同样可写在 backup 之前）:
  --base <基线>          --ver 的别名，本地 tag / branch / commit
  --num <N>              备份序号，正整数；不指定则自动递增
  --src <目录>           源码 Git 仓库根目录（默认工作区 src）
  --patches, --output <目录>  备份根目录（默认工作区 patches）
  --tracked-only         不包含未跟踪文件（仍包含已暂存的新文件）

子命令其余参数原样透传，详见: .\one.ps1 <命令> -h
'@

function Show-Usage {
    Write-Host $UsageText
}

# 退出码：2 = 参数 / 用法错误（与 Python argparse 一致）；1 = 环境错误（缺 Python）
function Fail([string] $Message) {
    [Console]::Error.WriteLine("[ERROR] $Message")
    [Console]::Error.WriteLine('')
    [Console]::Error.WriteLine($UsageText)
    exit 2
}

# Python 探测沿用 scripts\_find_python.ps1（与 fetch.ps1 / build.ps1 / backup.ps1 相同）
. (Join-Path $Root 'scripts\_find_python.ps1')

# 缺 Python 时提示后缀，与 one.sh 的 hint_of 一致
function Get-Hint([string] $Script) {
    if ($Script -ceq 'fetch') { return ' —— depot_tools 也依赖它，请先安装' }
    if ($Script -ceq 'backup') { return '，请先安装' }
    return ''
}

# 子命令能接受的全局选项（新增共享选项时只改这里）
function Get-Allowed([string] $Command) {
    if ($Command -ceq 'backup') {
        return @('os', 'ver', 'num', 'src', 'patches', 'tracked-only', 'dry-run')
    }
    return @('os', 'arch', 'ver', 'jobs', 'dry-run')
}

# 共享选项按 "--name value" 归一化，最后统一追加到子命令参数尾部
# （argparse 与位置无关，追加不改变语义）
$GlobalNames = New-Object System.Collections.Generic.List[string]
$GlobalTokens = New-Object System.Collections.Generic.List[string]

$ValueOptions = @('--os', '--arch', '--ver', '--base', '--jobs', '-j', '--num', '--src', '--patches', '--output')
$EqualsOptions = @('--os', '--arch', '--ver', '--base', '--jobs', '--num', '--src', '--patches', '--output')
$FlagOptions = @('--dry-run', '--tracked-only')

# 把用户输入的选项归一化为 canonical 名称（用于能力校验与 token 拼接）
function Get-CanonicalName([string] $Option) {
    if ($Option -ceq '-j' -or $Option -ceq '--jobs') { return 'jobs' }
    if ($Option -ceq '--base') { return 'ver' }
    if ($Option -ceq '--output') { return 'patches' }
    return $Option.Substring(2)
}

function Add-GlobalValue([string] $Name, [string] $Value) {
    if ($Name -ceq 'jobs' -or $Name -ceq 'num') {
        if ($Value -notmatch '^[0-9]+$' -or $Value -match '^0+$') {
            Fail "--$Name 必须是正整数（收到: $Value）"
        }
    }
    $GlobalNames.Add($Name)
    $GlobalTokens.Add("--$Name")
    $GlobalTokens.Add($Value)
}

function Add-GlobalFlag([string] $Name) {
    $GlobalNames.Add($Name)
    $GlobalTokens.Add("--$Name")
}

# run_py 等价：探测 Python -> 转发到 scripts\<命令>.py -> 透传退出码
function Invoke-Subcommand {
    param(
        [string] $Script,
        [string[]] $ScriptArgs
    )

    $py = Find-Python3
    if (-not $py) {
        [Console]::Error.WriteLine("[ERROR] 未找到 Python 3.9+$(Get-Hint $Script)")
        [Console]::Error.WriteLine('        （若已安装：多半是 PATH 里只有 WindowsApps 的 python 存根，')
        [Console]::Error.WriteLine('         在 设置 > 应用 > 高级应用设置 > 应用执行别名 里关掉 python.exe/python3.exe）')
        exit 1
    }

    # 同上（build.ps1）: 原生命令的 stderr 不该终止脚本，失败看退出码。
    $ErrorActionPreference = 'Continue'
    $pyArgs = @()
    if ($py.Pre) { $pyArgs += $py.Pre }
    $pyArgs += (Join-Path $Root "scripts\$Script.py")
    $pyArgs += $ScriptArgs
    $pyArgs += @($GlobalTokens)
    & $py.Exe @pyArgs
    exit $LASTEXITCODE
}

# 参数归一化为字符串列表：$args 里的裸数字可能是数值类型，统一按文本处理
$argv = New-Object System.Collections.Generic.List[string]
foreach ($a in $args) { $argv.Add([string] $a) }

$index = 0
$cmd = ''
while ($index -lt $argv.Count) {
    $token = $argv[$index]

    if ($token -ceq '--') {
        # 与 bash 版一致：其后的第一个词是命令
        if ($index + 1 -ge $argv.Count) { Fail '缺少命令' }
        $cmd = $argv[$index + 1]
        $index += 2
        break
    }

    if ($token -ceq '-h' -or $token -ceq '--help') {
        Show-Usage
        exit 0
    }

    if ($ValueOptions -ccontains $token) {
        $valueIndex = $index + 1
        if ($valueIndex -ge $argv.Count -or $argv[$valueIndex].StartsWith('-')) {
            Fail "$token 需要一个值"
        }
        Add-GlobalValue -Name (Get-CanonicalName $token) -Value $argv[$valueIndex]
        $index += 2
        continue
    }

    $equalsOption = $null
    foreach ($candidate in $EqualsOptions) {
        if ($token.StartsWith("$candidate=")) {
            $equalsOption = $candidate
            break
        }
    }
    if ($null -ne $equalsOption) {
        $value = $token.Substring($equalsOption.Length + 1)
        if ([string]::IsNullOrEmpty($value)) { Fail "$equalsOption 需要一个值" }
        Add-GlobalValue -Name (Get-CanonicalName $equalsOption) -Value $value
        $index += 1
        continue
    }

    if ($FlagOptions -ccontains $token) {
        Add-GlobalFlag (Get-CanonicalName $token)
        $index += 1
        continue
    }

    # 其余以 '-' 开头的词是未知全局选项；单个 '-' 仍按命令处理
    if ($token.StartsWith('-') -and $token.Length -gt 1) {
        Fail "未知全局选项: $token"
    }

    $cmd = $token
    $index += 1
    break
}

if ([string]::IsNullOrEmpty($cmd)) {
    [Console]::Error.WriteLine($UsageText)
    exit 2
}
$cmd = $cmd.ToLowerInvariant()

# 命令名大小写不敏感，与 one.sh 的 tr 归一化一致
if ($cmd -ceq 'help') {
    $target = ''
    if ($index -lt $argv.Count) { $target = $argv[$index] }
    if ([string]::IsNullOrEmpty($target)) {
        Show-Usage
        exit 0
    }
    $target = $target.ToLowerInvariant()
    if ($target -ceq 'fetch' -or $target -ceq 'build' -or $target -ceq 'backup') {
        Invoke-Subcommand -Script $target -ScriptArgs @('-h')
    }
    Fail "未知命令: $target"
}

if (-not ($cmd -ceq 'fetch' -or $cmd -ceq 'build' -or $cmd -ceq 'backup')) {
    Fail "未知命令: $cmd"
}

$allowed = Get-Allowed $cmd
foreach ($name in $GlobalNames) {
    if ($allowed -cnotcontains $name) {
        Fail "--$name 不适用于 $cmd"
    }
}

$rest = @()
for ($j = $index; $j -lt $argv.Count; $j++) { $rest += $argv[$j] }

Invoke-Subcommand -Script $cmd -ScriptArgs $rest
