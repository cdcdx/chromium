# ==== 用法开始
# 把 src\out\arupa-win-{arch}-{ver}-static 里的桌面端内核件整理成交付包：
#     dist\arupa-win-{arch}-{ver}-static-{n}\
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\package-arupa-desktop.ps1 [选项]
#
# 输出目录（目录自带 arch，所以 kernel\ 是平铺的，不再按架构分层）：
#   dist\arupa-win-x64-154.0.8037.21-static-1\
#     kernel\{arupa_kernel.dll, arupa_render.exe, arupa_plugin_host.exe, *.pak, icudtl.dat,
#            snapshot_blob.bin, v8_context_snapshot.bin, libEGL.dll, libGLESv2.dll,
#            vk_swiftshader.dll, vulkan-1.dll, d3dcompiler_47.dll, dxcompiler.dll,
#            msvcp140*.dll, vcruntime140*.dll, …, angledata\, hyphen-data\,
#            resources\,
#            .arupa-version, .arupa-delivery-id}
#     include\{arupa_kernel_capi.h, arupa_kernel_capi_nomad.h}
#     docs\ dotnet\ …        ← <repo>\package\package_desktop 下的一级文件/文件夹整份搬过来（有则带）
#     SHA256SUMS.txt / MANIFEST.md
#
# 参考（仓库里已有的旧口径 Windows 包）：dist\arupa-win-154.0.8037.21+17
#   —— 那份不带 arch，另外还带了 README.md / dotnet\；想要同样的目录名用 --dist-name。
#
# 选项（两种写法都收：-Arch X 与 --arch X 等价，短名同 sh 版）：
#       --os OS          win（默认 win；mac / linux 请用对应的 .sh 脚本）
#       --arch ARCH      x86 | x64 | arm64 | all（默认宿主架构；all = 两架构各出一份，共用同一 n）
#       --ver VER        版本号，默认读 src\chrome\VERSION
#   -o, --out-dir NAME  覆盖 out 目录名（默认 arupa-win-{arch}-{ver}-static，只支持单架构）
#       --out PATH      直接指定 out 目录（优先级高于 -o，只支持单架构）
#   -n, --num N         交付序号（默认：dist\arupa-win-{arch}-{ver}-static-* 最大 n + 1）
#       --dist-dir DIR  交付根（默认 <repo>\dist）
#       --dist-name NAME 直接指定交付包目录名（单架构；不用 -n 递增时用得上）
#       --pak FILE      主 pak 来源，默认 out\content_shell.pak，
#                       没有时取 out 根体积最大的 *.pak
#       --include-dir DIR      默认 <repo>\arupa_desktop\public（里面的 *.h 拷进 include\）
#       --package-dir DIR       把该目录下的一级文件/文件夹整份拷进交付根
#                               （默认 <repo>\package\package_desktop，常见: docs\ dotnet\）
#       --no-package    不拷 package 目录
#       --docs DIR      额外把该目录整份拷成 docs\
#       --probe DIR     额外拷成 probe-plugin\
#       --with-pdb      额外带上 *.pdb（arupa_kernel.dll.pdb 等调试符号）
#   -z, --zip           额外打 dist\arupa-win-{arch}-{ver}-static-{n}.zip
#   -f, --force         目标 dist 目录已存在时先删再打
#   -h, --help
#
# 复制映射（OUT = src\out\arupa-win-{arch}-{ver}-static）:
#   OUT\arupa_kernel.dll        → kernel\（内核本体，必需）
#   OUT\arupa_render.exe              → kernel\（渲染进程薄壳，必需）
#   OUT\arupa_plugin_host.exe   → kernel\（插件宿主，缺失终止打包）
#   OUT\<主 pak> + 其余 *.pak   → kernel\（原名平铺，必需至少一个）
#   OUT\icudtl.dat              → kernel\（必需）
#   OUT\snapshot_blob.bin / v8_context_snapshot*.bin → kernel\同名（至少一个，必需）
#   OUT\libEGL.dll / libGLESv2.dll / vk_swiftshader.dll / vulkan-1.dll /
#       dxcompiler.dll / dxil.dll / d3dcompiler_47.dll /
#       msvcp140.dll / msvcp140_atomic_wait.dll / vcruntime140.dll /
#       vcruntime140_1.dll / vccorlib140.dll / concrt140.dll /
#       dbgcore.dll / dbghelp.dll            → kernel\（有则带）
#   OUT\angledata\ hyphen-data\ resources\ locales\ → kernel\同名目录（有则带）
#   <repo>\arupa_desktop\public\*.h          → include\
#   <repo>\package\package_desktop\{docs,dotnet,…}            → 交付根同名（有则带，跟 scripts\builder\kernel.py
#                                               的 copy_assets 同一口径）
#   版本标记                                 → kernel\.arupa-version = ver
#                                              kernel\.arupa-delivery-id = ver+n
#
# 校验:
#   * 必需件缺失 -> 直接失败，并且不留半成品 dist 目录（免得把下次的序号顶上去）
#   * 可选件缺失 -> 打印 [!] 告警汇总，不阻断
#   * package 附加件与交付根已有同名项（kernel\ include\ docs\ …）-> 直接失败，不静默覆盖
#   * 收尾读内核库的 PE 头，核对机器类型与本次打包的 arch 是否一致
#     （x64 = IMAGE_FILE_MACHINE_AMD64；arm64 = IMAGE_FILE_MACHINE_ARM64）
# ==== 用法结束

