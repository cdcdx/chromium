# Chromium / Arupa 源码与构建脚本

根目录的 Shell / PowerShell 入口共用 Python 3.9+ 实现，参数保持一致。
`fetch` 下载源码和工具链；`build` 编译、打包内核及浏览器，不下载仓库或运行 gclient；
`backup` 将 src 相对指定版本的修改导出到 patches 目录。

## 原有流程与重构

| 原有问题 | 当前实现 |
|---|---|
| 根 build 仍以 `kernel/browser/android` 调度旧 nomad 项目，与独立 Arupa 脚本并存 | 根入口统一调度两个 Arupa 内核和两个 nomadbrowser 项目；内核打包实现在 `scripts/packaging/` |
| fetch 默认未同步 DEPS，更新失败仍可能保留旧 HEAD 继续运行 | 默认同步源码、DEPS、hooks；命令失败立即退出，不报告成功 |
| 内核、PC、Android 源码和版本混用 | 四个独立仓库分别配置 URL、tag/branch/commit |
| Linux / Windows 缺少 ARM64 配置，桌面打包未接受 x86 | 每个系统一份 GN 配置，由 arch/link 生成参数；Windows / Linux 打包接受 x86 |
| Android 指向旧 `chrome/browser/arupa`，桌面相对软链接错误 | 使用绝对路径挂载两个独立模块，并验证已有挂载目标 |
| 构建图修改上游 BUILD.gn，切换 Chromium 版本容易冲突 | 独立生成 `src/arupa_build/<project>/BUILD.gn`，通过 GN root target 接入 |
| 代理修改 Git 全局设置、dry-run 仍可能写文件 | 代理仅影响子进程，dry-run 不创建文件、不执行命令 |
| 部分架构不匹配只告警，失败后保留半成品 | 架构不匹配失败；打包失败清理本次不完整目录 |

`scripts/builder/` 保留旧实现供迁移参考，当前入口不再调用它；其中的旧项目命令不属于新接口。
不再支持旧 `down/test/manifest/print-delivery` 动作。PC 外壳通过 .NET 发布，Android 浏览器通过 Gradle 生成 APK。
业务仓库的下载仍属于 fetch。

公共逻辑按职责集中维护：`scripts/platforms.py` 定义系统名称、架构矩阵和宿主要求；
`scripts/apple_tools.py` 统一选择 Xcode 并检查 SDK/Metal；`scripts/native_tools.py` 检查宿主依赖；
`scripts/toolchains.py` 准备可下载工具；`scripts/concurrency.py` 计算 Ninja 并发数。
三个入口的 `--os` 均接受 `windows` / `macos` 别名，内部统一为 `win` / `mac`。
`fetch android` 自动选择 Android 依赖；组合 `--install-host-deps` 时安装对应的 Linux 宿主依赖。

## 源码拉取

同步执行命令时当前目录的一级子目录仓库（不递归，也不操作当前目录自身）：

```bash
bash fetch.sh pull
bash fetch.sh push
bash fetch.sh push --dry-run
```

Windows 对应 `./fetch.ps1 pull` / `./fetch.ps1 push`。
两个命令单独执行，按各仓库当前分支配置的 upstream 拉取或推送，不切换分支，不自动提交。
`pull` 仅允许快进，工作区有修改时拒绝；`push` 只推送当前 HEAD 到 upstream 分支，不强推或附带 tags。
裸仓库、游离 HEAD、无提交或无 upstream 的仓库报错并继续，最后汇总；任一错误返回非零退出码。
`--dry-run` 只列出计划，不执行 Git 或联网，也不验证 upstream。

查看**执行命令时当前目录的一级子目录**中的 Git 仓库，无需 `.env` 或仓库版本配置：

```bash
bash fetch.sh log          # 每个仓库最近 1 条提交：SHA、作者、日期、标题
bash fetch.sh branch       # 仅列本地分支，含当前分支标记、提交及 upstream 信息
bash fetch.sh log branch   # 同时查看
```

