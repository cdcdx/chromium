<!-- doc-type: spec -->
<!-- owner: 内核会话 -->
<!-- updated: 2026-09-29 -->
<!-- ssot-for: none -->
<!-- integration-line: android -->
# ChromiumWebView Android 对接 API 参考

> 📍 **这份在对接线里的位置**：安卓对接三份之一，本份回答的是
> **「某个方法/回调怎么调、参数什么含义」** —— 用到具体 API 时查它。
> 工程侧怎么摆（工具链 / AAR / Manifest / 打包）看对接文档那份；
> 接错会踩的语义差异与验收用例看接入清单那份。整条线的分工见主线「对接线地图」那一章。
>
> ⚠ 正文里的状态标记是写下那天的快照。**「某个方法现在有没有」以「实况 · AOSP 对照」为准**
> —— 那页读 AAR 的方法表现测，有闸防漂。

> 📌 **本文的「混合架构 · 选路决策」已收编进主线 `77-channel-choice`**
>(「对接选路 · 什么走直调、什么走协议」)。**API 清单没有收编** ——
> 主线 `81-api-android` 已有,而且逐个 API 的有无归探针(「实况 · AOSP 对照」读 AAR 现测)。
>
> 🔴 收编时修正了一处**本文自相矛盾**的地方: 文末那句选路口诀写
> 「UA / Cookie / **网络拦截** → CDP」, 而**同一份文档的正文早就写着**
> 导航拦截与资源拦截已全部 JNI 化、UA 与 Cookie 也补成了 JNI 直路。
> 口诀比它总结的正文旧 —— **一句朗朗上口的总结, 比正文更容易过期**,
> 因为更新正文的人不会回头看它。主线那一章把这条也写进去了。

> 面向 NomadBrowser（天枢 Android 消费仓）从 `android.webkit.WebView` 迁移到 arupa 内核。
> 类: `co.arupa.kernel.ChromiumWebView`（打进 `arupa-kernel.aar`）。
> 最后更新: 2026-06-02（#231/#232 全量 API + 审计后；全部 device-verified）。

---

## ★ 混合架构: JNI vs CDP（对接前必读，审计结论 2026-06-02）

内核对接面是**两条互补通道**，不是只有 ChromiumWebView JNI。审计 NomadBrowser 实际用法
后划清边界 —— **别把该走 CDP 的功能去找 JNI**（反之亦然）：

| 通道 | 是什么 | 适合 | NomadBrowser 实际用它做 |
|---|---|---|---|
| **ChromiumWebView JNI**（本文档） | 进程内 Java→JNI→native，同步 | View 实例化/渲染/生命周期、代理/身份特权、WebView-client 等价回调（页面事件/对话框/认证/下载/find/状态） | `setProxy`（+ render/lifecycle 隐式） |
| **CDP bridge（#217 NativeBridge over ADP）** | 加密 socket 控制面，异步 | UA 覆盖、Cookie 增删查、请求拦截/改写、JS eval、截图、Network/Page/Fetch 域、DNR | `Network.setUserAgentOverride`/`setCookie`/`getCookies`/`clearBrowserCookies`、`Fetch.fulfillRequest/failRequest/continueRequest`、`Runtime.evaluate`、`Page.captureScreenshot` |

**审计结论（关键，给对接团队）**:
- NomadBrowser 内核路径**几乎全走 CDP**，ChromiumWebView JNI 当前只直接调 `setProxy`。
- 之前以为缺的 **CookieManager / setUserAgentString / onPermissionRequest(摄像头麦克风)**
  **不是 JNI 缺口** —— 它们在 NomadBrowser 里走 CDP（`Network.setCookie`/`setUserAgentOverride`）
  或属于 **AOSP WebView 旧路径**（B 方案并行宿主里正被替换的那套，非内核路径）。
- `shouldOverrideUrlLoading`（导航拦截）**#234 起走内核 JNI**（`ArupaNavigationThrottle`，
  `Client.shouldOverrideUrlLoading` 同步返回 true=取消导航）；`shouldInterceptRequest`（资源
  拦截/改写）**#235 起走内核 JNI**（`ArupaProxyingURLLoaderFactory` 代理 URLLoaderFactory，
  `Client.shouldInterceptRequest` 返回 `ArupaWebResourceResponse` 替换 / null 放行）。
  **请求拦截域已全部 JNI 化，CDP `Fetch.*` 仅远程 agent 场景保留。**
- **无阻塞性 JNI 缺口**。#231/#232 把 WebView-client 回调（页面/对话框/认证/下载/地理位置/find/
  状态序列化）补成了 JNI **可选直路**，CDP 仍是 UA/Cookie/拦截的主路；两者可并用。
- 符号验证: libarupakernel.so 全部 #231/#232 JNI 已注册（jni_zero muxed）+ find_in_page native
  真链入（非 stub）+ 零 JNI stub。

> 选路口诀: **状态机/渲染/生命周期/per-tab 代理身份 → JNI；UA/Cookie/网络拦截/eval/截图 → CDP**。

---

## 0. 一句话定位 + 关键差异（必读）

`ChromiumWebView extends FrameLayout`，**不是 `android.webkit.WebView` 的子类**。它是内核
`content_public.browser.WebContents` 的薄 Java 封装。替换**不无缝**：

- 没有 `WebViewClient`/`WebChromeClient` —— 改用本类的 `Client` 接口（语义等价回调）。
- Cookie/存储是 **per-partition**（每实例独立 StoragePartition），不是全局 CookieManager。
- 多了 `setProxy`/`setTransportRoute`/`setPendingIdentity` 等内核特权 API（替换的真正动机）。
- 迁移建议: **B 方案并行宿主**（与 AOSP WebView 共存灰度切，不一次性动所有 controller）。
  详见 [[project_chromiumwebview_not_aosp_webview_subclass]]。
- ⚠️ **禁忌**: in-process chromium 不能和系统 `android.webkit.WebView` 同进程（JNI_OnLoad
  SIGTRAP）。同一进程二选一。

---

## 1. 接入前提（生命周期）

```kotlin
// ① 主进程 Application.onCreate（仅主进程，子进程守卫）—— 进程级一次性 init
class MyApp : Application() {
  override fun onCreate() {
    super.onCreate()
    if (isMainProcess()) ArupaKernel.initialize(this)   // 加载 libarupakernel.so + 资源
  }
}

// ② 每个 WebView 实例（Activity 内）
val web = ChromiumWebView(activity)        // 单参; 内部自造 WindowAndroid
setContentView(web)                         // 或 addView 进布局
web.loadUrl("https://example.com")

// ③ 销毁（Activity.onDestroy）
web.destroy()                               // 必调; 否则 WebContents 泄漏
```

工具链/打包契约（AGP/Gradle/SDK/资产/manifest 多进程）见
[[reference_nomad_kernel_integration]] 与 `docs/guides/nomad-hardened-stack-integration.md`。

---

## 2. 完整 API 表

