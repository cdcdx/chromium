<!-- doc-type: spec -->
<!-- owner: 内核会话 -->
<!-- updated: 2026-08-12 -->
<!-- ssot-for: android-integration-constraints -->
<!-- integration-line: android -->
# Arupa 内核 — 安卓端接入清单（交付给对接团队）

> 📍 **这份在对接线里的位置**：安卓对接一共三份，各回答一类问题。
> 先看主线「对接线地图」那一章决定读哪份 —— 本份回答的是
> **「接的时候有哪些硬约束、哪些语义跟 AOSP WebView 不一样、接完怎么验」**。
> 想查某个 API 怎么调看 API 参考那份；想知道工具链/AAR/Manifest 怎么摆看对接文档那份。
>
> ⚠️ **两处时效**（正文其余部分仍然有效）：
> ① 下面 §2 那些 `D` / `C` 勾验标记是 2026-06 手写的。
> **「某个方法现在到底有没有」以「实况 · AOSP 对照」为准** —— 那页读 AAR 的方法表现测。
> ② 版本不写在这里：内核版本看 `docs/canonical-paths.md` 的四标记，
> 交付件清单看 `dist/android/current/LATEST.md`。写进正文的版本号必然过期。
>
> 🔴 **加速相关的内容已经从本文删掉**（原 §6.5 定责表 / §6.6 bundle 自检闸门）。
> 那两节整节建立在**我们自建的那条加速线**上，2026-08-10 已整体弃用；
> 现在的形态是「只出标准、三方自建」：三方跑自己的进程并监听本地 SOCKS5，
> 内核只需 `setProxy(socks5://127.0.0.1:<port>)` 连它。见 §6.5。

---

## 0. 一句话就绪结论

**内核侧：✅ 就绪，核心面已 Pixel 7 真机 device-verified。**
**安卓侧：⏳ 待你们做一次集成验收**（按 §5 用例在 NomadBrowser 工程内跑一遍）。

「就绪」= 内核把 NomadBrowser 替换 `android.webkit.WebView` 所需的 in-app 控制面
（渲染/生命周期 + WebViewClient/WebChromeClient 等价回调 + 代理/身份特权 + 请求拦截）
全部以**同步 JNI**提供并验证。剩下的「安卓端确认」是你们在自己工程里集成跑通——
这一步内核侧替不了，需要你们的 controller 接上去后按验收用例确认。

---

## 1. 接入前提（3 步，硬约束）

```kotlin
// ① 进程级一次性 init（仅主进程！子进程必须守卫）
class MyApp : Application() {
  override fun onCreate() {
    super.onCreate()
    if (isMainProcess()) ArupaKernel.initialize(this)   // 载 libarupakernel.so + 资源
  }
}
// ② 每个 view 实例
val web = ChromiumWebView(activity)   // 单参, 内部自造 WindowAndroid
setContentView(web)
web.setClient(myClient)               // 装回调（见 §3）
web.loadUrl("https://example.com")
// ③ 销毁（必调, 否则 WebContents 泄漏）
web.destroy()
```

- [ ] **🔴 同进程禁忌**：in-process chromium **不能**和系统 `android.webkit.WebView`
  同进程（双 `JNI_OnLoad` → SIGTRAP）。迁移走 **B 方案并行宿主**（内核 view 与 AOSP
  WebView 进程隔离，灰度切，不一次性动所有 controller）。
- [ ] **ABI**：内核 .so 仅 arm64-v8a（生产真机）。x86_64 模拟器需单独 x64 AAR。
- [ ] **打包契约**（AGP/Gradle/SDK/资产/多进程 manifest）见
  `docs/guides/nomad-hardened-stack-integration.md`。

---

## 2. 能力清单（逐项勾验，D=内核已真机验, C=编译+符号验待运行触发）

### 渲染 / 导航 / 生命周期
- [ ] `loadUrl / loadDataWithBaseURL / reload / stopLoading / goBack/Forward / clearHistory`（D）
- [ ] `getUrl / getTitle / isLoading`（D）
- [ ] `saveState():byte[]` / `restoreState(byte[])` — tab 持久化（D，内核格式非 AOSP Bundle）
- [ ] `destroy()` 生命周期（D）

### WebViewClient/WebChromeClient 等价回调（`setClient(Client)`）
- [ ] `onPageStarted / onPageFinished / onLoadProgressChanged / onReceivedTitle`（D）
  - ⚠️ `onPageFinished` 已门控 `hasCommitted()`（被取消/失败的导航不误报，#234 修）