Windows 使用 `.\fetch.ps1 log` / `.\fetch.ps1 branch`。从其他目录使用脚本绝对路径执行时，
扫描该调用目录的直接子目录，而不是脚本所在目录。不包含当前目录自身，也不递归进入 `src` 的依赖仓库。
支持一级子目录中的普通仓库、worktree（.git 文件）和裸仓库；跳过 `.git` 及符号链接目录。
空仓库显示“尚无提交/分支”；单个仓库出错仍继续，最后返回非零退出码。无仓库时显示数量 0。
这两个命令不联网、不 fetch、不切换分支；`branch` 使用 `git branch -vv`，不列远端分支。
本地分支的 upstream 信息仍会显示，处于分离 HEAD 时保留其状态提示；不能与拉取或安装动作混用。
`--dry-run` 只列出仓库及查看计划，不执行 Git 命令。

复制 `.env.example` 为 `.env`，配置四个仓库地址和版本。现有 `.env` 不会被重构脚本覆盖。
优先级为命令行 > 环境变量（支持同名大写）> `.env` > 默认值。

| 仓库目录 / fetch 目标 | `.env` 地址 | `.env` 版本 |
|---|---|---|
| `src`（目标名 `chromium`） | `chromium_src` | `chromium_ver` |
| `arupa_desktop` | `arupa_desktop_src` | `arupa_desktop_ver` |
| `arupa_android` | `arupa_android_src` | `arupa_android_ver` |
| `nomadbrowser.pc` | `nomad_desktop_src` | `nomad_desktop_ver` |
| `nomadbrowser.android` | `nomad_android_src` | `nomad_android_ver` |

Chromium 版本使用四段 tag。业务仓库版本接受 tag、branch 或 commit SHA；要可重现，请使用 tag 或完整 SHA。
同名 tag/branch 应显式写 `refs/tags/...` 或 `refs/heads/...`。
脚本按 FETCH_HEAD 分离检出；业务仓库保留历史，Chromium 默认浅拉取。
已有工作区有本地改动时停止，不 reset、不覆盖、不自动 stash。已有非 Git 目录不会被覆盖。
Git 认证使用本机 credential helper / SSH 配置。

```bash
# 完整流程：更新 depot_tools 最新远端 HEAD、自举 Python/CIPD，
# 检出 Chromium、同步 DEPS/hooks，然后拉取四个业务仓库。
bash fetch.sh --ver 154.0.8037.21

# Linux 上准备 Android 依赖；完整流程仍包含全部业务仓库。
bash fetch.sh --os android --ver 154.0.8037.21

# 单独更新一个业务仓库；地址也可通过 --arupa-desktop-src 指定。
bash fetch.sh arupa_desktop --arupa-desktop-ver refs/tags/v1.0.0
bash fetch.sh nomadbrowser.android --nomad-android-ver refs/heads/release

# 仅更新 Chromium 与工具/依赖，不要求四个业务仓库的配置。
bash fetch.sh update --ver 154.0.8037.21 --save
bash fetch.sh depot_tools
bash fetch.sh deps --ver 154.0.8037.21 --nohooks
bash fetch.sh hooks

# 仅预览；首次完整流程要求四个仓库配置齐全。
bash fetch.sh --dry-run
```

Windows 使用相同参数，例如 ` .\fetch.ps1 --ver 154.0.8037.21`。
`android` 目标是 `update --os android` 的便捷写法；`arupa_android` 目标只拉取该内核仓库。
`--full-history` 关闭 Chromium 浅拉取，并在已有浅仓库上执行 unshallow。
`--save` 只在全部请求步骤成功后保存显式 `--ver` 到 `.env`。
`.gclient` 保留已有 custom_vars/custom_deps，合并 `--os` 指定的目标系统、宿主系统，以及 x86/x64/arm64 target_cpu；重复执行不会添加重复项。
依赖同步显式传入当前 Chromium commit，避免 managed=False 时版本漂移。
`--nohooks` 会跳过 Chromium hooks，之后需执行 `fetch hooks`。

## 编译与打包

缺失工具链通过 fetch 准备，build 只检查和使用工具，不在编译途中安装：