#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$SrcDir = Join-Path $Root 'src'

# ------------------------------------------------------------------- 输出工具
function Write-Info { param([string]$Message) Write-Host "[i] $Message" }
function Write-Step { param([string]$Message) Write-Host "[+] $Message" }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }
function Fail       { param([string]$Message) throw $Message }

function Show-Usage {
    $inside = $false
    foreach ($line in (Get-Content -LiteralPath $PSCommandPath -Encoding UTF8)) {
        if ($line -match '^# ==== 用法开始') { $inside = $true; continue }
        if ($inside -and $line -match '^# ==== 用法结束') { break }
        if ($inside) { Write-Host ($line -replace '^\s*#\s?', '') }
    }
}

function Format-Size {
    param([long]$Bytes)
    $units = @('B', 'K', 'M', 'G')
    $size = [double]$Bytes
    $idx = 0
    while ($size -ge 1024 -and $idx -lt 3) { $size = $size / 1024; $idx++ }
    if ($idx -eq 0) { return "$Bytes B" }
    return ('{0:N1}{1}' -f $size, $units[$idx])
}

# ------------------------------------------------------------------- 参数解析
$script:RawArgs = @()
if ($null -ne $args) { $script:RawArgs = @($args) }
$script:Idx = 0

function Read-Value {
    param([string]$Name)
    $script:Idx++
    if ($script:Idx -ge $script:RawArgs.Count) { throw "--$Name 需要参数" }
    return [string]$script:RawArgs[$script:Idx]
}

# 短名 -> 长名（与 sh 版的 -o/-n/-z/-f 对齐）
$AliasMap = @{
    'o' = 'out-dir'
    'n' = 'num'
    'z' = 'zip'
    'f' = 'force'
}

$OS        = 'win'
$Arch      = ''
$Ver       = ''
$OutName   = ''
$OutOverride = ''
$Num       = ''
$DistDir   = Join-Path $Root 'dist'
$DistName  = ''
$PakSrc    = ''
$IncDir    = ''
$PackageDir = ''
$NoPackage = $false
$DocsDir   = ''
$ProbeDir  = ''
$DoZip     = $false
$Force     = $false
$WithPdb   = $false