授权列: **开放** = 无门控（CDP 等价渲染面）; **特权** = `VendorAuthn.isTrusted()` 门控
（ADP 等价, 未授信 vendor 调用静默 no-op + logDenied）。
验证列: **D** = device-verified; **C** = 编译+符号验证（运行未单独触发）。

### 2.1 导航 / 加载
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `loadUrl(String)` | `loadUrl` | 开放 | D |
| `loadDataWithBaseURL(baseUrl, data, mime, encoding, historyUrl)` | 同名 | 开放 | D |
| `reload()` | `reload` | 开放 | D |
| `stopLoading()` | `stopLoading` | 开放 | D |
| `canGoBack()` / `goBack()` | 同名 | 开放 | D |
| `canGoForward()` / `goForward()` | 同名 | 开放 | D |
| `clearHistory()` | `clearHistory` | 开放 | D |
| `isLoading()` | (近似 progress<100) | 开放 | D |
| `getUrl()` | `getUrl` | 开放 | D |
| `getTitle()` | `getTitle` | 开放 | D |
| `getOriginalUrl()` | `getOriginalUrl` | 开放 | C |

### 2.2 状态序列化（tab 持久化 / 进程重建）
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `byte[] saveState()` | `saveState(Bundle)` | 开放 | D |
| `boolean restoreState(byte[])` | `restoreState(Bundle)` | 开放 | D |

> 内核格式（非 AOSP Bundle）。`saveState` 返 null = 无历史/未就绪。`restoreState` 须在
> WebContents 就绪后（先 loadUrl 或本 view 已加载过）。单页 ~1.5KB，多页线性增长。

### 2.3 回调监听（WebViewClient/WebChromeClient 等价）
| 方法 | 说明 | 授权 | 验证 |
|---|---|---|---|
| `setClient(Client)` | 装回调监听; null 清除 | 开放 | D |