```bash
# 已有 Chromium 源码：按当前配置版本同步 DEPS/hooks（GN、Ninja、Clang 等）。
# macOS 同时补充当前 Xcode 的 Metal Toolchain。
bash fetch.sh toolchains
# 仅修复 Metal；完整 Xcode 必须已安装，可重复执行。
bash fetch.sh metal
# 从 Microsoft 官方安装脚本安装浏览器 .NET SDK 到 .tools/dotnet。
bash fetch.sh dotnet
# Linux：复用 Chromium DEPS 固定的 Android SDK/JDK，可按配置追加 SDK 包。
bash fetch.sh android-sdk
bash fetch.sh jdk
```

Windows 对应使用 `fetch.ps1 toolchains` / `fetch.ps1 dotnet`。
`toolchains` 使用 `.env chromium_ver` 或显式 `--ver`，必须与当前 src 版本一致；首次准备源码请用 `fetch update`。
Android 可用 `fetch toolchains --os android`；单独 `android-sdk` / `jdk` 在工具缺失时同步当前 src HEAD 的 DEPS 并运行 hooks。
不配置 `android_home` / `java_home` 时，build 自动识别 `src/third_party/android_sdk/public` 和 `src/third_party/jdk/current`。
Chromium 固定的 JDK 不保证满足浏览器 Gradle 的主版本要求；如项目要求另一版本，需配置对应完整 JDK 的 `java_home`。
额外 SDK 包通过 `.env android_sdk_packages` 指定；sdkmanager 保留许可证交互，不自动同意许可证。
完整 Xcode 和 Node.js/npm 仍需在宿主安装；Linux 开发包和 Windows C++ 组件可通过以下显式入口准备。

| 目标 | 自动下载/复用 | 编译前检查 |
|---|---|---|
| Windows | Chromium DEPS/hooks 的 GN、Ninja、Clang/LLD；指定官方 VS 安装器可安装 C++ 组件 | Chromium 自带 VS/SDK 检测；各目标架构的 MSVC、ATL/MFC、UCRT/UM 库及 x64 Debugging Tools |
| macOS | Chromium DEPS/hooks；Xcode Metal 组件 | 完整 Xcode、macOS SDK、可执行的 Metal 编译器、Clang/LLD |
| Linux | Chromium DEPS/hooks；每个目标架构的 sysroot | 上游开发包 quick-check、Clang/LLD、所选架构的 sysroot |
| Android（Linux 宿主） | Chromium DEPS 固定的 NDK、SDK、JDK | Linux 开发包（含 32 位宿主库）、SDK 精确版本、NDK sysroot、Java/javac |

```bash
# Linux：安装发行版开发依赖，再同步工具与全部目标架构的 sysroot。
bash fetch.sh toolchains --os linux --arch all --install-host-deps
# Android：安装 Linux 宿主依赖，再同步 NDK/SDK/JDK。
bash fetch.sh toolchains --os android --install-host-deps
# 也可单独安装宿主依赖或补一个架构的 sysroot。
bash fetch.sh host-deps --os linux --arch x86
bash fetch.sh sysroots --os linux --arch arm64
```

```powershell
# 使用符合当前 Chromium 要求的微软官方 VS bootstrapper，保留安装界面。
.\fetch.ps1 host-deps --os win --arch all --vs-installer C:\Downloads\vs_Community.exe
.\fetch.ps1 toolchains --os win --arch all
```

`--arch` 在工具链准备中默认 all。常规 `fetch toolchains` 不隐式安装系统包；
仅 `host-deps` 或 `--install-host-deps` 执行宿主安装。Linux 调用当前源码的
`build/install-build-deps.py`，由其处理发行版包清单、sudo 和交互；不会强行启用不受支持的发行版。
Windows 安装器使用 NativeDesktop、ATL/MFC，并按需添加 ARM64 工具和 MFC；SDK 和 Debugging Tools
仍需在安装界面按 `src/docs/windows_build_instructions.md` 选择，安装后检查缺项，不自动接受许可或重启。
VS/SDK 版本解析使用当前 Chromium 的 `build/vs_toolchain.py`，不在外层脚本写死版本。
`--nohooks` 仅用于源码/依赖同步，不能与 `toolchains`、`android-sdk`、`jdk` 同用；
这些工具链准备命令需要执行 hooks，参数冲突会在下载或安装前报错。
仅 package 不执行内核工具链检查；PC WebUI 构建会检查 Node.js/npm，`--no-web` 跳过此项。