try {
    while ($script:Idx -lt $script:RawArgs.Count) {
        $tok = [string]$script:RawArgs[$script:Idx]
        if ($tok -match '^[-/]+(.+)$') {
            $key = $Matches[1].ToLower()
            if ($AliasMap.ContainsKey($key)) { $key = $AliasMap[$key] }
        } else {
            throw "未知参数: $tok"
        }

        switch ($key) {
            'os' { $OS = (Read-Value $key); break }
            'arch' { $Arch = (Read-Value $key); break }
            'ver' { $Ver = (Read-Value $key); break }
            'out-dir' { $OutName = (Read-Value $key); break }
            'out' { $OutOverride = (Read-Value $key); break }
            'num' { $Num = (Read-Value $key); break }
            'dist-dir' { $DistDir = (Read-Value $key); break }
            'dist-name' { $DistName = (Read-Value $key); break }
            'pak' { $PakSrc = (Read-Value $key); break }
            'include-dir' { $IncDir = (Read-Value $key); break }
            'package-dir' { $PackageDir = (Read-Value $key); break }
            'no-package' { $NoPackage = $true; break }
            'docs' { $DocsDir = (Read-Value $key); break }
            'probe' { $ProbeDir = (Read-Value $key); break }
            'with-pdb' { $WithPdb = $true; break }
            'zip' { $DoZip = $true; break }
            'force' { $Force = $true; break }
            'help' { Show-Usage; exit 0 }
            default { throw "未知参数: $tok" }
        }
        $script:Idx++
    }
} catch {
    Write-Err $_.Exception.Message
    Show-Usage
    exit 2
}

if (-not (Test-Path -LiteralPath $SrcDir -PathType Container)) { Fail "找不到源码树: $SrcDir" }

# ---------------------------------------------------------------- 平台 / 架构
function Format-Cpu {
    param([string]$Cpu)
    switch ($Cpu.ToLower()) {
        'arm64' { return 'arm64' }
        'aarch64' { return 'arm64' }
        'x64' { return 'x64' }
        'x86_64' { return 'x64' }
        'amd64' { return 'x64' }
        default { return $Cpu.ToLower() }
    }
}

function Test-WindowsHost {
    $isWin = $false
    if (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) { $isWin = [bool]$IsWindows }
    if (-not $isWin -and $env:OS -eq 'Windows_NT') { $isWin = $true }
    if (-not $isWin) { $isWin = ([System.Environment]::OSVersion.Platform -eq 'Win32NT') }
    return $isWin
}
if (-not (Test-WindowsHost)) {
    Fail "本脚本只支持 Windows 宿主（macOS / Linux 请用 scripts\package-arupa-desktop.sh）"
}

if ($OS -ne 'win') { Fail "--os 只支持 win: $OS（mac / linux 请用 scripts\package-arupa-desktop.sh）" }

function Get-HostCpu {
    $cpu = [string]$env:PROCESSOR_ARCHITECTURE
    if ($cpu -eq 'x86' -and $env:PROCESSOR_ARCHITEW6432) { $cpu = [string]$env:PROCESSOR_ARCHITEW6432 }
    return (Format-Cpu $cpu)
}

if (-not $Arch) { $Arch = Get-HostCpu }
switch ((Format-Cpu $Arch)) {
    'arm64' { $Archs = @('arm64') }
    'x64' { $Archs = @('x64') }
    'x86' { $Archs = @('x86') }
    'all' { $Archs = @('x86', 'x64', 'arm64') }
    default { Fail "--arch 只支持 x86 | x64 | arm64 | all: $Arch" }
}

if (($OutOverride -or $OutName) -and $Archs.Count -gt 1) {
    Fail "-o/--out 只能用于单架构（--arch arm64|x64）"
}

function Get-LibName { return 'arupa_kernel.dll' }
function Get-HelperName { return 'arupa_render.exe' }

# ------------------------------------------------------------------- 版本号
if (-not $Ver) {
    $versionFile = Join-Path $SrcDir 'chrome\VERSION'
    if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        $vmap = @{}
        foreach ($line in (Get-Content -LiteralPath $versionFile)) {
            $trimmed = $line.Trim()
            if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
            if ($trimmed -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$') { $vmap[$Matches[1]] = $Matches[2].Trim() }
        }
        if ($vmap.Count -gt 0) {
            function Get-VersionPart { param([hashtable]$Map, [string]$Key) if ($Map.ContainsKey($Key)) { return $Map[$Key] } else { return '0' } }
            $Ver = '{0}.{1}.{2}.{3}' -f (Get-VersionPart $vmap 'MAJOR'), (Get-VersionPart $vmap 'MINOR'), (Get-VersionPart $vmap 'BUILD'), (Get-VersionPart $vmap 'PATCH')
        }
    }
    if (-not $Ver) { Fail '读不到版本号，请用 --ver 指定' }
}

