#!/usr/bin/env pwsh
# 编译入口（Windows）—— 与 start.sh 功能一一对应，仅转发到 build.ps1
# （Windows 上没有 bash，build.sh 不适用）
# 用法: .\start.ps1 [arupa|nomad|publish|all] [release|debug]
#
# 不用 param() 声明命令参数：与 start.sh 一样按位置取词，这样 -h / --help
# 这类以 '-' 开头的词不会被 PowerShell 当成参数名而绑定失败（同 build.ps1）。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildScript = Join-Path $Root 'build.ps1'
$Jobs = 8

$Command = if ($args.Count -ge 1 -and -not [string]::IsNullOrEmpty($args[0])) { [string] $args[0] } else { 'all' }
$Variant = if ($args.Count -ge 2 -and -not [string]::IsNullOrEmpty($args[1])) { [string] $args[1] } else { 'release' }

# 单步执行：标题 + build.py 参数。失败即中止，退出码透传。
# 原生命令的 stderr 在 build.ps1 内已降级为 Continue（见其顶部注释），
# 这里保持 Stop，只捕获脚本级错误。
function Invoke-Step {
    param(
        [string] $Title,
        [string[]] $BuildArgs
    )

    Write-Host ''
    Write-Host ''
    Write-Host "=======> $Title" -ForegroundColor Cyan
    & $BuildScript @BuildArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-ArupaBuild {
    Invoke-Step 'arupa gen build' @('arupa_desktop', 'gen', 'build', '--jobs', "$Jobs")
    Invoke-Step 'arupa package' @('arupa_desktop', 'package', '--zip')
}

function Invoke-ArupaPublish {
    Invoke-Step 'arupa publish' @('arupa_desktop', 'publish')
}

function Invoke-NomadBuild {
    param([string] $Variant)

    Invoke-Step "nomad build ($Variant)" @('nomad_desktop', 'build', '--variant', $Variant)
    Invoke-Step "nomad package ($Variant)" @('nomad_desktop', 'package', '--variant', $Variant)
}

function Show-Usage {
    Write-Host @'
用法: .\start.ps1 [命令]

  arupa                  编译 + 打包 arupa 内核
  nomad [release|debug]  编译 + 打包 nomad（默认 release）
  publish                推送 arupa 交付包
  all                    编译 arupa + nomad（默认）
  help                   显示本帮助
'@
}

# PowerShell 的 switch 与 -in / -notin 本身大小写不敏感，与 start.sh 的小写归一化一致
switch ($Command) {
    'arupa' {
        Invoke-ArupaBuild
    }
    'nomad' {
        if ($Variant -notin 'release', 'debug') {
            Write-Host "[ERROR] 未知构建配置: $Variant（可选 release / debug）" -ForegroundColor Red
            exit 1
        }
        Invoke-NomadBuild $Variant
    }
    'publish' {
        Invoke-ArupaPublish
    }
    'all' {
        Invoke-ArupaBuild
        Invoke-NomadBuild 'release'
    }
    { $_ -in 'help', '-h', '--help' } {
        Show-Usage
    }
    default {
        Write-Host "[ERROR] 未知命令: $Command" -ForegroundColor Red
        Write-Host ''
        Show-Usage
        exit 1
    }
}