`.NET` 优先使用 `dotnet_version`，其次读取浏览器 `global.json`，均没有时使用 `dotnet_channel`（默认 10.0）。
安装后 build 自动发现 `.tools/dotnet`；显式 `--dotnet` / `dotnet_path` 仍优先。
安装器使用 [Microsoft 官方安装脚本](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-install-script)，不会修改系统 PATH。
下载脚本时若 Python 报 CA 证书验证失败，会改用 curl（macOS 优先系统 curl），仍验证 HTTPS 证书，且仅允许 HTTPS 重定向。
如果代理使用自签 CA，可设置 `SSL_CERT_FILE=/path/to/trusted-ca.pem`；Python 和备用 curl 均使用该证书文件，不关闭证书验证。

| 入口 | 项目 | 目标系统 | CPU |
|---|---|---|---|
| `build.ps1` | `arupa_desktop` / `nomadbrowser.pc` | win | x86 / x64 / arm64 |
| `build.sh` | `arupa_desktop` / `nomadbrowser.pc` | macos（别名 mac） | x64 / arm64 |
| `build.sh` | `arupa_desktop` / `nomadbrowser.pc` | linux | x86 / x64 / arm64 |
| `build.sh` | `arupa_android` / `nomadbrowser.android` | android | x64 / arm64 |

桌面目标在相应系统宿主构建，同系统内可选择不同 CPU。Android 在 Linux 宿主构建。
`arupa_desktop` 省略 `--os` / `--arch` 时默认当前系统、当前芯片；
`arupa_android` 默认 `--os android --arch all`，同时编译 x64 和 arm64；可显式 `--arch` 只编译一种。
内核 Ninja 的 `--jobs/-j` 默认自动计算：取 CPU 逻辑核心数与内存允许任务数中的较小值，最低为 1。
内存按每个编译任务 2 GiB 估算，并预留至少 2 GiB 或总物理内存的 20%（取较大值）；
例如 16 核/16 GiB 默认 6 个任务，16 核/32 GiB 默认 12 个任务。Linux 同时遵守进程 CPU affinity。
无法获取内存时保守使用 1 个任务；可用 `--jobs 8` 手动覆盖。该估算不是硬性内存限制，
不计其他进程当前占用或容器内存配额；内存紧张或容器内构建时请手动调低。
任务数在启动时计算一次，日志显示计算依据；浏览器构建仍保留默认 8 个任务。
例如 `bash build.sh arupa_desktop build` 编译本机桌面内核，`bash build.sh arupa_android build` 编译两种 Android 内核。
这一宿主限制与 [Chromium Android 构建说明](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/android_build_instructions.md) 一致。
需要事先准备系统构建依赖、macOS Xcode 或 Windows Visual Studio/SDK；build 阶段不会自动安装系统软件。
Windows 默认使用本机 VS 工具链（DEPOT_TOOLS_WIN_TOOLCHAIN=0）。

macOS 内核构建会在写入构建文件前检查完整 Xcode 和 macOS SDK。
仅安装 Command Line Tools 不够；安装完整 Xcode 后，可在 `.env` 设置
`chromium_developer_dir=/Applications/Xcode.app/Contents/Developer`（路径按实际安装位置修改）。
选择顺序为该配置、已有 `DEVELOPER_DIR`、系统 `xcode-select`；系统未选中完整 Xcode 时，
尝试 `/Applications` 和 `~/Applications` 中唯一的 `Xcode*.app`，多个候选需显式配置。
路径仅通过构建进程的 `DEVELOPER_DIR` 传递，不修改系统的 `xcode-select` 设置。
预检查会实际执行 `metal --version`，避免只找到占位程序却没有 Metal Toolchain；缺失时先运行 `fetch metal`。
预检查失败会保留 Xcode 返回的许可证、SDK 等错误信息；`--dry-run` 不运行这些检查。