# ---------------------------------------------------------------- 交付序号 n
if (-not $Num) {
    $max = 0
    foreach ($cpu in $Archs) {
        $prefix = "arupa-win-$cpu-$Ver-static-"
        if (-not (Test-Path -LiteralPath $DistDir -PathType Container)) { continue }
        foreach ($dir in (Get-ChildItem -LiteralPath $DistDir -Directory -Filter "$prefix*" -ErrorAction SilentlyContinue)) {
            $tail = $dir.Name.Substring($prefix.Length)
            if ($tail -notmatch '^\d+$') { continue }
            $value = [int]$tail
            if ($value -gt $max) { $max = $value }
        }
    }
    $Num = [string]($max + 1)
}
if ($Num -notmatch '^\d+$') { Fail "--num 必须是数字: $Num" }

$DeliveryId = "$Ver+$Num"

# ---------------------------------------------------------------- 公共附件
if (-not $IncDir) { $IncDir = Join-Path $Root 'arupa_desktop\public' }

# package 附加件（docs\ dotnet\ …）：一级文件/文件夹整份搬进交付根，
# 口径同 scripts\builder\kernel.py 的 copy_assets
if (-not $PackageDir) { $PackageDir = Join-Path $Root 'package\package_desktop' }
if ($NoPackage) {
    $PackageDir = ''
} elseif (-not (Test-Path -LiteralPath $PackageDir -PathType Container)) {
    Fail "找不到交付附件目录: $PackageDir"
}

# 主 pak：out 根 content_shell.pak，没有则体积最大的 *.pak
function Resolve-Pak {
    param([string]$OutPath)
    if ($script:PakSrc) {
        if (-not (Test-Path -LiteralPath $script:PakSrc -PathType Leaf)) { throw "--pak 找不到: $($script:PakSrc)" }
        return
    }
    $candidate = Join-Path $OutPath 'content_shell.pak'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $script:PakSrc = $candidate
        return
    }
    $largest = Get-ChildItem -LiteralPath $OutPath -File -Filter '*.pak' -ErrorAction SilentlyContinue |
        Sort-Object -Property Length -Descending | Select-Object -First 1
    if ($largest) {
        Write-Warn "out 里没有 content_shell.pak —— 取体积最大的 $($largest.Name) 当主 pak"
        $script:PakSrc = $largest.FullName
        return
    }
    throw "out 里没有 *.pak（$OutPath）—— 资源 pak 缺失，内核起来也会缺资源。`n    用 --pak <file> 指定，或先跑 scripts\build-arupa-desktop.ps1 把 pak 目标编出来。"
}

# ---------------------------------------------------------------- 收件工具
function Add-RequiredFile {
    param([string]$Src, [string]$DestDir, [string]$Hint = '')
    if (-not (Test-Path -LiteralPath $Src -PathType Leaf)) {
        $msg = "缺必需件: $Src"
        if ($Hint) { $msg = "$msg（$Hint）" }
        throw $msg
    }
    Copy-Item -LiteralPath $Src -Destination $DestDir -Force
    Write-Step ("  {0}  {1}" -f (Split-Path -Leaf $Src), (Format-Size (Get-Item -LiteralPath $Src).Length))
}

function Add-OptionalFile {
    param([string]$Src, [string]$DestDir)
    if (-not (Test-Path -LiteralPath $Src -PathType Leaf)) {
        $script:MissingOptional.Add((Split-Path -Leaf $Src))
        return
    }
    Copy-Item -LiteralPath $Src -Destination $DestDir -Force
    Write-Step ("  {0}  {1}" -f (Split-Path -Leaf $Src), (Format-Size (Get-Item -LiteralPath $Src).Length))
}