- [ ] `onReceivedError / onRenderProcessGone`（C — 需触发错误/崩溃才验）
- [ ] `onJsAlert / onJsConfirm / onJsPrompt`（D，#232，result 回传 JS）
- [ ] `onReceivedHttpAuthRequest`（D，#232，handler.proceed/cancel 可同步调）
- [ ] `onGeolocationPermissionsShowPrompt`（D，#232；其余权限内核 default-deny 反指纹）
- [ ] `onPermissionRequest`(摄像头/麦克风)（D，#233）
- [ ] `onReceivedIcon(url)`（D，#233，传 URL 非 Bitmap）
- [ ] `onDownloadStart`（D，#232）
- [ ] `onFindResult`（D，#231）
- [ ] `onShowFullscreen/Hide / onOpenNewWindow / onSslStateChanged / onJsBeforeUnload`（C）
- [ ] `onShowFileChooser`(`<input type=file>`)（C — 需真实用户手势触发, 接线已就绪）

### in-app 控制（#233/#234/#235 — 三批 JNI，全 D）
- [ ] **UA 覆盖** `setUserAgent(ua)`（D，#233；导航生效需 reload）
- [ ] **Cookie** `getCookies/setCookie/removeAllCookies/flushCookies`（D，#233，**per-partition**）
- [ ] **导航拦截** `Client.shouldOverrideUrlLoading(url,isMainFrame,isRedirect,hasGesture)`
  → true=取消（D，#234；仅主框架 + renderer-initiated + 重定向，不拦自家 loadUrl）
- [ ] **资源拦截** `Client.shouldInterceptRequest(url,method,isForMainFrame,headers)`
  → 返 `ArupaWebResourceResponse` 替换 / null 放行（D，#235）

### 内核特权（VendorAuthn.isTrusted() 门控，未授信静默 no-op）
- [ ] `setProxy(socks5://host:port)`（D — 代理命门，#228 已修路由）
- [ ] `setTransportRoute / setPendingIdentity`（D）

---

## 3. 🔴 必须处理的约束（不是 bug，是语义差异，接错会踩）

- [ ] **没有 `WebViewClient`/`WebChromeClient` 子类**：改 override 本类 `Client` 接口
  （语义等价，方法名见 §2）。`ChromiumWebView extends FrameLayout`，**不是**
  `android.webkit.WebView` 子类 → 直接替换字段类型/cast 会编译失败，需适配层。
- [ ] **Cookie 是 per-partition（每 view 独立 StoragePartition）**，不是全局
  `CookieManager`。跨 view 共享 cookie 的逻辑要重新设计（或共用同 partitionName）。
- [ ] **`shouldInterceptRequest` 回调跑在网络 IO 线程**（非 UI，同 AOSP 契约）：
  你们的实现**必须线程安全 + 快速返回**（会同步阻塞该请求加载）。勿在此做慢 IO /
  勿触碰仅 UI 线程的状态。阻断一个请求 = 返回空 body 或自定义错误页（非抛异常）。
- [ ] **`shouldOverrideUrlLoading` 须同步返回**，不在回调内同步调 `loadUrl`
  （要改导航请 post 到下一轮）。不对自家 `loadUrl()` 触发（browser-initiated）。
- [ ] **代理 URL 铁律**：用干净 `socks5://host:port`，**别**拼 `cred@host`。
- [ ] **反取证**：内核默认 incognito（存储全 in-memory 不落盘）。需登录态持久的 vendor
  要显式关 `arupa_default_off_the_record`（退回 wipe-on-exit）。

---

## 4. 选路（什么走 JNI，什么走 CDP）

**这件事不写在这里** —— 选路决策已收编进主线「对接选路 · 什么走直调、什么走协议」那一章，
两端（安卓 JNI / 桌面 C ABI）共用同一套判据，写两份必然漂。

只留一条与本清单直接相关的事实：**请求拦截（导航 + 资源）与 Cookie / UA / 权限
已经全部有 JNI 直路**，不必再走 CDP 的 `Fetch.*` / `Network.*`；
历史上为绕开缺口而走 CDP 的 in-app 路径**应迁回 JNI**（那是 workaround，不是最佳）。
CDP 仍保留，给的是**远程 agent** 场景。

---

## 5. 集成验收用例（你们在 NomadBrowser 工程内跑一遍 = 「安卓端确认」）

内核侧这些已在 demo（`demo`）device-verified；你们接上 controller 后复现即可：