以下 GN 配置适用于 Arupa 内核；浏览器通过 SDK/AAR 使用内核，不接收 `--args`。
GN 配置只保留四份：`build/win/args.gn`、`build/mac/args.gn`、
`build/linux/args.gn`、`build/android/args.gn`，原有按架构和动静态拆分的 18 份配置已合并。
构建可用 `--args build/<os>/args.gn` 显式指定，省略时自动选择目标系统的配置。
自定义配置也必须位于工作区 `build/` 内，且 `target_os` 必须与 `--os` 一致。
生成时按 `--arch` 替换 `target_cpu`，按 `--link static|dynamic` 替换
`is_component_build=false|true`，保留赋值在模板中的顺序，避免后续条件判断读取旧值。
同一受控参数有多处赋值时明确报错。输出到构建目录的 `args.gn`，不修改模板。
生成文件采用临时文件加原子替换，写入失败保留原文件；执行 `gn gen` 前清除旧成功标记，
生成失败后必须重新 `gen`，不能使用上一次残留的构建图。
`--arch all` 从同一份模板为各架构分别生成参数。Android 仅支持 static，传入 dynamic 会在生成配置前报错。
Android 的 `include_both_v8_snapshots` 由构建入口按架构强制设置，覆盖模板中的旧值；模板未写该参数时也会补齐：

| Android 架构 | is_component_build | include_both_v8_snapshots |
|---|---|---|
| arm64 | false | true |
| x64 | false | false |

`--arch all` 对两份输出分别应用该规则，不修改公共模板。已有输出参数不一致时需重新执行 `gen`。