`Client` 接口（全部 default 空实现, 按需 override）:
| 回调 | WebView 等价 | 验证 |
|---|---|---|
| `onPageStarted(url)` | `onPageStarted` | D |
| `onPageFinished(url)` | `onPageFinished` | D |
| `onLoadProgressChanged(int 0..100)` | `onProgressChanged` | D |
| `onReceivedTitle(title)` | `onReceivedTitle` | D |
| `onReceivedError(failingUrl, errorCode)` | `onReceivedError`（errorCode=net error 负值）。🔴 **2026-09-13 起主框架导航阶段的错误(DNS 失败 / 代理连不上 / 断流)才真的会回调** —— 此前只挂在 `didFailLoad` 上, 导航错误从不来; 现在从导航结束处报出, **先 onReceivedError 再 onPageFinished**(与 AOSP 同序), 同一次失败不重复报; **只报主框架**; **-3 ERR_ABORTED(被抢占 / 取消 / 转下载)不报**。出口 fail-closed 时 App 的"线路挂了"提示靠它(-130 出口没人听 · -131 PAC 取不到 · -336 出口串拼错) | C · 🆕 ExtHost RT6/RT7b |
| `onRenderProcessGone()` | `onRenderProcessGone` | C |
| `onShowFullscreen()` / `onHideFullscreen()` | `onShowCustomView`/`onHideCustomView` | C |
| `onOpenNewWindow(url)` | `onCreateWindow` | C |
| `onSslStateChanged()` | (锁图标刷新时机) | C |
| `onFindResult(active, total, isDoneCounting)` | `setFindListener` | D |
| `onShowFileChooser(requestId, acceptTypes[], mode)` | `onShowFileChooser` | C |
| `onDownloadStart(url, ua, contentDisposition, mime, contentLength)` | `DownloadListener.onDownloadStart` | **D**(#232) |
| `onJsAlert(url, message, JsResult)` | `WebChromeClient.onJsAlert` | **D**(#232) |
| `onJsConfirm(url, message, JsResult)` | `onJsConfirm` | **D**(#232) |
| `onJsPrompt(url, message, defaultValue, JsPromptResult)` | `onJsPrompt` | **D**(#232) |
| `onJsBeforeUnload(url, JsResult)` | `onJsBeforeUnload` | C |
| `onReceivedHttpAuthRequest(HttpAuthHandler, host, realm)` | `WebViewClient.onReceivedHttpAuthRequest` | **D**(#232) |

### 2.4 页内查找
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `findAllAsync(String)` | `findAllAsync` | 开放 | D |
| `findNext(boolean forward)` | `findNext` | 开放 | D |
| `clearMatches()` | `clearMatches` | 开放 | D |

> 结果走 `Client.onFindResult`（active=1-based 当前序号, total=总匹配, isDoneCounting=本轮完成）。
> ⚠️ **已知差异**: find **不索引 `loadDataWithBaseURL` 合成页文本**（返 0/0），真实 http 页正常。

### 2.5 JS 桥 / 求值
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `addJavascriptInterface(obj, name, requiredAnnotation)` | `addJavascriptInterface` | 开放 | D |
| `removeJavascriptInterface(name)` | 同名 | 开放 | D |
| `evaluateJavascript(script, Consumer<String> cb)` | 同名 | 开放 | D |
| `injectIsolatedJs(script, worldId, Consumer<String> cb)` | **无等价** | 开放 | C |

> `requiredAnnotation` 传 `JavascriptInterface::class.java`（仅暴露带注解方法）或 null（暴露全部
> public 方法 = pre-API17 行为, XSS 风险 App 侧自管）。注入在**下次导航**生效（先 add 再 navigate/reload）。
> `evaluateJavascript` 须主线程调; 结果是 JSON 编码字符串。

> 🔴 **`injectIsolatedJs` 与 `evaluateJavascript` 的差别是「注进哪个世界」**, 不是写法:
> 前者注进**隔离世界**(MV3 内容脚本所在), 后者注进**主世界**(与页面脚本同处)。
> AOSP WebView **没有等价物** —— 它只有主世界那一条路。
> · `worldId` 合法区间取自 chromium 的 `IsolatedWorldIds`
>   (`ISOLATED_WORLD_ID_CONTENT_END` … `ISOLATED_WORLD_ID_MAX`), **不硬编**, 上游改了自动跟。
> · **越界不会崩, 会被拒掉并回 `"null"`** —— native 侧
>   `RenderFrameHost::ExecuteJavaScriptInIsolatedWorld` 里是 `CHECK`, 传坏值等于让上层
>   能把内核 FATAL 掉, 所以在 Java 侧先挡。
> · **任何失败路径都回 `"null"`, 不静默丢弃** —— 拿不到结果时你会收到 `"null"` 而不是没有回调。
> ⚠ 这个方法是**补票不是新功能**: 在它之前安卓只有 `evaluateJavascript`, 而页面内注入
> 已经在产品里用起来了(内置翻译插件) ⇒ 插件脚本此前一直和页面脚本同处一个世界。

### 2.6 WebPreferences（per-tab, 跨导航持久）
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `setWebPrefs(Boolean forceDark, loadImages, javascript, domStorage, multipleWindows)` | `WebSettings` 多项 | 开放 | D |
| `setForceDark(boolean)` | `setForceDark`/algorithmic darkening | 开放 | D |
| `setLoadImages(boolean)` | `setLoadsImagesAutomatically` | 开放 | D |

> 每参 `@Nullable Boolean`: null=保持内核默认, true/false=覆盖。

### 2.7 内核特权 API（ADP 等价, VendorAuthn 门控）
| 方法 | 作用 | 授权 | 验证 |
|---|---|---|---|
| `setProxy(String?)` | per-tab 代理（`socks5://host:port`, 空=直连） | **特权** | D（#229 hy2 出口 US / #215 reality） |
| `setTransportRoute(socksEndpoint, String[] managedHosts)` | split-tunnel: 名单内走代理名单外 DIRECT | **特权** | D（#229 / P2-K1） |
| `setPacScript(String?)` | 🆕 走 **PAC**(扩展 `proxy.settings` 的 `pac_script`)。支持 `http(s)://` 与 `data:` 内联;空 = 撤销。与 `setTransportRoute` **互斥**;取不到 PAC **断流不回落直连**。可在 WebContents 建好前调(pending 回放) | **特权** | 🆕 KVPN-06 |
| `getWebRtcIpPolicy()` → `int` | 🆕 此刻的 WebRTC 档位: `1` 全封非代理 UDP · `0` 只封局域网 · `-1` 读不到(**不是 0**)。设了代理**或 PAC** 之后该分区每个活标签页都应读到 `1` | 开放(只读) | 🆕 ExtHost `-e mode webrtc` |
| `setTransportRoute(socksEndpoint, String[] managedHosts, String[] bypassHosts)` | 🆕 FB-P160 带**排除名单**:全局路由(managedHosts 空或 `"*"`)下 bypassHosts 内**直连**、其余走出口。`chrome.proxy` 的 `rules.bypassList` 映射到它。与分流白名单同给 / 有坏条目 ⇒ **不应用**(读 `ArupaProxyBridge.getLastErrorJson()`: `ROUTE_BYPASS_WITH_SPLIT` / `ROUTE_BYPASS_INVALID`)。可在 WebContents 建好前调(pending) | **特权** | 🆕 ExtHost `-e mode route` RT2–RT4 |
| `setPacScriptForExtension(extensionId, pacUrl, String[] managedHosts)` → `int` | 🆕 **扩展发起的**设 PAC(与 `ArupaProxyBridge.setPacScriptForExtension` 同一道门、同一套码,不必再反射取 WebContents)。pacUrl 空 = **撤销**(照样过门)。WebContents 还没建 ⇒ 回 **`EXT_ROUTE_PENDING`(8)**、首次 loadUrl 时补跑;宿主没过 VendorAuthn ⇒ **`EXT_ROUTE_HOST_NOT_TRUSTED`(-1)** | **特权** | 🆕 RT9(-1 那档) |
| `setPacScriptForExtension(extensionId, pacUrl, String[] managedHosts, Boolean mandatory)` → `int` | 🆕 `+26` 同上一行, 多带扩展传来的 `pacScript.mandatory`: PAC 取不到 / 解析不了 / 脚本出错时 `true` = **阻断**(不回落直连)、`false` = **回落直连**、`null` 按 `false`(与 Chrome 默认一致)。三参数那条仍按 `true` | **特权** | 🆕 |
| `getPacStatusJson()` → `String` | 🆕 `+26` 本 view 所在分区最近一次 PAC 失败的原因与走向: `pac · mandatory · state · kind · net_error · reason · outcome(blocked\|fallback_direct) · owner_ext · at_ms`。空串 = 没设 PAC / 还没失败过。同一失败还以 `chrome.proxy.onProxyError` 发给设 PAC 的扩展 | 开放(只读) | 🆕 |
| `setTransportRouteForExtension(extensionId, socksEndpoint, String[] managedHosts)` → `int`<br>`setTransportRouteForExtension(extensionId, socksEndpoint, String[] managedHosts, String[] bypassHosts)` → `int` | 🆕 **扩展发起的**设固定出口,码同上;带排除名单那个不合法回 **9** | **特权** | 🆕 |
| `getLastExtensionRouteResult()` → `int` | 🆕 最近一次扩展门面调用的**真实码**(含 pending 补跑那次)。**`EXT_ROUTE_NONE`(-2)** = 本 view 从没调过扩展门面;**8** = 已暂存、首次加载还没补跑(补跑后变成真实码);宿主没过 VendorAuthn 时实例方法**返回** -1 但**不改**这里的值。⚠ 回 8 之后要读它,**8 不等于已生效** · 🔴 `+29` 订正:`+28` 暂存期读到的是 -1,与 `HOST_NOT_TRUSTED` 撞码 | 开放(只读) | 🆕 RT9b |
| `getLastTransportRouteErrorJson()` → `String` | 🆕 `+30` 本 view **最近一次宿主出口路由调用**(`setTransportRoute` 两参 / 三参)的错误 JSON(形状同 `ArupaProxyBridge.getLastErrorJson()`)。WebContents 已建 ⇒ 调完即可读;pending ⇒ **补跑那一刻**才写入,补跑前是空串。按 view 存,别的标签页 / 别的调用不影响。空串 = 这次应用没记录失败 / 从没调过 / 还在 pending。宿主没过 VendorAuthn ⇒ 不改 | 开放(只读) | 🆕 RT9c(仅不受信那档) |
| `isTransportRoutePending()` → `boolean` | 🆕 `+30` 宿主设的出口路由还在等首次加载补跑(WebContents 未建)。为 true 时上一行还没有结果 | 开放(只读) | 🆕 RT9c(仅不受信那档) |
| `setPendingProxy(String?)` | 首次导航**前**预置代理（避免首导航直连泄漏） | **特权** | D |
| `setPendingIdentity(String? json)` | 首次导航前预置 per-tab 指纹身份 | **特权** | D（指纹注入冒烟） |


### 2.7b nomad.skills 调用通道（`co.arupa.plugin.ArupaPluginBridge` 静态方法, `153.0.8010.12+35` 起在件里）

> 复核 09 / KERNEL-002。App 侧 +36 复核第 06 项指出本表漏了这五个方法 —— 它们此前只写在 `01-插件对接` 那份 HTML 里, 这里补齐。语义以 HTML 那份为准, 本表只列签名与码。

| 方法 | 作用 | 授权 | 验证 |
|---|---|---|---|
| `listSkills(String extId)` → `String` | 清单里声明的技能 JSON（`"nomad": {"skills": [{"id","description"}]}`, 带点 / `nomad_` / `arupa_` 三种旧写法也认）。没登记 ⇒ **空串**; 登记了但没声明 ⇒ `[]` | 开放(只读) | 设备 ext-runtime S 组 |
| `setSkillResultObserver(SkillResultObserver o)` | 结果观察者 `onSkillResult(int requestId, String resultJson, String error)`。**每个 requestId 恰好回一次**: `error` 空串 = 成功; 否则是 `timeout` / `cancelled` / `disconnected` / 插件抛出的信息。主线程回调。**不注册就 invoke ⇒ 回 5, 不发** | 开放 | 同上 |
| `setSkillProgressObserver(SkillProgressObserver o)` | 进度观察者 `onSkillProgress(int requestId, String dataJson)`, 插件侧 `ctx.progress(data)` 触发, 可多次、只在终态之前; 不注册就静默丢弃, 结果照回 | 开放 | 同上 |
| `invokeSkill(String extId, String skill, String argsJson, int requestId, int timeoutMs)` → `int` | 发起调用: 内核把 `{type:"NOMAD_SKILL", skill, args, requestId}` 经 `runtime.connect` 长连接(name `nomad.skills`)投给该扩展 SW(没起会先唤醒)。`0` 已发起 · `1` 扩展不存在 · `2` 扩展被停用 · `3` 没有 service worker · `4` requestId 非正或与在途重复 · `5` 没注册观察者 · `6` argsJson 不是 JSON 对象 · `7` 本件未编入扩展层。**只有 0 表示之后会收到结果**; `timeoutMs ≤ 0` 取 30000 | 开放(用户授权在 App 侧先做) | 同上 |
| `cancelSkill(int requestId)` → `int` | 取消在途调用: 观察者**立刻**收 `error="cancelled"`, 内核再给插件发 `{type:"NOMAD_SKILL_CANCEL", requestId}`(插件侧 `ctx.signal` 触发)。`0` 已取消 · `1` 没有这个在途 request | 开放 | 同上 |

能力位: `arupa_kernel_supports("skills.invoke")`。桌面同名 C ABI(`arupa_kernel_*_skill*`), 两端同源。

### 2.7a 扩展改**浏览器全局出口**要用户二次确认(产品 2026-09-09 拍板)

`ArupaProxyBridge` 上的两条,**与 2.7 那条 `setTransportRoute` 是两个入口**——
两种意图的**默认值相反**:宿主自己的意图默认可信,扩展发起的默认要用户确认过。
合成一个入口默认值只能选一个,而**选错的那一侧就是静默放行**。

| 方法 | 作用 |
|---|---|
| `ArupaProxyBridge.setExtensionRouteConsent(String extensionId, boolean granted)` | 宿主弹完确认框之后**如实断言**。传 `false` 是**撤销** |
| `ArupaProxyBridge.setTransportRouteForExtension(WebContents wc, String extensionId, String socksEndpoint, String[] managedHosts)` → `int` | **扩展发起的**设出口路由,回码见下 |
| `ArupaProxyBridge.setPacScript(WebContents wc, String pacUrl)` | 宿主自己设 PAC(不走扩展判权)。宿主手里只有 ChromiumWebView 的,用 2.7 那条 `setPacScript(String?)` |
| `ArupaProxyBridge.setPacScriptForExtension(WebContents wc, String extensionId, String pacUrl, String[] managedHosts)` → `int` | **扩展发起的**设 PAC —— 与 `setTransportRouteForExtension` 走**同一道门**,回码同义 |
| `ArupaProxyBridge.getPacStatus(WebContents wc)` → `String` | 🆕 `+26` 与 2.7 的 `getPacStatusJson()` 同义 |
| `ArupaProxyBridge.getWebRtcIpPolicy(WebContents wc)` → `int` | 与 2.7 的 `getWebRtcIpPolicy()` 同义(1 / 0 / -1) |
| `ArupaProxyBridge.setTransportRoute(WebContents wc, String socksEndpoint, String[] managedHosts, String[] bypassHosts)` | 🆕 FB-P160 宿主自己设带排除名单的路由(语义同 2.7 那条 3 参) |
| `ArupaProxyBridge.setTransportRouteForExtension(WebContents wc, String extensionId, String socksEndpoint, String[] managedHosts, String[] bypassHosts)` → `int` | 🆕 扩展发起的、带排除名单;排除名单**不改变作用域判定**,不合法回 9 |

返回码(**每一档给各自的码,别在宿主侧折成 boolean**):
```
0 已应用 · 1 清单没声明 proxy(**弹框也没用**) · 2 未签名要更细作用域
3 🔴 要用户二次确认而宿主没断言过 ⇒ 去弹框 · 4 认不出作用域
5 取不到 WebContents · 6 内核没登记这个扩展 · 9 排除名单不合法(坏条目 / 与分流白名单同给)
(ChromiumWebView 门面另有: 8 = 已暂存待补跑 · -1 = 宿主没过 VendorAuthn)
```
- 🔴 **2026-09-13 订正**:此前这里把 5 写成"出口地址空",而出口传空其实是**撤销** ——
  旧实现对空串一律回 5,**扩展设的 PAC / 出口撤不掉**。现在空串 = 撤销,照样过同一道门。
- ⚠ **拒的时候一条路由都不会被应用**,上一份配置原样保留,**不会退回直连**。
- 🔴 **持久的那份真相在宿主手里**:内核那张表是**本进程内的缓存**,
  进程一起来就是空的 ⇒ 宿主要在**每次内核起来之后**重新断言(或在每次下发前 assert 一遍)。
- 🛑 **内核没有 UI 也不该有** —— 弹框、措辞、记住选择全在端侧;
  措辞照"**只是浏览器内的出口,不是系统代理**"写。

### 2.7c 问一句「哪些扩展挂了哪些事件监听」(2026-09-10)

| 方法 | 作用 |
|---|---|
| `ArupaPluginBridge.listExtListeners()` → `String` | 回一段 JSON:本进程内所有扩展当前挂着的事件监听 |

形状:

```json
{"schema":1,"supported":true,"listeners":[
  {"ext_id":"…","event":"webRequest.onBeforeRequest/s1","context":"lazy_sw",
   "worker_scope":"chrome-extension://…/","extra_info":["blocking"],
   "blocking":true,"async_blocking":false,"filter":{…},
   "honored":"yes","blocking_honored":"no"}
]}
```

**两位「我到底查没查」的标记,都是独立字段,不靠空数组兼职:**

- 顶层 `supported`:`false` = 本端**没有**这套登记(老件)。
  ⇒ 那时 `listeners: []` 的意思是**没查**,不是"没有监听"。
- 每条 `honored`(**四态**,回答"这个事件会不会来"):
  - `"yes"` 内核**自己派发**;
  - `"host"` 闸开着、归宿主答 —— **内核不知道它发不发**,问宿主自己;
  - `"no"` 面在、`addListener` 会成功、**永远不会派发**;
  - `"unknown"` 这个事件名不在本端登记里,**本端没查过**。
- 每条 `blocking_honored`(**三态**,回答另一个问题:"声明了 `blocking`,返回值算不算数"):
  - `"n/a"` 这条没声明 `blocking`/`asyncBlocking` ⇒ 不适用;
  - `"no"` 声明了,而内核**不看**它的返回值;
  - `"yes"` 声明了且内核真的会等它 —— 目前只有 `webRequest.onAuthRequired`(代理认证, `+27` 起; 此前误报 `"no"`, 同时 `honored` 也误报 `"no"`)。

🔴 **为什么这两个字段必须分开**(2026-09-10 当场吃到的亏):
`webRequest` 上有两个不同的问题 ——「事件会不会来」(会)与「blocking 的返回值算不算数」(不算)。
挤在一个格子里的后果是:`honored` 从 `"no"` 变成 `"yes"` 的那一刻,
宿主那条**本来正确**的警告(「这个插件想拦截请求,而本端只观测不拦截」)就随之丢了
—— 因为它是拿 `blocking && honored=="no"` 推出来的。
⇒ **宿主的分档明示请看 `blocking_honored=="no"`,别再自己按事件名推。**

⚠ 事件名带**子事件后缀**(`webRequest.onBeforeRequest/s1`) ——
那是上游 legacy 路径给每次 `addListener` 生成的唯一名字。
宿主按 `startsWith("webRequest.")` 匹配;**按精确等于匹配会全漏**。

### 2.7b 扩展消息用的标签页编号(FB-P149/P150, 2026-09-09)

| 方法 | 作用 | 授权 | 验证 |
|---|---|---|---|
| `setTabId(int tabId)` | 把**这个标签页的编号**告诉内核 | 不门控 | D(桌面 T5i/T5j/T5k 28/0 · 安卓真机三格全绿) |

🔴 **`chrome.tabs.sendMessage(tabId, …)` 与 `sender.tab.id` 都靠它。** 那个整数是
**宿主的命名空间**(`tabs.query` 答出去的就是它),而内核认识的只有 WebContents ——
不声明就没法把两者对上,消息投不出去。

- ⚠ **可以在 WebContents 建好之前调**:会先存成 pending,首次 `loadUrl` 建好时自动补上
  (与 `setTransportRoute` 同一条防丢法)。
- ⚠ **每次导航后重复调是安全的**:登记幂等;而 WebContents 可能因渲染进程崩溃被重建,
  "只登记一次"会让重建后的页面**安静地收不到消息**。
- ⚠ 传负数 = 解绑。内核**不猜**:没声明过的号一律投递失败并留一行日志
  (`[arupa][tab] …`),**不会**退化成"只有一个标签页那就是它"。
- ⚠ **不走 VendorAuthn 门控** —— 它只声明一个标识,不夺流量、不改身份,
  与 2.7 那一档不是同一类。

> 🔴 **代理 URL 铁律**: 用干净 `socks5://host:port`，**不要拼 `cred@host`**（chromium net 栈
> 不支持 SOCKS5 用户名密码认证 → 解析失败回落直连泄漏真实 IP）。SOCKS inbound 须 noauth。
> 未授信 vendor 调这 4 个 = 静默 no-op（不抛异常），开发机 `com.example.kernelapp` 已硬编可信。

### 2.8 UA / Cookie（#233，in-app 脱离 CDP）
| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `setUserAgent(String?)` | `WebSettings.setUserAgentString` | 开放 | D |
| `getCookies(url, Consumer<String>)` | `CookieManager.getCookie` | 开放 | D |
| `setCookie(url, cookieLine)` | `CookieManager.setCookie` | 开放 | D |
| `removeAllCookies()` | `CookieManager.removeAllCookies` | 开放 | D |
| `flushCookies()` | `CookieManager.flush` | 开放 | D |

> Cookie 是 **per-view（per-partition）** 不是全局。`setUserAgent` 下次导航生效（设完通常 `reload()`）。
> `getCookies` 异步回 `"name=value; name2=value2"`。

### 2.9 Client 回调补充（#232/#233）
| 回调 | WebView 等价 | 验证 |
|---|---|---|
| `onJsAlert/Confirm/Prompt(... JsResult)` | onJs* | D |
| `onReceivedHttpAuthRequest(HttpAuthHandler, host, realm)` | 同名 | D |
| `onGeolocationPermissionsShowPrompt(origin, GeolocationCallback)` | 同名 | D |
| `onPermissionRequest(origin, audio, video, PermissionRequestCallback)` | 同名（摄像头/麦克风） | D |
| `onReceivedIcon(iconUrl)` | 同名（传 URL 非 Bitmap） | D |

---

## 3. 典型用法

```kotlin
// 回调监听
web.setClient(object : ChromiumWebView.Client {
  override fun onPageStarted(url: String) { addressBar.setLoading(url) }
  override fun onPageFinished(url: String) { addressBar.setDone(url) }
  override fun onLoadProgressChanged(p: Int) { progressBar.progress = p }
  override fun onReceivedTitle(t: String) { tab.title = t }
  override fun onReceivedError(u: String, code: Int) { showErrorPage(u, code) }
  override fun onRenderProcessGone() { recreateWebView() }   // 必处理: 重建 view
  override fun onShowFullscreen() { enterImmersive() }
  override fun onHideFullscreen() { exitImmersive() }
  override fun onOpenNewWindow(url: String) { openTab(url) }
  override fun onFindResult(active: Int, total: Int, done: Boolean) { findBar.show(active, total) }
  override fun onShowFileChooser(id: Int, types: Array<String>, mode: Int) {
    pickFiles(types, mode) { paths -> web.onFileChooserResult(id, paths) }  // 取消传 null
  }
  override fun onDownloadStart(url: String, ua: String, cd: String, mime: String, len: Long) {
    downloadEngine.enqueue(url, mime, cd, len)               // 内核已拦截, App 自管下载
  }
})

// JS 桥
web.addJavascriptInterface(object {
  @JavascriptInterface fun token() = session.token
}, "Nomad", JavascriptInterface::class.java)

// 暗色 / 无图
web.setForceDark(true); web.setLoadImages(false)

// tab 持久化
val blob = web.saveState();  prefs.putBytes("tab1", blob)
web.restoreState(prefs.getBytes("tab1"))

// 代理（特权）—— 首导航前预置避免泄漏
web.setPendingProxy("socks5://127.0.0.1:1080")
web.setPendingIdentity(identityJson)
web.loadUrl("https://example.com")
```

---

## 4. 验证状态（诚实边界）

- **device-verified（D）**: 导航全套、saveState/restoreState、setClient 主回调、find、JS 桥、
  WebPrefs、setProxy/setTransportRoute/setPendingProxy/setPendingIdentity。
  证据: #231 多场景自检 dialog + #229 hy2 美国出口 + 指纹注入冒烟。
- **编译+符号验证但运行未单独触发（C）**: `onShowFullscreen`（需真视频全屏）、
  `onShowFileChooser`（需真 `<input type=file>` 点击）、`onDownloadStart`（需真下载触发）、
  `onRenderProcessGone`、`onOpenNewWindow`、`onSslStateChanged`、`onReceivedError`。
  这些 native delegate 已接线（WebContentsDelegateAndroid），运行期应工作，但未在设备用真页面触发过。

---

## 5. WebView 能力对照（审计后最终状态 2026-06-02）

| 能力 | WebView 等价 | 走哪条通道 / 状态 |
|---|---|---|
| 页面事件 | onPageStarted/Finished/Progress/Title/Error/RenderProcessGone | ✅ JNI Client（#231 device-verified）|
| JS 对话框 | onJsAlert/onJsConfirm/onJsPrompt | ✅ JNI（#232 device-verified，result 回传 JS）|
| HTTP 基础认证 | onReceivedHttpAuthRequest | ✅ JNI（#232 device-verified）|
| 地理位置权限 | onGeolocationPermissionsShowPrompt | ✅ JNI（#232 转发 App 决定）。其余权限 default-deny（反指纹）|
| 下载 | DownloadListener.onDownloadStart | ✅ JNI（#232 device-verified）|
| find / 状态序列化 / WebPrefs | findAllAsync / saveState / WebSettings | ✅ JNI（#231 device-verified）|
| **UA 覆盖** | setUserAgentString | ✅ **JNI** `setUserAgent(ua)`（#233 device-verified；CDP `Network.setUserAgentOverride` 亦可，远程场景用）|
| **Cookie 增删查** | CookieManager | ✅ **JNI** `getCookies/setCookie/removeAllCookies/flushCookies`（#233 device-verified，per-partition；CDP `Network.*Cookie*` 亦可，远程场景用）|
| 摄像头/麦克风权限 | onPermissionRequest | ✅ **JNI** `Client.onPermissionRequest`（#233 device-verified，App 决定 grant/deny；其余权限 default-deny 反指纹）|
| favicon | onReceivedIcon | ✅ **JNI** `Client.onReceivedIcon(url)`（#233 device-verified，传 URL 非 Bitmap）|
| **导航拦截** | shouldOverrideUrlLoading | ✅ **JNI**（#234 device-verified）`Client.shouldOverrideUrlLoading(url,isMainFrame,isRedirect,hasGesture)`→true=取消导航。内核 `ArupaNavigationThrottle`；仅主框架 + renderer-initiated（链接/JS 跳转）+ 服务器重定向触发，不拦 App 自身 loadUrl |
| **资源拦截/改写** | shouldInterceptRequest | ✅ **JNI**（#235 device-verified）`Client.shouldInterceptRequest(url,method,isForMainFrame,headers)`→返回 `ArupaWebResourceResponse`(mime/encoding/status/headers/byte[] body)替换该请求，或 null 放行。内核 `ArupaProxyingURLLoaderFactory`（代理 URLLoaderFactory，装在 `WillCreateURLLoaderFactory`）。⚠️ 回调在**网络 IO 线程**（非 UI，同 AOSP），须线程安全+快速返回 |
| 文件上传 / 全屏（运行触发） | onShowFileChooser / onShowCustomView | ✅ API+native delegate 已接线；需真实用户手势触发（无法 headless 验，标 C）|

> 🔴 **HTTP 认证接入铁律**：`HttpAuthHandler.proceed()/cancel()` 可在 `onReceivedHttpAuthRequest`
> 内**同步**调（内核已 PostTask 到下个事件循环避免 LoginDelegate 重入 CHECK → SIGTRAP，#232 device 实证此坑）。JS 对话框 result 同理可同步应答。

> **结论**: 无阻塞性 JNI 缺口。UA/Cookie/请求拦截走 CDP（#217 已实现）；页面事件/对话框/认证/
> 下载/地理位置/find/状态走 JNI（#231/#232 已 device-verified）。摄像头/favicon 当前内核路径未用，
> 需要时按既有模式半天接线 + 一次 AAR 重编。

---

## 5b. 其它对外类（不挂在 `ChromiumWebView` 上）

> 🔴 **2026-08-26 补**。此前本文只逐个交代 `ChromiumWebView`，而交付 AAR 里还有一批
> **对接侧真的会调**的类一个字都没写 —— 对接方照文档找 `setKillSwitch` / `applyIdentity`
> 会找不到，**而件里有**。闸 C55 当时只盯两个类，所以一直是绿的（见 §3 的说明）。
> 下面签名**逐个现读自交付件与源码**，不是手写。

### `co.arupa.kernel.ArupaKernel` — 进程级（全 static）

| 方法 | 做什么 |
|---|---|
| `loadNativeLib()` | 载入 native 库 |
| `initialize(Context appContext)` | 起内核。**其余方法都必须在它之后调** |
| `isLoaded()` → boolean · `isStarted()` → boolean | 两个状态分开：库载入了 ≠ 内核起来了 |
| `version()` → String | 内核版本串 |
| `clearBrowsingData(int mask, Callback<Boolean> cb)` | 按 mask 清浏览数据；未 initialize 时**回调 false 而不是静默** |
| `removeSessionCookies(Callback<Boolean> cb)` | 只清会话 cookie |
| `clearStoragePartition(String partitionName, Callback<String> cb)` | 🆕 `+16` FB-A092 **按分区名整块清数据**(卸载插件用; 主线程)。回 `{"code":N,"live_views":M}`: `0` 清完 · `1` 还有 M 个页面在用、本次未动 · `2` 名字空 · `3` 内核未起。先 `destroy()` 用过该分区的 view 再调。⚠ 从没用过的名字会先建一个空分区 |
| `setAcceptThirdPartyCookies(boolean)` · `acceptThirdPartyCookies()` → boolean | 第三方 cookie 开关 + 回读 |
| `setDoh(int mode, String dohTemplate)` | **全局 DoH / DNS 加密**（FB-A033，同 PC FB-P034）。`mode`: `0`=off(系统 DNS) · `1`=automatic(尽力升级 DoH，失败回落明文) · `2`=secure(强制 DoH，**失败不回落**)。`dohTemplate` 如 `https://dns.alidns.com/dns-query`，off 时忽略 |

### `co.arupa.proxy.ArupaProxyBridge` — 代理 / 出口路由（全 static）

| 方法 | 做什么 |
|---|---|
| `setProxy(WebContents wc, String proxyUrl)`<br>`setProxy(long webContentsPtr, String proxyUrl)` | per-view 代理。两个重载等价，拿得到 `WebContents` 就用前者 |
| `getProxy(long webContentsPtr)` → String · `clearProxy(long webContentsPtr)` | 回读 / 清除 |
| `setTransportRoute(WebContents wc, String socksEndpoint, String[] managedHosts)` | **split-tunnel 出口路由**：名单内走 socks、名单外直连。⚠ 这就是产品总纲里说的"**仍在且仍有效**"的那条接缝 —— 三方自带客户端监听本地 SOCKS5 之后，宿主用它把流量指过去 |
| `setTransportRoute(WebContents wc, String socksEndpoint, String[] managedHosts, String[] bypassHosts)` | 🆕 FB-P160 带排除名单(见 2.7 / 2.7a) |
| `setKillSwitch(boolean on)` | **断线阻断 / 防裸奔**（FB-A034） |
| `getLastErrorJson()` → String | 上一次失败的结构化说明 |

### `co.arupa.plugin.ArupaPluginBridge` — 传输引擎隔离进程的起停 · 扩展菜单码(2026-09-13 新增部分)

| 方法 / 常量 | 做什么 |
|---|---|
| `stopIsolatedEngine(Context)` → `int` | 🆕 FB-A073 **杀掉**隔离引擎进程(不是优雅退出 —— 第三方 Go 引擎跑收尾代码正是崩溃时机, FB-A068)。回杀掉的进程数。⚠ **不撤路由**:仍指向它的标签页会断流(fail-closed),要恢复上网宿主自己撤。宿主 manifest 没给 `ArupaPluginHost` 声明独立进程时**什么都不杀**(按名杀会杀掉内核自己) |
| `isIsolatedEngineRunning(Context)` → `boolean` · `isolatedEnginePid(Context)` → `int` | 🆕 隔离引擎进程此刻在不在 / 它的 pid(`-1` = 没在跑) |
| `startIsolatedEngine(Context, pluginId)` 的新行为 | 🔴 引擎已在跑时再 start = **真换**:先杀旧进程、等它退干净再起新的(原来会走「already loaded」**静默沿用旧引擎**并回 OK)。3 秒没退干净回 **`ISO_OLD_ENGINE_STILL_ALIVE`(4)**、不起新的 |
| `setExtensionSiteAccess(extensionId, site, granted)` → `String` | 🆕 `+16` FB-A093 **按站点授权 / 撤销**。回 `{"code":N,"reload_tab_ids":[…]}`: `0` 已生效 · `1` 扩展没加载、已记下 · `2` 站点写法不认识(含 `*` 一律拒) · `3` id 空。撤销压过清单任何站点权限(含 `<all_urls>`); 按主机名, 不连带子域; 内核不落盘, 起内核后在 `setExtensionRoot` 之前重放 |
| `revokedExtensionSites(extensionId)` → `String` | 🆕 `+16` 当前撤销的站点(主机名 JSON 数组, 已排序) |
| `CTXMENU_TAB_ID_NOT_INTEGER`(9) | 🆕 `dispatchContextMenuClick` 的 tabId 不是十进制整数 ⇒ 拒派发(与 action 同口径);派出去的 `tab.id` 现在是**整数**(原来是字符串) |

### `co.arupa.identity.ArupaIdentityBridge` — 反指纹身份（全 static）

| 方法 | 做什么 |
|---|---|
| `applyIdentity(WebContents wc)` / `applyIdentity(long webContentsPtr)` | 施加**全局**身份到这个 view |
| `setIdentityForTab(WebContents wc, String identityJson)` → boolean | **per-tab 身份**（P2.5）。返回是否成功 |
| `clearIdentityForTab(WebContents wc)` | 清掉这个 tab 的身份 |

### `co.arupa.kernel.VendorAuthn` — vendor 授信（全 static）

| 方法 | 做什么 |
|---|---|
| `verifyAndCache(Context appContext)` | 校验调用方签名并缓存结论 |
| `isTrusted()` → boolean | 结论 |
| `getCallerPackage()` → String · `getCallerCertSha256()` → String | 排查用：内核**认为**调用方是谁 |
| `logDenied(String apiName)` | 被拒时留痕 |

### `co.arupa.kernel.DevToolsBackend` — ADP/CDP 通道（实例，非 static）

| 方法 | 做什么 |
|---|---|
| `attach(MessageListener listener)` → `DevToolsBackend` | 接上，拿到实例 |
| `dispatch(String jsonRpcMessage)` | 发一条 CDP JSON-RPC |
| `detach()` | 断开 |

### `co.arupa.adp.ArupaAdpServer` — ADP 服务端的启停入口（全 static）

| 方法 | 做什么 |
|---|---|
| `start(...)` | 起 ADP 服务 |
| `stop()` | 停 |
| `on(...)` | 注册处理器 |
| `setFallback(...)` | 设兜底处理器（没有匹配的命令时走它） |

⚠ 只有这一个类是 ADP 那一族的对外入口，**其余 ADP 类是内部结构**（见下表）。

### `co.arupa.kernel.ArupaWebContentsDelegate` — 宿主要**实现**的回调（方向相反）

`WebChromeClient` 的等价物：内核 → 宿主。除已有回调外还有
`openNewTab` · `enterFullscreenModeForTab` / `exitFullscreenModeForTab` ·
`addMessageToConsole` · `visibleSSLStateChanged`。
⚠ 这一组是**内核调你**，不是你调内核 —— 不实现 = 那个行为没有宿主承接（如 `target=_blank` 打不开）。

### ⚪ 交付件里但**不对外**的类（写在这里，免得下一个人以为漏了）

> ⚠ 下表**逐个带上方法名**是有意的：闸 C55 认的是"这个名字在文档里被交代过"，
> 只写类名它仍会报漏。别把方法名删成省略号。

| 类 · 方法 | 为什么不对外 |
|---|---|
| **`ArupaPluginHost`** — `onBind` · `onStartCommand` | 传输引擎的**隔离进程宿主 Service**（对位桌面 `arupa_plugin_host.exe`）。这两个是 **Android 框架**调的生命周期回调，宿主只在 manifest 里声明它，不自己调 |
| **`AdpTransport`** / **`TcpTransport`** / **`LocalServerSocketTransport`** — 三者都是 `accept` · `dispose` · `endpoint`<br>**`AdpConnection`** — `close` · `output`<br>**`AdpCommandHandler`** — `handle`<br>**`ArupaAdpHandlers`** — `installAll` | ADP 服务端的**内部结构**（传输层与命令分发）。对接侧起服务走 `ArupaAdpServer`、发命令走 `DevToolsBackend`，不直接碰这些 |
| **`ConsentManager`** — `hasValidLease` · `getLeaseExpiry` · `getLeaseDaysRemaining` · `countValidLeases` · `listAllLeases` · `requestLoad` · `revokeLease` · `revokeAll` · `grantLeaseForTesting` | 知情同意租约（P3-UX1），**内核内部**流程。⚠ `grantLeaseForTesting` 顾名思义只给测试用 |
| **`LicenseClient`** — `refresh` · `refreshVendor` · `refreshCaKey` · `refreshCrl` · `getVendorCached`<br>**`ArupaLicenseBridge`** — `addTrustedCaKey` · `removeTrustedCaKey` | 授权服务器客户端与信任根维护（P3-LIC1），**内核内部**流程 |
| **`ChromiumWebView`** — `liveWebContentsCountForTesting` | 端上判据用的计数(此刻存活的 WebContents 数, 验 destroy() 真的释放)。顾名思义只给测试用, 宿主别依赖它 |
| `*Jni`（`ChromiumWebViewJni` / `ArupaMv3GuardJni` / …共 9 个） | JNI 桩，由 `jni_zero` 生成/调用，**不是 API 面**。C55 也不判它们 |

> ⚠ 上表是**判断**不是事实：哪天某个类要对外了，把它从这里挪上去并补方法。
> 闸 C55 认这一行说明（它要的是"这个名字在文档里被交代过"），所以**别删这张表**。

## 6. 关联文档
- ADP socket 控制面协议: `docs/archive/kernel-api.md`（#217 NativeBridge 用的加密 socket，非本 JNI）
- 嵌入/打包契约: `docs/guides/nomad-hardened-stack-integration.md` · [[reference_nomad_kernel_integration]]
- 授权模型决策: [[project_kernel_authz_adp_only_jni_open]]
- 设计详单: `docs/plans/chromiumwebview-api-batch.md`

### 2.10 视图缩放 / 滚动（FB-A014, FB-A007 长截图配套）

| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `zoomIn()` → boolean | `zoomIn` | 开放 | C |
| `zoomOut()` → boolean | `zoomOut` | 开放 | C |
| `zoomBy(float factor)` | `zoomBy` | 开放 | C |
| `setInitialScale(int percent)` | `setInitialScale` | 开放 | C |
| `scrollTo(int x, int y)` | `View.scrollTo` | 开放 | C |
| `scrollBy(int dx, int dy)` | `View.scrollBy` | 开放 | C |
| `getScrollY(Consumer<Integer> cb)` | `View.getScrollY()` | 开放 | C |
| `getContentHeight(Consumer<Integer> cb)` | `getContentHeight()` | 开放 | C |

> 🔴 **两处语义与 AOSP 不同, 照 AOSP 的习惯写会错**:
> ① **`getScrollY` / `getContentHeight` 是异步的** —— AOSP 那两个是**同步返回 int**,
>    我们走 `Consumer` 回调。照同步写法会拿不到值。
> ② **`scrollTo` / `scrollBy` 走的是页面滚动**(内部 `window.scrollTo/scrollBy`, CSS px),
>    不是 `View` 的视图滚动。单位是 **CSS px**, 不是设备像素。
> · `getContentHeight` 取的是 `document.documentElement.scrollHeight` = **整页文档高度**,
>   配合 `captureSnapshot` 滚动拼接长截图(FB-A007 方案 B)。

### 2.11 截图 / 打印（FB-A003, FB-A007, FB-A008）

| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `captureSnapshot(Consumer<Bitmap> cb)` | `capturePicture`(已废弃) | 开放 | C |
| `printToPdf(String? optionsJson, Callback<byte[]> cb)` | `createPrintDocumentAdapter` | 开放 | C |

> · `captureSnapshot`: `CopyFromSurface` **全尺寸原分辨率**, 异步。
>   **`null` = 无渲染面 / 截图失败** —— 调用方应 favicon 占位降级, 别当成空图。
> · `printToPdf`: chromium 自己的 print pipeline(HeadlessPrintManager), 出的是
>   **真矢量 PDF**(PDF 1.4+, 文本可选、按 web layout 自然分页), 不是把截图塞进 PDF。
>   `optionsJson` = JSON `{landscape, print_background, scale, paper_width, paper_height,
>   margin_top/bottom/left/right(英寸), page_ranges}`; 传空/`null` 走默认(Letter 8.5×11)。
>   `null` = 失败或页面未就绪。

### 2.12 存储持久化 / 语言（FB-P040, FB-A032, FB-A017）

| 方法 | WebView 等价 | 授权 | 验证 |
|---|---|---|---|
| `static setPersistentStorage(boolean)` | **无等价** | 开放 | C |
| `setPersistent(boolean)` | **无等价** | 开放 | C |
| `setPartitionName(String)` · `getPartitionName()` → String | **无等价** | 开放 | C |
| `setAcceptLanguages(String)` | **无等价**(AOSP 跟系统语言) | 开放 | C |

> 🔴 **两个持久化开关是两层, 别只调一个**:
> · `setPersistentStorage(boolean)` 是 **进程级、静态**: `true` = 普通 profile 把
>   cookie/localStorage/IndexedDB **落盘**到 App 数据目录(跨重启存活);
>   **默认/不调 = OTR 反取证**(存储全 in-memory, 退出即擦)。
>   ⚠ **必须在 App init、首个 WebContents 创建之前调** —— `IsOffTheRecord` 在 partition
>   创建时读, 之后不变。无痕模式进程**不调**它即维持 OTR。
> · `setPartitionName(String)`(🆕 `+16`, FB-A092): 指定本 view 的存储分区名, **必须在首次 `loadUrl` 之前调**;
>   同名的 view 共享一份 Cookie / 存储。不调 = 内核随机起名(旧行为)。卸载插件时拿 `getPartitionName()` 调
>   `ArupaKernel.clearStoragePartition` 整块清。建议按插件稳定标识起名(如 `"plugin:" + runtimeId`)。
> · `setPersistent(boolean)` 是 **per-view(per-partition)**: 标记本 view 的 partition
>   是否持久, 默认 `false`。**仅当全局 `setPersistentStorage(true)` 已开时才落盘生效**。
>   必须在本 view **首次 `loadUrl`(建 WebContents)之前**调。
> · `setAcceptLanguages("zh-CN,zh")`: 影响 `navigator.languages` / `navigator.language`
>   **与 `Accept-Language` 头**(三处同源)。**下次导航生效**。

---

## 3. 本文的覆盖度由闸盯着（2026-08-25 起）

**C55**(`build/roll/android-api-doc-coverage.py`)从**交付 AAR** 里现读 `ChromiumWebView`
的公有方法, 逐个在本文里找 —— 漏了会在提交时报红并点名。

🔴 **为什么要立这一道**: 2026-08-25 核对时发现 **50 个公有方法里 15 个本文只字未提**,
而它们**多半正是为回应工单才加的**:

| 漏掉的方法 | 当初的工单 |
|---|---|
| `captureSnapshot` | FB-A003「无截图 API」· FB-A007「无整页快照」 |
| `printToPdf` | FB-A008「无 printToPdf」 |
| `getOriginalUrl` | FB-A013「无 getOriginalUrl」 |
| `zoomIn/zoomOut/zoomBy/setInitialScale` | FB-A014「无 zoom API」 |
| `setAcceptLanguages` | FB-A017 · FB-A024 |
| `injectIsolatedJs` | FB-A041 · **FB-A051** |

于是形成一个闭不上的环: **对接侧查文档发现"没有" → 开工单 → 内核说"有啊, 早加了"
→ 关单 → 文档还是没有 → 下一个人再问一次。** `injectIsolatedJs` 已经是第二次了。

⚠ 本文头部那句「某个方法**现在有没有**以『实况 · AOSP 对照』为准」仍然成立, 但它解决的是
**"有没有"**, 不解决 **"怎么调"** —— 而后者正是本文的全部职责。实况页只会说
`captureSnapshot` 存在, 不会告诉你它失败时回的是 `null` 而不是空图。
**"有闸盯着有没有" ≠ "有人写怎么用"。**

⚠ 上表这批的**验证列一律填 `C`**(编译+符号验证), 因为本轮补录的依据是
**从交付件字节里读出的符号**, 不是设备实跑。谁手上有 device-verified 的证据, 谁去改成 `D` ——
**别为了表好看直接填 D**。