function Add-OptionalDir {
    param([string]$Src, [string]$DestDir)
    if (-not (Test-Path -LiteralPath $Src -PathType Container)) {
        $script:MissingOptional.Add((Split-Path -Leaf $Src) + '/')
        return
    }
    $destSub = Join-Path $DestDir (Split-Path -Leaf $Src)
    New-Item -ItemType Directory -Force -Path $destSub | Out-Null
    Copy-Item -Path "$Src\*" -Destination $destSub -Recurse -Force
    $bytes = (Get-ChildItem -LiteralPath $Src -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if (-not $bytes) { $bytes = 0 }
    Write-Step ("  {0}/  {1}" -f (Split-Path -Leaf $Src), (Format-Size ([long]$bytes)))
}

# 把 <repo>\package\package_desktop 下的一级文件/文件夹整份搬进交付根（docs\ dotnet\ …）
# 口径同 scripts\builder\kernel.py 的 copy_assets：平台无关件不塞进 kernel\，
# 直接平铺在交付根，宿主按 dist\docs、dist\dotnet 取用。
function Copy-PackageDir {
    param([string]$DistPath)
    if (-not $PackageDir) { return }        # --no-package，或 package 目录不存在
    if (-not (Test-Path -LiteralPath $PackageDir -PathType Container)) { return }

    $entries = @(Get-ChildItem -LiteralPath $PackageDir -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.DS_Store' })
    if ($entries.Count -eq 0) {
        Write-Warn "package 目录是空的，交付包里不会有 docs\ dotnet\ 等附加件: $PackageDir"
        return
    }

    $n = 0
    foreach ($e in $entries) {
        $dest = Join-Path $DistPath $e.Name
        # 撞名就停：静默覆盖（尤其 --docs / --probe 已经建了同名目录）会混出半新半旧的目录
        if (Test-Path -LiteralPath $dest) {
            throw "package 附加件与交付目录里已有的 $($e.Name) 撞名（$dest）—— 用 --package-dir 换个来源，或去掉 --docs / --probe"
        }
        if ($e.PSIsContainer) {
            Copy-Item -LiteralPath $e.FullName -Destination $dest -Recurse -Force
            $bytes = (Get-ChildItem -LiteralPath $e.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
            if (-not $bytes) { $bytes = 0 }
            Write-Step ("  {0}/  ← package\{0}  {1}" -f $e.Name, (Format-Size ([long]$bytes)))
        } else {
            Copy-Item -LiteralPath $e.FullName -Destination $dest -Force
            Write-Step ("  {0}  ← package\{0}  {1}" -f $e.Name, (Format-Size ([long]$e.Length)))
        }
        $n++
    }
    Write-Step "  package 附加 $n 项 ← $PackageDir"
}

# ---------------------------------------------------------------- 单个包的收件
function Invoke-PackageArch {
    param([string]$Cpu, [string]$OutPath, [string]$DistPath)

    $dest = Join-Path $DistPath 'kernel'
    $script:MissingOptional = New-Object System.Collections.Generic.List[string]
    $savedPak = $script:PakSrc

    Resolve-Pak -OutPath $OutPath

    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Write-Host ''
    Write-Step "── win/$Cpu ← $(Split-Path -Leaf $OutPath) ──"

    # 内核本体
    Add-RequiredFile -Src (Join-Path $OutPath (Get-LibName)) -DestDir $dest -Hint 'ninja 目标 chrome/browser/arupa_desktop:arupa_kernel'
    # 渲染进程薄壳：win 必需（BUILD.gn 的 win 分支里有 executable("render")）
    Add-RequiredFile -Src (Join-Path $OutPath (Get-HelperName)) -DestDir $dest -Hint 'ninja 目标 chrome/browser/arupa_desktop:render，少了它没有渲染进程'
    # 插件宿主：arupa_kernel 的 data_deps，三平台交付均必需
    Add-RequiredFile -Src (Join-Path $OutPath 'arupa_plugin_host.exe') -DestDir $dest

    # 主 pak
    Add-RequiredFile -Src $script:PakSrc -DestDir $dest
    Add-RequiredFile -Src (Join-Path $OutPath 'devtools_resources.pak') -DestDir $dest -Hint 'ninja 目标 chrome/browser/arupa_desktop:arupa_devtools_resources'

    # 其余 *.pak（shell_resources / ui_resources / extensions_* 等）
    $pakCount = 0
    foreach ($f in (Get-ChildItem -LiteralPath $OutPath -File -Filter '*.pak' -ErrorAction SilentlyContinue)) {
        $pakCount++
        if (Test-Path -LiteralPath (Join-Path $dest $f.Name) -PathType Leaf) { continue }
        Add-OptionalFile -Src $f.FullName -DestDir $dest
    }
    if ($pakCount -eq 0) { throw "out 里没有任何 *.pak: $OutPath" }

    # icudtl / 快照
    Add-RequiredFile -Src (Join-Path $OutPath 'icudtl.dat') -DestDir $dest
    $snapCount = 0
    foreach ($pattern in @('snapshot_blob.bin', 'v8_context_snapshot*.bin')) {
        foreach ($f in (Get-ChildItem -LiteralPath $OutPath -File -Filter $pattern -ErrorAction SilentlyContinue)) {
            Add-OptionalFile -Src $f.FullName -DestDir $dest
            $snapCount++
        }
    }
    if ($snapCount -eq 0) { throw "out 里没有 snapshot_blob.bin / v8_context_snapshot*.bin —— 内核起不来: $OutPath" }

    # ANGLE / SwiftShader / Vulkan / DirectX / MSVC 运行库 / 调试辅助
    foreach ($name in @(
        'libEGL.dll', 'libGLESv2.dll', 'vk_swiftshader.dll', 'libvk_swiftshader.dll',
        'vulkan-1.dll', 'libvulkan.dll', 'VkICD_mock_icd.dll', 'VkLayer_khronos_validation.dll',
        'd3dcompiler_47.dll', 'dxcompiler.dll', 'dxil.dll',
        'msvcp140.dll', 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll',
        'concrt140.dll', 'vccorlib140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll',
        'dbgcore.dll', 'dbghelp.dll'
    )) {
        Add-OptionalFile -Src (Join-Path $OutPath $name) -DestDir $dest
    }

    # 数据目录
    foreach ($name in @('angledata', 'hyphen-data', 'resources', 'locales')) {
        Add-OptionalDir -Src (Join-Path $OutPath $name) -DestDir $dest
    }

    # 调试符号（可选开关）
    if ($WithPdb) {
        foreach ($pattern in @('arupa_kernel.dll.pdb', 'arupa_render.exe.pdb', 'arupa_plugin_host.exe.pdb')) {
            Add-OptionalFile -Src (Join-Path $OutPath $pattern) -DestDir $dest
        }
    }


    # include/
    if (Test-Path -LiteralPath $IncDir -PathType Container) {
        $incDest = Join-Path $DistPath 'include'
        New-Item -ItemType Directory -Force -Path $incDest | Out-Null
        $headerCount = 0
        foreach ($h in (Get-ChildItem -LiteralPath $IncDir -File -Filter '*.h' -ErrorAction SilentlyContinue)) {
            Copy-Item -LiteralPath $h.FullName -Destination $incDest -Force
            $headerCount++
        }
        Write-Step "  include/ （$headerCount 个头文件）"
        if ($headerCount -eq 0) { Write-Warn "--include-dir 下没有 *.h: $IncDir" }
    } else {
        Write-Warn "找不到 C API 头文件目录: $IncDir（可用 --include-dir 指定）"
    }

    # 版本标记
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $dest '.arupa-version'), "$Ver`n", $utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $dest '.arupa-delivery-id'), "$DeliveryId`n", $utf8NoBom)
    Write-Step "  .arupa-version=$Ver  .arupa-delivery-id=$DeliveryId"

    # 可选附件
    if ($DocsDir) {
        if (-not (Test-Path -LiteralPath $DocsDir -PathType Container)) { throw "--docs 不是目录: $DocsDir" }
        $docsDest = Join-Path $DistPath 'docs'
        New-Item -ItemType Directory -Force -Path $docsDest | Out-Null
        Copy-Item -Path "$DocsDir\*" -Destination $docsDest -Recurse -Force
        Write-Step "  附带 docs\ ← $DocsDir"
    }
    if ($ProbeDir) {
        if (-not (Test-Path -LiteralPath $ProbeDir -PathType Container)) { throw "--probe 不是目录: $ProbeDir" }
        $probeDest = Join-Path $DistPath 'probe-plugin'
        New-Item -ItemType Directory -Force -Path $probeDest | Out-Null
        Copy-Item -Path "$ProbeDir\*" -Destination $probeDest -Recurse -Force
        Write-Step "  附带 probe-plugin\ ← $ProbeDir"
    }

    # <repo>\package\package_desktop 下的一级文件/文件夹（docs\ dotnet\ …）
    Copy-PackageDir -DistPath $DistPath

    # 系统垃圾文件清理
    foreach ($junk in @('Thumbs.db', '.DS_Store', 'desktop.ini')) {
        Get-ChildItem -LiteralPath $DistPath -Recurse -Force -File -Filter $junk -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }

    # 架构核对：内核库必须就是本次打包的 arch
    Assert-LibArch -LibPath (Join-Path $dest (Get-LibName)) -WantCpu $Cpu

    if ($script:MissingOptional.Count -gt 0) {
        Write-Warn "可选件缺失 $($script:MissingOptional.Count) 项（不阻断）: $($script:MissingOptional -join ' ')"
    }

    $script:PakSrc = $savedPak   # 下一个架构重新解析
}