动作：`gen` 生成构建图；`build` 编译（没有 build.ninja 时自动 gen）；`package` 打包已有产物；
内核的 `all` 按 gen → build → package 执行，也是省略动作时的默认行为；浏览器的 `all` 执行 build → package，不支持 gen。
多个动作按该依赖顺序执行并去重。构建使用树内 GN/Ninja；桌面 `dynamic` 支持 gen/build，交付打包仅支持 static。
GN 接入方式参见 [GN root target 文档](https://gn.googlesource.com/gn/+/HEAD/docs/reference.md#dotfile)；
只用 `--root-target` 限制构建图，不再传同名的 `--root-pattern`（两者生成的 `build.ninja` 相同，
但后者会在 Android 的 java build config 目标上误报生成输入错误）。

```bash
# macOS
bash build.sh arupa_desktop all --os mac --arch arm64 --zip
bash build.sh arupa_desktop gen build --os mac --args build/mac/args.gn --arch x64 --link dynamic --jobs 8

# Linux
bash build.sh arupa_desktop all --os linux --arch x86
bash build.sh arupa_desktop all --os linux --arch arm64

# Linux 上编译 Android，all 架构会合并为一个交付目录。
bash build.sh arupa_android all --args build/android/args.gn --arch all --link static --zip
bash build.sh arupa_android package --arch arm64

# 仅查看任意平台的计划，不要求本机具备该平台工具链。
bash build.sh arupa_desktop --os win --arch all --dry-run
```

```powershell
.\build.ps1 arupa_desktop all --arch x86 --zip
.\build.ps1 arupa_desktop all --args build/win/args.gn --arch x64 --link static --zip
.\build.ps1 arupa_desktop all --arch arm64 --zip
```

内核打包按项目使用附件目录，将其中的内容平铺复制到交付根目录，并纳入清单和 ZIP：

- `arupa_desktop`：`package/package_desktop/`，例如 `docs/`、`dotnet/`。
- `arupa_android`：`package/package_android/`，例如 `docs/`、`probe-plugin/`。

对应附件目录必须存在；根 build 入口在编译前检查，不再读取整个 `package/` 或另一平台的附件。
已删除 `--plugin-runtime` 参数及专用运行时检查、复制逻辑，不再向 `kernel/plugin-runtime` 或
`kernel/<arch>/plugin-runtime` 注入文件。附件目录内若有 `plugin-runtime/`，仅作为普通附件复制到交付根目录。

构建目录为 `src/out/arupa-<os>-<arch>-<version>-static`，dynamic 目录省略 `-static`。
版本默认读取 `src/chrome/VERSION`，显式 `--ver` 必须与它一致。
桌面交付目录：`dist/arupa-<os>-<arch>-<version>-static-<n>/`。
Android 交付目录：`dist/arupa-android-<version>-static-<n>/kernel/<arch>/`。
包包含版本标记、SHA256SUMS、MANIFEST；`--zip` 额外生成 ZIP。
`--num` 指定交付序号，`--dist-dir` 指定交付根。已有包不会被根 build 入口覆盖。
Linux 当前项目 GN 未定义 render 目标，交付中缺少 arupa_render 会沿用原规则告警。

## 浏览器编译与打包

先用 fetch 拉取对应浏览器源码。PC 需要项目要求的 .NET SDK、Node.js/npm，以及同系统、同架构、同 Chromium 版本的静态内核交付包。
dotnet 选择顺序为 `--dotnet`、`dotnet_path` 配置（支持大写环境变量）、工作区 `.tools/dotnet`、PATH。
PC 构建默认显式使用 `build/nuget.config` 的官方 NuGet v3 源，覆盖浏览器仓库和用户配置的源选择。
Mac 浏览器在 WebUI 构建和 publish 前执行项目的 `ValidateArupaDelivery`，提前检查完整交付包。
桌面内核 Ninja 目标包含 `bundle_hyphen_data`；Mac 打包要求 `hyphen-data/manifest.json` 和 `.hyb` 词典存在。
旧交付包缺少这些文件时，执行 `bash build.sh arupa_desktop gen build package --os mac --arch x64`
重新生成完整交付包（arm64 请替换架构），再编译浏览器。
该配置同时传给内核 .NET 门面、浏览器和 Windows 更新器的 build/publish。
需要私有源、认证或镜像时，用 `--nuget-config /path/to/nuget.config` 指定完整配置。
NuGet restore 不按源的排列顺序回退；不能仅把有效源放在前面就保留失效源。

脚本在浏览器仓库目录执行 `dotnet --version`，检查 `global.json` 所要求的 SDK 能否被选中；仅安装 Runtime 会提前报错。
还会按主工程显式声明的 `TargetFramework` / `TargetFrameworks` 检查 SDK 最低版本；
例如 `net10.0` 不能使用 SDK 6。缺少兼容 SDK 时先执行 `bash fetch.sh dotnet`（Windows 使用 `fetch.ps1 dotnet`）。
WebUI 构建前使用 npm 自带的 semver 校验 `package.json` 的 `engines.node`。
优先使用 `.env` 的 `node_path`，否则使用 PATH 中的 Node；后者不兼容时，尝试 `$NVM_DIR`（默认 `~/.nvm`）中已安装的版本。
选中的 Node 与配套 npm 一起加入本次构建的 PATH，确保 npm 子脚本使用相同版本，不修改 shell 默认版本。
没有兼容版本则在 npm 安装/构建前停止；不会删除 `--experimental-strip-types` 或跳过 WebUI 测试。
默认从工作区 `dist/` 选择最新匹配序号，或通过 `--delivery` 指定交付目录；`--dist-dir` 仅控制输出。
`--ver` 在浏览器命令中表示消费的内核版本，可在没有 Chromium 源码时显式指定。
默认 Release；`--variant debug` 可切换。默认构建 WebUI，`--no-web` 复用已有资源，资源缺失会报错。

```bash
bash build.sh nomadbrowser.pc all --os macos --arch arm64 --ver 154.0.8037.21 --zip
bash build.sh nomadbrowser.pc all --os linux --arch x64 --ver 154.0.8037.21 --delivery /path/to/kernel-delivery
bash build.sh nomadbrowser.android all --arch all --ver 154.0.8037.21 --zip
# 只打包此前成功生成且未经修改的浏览器产物。
bash build.sh nomadbrowser.pc package --os macos --arch arm64 --ver 154.0.8037.21 --zip
```

```powershell
.\build.ps1 nomadbrowser.pc all --arch x64 --ver 154.0.8037.21 --zip
.\build.ps1 nomadbrowser.pc all --arch arm64 --ver 154.0.8037.21 --delivery D:\deliveries\arupa-win-arm64
```

PC 默认工程分别为 `NomadBrowser.Avalonia`、`NomadBrowser.Avalonia.Mac`、`NomadBrowser.Avalonia.Linux` 下同名 csproj；
可用 `--pc-project` 指定实际工程。Windows 同时发布两个更新器；Linux 沿用现有工程的 net10.0 配置，并生成 `run.sh`；
macOS 要求工程的 `MacDistRoot` 下产出唯一 `.app`，且已包含 `Contents/Resources/arupa-mac` 内核。
Linux x86 参数已接入，但微软提供的 Linux .NET 运行时不含 x86，项目必须自行提供对应运行时及原生依赖，
普通官方 SDK 无法完成此目标；不能将参数支持视为已验证可交付。[官方运行时下载](https://dotnet.microsoft.com/en-us/download/dotnet/9.0)

Android 需要事先准备 JDK、Android SDK，以及浏览器仓库的 Gradle wrapper。
SDK 可通过 `--android-sdk`、`ANDROID_HOME` / `ANDROID_SDK_ROOT`（或 `.env` 的同名小写项）、
浏览器仓库 `local.properties` 的 `sdk.dir` 指定。已有 `sdk.dir` 与指定路径冲突时停止并提示统一路径，不覆盖该文件。
脚本预检查 SDK 的 platforms/build-tools 及 JDK 的 java/javac；具体 compileSdk、Build Tools、JDK 版本由项目 Gradle 配置约束。
JDK 可通过 `JAVA_HOME`（或 `.env` 的 `java_home`）指定。单独 package 不要求这些工具；dry-run 不执行工具预检查。
将对应 Arupa Android 内核交付件按浏览器协议接入 `app/libs/kernel/{arm64,x64}/arupa-kernel.aar`，
如有 `tools/ci/runtime-manifest.json`，需同步其文件指纹。`--delivery` 仅用于 PC。
脚本校验 AAR 中的目标 ABI，分别执行 `:app:clean :app:assembleRelease -PkernelAbi=<arch>`，
再校验 APK 仅包含所选 ABI；签名使用浏览器项目自身配置，不生成或替换签名密钥。

浏览器构建产物位于 `out/<project>-<os>-<arch>-<version>-<variant>/`；
交付位于 `dist/<project>-<os>-<arch>-<version>-<variant>-<n>/`，包含构建清单及 SHA256SUMS。
`--arch all` 每个架构独立构建、独立打包。`--zip` 保留 macOS bundle 的符号链接。
构建失败会使旧成功清单失效；单独 package 会核对配置和文件哈希，避免将旧产物或修改后的产物误打包。

## 源码修改备份

`backup.sh` / `backup.ps1` 共用 `scripts/backup.py`。必须显式传入基线版本，
支持本地 tag、branch 或 commit SHA；推荐 Chromium 四段 tag 或完整 SHA。
脚本不会联网；缺少基线时直接报错。fetch 现在会保留指定版本的本地 tag。

```bash
bash backup.sh --ver 154.0.8037.21
bash backup.sh --os mac --ver 154.0.8037.21 --patches /path/to/patches
bash backup.sh --os android --ver 154.0.8037.21 --num 3 --patches /path/to/patches
bash backup.sh --base <commit-sha> --src /path/to/src --output /path/to/patches
bash backup.sh --ver 154.0.8037.21 --tracked-only
bash backup.sh --ver 154.0.8037.21 --dry-run
```

```powershell
.\backup.ps1 --os win --ver 154.0.8037.21 --patches D:\backups\patches
```

默认备份根目录为工作区 `patches/`，自定义相对路径按当前调用目录解析。
每次创建目录 `src-{os}-{ver}-{n}.patches/`，例如
`patches/src-mac-154.0.8037.21-1.patches/`。`--os` 默认宿主系统，支持
win/mac/linux/android；备份 Android 修改时显式传 `--os android`。
序号 n 在同一系统和版本内从 1 自动递增，也可用 `--num` 指定正整数，已有备份拒绝覆盖。
分支名称中的路径分隔符会替换为下划线，原始基线和 commit 始终记录在清单中。
序号通过临时 `.patches.lock` 文件避免并发冲突，正常完成或失败时自动释放。
这里 `.patches` 是备份目录后缀，目录包含：

- `chromium.patch`：相对基线的最终工作区差异，支持二进制、文件删除和文件模式；重命名按删除+新增表示。
- `manifest.json`：基线 commit、当前 HEAD、文件变更清单、排除范围及补丁 SHA256。
- `README.md`：恢复步骤及备份范围。

备份覆盖 src 主仓库相对基线的已提交、暂存和未暂存修改，以及未被 Git 忽略的新文件。
`--tracked-only` 排除未跟踪文件，仍包含已暂存的新文件。补丁保存最终文件内容，
不保存提交历史，也不恢复暂存/未暂存的原始划分。无差异时仍生成清单及空补丁。

不递归备份 DEPS 子仓库或嵌套仓库的内部修改，也不包含 Gitlink 指针变化。
明确排除 `out/`、`arupa_build/`、`chrome/browser/arupa_desktop`、
`chrome/browser/arupa_android` 和旧 `chrome/browser/arupa` 挂载目录。
Arupa 内核源码属于独立 Git 仓库，不能靠此 src 补丁恢复。
嵌套仓库目录及排除范围会记录在清单中，非受 Git 管理的内容请另行备份。

备份使用隔离的临时 Git index 和对象目录，不修改 src 的 HEAD、实际暂存区、工作文件或对象库。
扫描时复用现有索引的文件缓存，只将发生变化的文件收集到临时索引，避免重新读取全部 Chromium 文件。
执行时显示扫描、收集、生成补丁和写入四个阶段；耗时较长的 Git 步骤每 10 秒报告一次等待状态。
存在未解决的合并冲突时停止。输出目录必须位于 src 之外；输出根路径不是目录、指定序号已存在或正被占用时，在扫描源码前报错。
完成后才将临时备份目录重命名为正式目录；失败会清理临时目录并释放本次序号锁。
备份期间请勿并发编辑源码或运行其他 Git 操作，HEAD 变化会被检测并报错。
`--dry-run` 仅通过只读 Git 命令检查基线和目标位置，不扫描并生成差异。

恢复时，在另一份干净检出中先切到清单里的 `base_commit`，再使用补丁绝对路径：

```bash
git checkout --detach <base_commit>
git apply --check /path/to/patches/<backup>/chromium.patch
git apply --binary /path/to/patches/<backup>/chromium.patch
```

空补丁无需执行 `git apply`。该步骤不恢复独立依赖仓库，请根据对应版本的 DEPS 准备依赖。

## 验证与限制

```bash
python3 -m unittest discover -s tests -v
bash -n build.sh fetch.sh backup.sh
bash -n scripts/packaging/package-arupa_desktop.sh scripts/packaging/package-arupa_android.sh
```

离线测试覆盖平台矩阵、参数约束、下载步骤顺序、独立业务版本配置、本地 Git 精确版本检出、
脏工作区保护、dry-run 无写入、macOS 打包和架构错误清理、Android ABI 错误清理。
新增备份测试通过临时 Git 仓库验证补丁恢复、源仓库不变、基线错误、排除嵌套仓库和重复备份。
测试使用临时仓库和合成产物，不证明内核代码可以通过完整 Chromium 编译。
浏览器测试覆盖 .NET 发布参数、Gradle ABI 分离、构建失败清单失效、产物篡改检查及 ZIP 符号链接。
两个浏览器源码仓库目前尚未拉取；适配依据仓库内原有构建逻辑，尚未用真实项目验证。
未执行实际远端拉取、完整 Chromium / .NET / Gradle 编译或 Windows PowerShell 打包。
