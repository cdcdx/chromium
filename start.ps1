#!/usr/bin/env pwsh
# 编译入口（Windows）—— 与 start.sh 功能一一对应，转发到 one.ps1（build 子命令）
# 用法: .\start.ps1 [arupa|nomad|publish|all] [release|debug]
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$One = Join-Path $Root 'one.ps1'
$Jobs = 8

# 全部参数先转字符串：$args 里的裸数字可能是数值类型；命令与配置按小写解析，
# 与 start.sh 的 lower() 归一化一致
$AllArgs = @($args | ForEach-Object { [string] $_ })
$Command = if ($AllArgs.Count -ge 1 -and $AllArgs[0] -ne '') { $AllArgs[0].ToLowerInvariant() } else { 'all' }
$Variant = if ($AllArgs.Count -ge 2 -and $AllArgs[1] -ne '') { $AllArgs[1].ToLowerInvariant() } else { 'release' }

# 单步执行：标题 + one.ps1 参数。失败即中止，退出码透传（对应 bash 的 set -e）。
# 原生命令的 stderr 在 one.ps1 内已降级为 Continue（见其顶部注释），
# 这里保持 Stop，只捕获脚本级错误。
function Invoke-Step {
    param(
        [string] $Title,
        [string[]] $OneArgs
    )

    Write-Host ''
    Write-Host ''
    Write-Host "=======> $Title" -ForegroundColor Cyan
    & $One @OneArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-ArupaBuild {
    Invoke-Step 'arupa gen build' @('build', 'arupa_desktop', 'gen', 'build', '--jobs', "$Jobs")
    Invoke-Step 'arupa package' @('build', 'arupa_desktop', 'package', '--zip')
}

function Invoke-ArupaPublish {
    Invoke-Step 'arupa publish' @('build', 'arupa_desktop', 'publish')
}

function Invoke-NomadBuild {
    param([string] $Variant)

    Invoke-Step "nomad build ($Variant)" @('build', 'nomad_desktop', 'build', '--variant', $Variant)
    Invoke-Step "nomad package ($Variant)" @('build', 'nomad_desktop', 'package', '--variant', $Variant)
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

# switch 与 -in / -notin 本身大小写不敏感，与 start.sh 的小写归一化行为一致
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