- [ ] **渲染**：loadUrl 真实站点，页面正常渲染 + onPageStarted/Finished 触发
- [ ] **代理**：setProxy(socks5) 后访问 ipinfo，出口 IP = 代理出口（非真实 IP）
- [ ] **Cookie**：setCookie → getCookies 读回 → removeAllCookies → 空
- [ ] **UA**：setUserAgent("X") + reload → `navigator.userAgent`=="X" + 请求头 UA=="X"
- [ ] **媒体权限**：getUserMedia → onPermissionRequest 触发 → grant/deny 生效
- [ ] **导航拦截**：链接点击/JS 跳转 → shouldOverrideUrlLoading 触发；return true → 不跳转
- [ ] **资源拦截**：某子资源 → shouldInterceptRequest 返自定义响应 → 页面拿到 mock；
  return null → 拿到真实网络响应（放行）
- [ ] **JS 对话框/HTTP 认证/下载**：触发后对应 Client 回调 + 应答回传生效
- [ ] **tab 持久化**：saveState → 进程重建 → restoreState → 历史恢复

> demo 自检命令（参考，你们可照搬测法）：
> `--es autoBatch1Test 1`(#233) / `--es autoNavTest 1`(#234) / `--es autoInterceptTest 1`(#235)

---

## 6. 已知边界 / 本批未做（需要时再开，非阻塞）

- [ ] **资源拦截首版用 byte[] body**（非流式）：大响应全进内存。流式（Java InputStream）
  需后续移植 `AndroidStreamReaderURLLoader`——有需求再开。
- [ ] **Worker / Service Worker 的请求未代理**（frame==null 跳过）：`shouldInterceptRequest`
  当前只覆盖文档 + 子资源（页面 frame 内）。SW fetch 不经拦截——有需求再开。
- [ ] **导航拦截仅主框架**：子框架（iframe）导航不触发 `shouldOverrideUrlLoading`。
- [ ] **pass-through 请求不改写头**：资源拦截只「整体替换 or 原样放行」，不支持改 header
  后再放行（要改写就走「替换」路径自己重发）。
- [ ] **C 级回调**（fullscreen/fileChooser/onReceivedError/onRenderProcessGone/...）：
  接线已就绪，但需真实运行场景（手势/全屏/崩溃）触发才能 device 验——你们集成时顺带验。
> 原先这里还挂着两条与安卓接入无关的「独立推进」条目（chromium 升级演练、桌面 Windows 内核）。
> 两件都已完成 —— 三端都已 roll 到当前基线并验收、桌面内核已编出并交付 ⇒ 已删，免得读的人以为还欠着。

---

## 6.5 出口异常时怎么定责（内核/集成 vs 三方加速客户端）

> 加速能力**不由我们实现** —— 三方跑自己的进程、监听一个本地 SOCKS5 端口，
> 内核只负责把某个单元的流量指过去。所以定责的第一刀是
> **「问题出在 socks 端口这一侧，还是那一侧」**。

| 现象 | 定责 | 动作 |
|---|---|---|
| 不走代理的域名正常，**走代理的域名空页**（`chrome-error://chromewebdata/`） | 🔴 **三方客户端 / 它的上游**，非内核非集成 | 先确认那个本地端口真的在监听、且用别的工具走它能出网；能出网才怀疑内核 |
| `setProxy` 后**所有**域名（含不该走代理的）都空 / 真实 IP 泄漏 | 🟠 集成侧：proxy URL 格式或作用域配错 | proxy URL 必须是干净的 `socks5://host:port`（**勿** `cred@host`）；再查接管域名清单 |
| 某 JNI 方法 / 回调没触发、行为与本清单不符 | 🟢 内核 / 集成侧 | 回内核发工单（见 §7） |

**一条经验（仍然成立，换了对象也一样）**：页面空白**不等于**线路被封。
历史上最常见的真因是**加速侧装了坏版本**，而不是网络封锁 —— 报"被封"之前，
先用同一个本地端口在浏览器之外验一次出网，再实测一次出口 IP。
这条判断当年省过好几轮真机试错。

---

## 7. 待安卓端反馈给内核的（闭环）

接入后若发现以下，回内核侧补：
- [ ] 缺的回调/方法（AOSP 有但本清单没列到的，你们 controller 真用到的）
- [ ] C 级回调运行中不符预期的
- [ ] 性能：资源拦截 IO 线程回调是否成为瓶颈（真实页面几十~上百请求）
- [ ] per-partition cookie 语义是否够用（是否需要全局 CookieManager 等价）

---

**结论**：内核侧我已交付并真机验证三批 JNI（#233/#234/#235）+ #231/#232 回调全套 +
代理/身份特权。**安卓端的「确认」= 你们按 §5 在 NomadBrowser 集成跑通**——这是流程上
属于你们的一步，内核侧已就绪等接入。
