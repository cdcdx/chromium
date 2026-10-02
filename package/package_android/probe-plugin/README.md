# 已知好包（先确认你的宿主接线是对的）

这个目录里是一个**签名过的最小传输插件**（安卓 ABI）。
它存在的理由：把「我的宿主接线对不对」与「我的插件对不对」**分开**。

## 用法

1. 把 `arupa-android-probe.arupa-plugin/` 整个目录拷进你 App 的 plugin root
   （就是你传给 `ArupaPluginBridge.setPluginRoot(...)` 的那个目录）；
2. 宿主 `AndroidManifest.xml` 里要有这两条 —— **内核替不了**：

```xml
<uses-permission android:name="android.permission.INTERNET" />
<service android:name="co.arupa.plugin.ArupaPluginHost"
         android:process=":arupaplugin"
         android:exported="false" />
```

3. 调：

```java
int rc = ArupaPluginBridge.startIsolatedEngine(context, "arupa-android-probe");
// rc==0 只表示"验签过了 + Service 拉起来了"，**不表示引擎能用**
boolean up = ArupaPluginBridge.isEngineReady(55088, 300);   // ⚠ 不能在主线程调(它探端口)
```

## 通了应该看到什么

```
adb shell ps -A | grep arupaplugin       →  <你的包名>:arupaplugin      ← 引擎在独立进程里
adb logcat -s ArupaFakeEngine            →  PROC ... name=<包名>:arupaplugin
                                            LISTEN=OK(127.0.0.1:55088)
```

🔴 **日志只在那个隔离进程里，主进程什么都看不到** —— 排查时一定要按 tag 抓
（`ArupaPluginHost` 与 `ArupaFakeEngine`），不然会误以为"什么都没发生"。

## 常见的三种失败，各自是什么

| 看到 | 意思 |
|---|---|
| `rc == 3`（`ISO_SERVICE_START_FAILED`） | manifest 里没声明那个 Service |
| `UnsatisfiedLinkError: ... nativeLoadEngine` | AAR 太旧 —— `152.0.7977.42+12` **之前**的件里那个 `.so` 一个符号都不导出（已修） |
| `LISTEN=FAIL(socket, errno=1)` | 没有 `INTERNET` 权限。⚠ 它报的是 `EPERM`，**长得完全不像"权限没声明"** |

## 它**不是**什么

- **不是** SOCKS 代理：只 listen 不转发。证明"装载链与进程归属"，**不证明**代理能用。
- **不是**可改的样板：签名覆盖 `manifest ‖ so_sha256`，改一个字节验签必失败。
- ⚠ **不要**给那个 Service 加 `android:isolatedProcess="true"` —— 那个没有网络权限，引擎起不来。

## 🆕 +35 起：四份包，两个 ABI × 好/坏（复核 13）

| 目录 | ABI | 用途 | 预期 |
|---|---|---|---|
| `arupa-android-probe.arupa-plugin/` | x86_64（模拟器） | 已知好包 | `startIsolatedEngine` rc=0，`LISTEN=OK(127.0.0.1:55088)` |
| `arupa-android-probe-arm64.arupa-plugin/` | arm64-v8a（真机） | 已知好包 | 同上 |
| `arupa-android-badabi.arupa-plugin/` | x86_64 | **受控错误 ABI 件** | 见下 |
| `arupa-android-badabi-arm64.arupa-plugin/` | arm64-v8a | **受控错误 ABI 件** | 见下 |

四份都用 dev CA 签，验签**都过**——坏包坏在 **vtable 的 `abi_version` 故意填错**，
布局其余字段与真契约逐字相同（源码 `build/roll/fake_transport_engine_android_badabi.c`）。

### 错误 ABI 件的验收判据（FB-P128 ②「拒绝时不碰 destroy」）

坏包针对的是**进程内加载路径**：`ArupaPluginBridge.load(pluginId)` → `plugin_loader.cc` → `AdoptCProvider`。

| 看到 | 判 |
|---|---|
| `load` 返回 **`LOAD_SKELETON_ONLY`（1）**，进程活着 | ✅ 通过：内核判定「布局不可信」后一个字段都没再碰 |
| logcat 有 `[ArupaTransport] vtable abi_version=… want=… —— 布局不可信, **不调 destroy**` | ✅ 拒绝路径就是这一条 |
| 进程当场没了 / tombstone 里有 `ArupaBadAbi` 的 `abort()` | ❌ 有人把拒绝路径上的 `destroy` 调用加回来了——坏包的 `destroy` 指向一个先打日志再 `abort()` 的函数，**它被调到就是判据红** |
| `LOAD_SIGNATURE_REJECTED`（4） | 不是 ABI 判据在红，是签名：确认用的是 dev CA 内核件 |

⚠ 坏包**不监听端口**，`isEngineReady(55088)` 永远 false 是正常的——它只用来测「拒绝时进程不死」。
⚠ `startIsolatedEngine`（隔离进程线）对坏包同样应当**不崩**；但 FB-P128 ② 的原始判据在进程内那条路，先按上表验。

### 原生 Debug（DCHECK）线程验证
交付件是 Release（`is_debug=false`，DCHECK 关）。三处后台 JNI 入口（`dispatchActionClick` /
`fireExtEvent` / `dispatchContextMenuClick`）的线程封送（`RunOnUiAndWait`）在 Release 上已由
App 真机 PASS；DCHECK 覆盖需要一份 `dcheck_always_on=true` 的内核件——那是一次全量编译（数小时），
不随本轮件出，另行排期；排期前**不宣称**DCHECK 已验。

## ⚠ 一条以后会咬人的

它用**开发 CA** 签。切正式 CA 之后这份包会验签失败 ——
到那时症状看起来像"工具坏了"，其实是样例过期。届时我们会重签并随件更新。