# ------------------------------------------------------------- PE 架构核对
function Get-PeMachine {
    param([string]$Path)
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $reader = New-Object System.IO.BinaryReader($stream)
        if ($reader.ReadUInt16() -ne 0x5A4D) { return 0 }     # MZ
        $stream.Position = 0x3C
        $eLfanew = $reader.ReadInt32()
        $stream.Position = $eLfanew
        if ($reader.ReadUInt32() -ne 0x00004550) { return 0 } # PE\0\0
        return $reader.ReadUInt16()
    } catch {
        return 0
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Assert-LibArch {
    param([string]$LibPath, [string]$WantCpu)
    $machine = Get-PeMachine -Path $LibPath
    switch ($machine) {
        0x8664 { $got = 'x64' }
        0xAA64 { $got = 'arm64' }
        0x014C { $got = 'x86' }
        default { $got = if ($machine -eq 0) { '读不到 PE 头' } else { '0x{0:X4}' -f $machine } }
    }
    if ($got -ne $WantCpu) {
        Fail "内核库架构对不上（期望 $WantCpu，实际 $got）: $LibPath"
        return
    }
    Write-Step "  架构核对 ✓ $WantCpu"
}

# ------------------------------------------------- SHA256SUMS / MANIFEST
function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Write-SumsAndManifest {
    param([string]$DistPath, [string]$Cpu)

    $root = (Resolve-Path -LiteralPath $DistPath).Path.TrimEnd('\')
    $entries = @()
    foreach ($f in (Get-ChildItem -LiteralPath $DistPath -Recurse -Force -File -ErrorAction SilentlyContinue)) {
        $relative = $f.FullName.Substring($root.Length).TrimStart('\') -replace '\\', '/'
        if ($relative -eq 'SHA256SUMS.txt' -or $relative -eq 'MANIFEST.md') { continue }
        $entries += ('{0}  {1}' -f (Get-FileSha256 -Path $f.FullName), $relative)
    }
    $entries = $entries | Sort-Object

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $lines = New-Object System.Collections.Generic.List[string]
    [void]$lines.Add('# sha256  文件  （相对交付根，本目录下的清单文件不在其中）')
    [void]$lines.Add("# 交付 id $DeliveryId · 打包 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    foreach ($e in $entries) { [void]$lines.Add($e) }
    [System.IO.File]::WriteAllText((Join-Path $DistPath 'SHA256SUMS.txt'), ($lines -join "`n") + "`n", $utf8NoBom)
    Write-Step "SHA256SUMS.txt（$($entries.Count) 项）"

    $manifest = New-Object System.Collections.Generic.List[string]
    [void]$manifest.Add("# Arupa 内核 · win($Cpu) 交付清单")
    [void]$manifest.Add('')
    [void]$manifest.Add('| | |')
    [void]$manifest.Add('|---|---|')
    [void]$manifest.Add("| 交付 id | ``$DeliveryId`` |")
    [void]$manifest.Add("| 内核版本 | ``$Ver`` |")
    [void]$manifest.Add("| 平台 | ``win`` / ``$Cpu`` |")
    [void]$manifest.Add("| 来源 out | ``$(Split-Path -Leaf $script:OutUsed)`` |")
    [void]$manifest.Add("| 打包时间 | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') |")
    [void]$manifest.Add('')
    [void]$manifest.Add('## 交付件 sha256')
    [void]$manifest.Add('')
    foreach ($f in (Get-ChildItem -LiteralPath (Join-Path $DistPath 'kernel') -Recurse -Force -File -ErrorAction SilentlyContinue)) {
        $relative = $f.FullName.Substring((Join-Path $DistPath 'kernel').Length).TrimStart('\') -replace '\\', '/'
        [void]$manifest.Add("- ``kernel/$relative``  ``$(Get-FileSha256 -Path $f.FullName)``")
    }
    [System.IO.File]::WriteAllText((Join-Path $DistPath 'MANIFEST.md'), ($manifest -join "`n") + "`n", $utf8NoBom)
    Write-Step 'MANIFEST.md'
}

function Remove-IncompletePackage {
    param([string]$DistPath)
    if (-not (Test-Path -LiteralPath $DistPath -PathType Container)) { return }
    Remove-Item -LiteralPath $DistPath -Recurse -Force
}

# ---------------------------------------------------------------- 逐架构打包
foreach ($cpu in $Archs) {
    if ($OutOverride) { $out = $OutOverride }
    elseif ($OutName) { $out = Join-Path $SrcDir "out\$OutName" }
    else { $out = Join-Path $SrcDir "out\arupa-win-$cpu-$Ver-static" }

    if (-not (Test-Path -LiteralPath $out -PathType Container)) {
        Fail "没有 out 目录（先跑 scripts\build-arupa-desktop.ps1 --arch $cpu）: $out"
    }
    $script:OutUsed = $out

    if ($DistName) {
        $dist = Join-Path $DistDir $DistName
    } else {
        $dist = Join-Path $DistDir "arupa-win-$cpu-$Ver-static-$Num"
    }

    if (Test-Path -LiteralPath $dist) {
        if ($Force) {
            Write-Warn "目标已存在，--force 删除重建: $dist"
            Remove-Item -LiteralPath $dist -Recurse -Force
        } else {
            Fail "目标已存在（加 --force 覆盖）: $dist"
        }
    }

    Write-Step "交付包: $dist"
    Write-Info "交付 id: $DeliveryId  （ver=$Ver n=$Num os=win arch=$cpu）"

    try {
        Invoke-PackageArch -Cpu $cpu -OutPath $out -DistPath $dist
        Write-SumsAndManifest -DistPath $dist -Cpu $cpu

        if ($DoZip) {
            $zipPath = "$dist.zip"
            if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
            Write-Step "zip: $zipPath"
            try {
                Compress-Archive -LiteralPath $dist -DestinationPath $zipPath -CompressionLevel Optimal -ErrorAction Stop
            } catch {
                Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
                throw "打 zip 失败: $($_.Exception.Message)"
            }
        }
    } catch {
        # 中途失败别留下空壳 dist 目录（会把下一次的序号顶上去，也会被误当成完整包）
        Remove-IncompletePackage -DistPath $dist
        Fail $_.Exception.Message
    }

    $kernelBytes = (Get-ChildItem -LiteralPath (Join-Path $dist 'kernel') -Recurse -Force -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if (-not $kernelBytes) { $kernelBytes = 0 }
    Write-Host "    kernel 体积: $(Format-Size ([long]$kernelBytes))"
}

Write-Host ''
Write-Host "[OK] 完成: win $Ver n=$Num" -ForegroundColor Green
foreach ($cpu in $Archs) {
    Write-Host "    dist\arupa-win-$cpu-$Ver-static-$Num"
}
