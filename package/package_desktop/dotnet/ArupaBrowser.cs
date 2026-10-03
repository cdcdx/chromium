// Arupa 桌面内核 托管高层 API (CefSharp 式)。
//   var kernel = new ArupaKernel(new ArupaKernelOptions { Locale = "en-US" });
//   var view = kernel.CreateWebView(new ArupaWebViewOptions { Width=1280, Height=800,
//                 PartitionName="tab1", ProxyUrl="socks5://127.0.0.1:1080" });
//   view.Paint += (s,e) => blit(e.Pixels, e.Width, e.Height);   // BGRA top-down
//   view.PageFinished += (s,url) => ...;
//   view.LoadUrl("https://example.com");
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Arupa
{
    public sealed class PaintEventArgs : EventArgs
    {
        public IntPtr Pixels { get; init; }   // BGRA, top-down, 仅事件期间有效, 须同步拷走
        public int Width { get; init; }
        public int Height { get; init; }
        public int Length => Width * Height * 4;
        // FB-P009: 脏矩形 (物理像素, 相对帧左上角)。内核 commit 9921f1f 经 viz capture_update_rect
        //   透出真 damage; 静态帧 damage 空 → DirtyW/DirtyH=0, 宿主可整屏跳过 blit。全屏脏 = (0,0,Width,Height)。
        //   旧 dll (恒传 0,0,w,h) 下退化为整帧, 无回归。
        public int DirtyX { get; init; }
        public int DirtyY { get; init; }
        public int DirtyW { get; init; }
        public int DirtyH { get; init; }
    }

    // FB-P002 B1: JS↔宿主桥调用 (异步; 含调用方 frame origin 供 nomad:// 门控)。
    public sealed class BridgeCallArgs
    {
        public string Name { get; init; } = "";       // 桥名 (AuthBridge/...)
        public string Method { get; init; } = "";      // 方法名
        public string ArgsJson { get; init; } = "[]";  // 参数数组 JSON
        public string? FrameOrigin { get; init; }       // 调用方 frame origin (nomad:// 门控)
    }

    // FB-P002 C2/B2: 资源拦截请求 (IO 线程同步回调; nomad:// 内容供应 + 请求观测共用)。
    public sealed class InterceptRequest
    {
        public string Url { get; init; } = "";
        public string Method { get; init; } = "GET";
        // Chromium 资源类型名 ("main_frame"/"sub_frame"/"stylesheet"/"script"/"image"/
        // "xmlhttprequest" …)。仅经 should_intercept_request_ex 下发; 老 DLL 走旧回调时为空串。
        public string ResourceType { get; init; } = "";
        public bool IsForMainFrame { get; init; }
        public string? Headers { get; init; }   // \n 分隔 "K: V"
    }

    // 拦截回调的处置方式 (capi.h ArupaInterceptResponse.action)。
    // 内核只在 action == 1 上分支 (arupa_intercept_loader.cc:493), 其余值都走"合成响应替换"。
    public enum InterceptAction
    {
        ServeResponse = 0,                 // 用 InterceptResponse 合成本次响应 (默认值, 旧行为)
        ContinueWithModifiedHeaders = 1,   // 只改头: 请求继续走网络, 不合成响应
    }

    // 返回此对象 = 按 Action 处置该请求; 返回 null = 放行 (原样走网络)。
    public sealed class InterceptResponse
    {
        public int StatusCode { get; set; } = 200;
        public string MimeType { get; set; } = "text/html";
        public string? Charset { get; set; } = "utf-8";
        public string? Headers { get; set; }    // \n 分隔 "K: V"; 可空
        public byte[]? Body { get; set; }        // 响应体; 可空=空体
        public InterceptAction Action { get; set; } = InterceptAction.ServeResponse;

        // 以下四个仅在 Action == ContinueWithModifiedHeaders 时被内核读取。
        // 格式: \n 分隔; set 为 "K: V", remove 为 "K" (行尾 \r/空格内核会裁)。null = 不改。
        public string? SetRequestHeaders { get; set; }
        public string? RemoveRequestHeaders { get; set; }
        public string? SetResponseHeaders { get; set; }
        public string? RemoveResponseHeaders { get; set; }
    }

    // FB-P002 B4: 下载开始 (注册了 DownloadStarted 即拦截原生下载, 宿主接管如 aria2)。
    public sealed class DownloadStartedEventArgs : EventArgs
    {
        public string? Url { get; init; }
        public string? Mime { get; init; }
        public string? SuggestedName { get; init; }
        public long ContentLength { get; init; }
        public string? Referer { get; init; }    // 主框架 URL (防盗链)
    }

    // FB-P003: 页内查找结果。
    public sealed class FindResultEventArgs : EventArgs
    {
        public int ActiveMatch { get; init; }   // 当前命中序号 (0-based)
        public int NumMatches { get; init; }     // 总命中数
        public bool Done { get; init; }          // 本次查找是否完成
    }

    // FB-P003: JS 对话框类型。
    public enum JsDialogKind { Alert, Confirm, Prompt, BeforeUnload }

    /// <summary>窗口动作；与网页 HTML 全屏的 FullscreenChanged 独立。</summary>
    public enum BrowserCommand { ToggleFullscreen = 1, OpenDevTools = 2 }

    // ── FB-P013 第三批: 权限/认证/文件选择请求 (宿主弹原生 UI 后经 Respond* 回应) ──
    public sealed class HttpAuthEventArgs : EventArgs
    {
        public int AuthId { get; init; }
        public string? Host { get; init; }
        public string? Realm { get; init; }
    }
    public sealed class GeolocationEventArgs : EventArgs
    {
        public int GeoId { get; init; }
        public string? Origin { get; init; }
    }
    /// <summary>A批: 证书错误决策 (CertError = net::ERR_* 负值, 如 -200 CERT_COMMON_NAME_INVALID)。</summary>
    public sealed class CertificateErrorEventArgs : EventArgs
    {
        public int CertErrorId { get; init; }
        public string? Url { get; init; }
        public int CertError { get; init; }
        public string? Pem { get; init; }   // 服务器证书 PEM (可空)
    }
    public sealed class MediaPermissionEventArgs : EventArgs
    {
        public int PermId { get; init; }
        public string? Origin { get; init; }
        public bool WantAudio { get; init; }
        public bool WantVideo { get; init; }
    }
    /// <summary>文件选择 mode (blink FileChooserParams::Mode)。</summary>
    public enum FileChooserMode { Open = 0, OpenMultiple = 1, UploadFolder = 2, Save = 3 }
    public sealed class FileChooserEventArgs : EventArgs
    {
        public int ChooserId { get; init; }
        public FileChooserMode Mode { get; init; }
        public string? AcceptTypes { get; init; }   // 逗号分隔 MIME/扩展
    }

    /// <summary>FB-P024: 网页右键命中的媒体类型 (blink ContextMenuDataMediaType)。</summary>
    public enum ContextMenuMediaType { None = 0, Image = 1, Video = 2, Audio = 3, Canvas = 4, File = 5, Plugin = 6 }
    /// <summary>FB-P024: 网页右键菜单接缝。宿主据上下文弹自定义菜单 (微应用注册项);
    /// **同步**回调 — 宿主若接管须在事件内置 <see cref="Handled"/>=true 抑制内核原生菜单
    /// (随后再异步弹自己的菜单)。回调内勿重入内核控制函数。</summary>
    public sealed class ContextMenuEventArgs : EventArgs
    {
        public ContextMenuMediaType MediaType { get; init; }
        public string? LinkUrl { get; init; }        // 右键在链接上时的目标 URL (可空)
        public string? SrcUrl { get; init; }          // image/video/audio 媒体源 URL (可空)
        public string? SelectionText { get; init; }   // 选中文字 (可空)
        public string? PageUrl { get; init; }         // 当前页 URL
        public int X { get; init; }                   // 视口坐标 (OSR 下宿主按 dsf 换算)
        public int Y { get; init; }
        public bool IsEditable { get; init; }         // 右键在 input/textarea
        /// <summary>置 true = 宿主已接管 → 抑制内核原生菜单。默认 false 走内核默认。</summary>
        public bool Handled { get; set; }
    }
    /// <summary>FB-P026: Web Notification 权限请求 (宿主弹 UI 后经 RespondNotificationPermission 应答)。</summary>
    public sealed class NotificationPermissionEventArgs : EventArgs
    {
        public int NotifId { get; init; }
        public string? Origin { get; init; }
    }
    /// <summary>FB-P025: 当前页 TLS 证书详情 (供地址栏安全面板"查看证书"; schema "arupa-cert/1")。</summary>
    public sealed class CertificateInfo
    {
        public string? Subject { get; init; }
        public string? Issuer { get; init; }
        public long ValidFrom { get; init; }          // unix 秒
        public long ValidTo { get; init; }
        public List<string> San { get; init; } = new();
        public string? Protocol { get; init; }        // 如 "TLS 1.3"
        public string? Cipher { get; init; }           // 如 "AES_128_GCM"
        public string? KeyExchange { get; init; }      // 可空
        public string? Pem { get; init; }              // 整链 PEM
    }

    // FB-P003: JS 对话框请求 (宿主弹原生 UI 后经 RespondJsDialog 回传结果)。
    public sealed class JsDialogEventArgs : EventArgs
    {
        public int DialogId { get; init; }
        public JsDialogKind Kind { get; init; }
        public string? Url { get; init; }
        public string? Message { get; init; }
        public string? DefaultValue { get; init; }   // 仅 Prompt
    }

    // ── FB-P013 护城河: 传输路由 + 指纹身份 typed schema (内核 ANSWERED 权威 schema, source-verified) ──

    /// <summary>set_transport_route 路由 (FB-P007 split-tunnel, capi_impl:2091)。
    /// managed_hosts 名单内走 socks / 名单外直连; reverse_bypass 内核恒 true 不传。</summary>
    public sealed class ArupaTransportRoute
    {
        public string SocksEndpoint { get; set; } = "";        // "socks5://127.0.0.1:5508x"
        public List<string> ManagedHosts { get; set; } = new();
        public string ToJson() => JsonSerializer.Serialize(new
        {
            socks_endpoint = SocksEndpoint,
            managed_hosts = ManagedHosts,
        });
    }

    /// <summary>set_pending_identity 身份 (ParseIdentityJson 全 schema, identity.cc:111)。
    /// 经 set_pending_identity 激活 C++ 深档反指纹层 (下次导航生效)。</summary>
    public sealed class ArupaIdentity
    {
        public string Id { get; set; } = "";
        public string? Timezone { get; set; }

        /// <summary>navigator.languages 的纯列表 (如 ["en-US","en"])。⚠ 不带 q 值 —— 那是出站
        /// Accept-Language 头的形状, 混进 JS 面就是破绽。空/null = 不覆盖, 渲染器保持进程级默认。</summary>
        public IReadOnlyList<string>? Languages { get; set; }

        /// <summary>ICU/Intl locale (如 "en-US")。不给则内核从 Languages 首项派生 ——
        /// 两者必须同源, 否则 navigator.language 与 Intl.* 会互相矛盾(比"没改"更硬的反爬信号)。</summary>
        public string? Locale { get; set; }

        public ArupaFingerprint? Fingerprint { get; set; }

        public string ToJson()
        {
            var root = new Dictionary<string, object?> { ["id"] = Id };
            if (!string.IsNullOrEmpty(Timezone)) root["timezone"] = Timezone;
            if (Languages is { Count: > 0 }) root["languages"] = Languages;
            if (!string.IsNullOrEmpty(Locale)) root["locale"] = Locale;
            if (Fingerprint != null) root["fingerprint"] = Fingerprint.ToMap();
            return JsonSerializer.Serialize(root);
        }
    }

    /// <summary>指纹树 (schema (c); 注意 webGL 大写 GL / canvasV2 与 canvas 同种子)。</summary>
    public sealed class ArupaFingerprint
    {
        public (int Width, int Height, int ColorDepth, int Dpr)? Screen { get; set; }
        public (string Platform, string PlatformVersion, string Architecture, string Model, string Bitness)? Uach { get; set; }
        public (string Vendor, string Renderer)? WebGL { get; set; }
        public (bool Noise, long Seed)? Audio { get; set; }
        public (bool Noise, long Seed)? Canvas { get; set; }     // canvas + canvasV2 共用
        public (int Concurrency, int DeviceMemoryGb)? Hardware { get; set; }
        public (bool IdRandomization, long IdSeed)? MediaDevices { get; set; }

        /// <summary>geolocation 覆盖。给了就是要覆盖 (内核那个 override 布尔恒为 true —— 不想覆盖
        /// 就别给这个字段)。AccuracyMeters 建议给粗粒度值: 坐标本身是城市级的, 报一个 GPS 级精度反而矛盾。</summary>
        public (double Latitude, double Longitude, double AccuracyMeters)? Geo { get; set; }

        internal Dictionary<string, object?> ToMap()
        {
            var m = new Dictionary<string, object?>();
            if (Screen is { } s) m["screen"] = new { width = s.Width, height = s.Height, colorDepth = s.ColorDepth, devicePixelRatio = s.Dpr };
            if (Uach is { } u) m["uach"] = new { platform = u.Platform, platformVersion = u.PlatformVersion, architecture = u.Architecture, model = u.Model, bitness = u.Bitness };
            if (WebGL is { } g) m["webGL"] = new { vendor = g.Vendor, renderer = g.Renderer };   // 大写 GL
            if (Audio is { } a) m["audio"] = new { noiseEnabled = a.Noise, noiseSeed = a.Seed };
            if (Canvas is { } c)
            {
                m["canvas"] = new { noiseEnabled = c.Noise, noiseSeed = c.Seed };
                m["canvasV2"] = new { noiseEnabled = c.Noise, noiseSeed = c.Seed };   // 同种子
            }
            if (Hardware is { } h) m["hardware"] = new { concurrency = h.Concurrency, deviceMemoryGb = h.DeviceMemoryGb };
            if (MediaDevices is { } d) m["mediaDevices"] = new { idRandomizationEnabled = d.IdRandomization, idSeed = d.IdSeed };
            if (Geo is { } geo) m["geo"] = new { @override = true, latitude = geo.Latitude, longitude = geo.Longitude, accuracy = geo.AccuracyMeters };
            return m;
        }
    }

    /// <summary>set_web_prefs 偏好 (内核 ANSWERED schema (a): 仅此 5 键全 bool, null=不改)。
    /// ⚠ DoH/safebrowsing 不在此 (DoH 走 transport/DNS 专项)。</summary>
    public sealed class ArupaWebPrefs
    {
        public bool? ForceDark { get; set; }            // 强制深色 (夜间模式 applier)
        public bool? LoadImages { get; set; }           // 载图 (省流)
        public bool? JavascriptEnabled { get; set; }
        public bool? DomStorage { get; set; }
        public bool? MultipleWindows { get; set; }
        public string ToJson()
        {
            var m = new Dictionary<string, object?>();
            if (ForceDark is { } a) m["force_dark"] = a;
            if (LoadImages is { } b) m["load_images"] = b;
            if (JavascriptEnabled is { } c) m["javascript_enabled"] = c;
            if (DomStorage is { } d) m["dom_storage"] = d;
            if (MultipleWindows is { } e) m["multiple_windows"] = e;
            return JsonSerializer.Serialize(m);
        }
    }

    public sealed class ArupaKernelOptions
    {
        public string? UserDataDir { get; set; }       // null = incognito
        public string Locale { get; set; } = "en-US";
        public string? PakDir { get; set; }
        public bool FingerprintHardening { get; set; } = true;
        /// <summary>FB-P011: OSR 设备缩放。0/1 = 不强制(dpr=1); 1.5 = 150% DPI 物理出帧+锐利
        /// (宿主须配合物理出帧 Resize(w*dsf, h*dsf))。默认 0 = 不强制。</summary>
        public double DeviceScaleFactor { get; set; }
    }

    public sealed class ArupaWebViewOptions
    {
        public string? PendingContentsToken { get; set; }

        public int Width { get; set; } = 1280;
        public int Height { get; set; } = 800;
        public string? PartitionName { get; set; }     // per-view 隔离 (Cookie/cache/proxy)
        public bool OffTheRecord { get; set; } = true;
        public string? ProxyUrl { get; set; }       // per-view 代理 (创建期设)
        // FB-P002 B4: 拦截原生下载交宿主 (内核"注册即取消"语义, 须创建期决定)。开后订阅
        // DownloadStarted, 所有下载被内核取消并抛事件给宿主路由 (aria2)。PC 恒取消语义设 true。
        public bool InterceptDownloads { get; set; }
    }

    /// <summary>FB-P020: 插件加载结果 (load_plugin 回传)。宿主据 SocksEndpoint + ManagedHosts
    /// 调 view.SetTransportRoute 把 view 路由进插件 (名单内走代理/外直连)。</summary>
    public sealed class ArupaPluginInfo
    {
        public string PluginId { get; init; } = "";
        public string SocksEndpoint { get; init; } = "";   // socks5://127.0.0.1:PORT
        public IReadOnlyList<string> ManagedHosts { get; init; } = Array.Empty<string>();
    }

    public sealed class ArupaKernel : IDisposable
    {
        /// <summary>
        /// 读**本线程**最近一次失败的结构化说明; 没有则 null。形状 {code,what,why,next,detail}。
        ///
        /// 🔴 三条用法边界(照 capi.h 的错误通道语义, 别当通用错误码使):
        ///   · **thread_local** —— 谁调的谁读。跨线程读到的是那个线程的记录, 多半是 null。
        ///     ⚠ 对 async 宿主尤其要小心: `await` 之后的续体常常已经换线程了。
        ///   · **只在紧接着的失败之后读才有意义**。
        ///   · **null 不等于"上一次成功了"** —— 只是本线程没记录过失败(并非每个函数都写它)。
        /// </summary>
        public static string? LastErrorJson()
        {
            IntPtr p = IntPtr.Zero;
            try
            {
                p = Interop.arupa_last_error_json();
                return p == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(p);
            }
            catch { return null; }
            finally { if (p != IntPtr.Zero) Interop.arupa_free(p); }
        }

        /// <summary>Run a dedicated Mac kernel host from its synchronous Main entry.
        /// The callback runs on a worker; native Chromium owns the process main loop.
        /// This is a once-per-process API and requires ABI 1.22.</summary>
        public static int RunMacHost(Func<int> hostMain)
        {
            ArgumentNullException.ThrowIfNull(hostMain);
            if (!OperatingSystem.IsMacOS()) return hostMain();
            if (Interop.arupa_kernel_supports("mac.main_loop") != 1)
                throw new NotSupportedException("Mac KernelHost requires a matching ABI 1.22+ delivery with mac.main_loop.");
            System.Runtime.ExceptionServices.ExceptionDispatchInfo? failure = null;
            Interop.HostMainCb callback = _ => {
                try { return hostMain(); }
                catch (Exception e) {
                    failure = System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(e);
                    return -1;
                }
            };
            int result = Interop.arupa_kernel_run_mac_host(callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            failure?.Throw();
            if (result < 0)
                throw new InvalidOperationException($"Mac main-loop host failed: {result}");
            return result;
        }

        private IntPtr _handle;
        public ArupaKernel(ArupaKernelOptions? opts = null)
        {
            opts ??= new ArupaKernelOptions();
            var cfg = new Interop.KernelConfig
            {
                abi_major = Interop.ABI_MAJOR,
                user_data_dir = opts.UserDataDir,
                locale = opts.Locale,
                pak_dir = opts.PakDir,
                enable_fingerprint_hardening = opts.FingerprintHardening ? 1 : 0,
                device_scale_factor = opts.DeviceScaleFactor,   // FB-P011 (0=不强制)
            };
            int r = Interop.arupa_kernel_create(in cfg, out _handle);
            if (r != 0)
                throw new InvalidOperationException($"arupa_kernel_create failed: {r}");
        }

        public ArupaWebView CreateWebView(ArupaWebViewOptions? opts = null)
            => new ArupaWebView(_handle, opts ?? new ArupaWebViewOptions());

        /// <summary>预热一个空闲渲染进程 (spare renderer): kernel 创建后调一次, 首次导航直接领用 → 首帧/首字节更快。
        /// 失败静默 (非致命; 内核会按需起渲染进程)。内核 06-24+ dll 才有此导出。</summary>
        public void WarmupRenderer()
        {
            try { Interop.arupa_kernel_warmup_renderer(_handle); } catch { /* 旧 dll 无此导出 / 失败不致命 */ }
        }

        // ── 插件加载 (FB-P020): 签名门控 native 传输引擎 (happyview/fastview) ──────────
        /// <summary>设签名插件 bundle 根目录 (&lt;root&gt;/&lt;id&gt;.arupa-plugin/...)。</summary>
        /// <remarks>🔴 这是**传输插件**根, 不是 MV3 扩展根 —— 装扩展请用 <see cref="SetExtensionRoot"/>。</remarks>
        public void SetPluginRoot(string dir)
            => Interop.arupa_kernel_set_plugin_root(_handle, dir);

        // ── FB-P104: 扩展 API 转宿主 ───────────────────────────────────────────
        //
        // 🔴 委托必须**活到进程结束** —— 它不是一次性回调, 是注册一次之后每次插件调
        //   `chrome.tabs.*` 都会进来。局部变量会被 GC 掉, 然后内核回调时野跳。
        //   这里用 static 字段保活(比 GCHandle 更难写错: 没有"忘了 Free"也没有"提前 Free")。
        private static Interop.ArupaExtApiCb? s_extApiCb;
        private static Action<int, string, string, string>? s_extApiHandler;

        /// <summary>FB-P104: 接管插件调用的 <c>chrome.tabs</c> / <c>action</c> /
        /// <c>contextMenus</c> / <c>sidePanel</c> / <c>permissions</c>。
        /// <para>这些 API 的语义**内核答不出来**(哪些标签页存在、侧栏怎么渲、右键菜单挂哪 ——
        /// 都在你这一侧), 所以内核只把调用如实转出来。不接管的话, 插件会收到一句明确的
        /// 「宿主未接管这条 API」错误 —— 那**不是崩**, 插件 catch 得到, 但功能确实没有。</para>
        /// <para>回调参数: <c>(requestId, extensionId, name, argsJson)</c>。
        /// <c>name</c> 形如 <c>"tabs.query"</c>; <c>argsJson</c> 是**已经过 schema 校验**的
        /// 参数数组 JSON。处理完(同步异步都行)**必须**调一次
        /// <see cref="RespondExtensionApi"/> —— 不答的话插件那边永远 pending
        /// (内核有 10 秒超时兜底会替你回一个错误, 但那是兜底不是设计)。</para>
        /// <para>传 <c>null</c> = 撤销接管。⚠ 进程级、只在 UI 线程调。</para></summary>
        public void SetExtensionApiHandler(Action<int, string, string, string>? handler)
        {
            s_extApiHandler = handler;
            if (handler == null)
            {
                try { Interop.arupa_kernel_set_ext_api_handler(null, IntPtr.Zero); }
                catch (EntryPointNotFoundException) { }
                s_extApiCb = null;
                return;
            }
            s_extApiCb = (user, requestId, extId, name, argsJson) =>
            {
                // ⚠ 三个指针只在回调期有效 —— 先拷成托管 string 再交出去。
                var h = s_extApiHandler;
                if (h == null) { return; }
                h(requestId,
                  Marshal.PtrToStringUTF8(extId) ?? string.Empty,
                  Marshal.PtrToStringUTF8(name) ?? string.Empty,
                  Marshal.PtrToStringUTF8(argsJson) ?? "[]");
            };
            try { Interop.arupa_kernel_set_ext_api_handler(s_extApiCb, IntPtr.Zero); }
            catch (EntryPointNotFoundException)
            {
                // 旧 dll 没有这个导出。不抛 —— 与 SetExtensionRoot 同一处理; 后果是那几个
                // 命名空间的调用停在内核的「宿主未接管」上, 内核日志里会说。
                s_extApiCb = null;
            }
        }

        /// <summary>FB-P104: 答复一次 <see cref="SetExtensionApiHandler"/> 收到的调用。
        /// <para>二选一: <paramref name="resultJson"/> 非空 = 成功, 内容是**回调参数数组**的
        /// JSON(例: <c>chrome.tabs.query</c> 回一个数组, 所以是 <c>"[[{...tab...}]]"</c>);
        /// <paramref name="error"/> 非空 = 失败, 内容会**原样出现在插件那边的错误里**, 请写人话。</para>
        /// <para>⚠ 同一个 requestId 只答一次。超时之后再答会被内核如实记一条 WARNING 并丢弃。</para></summary>
        public void RespondExtensionApi(int requestId, string? resultJson, string? error)
        {
            try { Interop.arupa_kernel_respond_ext_api(requestId, resultJson, error); }
            catch (EntryPointNotFoundException) { }
        }

        /// <summary>FB-P133: 把一个事件发给某个扩展的 service worker(**内核 → 扩展**方向)。
        /// <para>🔴 **别拿它发 <c>action.onClicked</c>** —— 那条走
        /// <see cref="DispatchActionClick"/>, 带**手势断言**与 popup 语义
        /// (有 popup 时按 MV3 不该派发 onClicked)。用错的表现是"点了没反应"
        /// 或"popup 与 onClicked 同时来", 两种都难查。</para>
        /// <para><paramref name="argsJson"/> 是 JSON **数组** = 监听器的实参列表
        /// (例: <c>tabs.onRemoved</c> 是 <c>[42, {"windowId":1,"isWindowClosing":false}]</c>)。
        /// 空参传 <c>"[]"</c> —— **不是 null, 不是空串**。</para>
        /// <para>返回码 —— 🔴 **每一种都说得出原因, 别折成 bool**:
        /// <c>0</c> 成功 ·
        /// <c>1</c> 这个事件名**不在宿主可发的边界内**(边界防的是宿主伪造
        /// <c>runtime.onInstalled</c> / <c>cookies.onChanged</c> 这类**内核自己该发**的事件,
        /// 扩展分不出真假) ·
        /// <c>2</c> argsJson 不是合法 JSON 数组 ·
        /// <c>3</c> 该扩展的 SW 现在不在 —— <b>不是"发了"</b>, 宿主要如实处理, 别当成功 ·
        /// <c>4</c> extensionId 为空(**没有广播语义**)。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 返回 <c>-1</c>(**与码 0..4 不冲突**),
        /// 表示"这个内核件根本答不了这个问题" —— 与"发失败"是两回事, 别合起来读。</para></summary>

        public int FireExtensionEvent(string extensionId, string eventName, string argsJson)
        {
            try
            {
                return Interop.arupa_kernel_fire_ext_event(extensionId, eventName, argsJson);
            }
            catch (EntryPointNotFoundException)
            {
                // 🔴 回 -1 而不是某个失败码: "这个 dll 没有这个能力"与"发了但失败了"
                //   是两个问题。折成同一个码的话, 宿主会去查事件名/参数, 而真相是件太旧。
                return -1;
            }
        }

        /// <summary>权限项的**一份真相源**:名字 / 判定 / 能力映射 / 给用户看的那句话。
        /// <para>🔴 由内核出而不是各处自己写一份 —— 那份说明至少要用在**三处**
        /// (授权弹框 · 权限管理页 · 商店的"这个插件要什么权限"预览),
        /// 各处一份的话同一条权限在两个地方的说法会不一样。</para>
        /// <para>返回 JSON:<c>{"version":"…","count":N,"withNote":M,"items":[…]}</c>。
        /// <c>version</c> 是**内容哈希** —— 缓存之后拿它对一下就知道还对不对得上
        /// (为性能缓存一次而不校验, 那份缓存就是又一个会漂的副本, 只是漂得慢一点)。</para>
        /// <para>⚠ 某一项的 <c>note</c> 为 <c>null</c> = **内核没有这句话**,
        /// <b>不是</b>"这条权限无害"。目前只有指纹风险那一族写了文案。
        /// **别拿名字顶替说明**发给用户 —— 缺就如实缺。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 返回 <c>null</c>。</para></summary>
        public string? GetPermissionDescriptions()
        {
            try
            {
                return Interop.TakeOwned(Interop.arupa_kernel_get_permission_descriptions());
            }
            catch (EntryPointNotFoundException)
            {
                return null;
            }
        }

        /// <summary>断言"用户已经为这个扩展**确认过**改浏览器全局代理出口这件事"。
        /// <para>🔴 产品 2026-09-09 拍板: 扩展改全局出口**要用户二次确认, 由端侧弹框**。
        /// 内核只**判**这次算不算数, <b>弹框、措辞、记住选择都是端侧的事</b>。</para>
        /// <para>🔴 <b>内核不猜用户点了什么</b> —— 没调过这个方法 ⇒ 按"没同意"算
        /// (fail-closed), 而 <see cref="ArupaWebView.SetTransportRouteForExtension"/>
        /// 会回码 3(要确认)。</para>
        /// <para>⚠ 传 <c>false</c> 是**撤销**(用户改主意 / 扩展被重装)——
        /// 撤销要能表达, 否则"同意过一次"就等于**永久授权**。</para>
        /// <para>⚠ 影响面**只是浏览器内的全局出口**, 不是系统代理 ——
        /// 确认框的措辞要照这个写: 写重了用户会拒, 写轻了用户不知道在同意什么。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 返回 <c>false</c>(**这次断言没生效**)。</para></summary>
        public bool SetExtensionRouteConsent(string extensionId, bool granted)
        {
            try
            {
                Interop.arupa_kernel_set_extension_route_consent(extensionId,
                                                                 granted ? 1 : 0);
                return true;
            }
            catch (EntryPointNotFoundException)
            {
                // 🔴 回 false 而不是静默成功: 宿主会以为自己断言过了,
                //   而内核那边一直按"没同意"算。
                return false;
            }
        }

        /// <summary>断言某个扩展是**用户自己加载的开发包**(旁加载 / 开发者模式)。
        /// <para>🔴 这条决定 <c>nativeMessaging</c> 那一族的准入档位:
        /// 未签名的包**只有**被这样断言过才放行(风险由用户承担);
        /// 商店装的未签名包一律拒。</para>
        /// <para>🔴 <b>内核不猜来源</b> —— 它只看得到"有个目录里有个 manifest",
        /// 看不出是用户拖进来的还是商店下下来的。<b>不调 ⇒ 按 false 算(fail-closed)</b>,
        /// 也就是那一档永远走不通。</para>
        /// <para>⚠ <b>谎报 true 等于把一个还没做的入口当成做了</b> ——
        /// 若产品还没有"加载开发包"这个入口, 如实传 false 才是准确, 不是保守。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 返回 <c>false</c>(**调用没有生效**),
        /// 与"调了且设成了"分得开。</para></summary>
        /// <returns>true = 这次断言真的送到内核了; false = 这个内核件没有这个导出。</returns>
        public bool SetExtensionUserLoaded(string extensionId, bool userLoaded)
        {
            try
            {
                Interop.arupa_kernel_set_extension_user_loaded(extensionId,
                                                               userLoaded ? 1 : 0);
                return true;
            }
            catch (EntryPointNotFoundException)
            {
                // 🔴 回 false 而不是静默成功: "这个 dll 没有这个能力"必须让调用方知道 ——
                //   否则宿主会以为自己断言过了, 而内核那边一直是 fail-closed。
                return false;
            }
        }

        /// <summary>设 **MV3 扩展**根目录: 该目录下每个带 manifest.json 的子目录算一个扩展,
        /// renderer 进程 ready 时内核自动逐个下发 (宿主不需要逐个调什么)。
        /// <para>🔴 与 <see cref="SetPluginRoot"/> **是两个东西, 别传同一个目录**:
        /// 那个是传输插件根 (签名 bundle), 这个是解包后的扩展目录。传混了的后果见 FB-A050:
        /// 扩展被拿去传输目录里找, **零下发, 且不报错**。内核**不会**在没设扩展根时
        /// 拿传输根顶替 —— 它宁可一个都不发并在日志里说清楚。</para>
        /// <para>🔴 **什么时候调都行, 不必赶在开页面之前**: 根设进来时若已经有 ready 的
        /// renderer, 内核会回头补发给它们 (FB-A045 的时序坑)。重复设同一个值是空操作。</para>
        /// <para>内核 2026-08-25+ (ABI MINOR 11) 的 dll 才有此导出; 旧 dll 上静默失败 ——
        /// 与 <see cref="WarmupRenderer"/> 同一处理, 但**后果不同**: 这个静默失败意味着
        /// **扩展一个都装不上**, 所以内核日志里会有一条 "扩展根未设置" 的 WARNING 兜底。</para></summary>
        public void SetExtensionRoot(string dir)
        {
            try { Interop.arupa_kernel_set_extension_root(_handle, dir); }
            catch (EntryPointNotFoundException)
            {
                // 旧 dll (< MINOR 11) 没有这个导出。不抛 —— 宿主对内核版本无能为力,
                // 抛了只会让 initialize 整个失败; 扩展装不上会由内核日志报出来。
            }
        }

        /// <summary>启用/禁用**单个** MV3 扩展 (内核 2026-08-25+ / ABI MINOR 12)。
        /// <para>禁用 = ① 之后的扫描跳过它; ② 对所有已 ready 的 renderer 发卸载 ⇒ **立刻失效**,
        /// 不用等下一个页面; ③ 从"已下发"记录里摘掉, 重新启用时发得回去。</para>
        /// <para>🔴 别用"把目录移出扩展根"代替 —— 那**做不到立刻生效**: 已经下发过的 renderer 里
        /// 那个扩展仍然活着。用户在插件管理页点"禁用"却发现它还在跑, 就是这个差别。</para>
        /// <para>⚠ 禁用状态**只活在内核进程内, 不落盘** —— 用户的选择由宿主的插件管理页持久化,
        /// 宿主每次起来后按自己的记录重设一遍。</para>
        /// <para>⚠ <paramref name="extensionId"/> 是 MV3 那个 32 字符 id, **不是目录名**:
        /// manifest 里有 <c>key</c> 时 id 从 key 派生, 与目录名可以不同。</para></summary>
        public void SetExtensionEnabled(string extensionId, bool enabled)
        {
            try { Interop.arupa_kernel_set_extension_enabled(_handle, extensionId, enabled ? 1 : 0); }
            catch (EntryPointNotFoundException)
            {
                // 旧 dll (< MINOR 12) 没有这个导出 —— 静默失败意味着**禁用不生效**,
                // 宿主该按内核版本决定要不要在 UI 上把开关置灰, 而不是假装它成功了。
            }
        }

        /// <summary>列出内核**已经认识的**扩展 (解析成功并下发过至少一次)。
        /// <para>🔴 为什么需要它：<see cref="SetExtensionEnabled"/> 要的是 MV3 那个 32 字符 id，
        /// 而 manifest 里有 <c>key</c> 时 id **从 key 派生、与目录名不同**（实测 TunnelBear 就是这种）
        /// ⇒ 按目录名猜就会**静默不生效**。装完扩展调一次这个，拿权威 id 再去启停。</para>
        /// <para>⚠ **它回答的是"内核已经认识哪些"，不是"当前根下有哪些"** —— 两条都别当判据用错：
        /// ①"已登记" ≠ "当前启用"（被禁用的仍在表里）；
        /// ②"已登记" ≠ "在当前扩展根下"——**换了根之后，旧根那些扩展仍在表里**，
        /// 因为登记发生在"解析成功并下发过至少一次"那一刻，而这张表**只增不减**。
        /// 🔴 第二条是 2026-08-25 PC 侧实撞的：反测拿"换成空根之后列表应该变空"当判据，当场红 ——
        /// 判据本身错了，不是内核错了。要判"当前根下有什么"请自己扫目录。</para>
        /// <returns>(id, dir) 列表；旧 dll (&lt; MINOR 12) 或没有扩展时返回空列表。</returns></summary>
        public List<(string Id, string Dir)> ListExtensions()
        {
            var result = new List<(string, string)>();
            IntPtr p;
            try { p = Interop.arupa_kernel_list_extensions(_handle); }
            catch (EntryPointNotFoundException) { return result; }   // 旧 dll: 与"没有扩展"同形
            if (p == IntPtr.Zero) return result;
            string? json;
            try { json = Marshal.PtrToStringUTF8(p); }
            finally { Interop.arupa_free(p); }
            if (string.IsNullOrEmpty(json)) return result;
            using var doc = JsonDocument.Parse(json);
            foreach (var el in doc.RootElement.EnumerateArray())
            {
                string id = el.TryGetProperty("id", out var i) ? (i.GetString() ?? "") : "";
                string dir = el.TryGetProperty("dir", out var d) ? (d.GetString() ?? "") : "";
                if (id.Length > 0) result.Add((id, dir));
            }
            return result;
        }

        /// <summary>重扫扩展根, 把新发现的扩展补发给**已经开着的页面** (内核 2026-08-25+ / ABI MINOR 12)。
        /// <para>🔴 什么时候需要: 刚往扩展根里装了一个新扩展, 想让它立刻生效。内核的自动下发是在
        /// renderer 进程 ready 那一刻扫一次盘 —— **不是文件系统监听** —— 所以不调它的话,
        /// 新扩展要等**下一个 renderer 进程**起来才被扫到 (⚠ 同一个 renderer 里继续导航不会重扫)。</para>
        /// <para>⚠ 已下发过的不会重复下发, 多调几次是安全的; 被禁用的不会被重扫带回来。</para>
        /// <para>⚠ 2026-08-25 修: 这段说明原先写在 <see cref="ListExtensions"/> 的 summary 前面
        /// 且没闭合 ⇒ 它实际挂在了那个方法上, 而本方法一句注释都没有 —— IDE 里悬停看到的是错的那份。</para></summary>
        public void RescanExtensions()
        {
            try { Interop.arupa_kernel_rescan_extensions(_handle); }
            catch (EntryPointNotFoundException) { /* 旧 dll (< MINOR 12): 只能等下一个 renderer */ }
        }

        /// <summary>一个扩展的 background service worker 处在什么状态 (ABI MINOR 13, FB-P090)。</summary>
        public enum ExtensionSwState
        {
            /// <summary>内核**不认识这个 id** —— 没装, 或者**清单在解析那一层就没过**。
            /// <para>🔴 FB-P090 的原病就在这一档: 带 <c>background.service_worker</c> 的清单
            /// 因为 <c>chrome-extension</c> 没被注册成 scheme 而 <c>Extension::Create</c> 整个失败,
            /// 于是它连 id 都不存在 —— 而当时唯一的表现是"<see cref="ListExtensions"/> 里没有它"。</para>
            /// <para>⚠ 旧 dll (&lt; MINOR 13) 没有这个导出, 也归到这一档 —— 判据用的时候要知道
            /// "问不出来"与"内核不认识"在这里是同形的。</para></summary>
            Unknown = -1,

            /// <summary>清单里**有** SW, **但没注册上** —— 装是装上了, background 一行没跑。
            /// <para>真因看内核日志的 <c>[arupa][sw] 注册失败 … status=&lt;n&gt;</c>。</para></summary>
            NotRegistered = 0,

            /// <summary>清单里有 SW, **且已经注册成功**(它在跑, 或随时会被叫起来)。</summary>
            Registered = 1,

            /// <summary>清单里**本来就没有** <c>background.service_worker</c> —— 内容脚本型扩展, 正常。
            /// <para>🔴 这一档必须与 <see cref="NotRegistered"/> 分开: 合成一个 false 的话,
            /// "这个扩展本来就没有 background"与"它的 SW 起不上来"长得一模一样。</para></summary>
            NoServiceWorker = 2,
        }

        /// <summary>问这个扩展的 background service worker 到底起没起 (内核 2026-08-25+ / ABI MINOR 13)。
        /// <para>🔴 为什么需要它: 在它之前, "带 background 的扩展装不上/不工作"这件事在宿主侧
        /// **完全是静默的** —— FB-P090 那次对接侧是拿三份只差一个字段的清单做对照才定位到的。</para>
        /// <para>⚠ <paramref name="extensionId"/> 用 <see cref="ListExtensions"/> 拿的权威 id,
        /// 别按目录名猜(带 <c>key</c> 的扩展 id 与目录名无关)。</para>
        /// <para>⚠ 本调用**不改变任何状态**, 随时可调。</para></summary>
        public ExtensionSwState ExtensionServiceWorkerState(string extensionId)
        {
            try
            {
                int v = Interop.arupa_kernel_extension_sw_state(_handle, extensionId);
                // 内核只会回这四个值; 真回了别的就当"问不出来", 别硬转成一个看起来确定的答案。
                return v == 0 || v == 1 || v == 2
                    ? (ExtensionSwState)v
                    : ExtensionSwState.Unknown;
            }
            catch (EntryPointNotFoundException)
            {
                // 旧 dll (< MINOR 13): 问不出来。**不是**"没有 SW", 也**不是**"起来了"。
                return ExtensionSwState.Unknown;
            }
        }

        /// <summary>点一下工具栏按钮之后, 内核该怎么答 (FB-P121)。</summary>
        /// <remarks>
        /// 🔴 <b>八个都要逐个分支, 别折叠成 bool</b>。尤其
        /// <see cref="OpenPopupInstead"/> <b>不是失败</b> —— 它是"这个扩展有 popup,
        /// 请你去开 popup", 而且那一支<b>也已经授了 activeTab</b>。
        /// <para>把它折进"失败"是 FB-P116 那个错的同族: 那次把判定码 7「走替代」折成"拒",
        /// 结果权限被摘、扩展的 service worker 顶层直接崩。</para>
        /// </remarks>
        public enum ActionClickResult
        {
            /// <summary>onClicked 已送进该扩展的 service worker。</summary>
            Dispatched = 0,
            /// <summary>该扩展有 popup ⇒ <b>宿主去开 popup</b>; 按 MV3 语义<b>不派发 onClicked</b>
            /// (两样都做是错的)。⚠ activeTab <b>已经授了</b>, popup 里的脚本同样要读当前页。</summary>
            OpenPopupInstead = 1,
            ExtensionNotFound = 2,
            ExtensionDisabled = 3,
            ActionNotFound = 4,
            /// <summary>没有用户手势。内核<b>不猜</b>手势 —— 由宿主如实断言。</summary>
            NoUserGesture = 5,
            TabNotFound = 6,
            /// <summary>该扩展的 SW 现在不在。<b>不是"发了"</b> —— 要如实处理, 别当成成功。</summary>
            ServiceWorkerUnavailable = 7,
            /// <summary>🔴 <paramref name="tabId"/> 不是<b>十进制整数的字符串</b> ⇒ 这次派发被拒
            /// (FB-P123, 2026-09-08)。
            /// <para>由来: 标准 <c>chrome.tabs.Tab.id</c> 是<b>整数</b>, 而内核此前把你们给的
            /// 字符串原样塞进 <c>tab.id</c>。扩展<b>拿得到</b>那个对象, 一转手喂给
            /// <c>tabs.sendMessage</c> / <c>scripting.executeScript</c> 就被参数校验当场拒 ——
            /// 实测派进去的真商店包 <b>19.4%</b> 死在这一条上。</para>
            /// <para>⚠ 内核<b>不发 0/-1 兜底</b>: 那会让扩展去操作<b>别的标签页</b>。</para>
            /// <para>⚠ 与 <see cref="TabNotFound"/> 分开是刻意的: 6 = "你们没给",
            /// 8 = "给了、格式不对" —— 两条排查路完全不同。</para></summary>
            TabIdNotInteger = 8,
            /// <summary>旧 dll 里没有这个导出 ⇒ <b>问不出来</b>。不是成功也不是失败。</summary>
            Unavailable = -1,
        }

        /// <summary>把一次工具栏点击派发给扩展的 <c>chrome.action.onClicked</c> (FB-P121)。</summary>
        /// <remarks>
        /// 🔴 <b>为什么必须走内核</b>: 监听器活在扩展自己的 service worker 里, 宿主拿不到
        /// 那个 JS 函数。按插件 id 写死业务逻辑的话, 用户"看起来能用"而<b>插件代码一次都没跑</b>
        /// —— 那不是 MV3 Action 支持, 是仿造。
        /// <para>⚠ <paramref name="userGesture"/> <b>缺省按拒处理</b>。你们是可信嵌入方:
        /// 手指点的、还是产品的 AI 点的, 都算你们的手势 —— 但要<b>如实</b>断言。</para>
        /// <para>🔴 <paramref name="tabId"/> <b>必须是十进制整数的字符串</b>(如 <c>"42"</c>) ——
        /// 它会作为<b>整数</b> <c>tab.id</c> 交到扩展手里(标准如此)。不是整数 ⇒ 回
        /// <see cref="ActionClickResult.TabIdNotInteger"/> 并拒这次派发, 见 FB-P123。
        /// ⚠ 别用下标: 下标会随开关标签页移位, 而 activeTab 的授权绑在它上面 ⇒
        /// 那是<b>撤销撤错对象</b>, 比撤不掉更坏。</para>
        /// <para>⚠ <paramref name="navigationId"/> 是 activeTab 绑定的 <b>document</b> 维度,
        /// <b>每次导航必须换新值</b>; 而且撤销时要用<b>同一个值</b>调
        /// <see cref="RevokeActiveTabDocument"/> —— 两处各编一个字符串, 撤销就对不上。</para>
        /// </remarks>
        public ActionClickResult DispatchActionClick(string extensionId, string tabId,
                                                     string navigationId, string url,
                                                     string title, bool userGesture)
        {
            string ctx = "{\"tabId\":" + JsonStr(tabId)
                       + ",\"navigationId\":" + JsonStr(navigationId)
                       + ",\"url\":" + JsonStr(url)
                       + ",\"title\":" + JsonStr(title)
                       + ",\"userGesture\":" + (userGesture ? "true" : "false") + "}";
            try
            {
                int v = Interop.arupa_kernel_dispatch_action_click(extensionId, ctx);
                // 内核只会回 0..8; 回了别的就当"问不出来", 别硬转成一个看起来确定的答案
                // (与 ExtensionServiceWorkerState 同一条纪律)。
                // ⚠ 上界 2026-09-08 由 7 提到 8(FB-P123 新增 TAB_ID_NOT_INTEGER) ——
                //   忘了提的表现是**新码被当成"问不出来"**, 而那是一个看起来无害的降级:
                //   宿主会以为内核太旧, 而真相是它刚拒了这次派发并说明了理由。
                return (v >= 0 && v <= 8) ? (ActionClickResult)v : ActionClickResult.Unavailable;
            }
            catch (EntryPointNotFoundException)
            {
                return ActionClickResult.Unavailable;
            }
        }

        /// <summary>activeTab 撤销 —— <b>三个时机都要调</b>: ①导航 ②标签页关闭 ③扩展停用/卸载。</summary>
        /// <remarks>⚠ 内核<b>不监听</b>宿主的标签页生命周期, 漏调不会报错, 但插件会在
        /// 它不该有权限的页面上继续有权限。</remarks>
        public void RevokeActiveTabDocument(string tabId, string navigationId)
        {
            try { Interop.arupa_kernel_revoke_active_tab_document(tabId, navigationId); }
            catch (EntryPointNotFoundException) { }
        }

        /// <inheritdoc cref="RevokeActiveTabDocument"/>
        public void RevokeActiveTabTab(string tabId)
        {
            try { Interop.arupa_kernel_revoke_active_tab_tab(tabId); }
            catch (EntryPointNotFoundException) { }
        }

        /// <inheritdoc cref="RevokeActiveTabDocument"/>
        public void RevokeActiveTabExtension(string extensionId)
        {
            try { Interop.arupa_kernel_revoke_active_tab_extension(extensionId); }
            catch (EntryPointNotFoundException) { }
        }

        /// <summary>现有 activeTab 授权条数 —— 给判据用: <b>反测要能看见"撤销之后真的少了一条"</b>。
        /// 旧 dll 回 -1(问不出来)。</summary>
        public int ActiveTabGrantCount()
        {
            try { return Interop.arupa_kernel_active_tab_grant_count(); }
            catch (EntryPointNotFoundException) { return -1; }
        }

        /// <summary>有 Action 的扩展清单 (原样 JSON 数组; 旧 dll 回空串)。</summary>
        /// <remarks>⚠ <c>popupPath</c> 为 null ⇒ 点击该派发 onClicked; 非 null ⇒ 只开 popup。
        /// ⚠ <b>MV3 扩展都在里面</b>(上游给 MV3 一个默认 Action, 清单没写 action 键也一样),
        /// 与 Chrome 一致 —— 别读成"清单里写了 action 键的那些"。</remarks>
        public string ListExtensionActionsJson()
        {
            try
            {
                IntPtr p = Interop.arupa_kernel_list_extension_actions();
                // ⚠ 内核那边是 thread_local 指针 —— **立刻拷走**, 别存着以后用。
                return p == IntPtr.Zero ? "" : (Marshal.PtrToStringUTF8(p) ?? "");
            }
            catch (EntryPointNotFoundException) { return ""; }
        }

        // JSON 字符串字面量 —— 只做最小转义(引号/反斜杠/控制字符)。
        // ⚠ 不引 System.Text.Json: 这个 wrapper 刻意零依赖, 见文件头。
        private static string JsonStr(string s)
        {
            if (s == null) return "null";
            var sb = new System.Text.StringBuilder(s.Length + 2);
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        /// <summary>回显内核**真正记住的** MV3 扩展根 (未设 / 旧 dll = 空串)。
        /// <para>拿它在 initialize 之后对一下, 就能当场知道 <see cref="SetExtensionRoot"/>
        /// 到底生效没有 —— 这条线出过的两次事 (FB-A050 传输根顶替扩展根 · FB-P089 压根没有
        /// 门面可调) 都是"值悄悄没生效, 而没有任何视图会问它一句"。</para></summary>
        /// <remarks>⚠ 2026-08-26 修: 本段注释原先与下面 <c>SetExtensionEnabled</c> 的
        /// summary **连着写在同一处**(中间没有代码) ⇒ 两个 <c>summary</c> 都挂在了那个方法上,
        /// 而本方法**一句注释都没有** —— IDE 里悬停看到的是错的那份。
        /// 🔴 同族的病这个文件里一共 **6 个实例**(第 6 个是闸 <c>C57</c> 抓的, 人核那轮漏了):
        /// <see cref="RescanExtensions"/> 的注释自己写着
        /// 「这段说明原先写在 ListExtensions 的 summary 前面 ⇒ 它实际挂在了那个方法上」(08-25 修的第 1 个),
        /// 2026-08-26 又查出 4 个 —— 本方法 · <c>SetTransportRoute</c> · <see cref="InjectIsolatedJsAsync"/>
        /// (三个 summary 连挂在 <see cref="LastInjectError"/> 上) · 以及 <c>ArupaPluginStorage.Set</c> 的
        /// param 里留着一句**已被订正掉的错话**。
        /// ⚠ **写下"这次修好了"不等于免疫** —— 08-25 修第 1 个时旁边就躺着另外 3 个。
        /// ⇒ 已立闸 <c>C57</c> 机器判这件事(`build/roll/dotnet-doc-comment-check.py`)。</remarks>
        public string GetExtensionRoot()
        {
            try
            {
                IntPtr p = Interop.arupa_kernel_get_extension_root(_handle);
                // ⚠ 内核那块是 thread_local 缓冲, **不释放** (不是 arupa_free 的对象);
                //   PtrToStringUTF8 会拷一份出来, 拷完就与内核那块无关了。
                return p == IntPtr.Zero ? string.Empty : (Marshal.PtrToStringUTF8(p) ?? string.Empty);
            }
            catch (EntryPointNotFoundException)
            {
                return string.Empty;  // 旧 dll (< MINOR 11): 与"没设"同形, 由日志兜底
            }
        }

        /// <summary>验签 (Ed25519) 加载 native 传输引擎插件, 引擎自起本地 SOCKS inbound。
        /// 🔴 同步阻塞 (含验签/dll sha256/LoadLibrary), 请在后台线程调避免卡 UI。成功返插件
        /// 信息 (含 SocksEndpoint); 据它调 view.SetTransportRoute 把 view 路由进插件。失败抛。
        /// 🔴 Go c-shared 加载后常驻进程不卸 (RevokePlugin 只撤路由不卸 .dll)。</summary>
        public ArupaPluginInfo LoadPlugin(string pluginId)
        {
            int r = Interop.arupa_kernel_load_plugin(_handle, pluginId, out IntPtr info);
            string? json = null;
            if (info != IntPtr.Zero)
            {
                try { json = Marshal.PtrToStringUTF8(info); }
                finally { Interop.arupa_free(info); }
            }
            if (r != 0)
                throw new InvalidOperationException(
                    $"arupa_kernel_load_plugin('{pluginId}') failed: {r}");
            if (string.IsNullOrEmpty(json))
                return new ArupaPluginInfo { PluginId = pluginId };
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var hosts = new List<string>();
            if (root.TryGetProperty("managed_hosts", out var mh) &&
                mh.ValueKind == JsonValueKind.Array)
                foreach (var h in mh.EnumerateArray())
                    if (h.GetString() is string s) hosts.Add(s);
            return new ArupaPluginInfo
            {
                PluginId = root.TryGetProperty("plugin_id", out var pid)
                    ? pid.GetString() ?? pluginId : pluginId,
                SocksEndpoint = root.TryGetProperty("socks_endpoint", out var se)
                    ? se.GetString() ?? "" : "",
                ManagedHosts = hosts,
            };
        }

        /// <summary>撤销 active 插件标记 (引擎 SOCKS 仍在跑, 宿主须同时把相关 view 路由撤回 DIRECT)。</summary>
        public void RevokePlugin()
            => Interop.arupa_kernel_revoke_plugin(_handle);

        /// <summary>FB-P034 全局 DoH (进程级, 作用于全局 NetworkService 所有 NetworkContext)。
        /// mode: 0=off(系统DNS) / 1=automatic(尽力升级失败回落) / 2=secure(强制DoH失败不回落)。
        /// dohTemplate = DoH URL 模板 (如 "https://dns.alidns.com/dns-query"), off 时忽略。
        /// 返 ArupaResult (0=OK, 4=INVALID_ARG 模板非法/secure 缺模板)。
        /// ⚠ MINOR 7 dll 才有此导出; 旧 dll 调会抛 EntryPointNotFoundException, 调用方须兜底。</summary>
        public int SetDoh(int mode, string dohTemplate)
            => Interop.arupa_kernel_set_doh(_handle, mode, dohTemplate ?? "");

        public void Dispose()
        {
            if (_handle != IntPtr.Zero)
            {
                Interop.arupa_kernel_destroy(_handle);
                _handle = IntPtr.Zero;
            }
            GC.SuppressFinalize(this);
        }
        ~ArupaKernel() => Dispose();
    }

    /// <summary>
    /// 页面内注入的**域名粒度授权**判定 (capi.h:719/727; 2026-08-07 内核按 FB-P062 W-b 加的)。
    ///
    /// 为什么单独一套: 注入是**持续生效**的(同意一次, 之后每页都注), 而别处的能力模型是
    /// "一次调用一次门控" —— 两者不同形, 不能沿用。
    /// 判定顺序由内核负责(先红线、再声明、后授权), 宿主只按返回码决定 拒 / 弹授权 / 放行 ——
    /// **不要在宿主再写一套判据**, 那会立刻产生第二真相源。
    ///
    /// ⚠ 方案A(同 FB-P013 那批): 本包装由对接侧(桌面端会话)按 FB-P062 草拟, 属内核 owner 域, 待 review。
    /// </summary>
    public static class ArupaInject
    {
        public const int Allow = 0;                  // 放行
        public const int DenyNotDeclared = 1;        // 清单没声明 —— 不该弹授权, 直接拒
        public const int DenyNotGranted = 2;         // 声明了没授权 —— 宿主应据此弹授权
        public const int DenyProtectedScheme = 3;    // 非 http(s): 内核页/devtools/file/about
        public const int DenyProxiedHost = 4;        // 被代理接管 —— 与 DNR 护栏同一条红线
        public const int DenyBroadNotGranted = 5;    // 声明含 <all_urls> 但未给全站授权

        /// <summary>三个清单都是逗号分隔("a.com,*.b.com"), 空串 = 空清单。返回上面的常量之一。</summary>
        public static int Check(string url, string declaredCsv, string grantedCsv, string managedHostsCsv = "")
            => Interop.arupa_inject_check(url ?? "", declaredCsv ?? "", grantedCsv ?? "", managedHostsCsv ?? "");

        /// <summary>插件更新后声明范围**扩大**了吗 (true = 要重新征求同意; 缩小不算变更)。</summary>
        public static bool NeedsReconsent(string oldDeclaredCsv, string newDeclaredCsv, string grantedCsv)
            => Interop.arupa_inject_needs_reconsent(oldDeclaredCsv ?? "", newDeclaredCsv ?? "", grantedCsv ?? "") != 0;
    }

    /// <summary>
    /// MV3 permission 的**内核**风险分级 (capi.h:752/755)。运行时 host.guard.mapPermission 吃它。
    ///
    /// 🔴 判定在内核 (三端同一份, 64 条测试盯着), 宿主不重写 —— 运行时文件头自己写着:
    /// 宿主没提供 guard 时它**不静默放行**, 而是如实标"未经内核校验"。所以这条接上 = 让风险分级真生效。
    /// ⚠ 方案A: 由对接侧(桌面端会话)按 FB-P062/FB-P068 草拟, 属内核 owner 域, 待 review。
    /// </summary>
    public static class ArupaMv3
    {
        public const int AllowMapped = 0;
        public const int AllowLowRisk = 1;
        // ⚠ 2026-09-04 订正: 这两行的行尾注释一度过期(还写着 webRequest / management 在这两档),
        //   而那一天产品拍板后它们已经挪走了。**这里不再逐条列权限名** ——
        //   "哪条权限在哪一档"是会动的, 抄在注释里必然漂; 要看当下的分档就去问内核
        //   (<see cref="MapPermission"/>), 那是唯一真值源。
        public const int DenyPrivileged = 2;        // 特权逃逸(会拿到内核/宿主机能力的那一类)
        public const int DenyFingerprintRisk = 3;   // 指纹/画像面
        public const int DenyUnknown = 4;           // 默认拒: **表里没有这个词**
        /// <summary>
        /// 5 = 厂商命名空间 (`nomad.*`, capi.h 2026-08-19 加, FB-P072 问题二)。
        ///
        /// 🔴 **既不是放行也不是拒**: 这张表只管标准 MV3 权限, 我们自己新增的接口由**各自那套
        /// 判定**决定准入 (`nomad.route` → <see cref="ArupaRoute.Check"/>)。
        /// 拿到 5 当放行 = 凭一个名字前缀发能力; 当拒 = 加速类插件在装的那一刻就被摘权限
        /// (桌面 2026-08-19 实跑撞到的正是后者)。
        /// ⚠ 调用方**别自己写前缀判断** —— 边界由内核给, 各端各写一份必漂。
        /// </summary>
        public const int VendorNamespace = 5;

        /// <summary>
        /// 6 = **认识这个权限、也认它是合法的 MV3 权限, 但能力还没做**。
        ///
        /// 🔴 **与 <see cref="DenyUnknown"/>(4) 的差别对宿主很值钱, 别合并**:
        /// · 4 = "表里没有这个词" —— 可能是标准新加的, 也可能是我们漏了;
        /// · 6 = "我们认得它, 只是**还没实现**" —— **会随实现落地改判**。
        /// 给用户的那句话也不一样: 4 该说"未知权限", 6 该说"暂未支持"。
        /// 合并的话商店筛不出来, 文案也是错的。
        ///
        /// ⚠ 本常量 **2026-09-04 才补上**, 而内核从 `153.0.8010.12+2` 起对 14 条权限返 6
        /// (`webRequest` 族 / `management` / `cookies` / `history` / `bookmarks` /
        /// `downloads` / `topSites` / `browsingData` / `privacy` …)。
        /// 在此之前照本类判定的宿主, 拿到 6 会落进"不认识"分支 —— 那是发出去的错, 不是你的。
        /// </summary>
        public const int DenyUnsupported = 6;

        /// <summary>
        /// 7 = **走逐风的替代实现**。上游那条路我们没有, 换了一条。
        ///
        /// 🔴 **这不是拒。** 拿到 7 的宿主要让插件**装上、跑起来** ——
        /// 只是这条能力走的不是上游那套, 行为可能有差异。
        /// 用 <c>PermissionCapability(perm)</c> 拿"替代走的是哪条能力"
        /// (例如 <c>webRequest</c> → <c>cap.net.declarative</c>)。
        ///
        /// 现役例子: <c>webRequest</c> —— 实测**对象在**(开出 23 个入口)而
        /// **拦不到东西**(FB-P111), 判"没做"和判"放行"都是假话。
        /// 它的拦截类用法可以走 <c>declarativeNetRequest</c>(我们真支持);
        /// ⚠ **观察类用法(onCompleted 统计、读响应头)覆盖不了** —— 那部分仍不可用,
        /// 宿主该提示。
        /// </summary>
        public const int Substituted = 7;

        /// <summary>
        /// 8 = **认识 · 这条能力确实没有 · 但不阻止安装**。
        ///
        /// 🔴 **这不是拒。** 与 6 的区别只有一件事: **6 阻装, 8 不阻装**。
        /// 🔴 拿到 8 的宿主**必须给出可见提示**
        /// (「本插件的 XX 功能在逐风浏览器上不可用」)—— 这是**契约, 不是建议**。
        /// 不提示就退化成"装上了、然后一声不响不工作", 而那正是码 6 当初拒绝放行
        /// 要防的东西; 那种情况下还不如维持整包拒绝。
        ///
        /// 🔴 为什么要有这一档 —— 一个实测数字:
        /// 此前一条权限判拒 = 整个插件装不上。语料 2858 个商店扩展里被拒 1517 个(53.1%),
        /// 而**其中 45%(678 个)只被 1 条权限挡住** —— 678 个插件的全部功能被砍掉,
        /// 只因为其中一个功能我们没做。改判之后"能装能跑"从 46.9% 升到 **88.8%**。
        ///
        /// ⚠ 2026-09-05 起, 此前判 6 的绝大多数(cookies / downloads / identity /
        /// webNavigation / notifications / privacy …)改判 8。
        /// 在此之前照本类判定的宿主, 拿到 7/8 会落进"不认识"分支按拒处理 ——
        /// **方向安全但可观测性差**: 表现成"插件装不上"而不是"某功能不可用"。
        /// </summary>
        public const int DegradedUnavailable = 8;

        /// <summary>
        /// 判定码 → 名字(`"ALLOW_MAPPED"` / `"DENY_UNSUPPORTED"` …)。**直接问内核。**
        ///
        /// 🔴 存在的理由是**让你不必维护上面那张表**。抄一份表的代价已经收过好几次利息:
        /// 内核自己的判据抄漏过两次, 对接端的码表只声明了一半, 而**本类到 2026-09-04
        /// 都还缺 6** —— 三次都是同一个形状, 而且**坏的时候不报错**。
        ///
        /// ⚠ 认不出的码回 <c>null</c>(**不是**回 <c>"DENY_UNKNOWN"</c> —— 那会与码 4 撞名,
        /// 分不出"码是 4"和"这个码我不认识")。拿到 null 的正确处理是
        /// **按拒 + 明说"本版本不认识这个码"**, <b>不许猜成放行</b>。
        ///
        /// ⚠ 采用条件是**你的生产基线含这个导出**(`153.0.8010.12+2` 起), 不是"我这台机器上有"。
        /// 旧内核上调它会 <c>EntryPointNotFoundException</c>。
        /// </summary>
        public static string VerdictName(int verdict)
            => Interop.arupa_mv3_verdict_name(verdict);

        /// <summary>
        /// ① 这条能力**能不能用**。<c>0</c>/<c>1</c> 能用 ·
        /// <b><c>7</c> 也能用</b>（走替代）· <c>8</c> 不能用（确实没有）· 其余不能用。
        ///
        /// 🔴 <b>拿到 <c>7</c> 时必须同时读 <see cref="SubstitutionGap"/></b>：
        /// 替代能用不等于等价，缺口非空就要给用户可见提示。
        /// 只读本方法会把"有替代"当成全量等价 ⇒ 覆盖不到的那半照样静默失效。
        ///
        /// ⚠ <c>5</c>（厂商命名空间）回 <c>false</c> —— 它的意思是"**不归这张表判**"，
        /// 你要去问对应的那套判定（如 <c>RouteCheck</c>）。把"不归我判"读成"可以"，
        /// 就是凭一个名字前缀发能力。
        /// </summary>
        public static bool IsPermUsable(int verdict)
            => Interop.arupa_mv3_perm_usable(verdict) != 0;

        /// <summary>
        /// ② 这插件**装不装得上**。<c>0</c>/<c>1</c>/<c>7</c> 装得上 ·
        /// <b><c>8</c> 也装得上</b> · <c>2</c>/<c>3</c>/<c>4</c>/<c>6</c> 装不上。
        ///
        /// 🔴 <b>与 <see cref="IsPermUsable"/> 只在码 8 上不同</b> —— 而 8 正是本版新增那一档。
        /// 此前只有一个函数在答这两个问题，它对 7 和 8 一律回 <c>false</c>，
        /// <b>两个问题各答错一半</b>：照它写 <c>if (!allowed) 拒装</c>，
        /// 会把本版从 46.9% 抬到 88.8% 的那批插件原样拒回去，而且不报错。
        ///
        /// 🔴 判 8 装得上是**带条件的**：宿主必须给出可见提示
        ///（「本插件的 XX 功能在逐风浏览器上不可用」）。不提示就退化成
        /// "装上了然后一声不响不工作" —— 那还不如整包拒绝。<b>这是契约不是建议。</b>
        /// 提示文案的唯一来源是管理系统 <c>/api/plugin-docs</c> 的 <c>degrade.tips</c>。
        /// </summary>
        public static bool IsPluginInstallable(int verdict)
            => Interop.arupa_mv3_plugin_installable(verdict) != 0;

        public static int MapPermission(string permission)
            => Interop.arupa_mv3_map_permission(permission ?? "");

        /// <summary>🔴 native 通道(<c>chrome.runtime.sendNativeMessage</c>)的准入。
        /// <para>产品 2026-09-08:「nativemessaging 最主要是我们 vpn 程序必须支持」·
        /// (三方 VPN 插件给不给签名)「给」·「不给签名的也要用户自己加载自己的开发包,
        /// <b>风险自担, 权力还是用户自己</b>」。</para>
        /// <para><b>0 和 1 都不是拒, 别折成 bool</b>:
        /// <c>0</c> 签名包(我们背书) ·
        /// <c>1</c> 未签名 + <b>用户自己加载的开发包</b> —— 放行, 而<b>必须先让用户知道后果</b> ·
        /// <c>2</c> 清单没声明(连问的资格都没有) ·
        /// <c>3</c> 商店装的未签名包(谁都没背书)。</para>
        /// <para>🔴 <b>码 1 的同意提示要写实话</b>: 通了之后这个插件可以跟一个本地程序
        /// 双向说话, <b>它已经不在浏览器的沙箱里了</b>。不是"该插件需要额外权限"那种话 ——
        /// 用户读完不知道自己在同意什么。</para>
        /// <para>⚠ <paramref name="userLoaded"/> 由<b>宿主如实断言</b>, 内核不猜 ——
        /// 与 <c>userGesture</c> 同一条纪律: <b>缺省按"没有"处理, 不按有</b>。
        /// 入口没做就如实传 <c>false</c>; 谎报 true 等于把它当做了。</para>
        /// <para>⚠ 本方法<b>只判准入, 不判"哪个程序"</b> —— 能连哪个本地程序由
        /// <b>宿主登记的名单</b>决定, 内核<b>不读注册表</b>。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 回 <c>3</c>(拒)。
        /// <b>判不了就不放行</b> —— 这一条的后果是沙箱外权限, 不许"问不通按放行"。</para></summary>
        public static int NativeCheck(string pluginId, bool declared, bool userLoaded)
        {
            try
            {
                return Interop.arupa_native_check(pluginId ?? "", declared ? 1 : 0,
                                                  userLoaded ? 1 : 0);
            }
            catch (EntryPointNotFoundException)
            {
                // 🔴 回 3(拒)而不是某个"未知"码: 这条的后果是**沙箱外权限**,
                //   而本仓对护栏的既定教条是「问不通按拒处理, 不按放行」。
                return 3;
            }
        }

        /// <summary>🔴 这条权限<b>能看到用户的什么</b> —— 一句给普通用户看的中文。
        /// <para>2026-09-08 产品拍板「<b>说明隐私风险, 而不是拒绝</b>」之后,
        /// <c>management</c> / <c>clipboardRead</c> / <c>system.*</c> /
        /// <c>&lt;all_urls&gt;</c> 这 8 条不再阻装 ——
        /// <b>而这句话就是放行之后唯一还在的保护</b>。</para>
        /// <para>🔴 <b>宿主必须把它显示出来。</b>不显示的话我们就是<b>把保护去掉了而
        /// 什么都没换上</b> —— 那比原来一刀切拒<b>更糟</b>, 因为用户连"这里有过一道门"
        /// 都不知道。而"真的显示了"这一格<b>内核验不了</b>。</para>
        /// <para>⚠ 回<b>空串 = 这条权限没写隐私说明</b>, <b>不等于</b>"它没有隐私风险"。
        /// 两者压成一个读数的话, 会把"我们还没写"显示成"这个插件很安全" ——
        /// 拿不到时该显示一句<b>通用</b>提醒, 而不是什么都不显示。</para>
        /// <para>⚠ 参数是<b>权限名</b>(如 <c>"clipboardRead"</c>), 不是判定码。
        /// 只逐字匹配: <c>&lt;all_urls&gt;</c> 取得到, 而
        /// <c>https://*.example.com/*</c> 这类 host pattern 一律回空 ——
        /// 逐站点的文案是<b>站点权限</b>那条路的事。</para></summary>
        public static string PermissionPrivacyNote(string permission)
        {
            try
            {
                return Marshal.PtrToStringUTF8(
                    Interop.arupa_mv3_perm_privacy_note(permission ?? "")) ?? "";
            }
            catch (EntryPointNotFoundException)
            {
                // 旧 dll: 回空串 = "没有说明"。⚠ 而按上面那条契约, 宿主拿到空串时
                //   该显示一句**通用**提醒, 不是什么都不显示。
                return "";
            }
        }

        /// <summary>ALLOW_MAPPED 时映射到的 capability 名 (如 "cap.net.declarative"); 否则空串。</summary>
        public static string PermissionCapability(string permission)
            => Marshal.PtrToStringUTF8(Interop.arupa_mv3_permission_capability(permission ?? "")) ?? "";

        /// <summary>
        /// 判 <b>7 <c>Substituted</c></b> 时，"替代实现**顶不到哪儿**"。
        ///
        /// 🔴 与 <see cref="PermissionCapability"/> 是**一对**：那个答"替代走的是哪条能力"，
        /// 本方法答"那条能力顶不到哪儿"。<b>只读前者会把有替代当成全量等价</b> ⇒ 不提示 ⇒
        /// 覆盖不到的那半照样静默失效。<b>缺口非空就必须给用户可见提示。</b>
        ///
        /// ⚠ 返回**空串 = 替代是完整的**（或该权限根本不判 7），<b>不是"没查"</b> ——
        /// 每一条判 7 的权限都必须在内核的缺口表里有一行，漏登记会被判据拦下。
        ///
        /// ⚠ 本方法 <b>2026-09-07 才补进 .NET 门面</b>，而内核 09-06 就有了这条导出 ——
        /// 中间那段时间对接文档写着 <c>ArupaMv3.SubstitutionGap(perm)</c> 而它并不存在。
        /// 采用条件是**你的生产基线含这个导出**，不是"我这台机器上有"。
        /// </summary>
        public static string SubstitutionGap(string permission)
            => Marshal.PtrToStringUTF8(Interop.arupa_mv3_substitution_gap(permission ?? "")) ?? "";

        // ── 声明式网络规则护栏 (capi.h:731) ────────────────────────────────
        // 是**护栏**不是规则引擎本体: 宿主的 DNR 引擎把插件规则接进拦截链**之前**,
        // 逐条经此校验, DENY 的丢弃 + 记审计。判定在内核(三端同一份, C9 闸盯着)。
        public const int RuleAllow = 0;
        public const int RuleDenyNonDeclarative = 1;   // 非声明式 action(webRequest/JS 回环)
        public const int RuleDenyProtectedScheme = 2;  // 模式触及 nomad:// 等内核资源
        public const int RuleDenyProxiedHost = 3;      // 触及被代理接管的域名, 或过宽 catch-all

        /// <summary>声明式 action 取值 —— 其它值一律被内核判为"非声明式"并拒。</summary>
        public const int ActionBlock = 0;
        public const int ActionRedirect = 1;
        public const int ActionModifyHeaders = 2;

        /// <param name="managedHostsCsv">当前分区被代理接管的域名(逗号分隔), 与注入授权取同一份。</param>
        public static int CheckDeclarativeRule(string urlPattern, int actionType,
                                               string managedHostsCsv = "")
            => Interop.arupa_mv3_check_declarative_rule(urlPattern ?? "", actionType,
                                                        managedHostsCsv ?? "");
    }

    /// <summary>
    /// 出口路由的**档位**护栏 (capi.h:735, 2026-08-19)。回答:
    /// 这个插件能不能拿到**这一档作用域**的出口路由。
    ///
    /// 🔴 判定在内核, 宿主**不重判**: 签名包四档全给; 未签名只给 global 且首次要用户点头一次,
    /// 请求更细的作用域一律拒。<c>is_signed</c> 不是入参 —— 内核自己查 active 签名包,
    /// 做成入参等于把信任交给调用方, 那就没有门控了。
    /// 🔴 拿到 <see cref="DenyScopeNeedsSign"/> 时**必须报错**, 不许静默降级成 global ——
    /// 那会让插件以为分流生效了, 而实际全局出口都变了。
    ///
    /// ⚠ **老交付件上没有这个导出**(内核 2026-08-19 才落地) —— 调用方必须先探测,
    /// 探不到就如实降级, 直接调会抛 <see cref="EntryPointNotFoundException"/>。
    /// ⚠ 方案A: 本门面由对接侧(桌面端会话)按 capi.h 草拟, 属内核 owner 域, 待 review。
    /// </summary>
    public static class ArupaRoute
    {
        public const int Allow = 0;                 // 放行
        public const int AllowNeedsConsent = 1;     // 未签名 + global: 先要用户点头一次
        public const int DenyScopeNeedsSign = 2;    // 未签名 + 更细作用域: 拒(必须报错, 不许降级)
        public const int DenyNotDeclared = 3;       // 清单没声明 nomad.route / proxy
        public const int DenyUnknown = 4;           // 认不出的作用域 / 参数不全 / 兜底

        /// <summary>scope 取 "global" | "tab" | "domain" | "profile"; declared = 清单里声明了吗。</summary>
        public static int Check(string pluginId, string scope, bool declared)
            => Interop.arupa_route_check(pluginId ?? "", scope ?? "", declared ? 1 : 0);
    }

    /// <summary>
    /// per-plugin 隔离存储 (capi.h:738–749) —— <c>chrome.storage</c> 的后端。
    /// 按插件隔离(A 读不到 B) · 持久 profile 落盘 <c>&lt;udd&gt;/PluginStorage/&lt;id&gt;.json</c> ·
    /// OTR 纯内存(反取证) · 卸载 <see cref="Clear"/> 擦除。
    ///
    /// 🔴 **value 必须是 JSON 串** —— 这是 capi.h 写死的契约, 不是建议:
    /// 内核 <c>set</c> 先 <c>JSONReader::Read</c> 解析(解析不了才当字符串存), <c>get</c> 一律
    /// <c>WriteJson</c> 输出 ⇒ 传裸 <c>hello</c> 读回来是 <c>"hello"</c>(带引号)。
    /// 调用方统一 <c>JSON.stringify</c> 写 / <c>JSON.parse</c> 读, 非法 JSON 直接拒, 别指望原样往返。
    /// ⚠ 本处此前写的是"内核不解析它, 原样存原样还" —— 那句是错的, 2026-08-19 内核确认后订正。
    ///
    /// 🔴 内核那侧 get/get_all 返回的是 **thread_local** 缓冲区("有效至同线程下次 get"),
    /// 所以这里每次都**立刻拷成托管 string** 再返回 —— 调用方拿到的是自己的副本, 可以存。
    /// ⚠ 方案A: 由内核会话按 capi.h 补齐(2026-08-18)。补它的起因是那张能力表:
    /// D 组 <c>per-plugin 隔离存储</c> / <c>声明式规则护栏</c> 两格桌面报「无」, 追下去发现
    /// **不是 App 没接, 是内核 wrapper 压根没给门面** —— 桌面想接也没东西可调。
    /// </summary>
    public static class ArupaPluginStorage
    {
        /// <summary>
        /// 读一个键; **键不存在返回 null**。
        ///
        /// 🔴 内核那侧 <b>永不返回 NULL 指针</b> —— 键不存在时返回的是**空串**
        /// (`arupa_kernel_capi_impl.cc:2598`: thread_local std::string, miss 就是空)。
        /// 这条 capi.h 此前没写, 是 2026-08-18 对着交付件实跑才知道的。
        /// 空串安全地等价于"没有": value 按契约是 JSON 串, 而合法 JSON 最短也有 2 个字符
        /// (`""`), 长度 0 不可能是一个真实存下去的值。
        /// ⇒ 这里折成 null, 好让 <c>chrome.storage</c> shim 区分「没这个键」与「有值」——
        /// 那是 MV3 语义要求的(<c>get</c> 的结果对象里根本不该出现缺失的键)。
        /// </summary>
        public static string? Get(string pluginId, string key)
        {
            IntPtr p = Interop.arupa_plugin_storage_get(pluginId ?? "", key ?? "");
            string? s = p == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(p);
            return string.IsNullOrEmpty(s) ? null : s;
        }

        /// <summary>
        /// 读**全部**键, 返回 JSON 对象串(<c>{}</c> = 空)。
        /// 给 <c>chrome.storage.local.get(null)</c> 用 —— 那个语义是"给我全部",
        /// 逐键 Get 表达不了(调用方根本不知道有哪些键)。
        /// </summary>
        public static string GetAll(string pluginId)
        {
            IntPtr p = Interop.arupa_plugin_storage_get_all(pluginId ?? "");
            return (p == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(p)) ?? "{}";
        }

        /// <param name="valueJson">
        /// **必须是合法 JSON 串** —— 这是 capi.h 写死的契约, 不是建议。
        /// <para>内核 <c>set</c> 先 <c>JSONReader::Read</c> 解析(解析不了才当字符串存),
        /// <c>get</c> 一律 <c>WriteJson</c> 输出 ⇒ 传裸 <c>hello</c> 读回来是 <c>"hello"</c>(带引号)。
        /// 统一 <c>JSON.stringify</c> 写 / <c>JSON.parse</c> 读, 非法 JSON 直接拒, **别指望原样往返**。</para>
        /// <para>⚠ 2026-08-26 修: 本处原写「内核不解析它, 原样存原样还」—— **那句 2026-08-19 就被订正过了**,
        /// 订正写在本类的 summary 里(「本处此前写的是…那句是错的」), 而**被订正掉的原话还留在这一行上**,
        /// 于是同一个类里两种说法并存, 且下面这一行是错的那份。</para>
        /// </param>
        public static void Set(string pluginId, string key, string valueJson)
            { Interop.arupa_plugin_storage_set(pluginId ?? "", key ?? "", valueJson ?? ""); ThrowStorageError(); }

        public static void Remove(string pluginId, string key)
            { Interop.arupa_plugin_storage_remove(pluginId ?? "", key ?? ""); ThrowStorageError(); }

        /// <summary>擦掉这个插件的全部存储 —— 卸载时调。</summary>
        public static void Clear(string pluginId)
            { Interop.arupa_plugin_storage_clear(pluginId ?? ""); ThrowStorageError(); }
        private static void ThrowStorageError()
        {
            var error = ArupaKernel.LastErrorJson();
            if (!string.IsNullOrEmpty(error)) throw new InvalidOperationException(error);
        }
    }

    // One process-lifetime callback entry, not one delegate/GCHandle per request.
    // Native retains only a never-reused integer token. Late/duplicate callbacks
    // after timeout or disposal cannot resolve a new request or touch its bytes.
    internal sealed class NativeEvalRequests : IDisposable
    {
        internal const int PerViewLimit = 128;
        internal const int GlobalLimit = 4096;
        private static readonly object Gate = new();
        private static readonly Dictionary<long, Request> Pending = new();
        private static long _nextToken;
        private static readonly Interop.EvalCbNative Callback = OnResult;
        internal static readonly IntPtr CallbackPointer = Marshal.GetFunctionPointerForDelegate(Callback);
        private static readonly Interop.ArupaPdfCb PdfCallback = OnPdf;
        internal static readonly IntPtr PdfCallbackPointer = Marshal.GetFunctionPointerForDelegate(PdfCallback);
        private static void OnPdf(IntPtr user, int ok, IntPtr data, IntPtr length)
        {
            lock (Gate)
            {
                if (!Pending.Remove(user.ToInt64(), out var request)) return;
                request.Owner._tokens.Remove(request.Token);
                string? result = null;
                try
                {
                    long size = length.ToInt64();
                    if (ok != 0 && data != IntPtr.Zero && size > 0 && size <= 32 * 1024 * 1024)
                    {
                        var bytes = new byte[(int)size];
                        Marshal.Copy(data, bytes, 0, bytes.Length);
                        result = Convert.ToBase64String(bytes);
                    }
                }
                catch { }
                request.Completion.TrySetResult(result);
            }
        }
        private readonly HashSet<long> _tokens = new();
        private bool _disposed;

        internal sealed class Request
        {
            internal readonly NativeEvalRequests Owner;
            internal readonly long Token;
            internal readonly TaskCompletionSource<string?> Completion =
                new(TaskCreationOptions.RunContinuationsAsynchronously);
            internal IntPtr User => new(Token);
            internal Request(NativeEvalRequests owner, long token) { Owner = owner; Token = token; }
            internal async Task<string?> WaitAsync(int timeoutMs)
            {
                try { return await Completion.Task.WaitAsync(TimeSpan.FromMilliseconds(timeoutMs)).ConfigureAwait(false); }
                catch (TimeoutException) { return null; }
                finally { Cancel(); }
            }
            internal void Cancel()
            {
                lock (Gate)
                {
                    if (!Pending.Remove(Token)) return;
                    Owner._tokens.Remove(Token);
                    Completion.TrySetResult(null);
                }
            }
        }

        internal Request? TryStart()
        {
            lock (Gate)
            {
                long maxToken = IntPtr.Size == 8 ? long.MaxValue : int.MaxValue;
                if (_disposed || _tokens.Count >= PerViewLimit || Pending.Count >= GlobalLimit || _nextToken == maxToken)
                    return null;
                var request = new Request(this, ++_nextToken);
                Pending.Add(request.Token, request);
                _tokens.Add(request.Token);
                return request;
            }
        }

        private static void OnResult(IntPtr user, IntPtr json)
        {
            // Keep lookup, byte copy and terminal transition atomic with Dispose.
            // Continuations are asynchronous: no application code runs under Gate.
            lock (Gate)
            {
                if (!Pending.Remove(user.ToInt64(), out var request)) return;
                request.Owner._tokens.Remove(request.Token);
                string? value = null;
                try { value = json == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(json); }
                catch { /* Preserve the async facade's null-on-failure contract. */ }
                request.Completion.TrySetResult(value);
            }
        }

        public void Dispose()
        {
            lock (Gate)
            {
                if (_disposed) return;
                _disposed = true;
                foreach (long token in _tokens)
                    if (Pending.Remove(token, out var request)) request.Completion.TrySetResult(null);
                _tokens.Clear();
            }
        }
    }

    public sealed class ArupaWebView : IDisposable
    {
        private IntPtr _handle;
        private readonly object _evalLifetime = new();
        private readonly NativeEvalRequests _evalRequests = new();
        // 保持托管 delegate 存活 (防 GC 回收导致 native 调空指针)。
        private readonly Interop.OnPaintNative _onPaint;
        private readonly Interop.OnCursorNative _onCursor;   // FB-P016
        private readonly Interop.OnIntNative _onRenderGone;  // FB-P017: 渲染进程崩溃
        private readonly Interop.OnStringNative _onPageFinished;
        private readonly Interop.OnErrorNative _onError;
        private readonly Interop.ShouldInterceptNative _onIntercept;   // FB-P002 C2/B2
        private readonly Interop.ShouldInterceptExNative _onInterceptEx; // #34 (带 resource_type)
        private readonly Interop.OnDownloadNative _onDownload;         // FB-P002 B4
        // FB-P003: 高层 wiring 回调 (保活防 GC)。
        private readonly Interop.OnStringNative _onTitle;
        private readonly Interop.OnFaviconUrlsNative _onFaviconUrls;   // FB-P039: 页面 favicon 候选列表
        private readonly Interop.OnIntNative _onProgress;
        private readonly Interop.OnIntNative _onSslState;   // FB-P025: on_ssl_state_changed(secure 0/1)
        private readonly Interop.OnFindResultNative _onFindResult;
        private readonly Interop.OnJsDialogNative _onJsAlert;
        private readonly Interop.OnJsDialogNative _onJsConfirm;
        private readonly Interop.OnJsPromptNative _onJsPrompt;
        private readonly Interop.OnJsBeforeUnloadNative _onJsBeforeUnload;
        private readonly Interop.OnVoidNative _onShowFullscreen;
        private readonly Interop.OnVoidNative _onHideFullscreen;
        private readonly Interop.OnStringNative _onPictureInPictureRequest;   // MINOR24 pip.host
        private readonly Interop.OnStringNative _onNavigationState;
        private readonly Interop.OnPresentationChangedNative _onPresentationChanged;   // MINOR25 presentation.v1
        private readonly Interop.OnStringNative _onNewWindow;
        private readonly Interop.OnNewContentsNative _onNewContents;
        private readonly Interop.OnBrowserCommandNative _onBrowserCommand;
        /// <summary>F11/F12 请求，在内核 UI 线程触发。处理器须投递到宿主 UI 线程。
        /// 未订阅时按键继续交给网页。F11 切换宿主窗口；F12 创建/激活专用 OSR view 并调用 OpenDevTools。</summary>
        public event Action<BrowserCommand>? BrowserCommandRequested;
        public event Action<string>? NewContentsRequested;
        private readonly Interop.ShouldOverrideNative _onShouldOverride;
        // FB-P013 第三批: 权限/认证/文件选择回调 (保活防 GC)。
        private readonly Interop.OnHttpAuthNative _onHttpAuth;
        private readonly Interop.OnGeoPromptNative _onGeoPrompt;
        private readonly Interop.OnPermissionNative _onPermission;
        private readonly Interop.OnFileChooserNative _onFileChooser;
        // A批 (FB-P018 同步): console/证书错误/关窗 + OSR popup (保活防 GC)。
        // ⚠ on_download_updated 故意不装配 (注册即改下载语义: 内核放行自下载, 与现行
        //   on_download_start"注册即取消交宿主"互斥; 宿主要内核侧下载进度时再开)。
        private readonly Interop.OnConsoleMessageNative _onConsoleMessage;
        private readonly Interop.OnCertificateErrorNative _onCertError;
        private readonly Interop.OnVoidNative _onCloseWindow;
        private readonly Interop.OnIntNative _onPopupShow;
        private readonly Interop.OnPopupSizeNative _onPopupSize;
        private readonly Interop.OnPaintNative _onPopupPaint;
        // MINOR6 (FB-P024/P026): 右键菜单 / 通知权限 (保活防 GC)。
        private readonly Interop.OnContextMenuNative _onContextMenu;
        private readonly Interop.OnNotificationPermNative _onNotificationPerm;
        private Interop.OnCdpNative? _onCdp;
        // CDP attach 后由 Kernel 通过 CLR FailFast 报 "callback was made on a garbage collected
        // delegate of type '...+OnCdpNative::Invoke'"。原因: _onCdp 是 lazy init 的实例字段,Kernel
        // 把 marshal 出来的函数指针常驻到 arupa_webview_destroy 才会清——期间 _onCdp delegate 对象
        // 只能靠 Marshal.GetFunctionPointerForDelegate 内部 keep alive;它对**唯一引用 root**的
        // 托管委托会在 GC 时回收 → Kernel 端回调野指针 → FailFast。
        // 修法: 显式 GCHandle.Alloc Normal handle 并保到实例字段,Dispose 时 Free。
        //   参考 Microsoft Learn 原文: "You must manually keep the delegate from being collected
        //   by the garbage collector from managed code."
        private GCHandle _onCdpHandle;

        // FB-P002 C2/B2: 拦截响应单槽缓冲 (IO 线程串行回调; 内核拷走后下次/Dispose 才释放)。
        private IntPtr _lastInterceptResp = IntPtr.Zero;
        private readonly object _interceptLock = new();

        public event EventHandler<PaintEventArgs>? Paint;
        /// <summary>FB-P016: 页面光标类型变化 (ui::mojom::CursorType int: 0=kPointer/2=kHand/3=kIBeam/...)。
        /// 宿主据此 SetCursor 自己的窗口 → 解 hover 链接"光标不变/无反应"感知。</summary>
        public event Action<int>? CursorChanged;
        /// <summary>FB-P017: 渲染进程消失 (crashed=true 崩溃 / false 正常退出)。崩溃后该 view OSR 停帧
        /// (页面"卡死"), 宿主可据此重载/显示崩溃页恢复。</summary>
        public event Action<bool>? RenderProcessGone;
        public event EventHandler<string?>? PageFinished;
        public event Action<string?, int, string?>? ReceivedError;  // url, code, desc
        public event Action<string?>? CdpMessage;                   // CDP JSON

        // FB-P002 C2/B2: 资源拦截钩子 (同步, IO 线程)。返 InterceptResponse=替换; 返 null=放行。
        // PC: view.ShouldInterceptRequest = req => NomadScheme.Route(req.Url); —— nomad:// WebUI
        // 内容供应 + 请求观测共用。
        public Func<InterceptRequest, InterceptResponse?>? ShouldInterceptRequest { get; set; }
        // FB-P002 B4: 下载拦截。订阅即拦截原生下载 (内核取消), 宿主接管 (aria2/IDownloadInterceptor)。
        public event EventHandler<DownloadStartedEventArgs>? DownloadStarted;

        // ── FB-P003: 高层 wiring 事件 ─────────────────────────────────────────────
        public event Action<string?>? TitleChanged;                       // 标签标题变更
        public event Action<string?, string?>? FaviconUrls;               // FB-P039: (pageUrl, iconsJson) 页面声明的 favicon 候选列表(JSON: url+sizes+type)
        public event Action<string?>? NavigationStateChanged; // native snapshot JSON, borrowed data copied by thunk
        public event Action<int>? ProgressChanged;                        // 0..100
        /// <summary>FB-P025: 内核 on_ssl_state_changed 回调 — 主帧安全态变化 (true=secure=1)。
        /// 诊断"证书 getter 返空"用: 若真 HTTPS 页从未以 true 回调 → 内核 ssl_info 没挂到 entry。</summary>
        public event Action<bool>? SslStateChanged;
        public event Action<bool, int>? LoadingStateChanged;              // (isLoading, progress) 进度条
        public event EventHandler<FindResultEventArgs>? FindResult;        // 页内查找
        public event EventHandler<JsDialogEventArgs>? JsDialog;            // alert/confirm/prompt — 经 RespondJsDialog 回应
        public event Action<string?>? NewWindowRequested;                 // target=_blank / window.open
        public event Action<bool>? FullscreenChanged;                     // HTML5 全屏 (true=进入)
        /// <summary>MINOR24 pip.host: 网页调 requestPictureInPicture()。内核无原生 PiP 窗 (该请求已按
        /// kNotSupported 拒绝), 参数为发起请求的页面 URL — 宿主据此用自己的 PiP 窗呈现。</summary>
        public event Action<string?>? PictureInPictureRequested;
        /// <summary>MINOR25 presentation.v1: 统一呈现通道 (全屏 + 画中画一条出口)。(mode, reason, seq)
        /// 装配后内核不再单独触发 FullscreenChanged / PictureInPictureRequested —— 那两个事件由本回调
        /// 合成, 故 PC 侧订阅语义完全不变 (不存在"两条通道各触发一次"的双发)。</summary>
        public event Action<int, int, ulong>? PresentationChanged;

        // 呈现模式 / 变更原因 (与 capi.h ARUPA_PRESENTATION_* 一致)。
        public const int PresentationModeNone = 0;
        public const int PresentationModeFullscreen = 1;
        public const int PresentationModePictureInPicture = 2;
        public const int PresentationReasonPageRequest = 0;
        public const int PresentationReasonPageExit = 1;
        public const int PresentationReasonHostExit = 2;
        public const int PresentationReasonViewDetached = 3;
        // 导航拦截 (可选): 返 true=取消该导航。在内核 IO/UI 同步调用,勿重入内核控制函数。
        public Func<string?, bool>? NavigationStarting { get; set; }

        /// <summary>
        /// 一次导航被问到时的全部信息(FB-P156)。返回 true = **宿主接管**, 这次导航
        /// 取消且**请求不发出去**, 也不出错误页。
        /// </summary>
        /// <remarks>
        /// 🔴 内核在 <c>WillStartRequest</c> **与** <c>WillRedirectRequest</c> 两处都问 ——
        ///   两处都在请求发出去之前。<see cref="IsRedirect"/> 区分是哪一处。
        /// ⚠ <see cref="Url"/> 是**这一跳**的地址(重定向后就是新地址), 不是起始地址。
        /// </remarks>
        public readonly record struct NavigationStartingInfo(
            string? Url,
            bool IsMainFrame,
            bool IsRedirect,
            bool HasUserGesture);

        /// <summary>
        /// 与 <see cref="NavigationStarting"/> 同一条接缝, 但**给全四个参数**。
        /// 订了这个就用这个; 没订才回落到 <see cref="NavigationStarting"/>。
        /// </summary>
        /// <remarks>
        /// 🔴 为什么另开一个而不是改 <see cref="NavigationStarting"/> 的签名:
        ///   对接侧直接编译本工作树, 改签名 = 当场打断他们的构建。
        /// 🔴 典型用途 `chrome.identity.launchWebAuthFlow`: 认 URL 前缀
        ///   <c>https://&lt;扩展id&gt;.chromiumapp.org/</c>, 返 true 把这一跳截住 ——
        ///   那个 URL 的 query/fragment 里**装着 OAuth 授权码**, 而
        ///   <c>chromiumapp.org</c> 是真实解析得出的域名。
        ///   **只"看得见"而拦不住的钩子会把授权码送出去**, 上游从来不发这个请求。
        ///   ⚠ 授权码常常是从授权页 302 过来的 ⇒ <b>别只判 IsRedirect==false 那一跳</b>。
        /// </remarks>
        public Func<NavigationStartingInfo, bool>? NavigationStartingDetailed { get; set; }

        // ── FB-P013 第三批: 权限/认证/文件选择事件 (宿主订阅 → 弹原生 UI → 调 Respond*) ──
        public event EventHandler<HttpAuthEventArgs>? HttpAuthRequested;          // 经 RespondHttpAuth 回应
        public event EventHandler<GeolocationEventArgs>? GeolocationRequested;     // 经 RespondGeolocation
        public event EventHandler<MediaPermissionEventArgs>? MediaPermissionRequested; // 经 RespondMediaPermission
        public event EventHandler<FileChooserEventArgs>? FileChooserRequested;     // 经 RespondFileChooser

        // ── A批 (2026-06-11): console / 证书错误 / window.close / OSR popup widget ──
        /// <summary>页面 console 消息: (level 0=verbose 1=info 2=warning 3=error, message, sourceId, line)。</summary>
        public event Action<int, string?, string?, int>? ConsoleMessage;
        /// <summary>证书错误决策 (经 RespondCertificateError 应答; 不应答 = 内核取消, 安全默认)。</summary>
        public event EventHandler<CertificateErrorEventArgs>? CertificateErrorOccurred;
        /// <summary>页面 window.close()。内核不自动毁 view; 宿主自行关 tab 或忽略。</summary>
        public event Action? CloseRequested;
        /// <summary>OSR popup widget (&lt;select&gt;/autocomplete) 显隐 (true=显示; false 后宿主擦掉 popup 区域)。</summary>
        public event Action<bool>? PopupShow;
        /// <summary>popup 在 view 坐标系的矩形 (x, y, w, h)。</summary>
        public event Action<int, int, int, int>? PopupSize;
        /// <summary>popup 帧 (语义同 Paint; blit 到 PopupSize 矩形之上)。</summary>
        public event EventHandler<PaintEventArgs>? PopupPaint;

        // ── MINOR6 (2026-06-13): 右键菜单接缝 / 通知权限 ──
        /// <summary>FB-P024: 网页右键 (同步)。宿主接管须置 e.Handled=true 抑制内核原生菜单, 再异步弹自定义菜单 (含微应用注册项)。</summary>
        public event EventHandler<ContextMenuEventArgs>? ContextMenuRequested;
        /// <summary>FB-P026: Web Notification 权限请求 (经 RespondNotificationPermission 应答; 不应答=默认拒绝)。</summary>
        public event EventHandler<NotificationPermissionEventArgs>? NotificationPermissionRequested;

        internal ArupaWebView(IntPtr kernel, ArupaWebViewOptions opts)
        {
            _onPaint = OnPaintThunk;
            _onCursor = (u, ct) => CursorChanged?.Invoke(ct);   // FB-P016
            _onRenderGone = (u, crashed) => RenderProcessGone?.Invoke(crashed != 0);   // FB-P017
            _onNavigationState = (u, json) => { try { NavigationStateChanged?.Invoke(Interop.Utf8(json)); } catch { } };
            _onPageFinished = OnPageFinishedThunk;
            _onError = OnErrorThunk;
            _onIntercept = OnInterceptThunk;
            _onInterceptEx = OnInterceptExThunk;   // #34: 内核优先走这条, 资源类型只在它上面
            _onDownload = OnDownloadThunk;
            // FB-P003 wiring 回调。
            _onTitle = (u, t) => TitleChanged?.Invoke(Interop.Utf8(t));
            _onFaviconUrls = (u, p, j) => FaviconUrls?.Invoke(Interop.Utf8(p), Interop.Utf8(j));   // FB-P039
            // 回调内勿重入内核控制函数 (capi §3): 用 progress<100 推断 loading, 不调 is_loading。
            _onProgress = (u, p) => { ProgressChanged?.Invoke(p);
                LoadingStateChanged?.Invoke(p < 100, p); };
            _onSslState = (u, secure) => SslStateChanged?.Invoke(secure != 0);   // FB-P025

            _onFindResult = (u, a, n, d) => FindResult?.Invoke(this,
                new FindResultEventArgs { ActiveMatch = a, NumMatches = n, Done = d != 0 });
            _onJsAlert = (u, id, url, msg) => RaiseJsDialog(JsDialogKind.Alert, id, url, msg, IntPtr.Zero);
            _onJsConfirm = (u, id, url, msg) => RaiseJsDialog(JsDialogKind.Confirm, id, url, msg, IntPtr.Zero);
            _onJsPrompt = (u, id, url, msg, dv) => RaiseJsDialog(JsDialogKind.Prompt, id, url, msg, dv);
            _onJsBeforeUnload = (u, id, url) => RaiseJsDialog(JsDialogKind.BeforeUnload, id, url, IntPtr.Zero, IntPtr.Zero);
            _onShowFullscreen = u => RaiseLegacyFullscreen(true);
            _onHideFullscreen = u => RaiseLegacyFullscreen(false);
            _onPictureInPictureRequest = (u, url) => RaiseLegacyPictureInPicture(Interop.Utf8(url));
            _onPresentationChanged = (u, mode, reason, seq) =>
            {
                // 新内核装配了本回调后不再触发旧回调 → 这里把统一事件还原成旧事件, 使 PC 侧订阅语义不变。
                // (旧内核不认识第 36 个字段 → 走旧回调, 本 handler 不会被调用, 也不会双发。)
                if (mode == PresentationModeFullscreen)
                    FullscreenChanged?.Invoke(reason == PresentationReasonPageRequest);
                else if (mode == PresentationModePictureInPicture &&
                         reason == PresentationReasonPageRequest)
                    PictureInPictureRequested?.Invoke(null);   // 统一回调不带 url: 宿主自己取当前页视频源
                PresentationChanged?.Invoke(mode, reason, seq);
            };
            _onBrowserCommand = (u, command) =>
            {
                var handler = BrowserCommandRequested;
                if (handler == null || command < 1 || command > 2) return 0;
                try { handler((BrowserCommand)command); return 1; }
                catch { return 0; } // Never unwind a managed exception through Chromium.
            };
            _onNewContents = (u, token, targetUrl, disposition, userGesture) =>
            {
                // The native callback carries fields, not a JSON pointer. Keep the IPC
                // token as a decimal string so its full int64 value survives serialization.
                try
                {
                    var handler = NewContentsRequested;
                    if (handler == null)
                    {
                        Console.Error.WriteLine("[arupa][new-contents] No host listener for pending contents.");
                        return;
                    }
                    handler(JsonSerializer.Serialize(new
                    {
                        token = token.ToString(System.Globalization.CultureInfo.InvariantCulture),
                        url = Interop.Utf8(targetUrl), disposition, userGesture = userGesture != 0,
                    }));
                }
                catch (Exception error)
                {
                    // Never unwind across the native callback, but retain a diagnostic.
                    Console.Error.WriteLine($"[arupa][new-contents] Callback failed: {error.GetType().Name}");
                }
            };
            _onNewWindow = (u, url) => NewWindowRequested?.Invoke(Interop.Utf8(url));
            _onShouldOverride = (u, url, m, r, g) =>
            {
                // FB-P156: 详细版优先; 没人订才回落到只有 URL 的旧版。
                var hd = NavigationStartingDetailed;
                if (hd != null)
                {
                    try
                    {
                        return hd(new NavigationStartingInfo(
                            Interop.Utf8(url), m != 0, r != 0, g != 0)) ? 1 : 0;
                    }
                    // ⚠ 抛了就**放行**, 不是拦 —— 一个写错的处理器不该把整个浏览器
                    //   变成"什么都打不开"。与本类其它 thunk 同一条纪律。
                    catch { return 0; }
                }
                var h = NavigationStarting;
                if (h == null) return 0;
                try { return h(Interop.Utf8(url)) ? 1 : 0; } catch { return 0; }
            };
            // FB-P013 第三批: 权限/认证/文件选择 thunk (事件为空即不抛, 总装配无副作用)。
            _onHttpAuth = (u, id, host, realm) => RaiseRequest(HttpAuthRequested,
                new HttpAuthEventArgs { AuthId = id, Host = Interop.Utf8(host), Realm = Interop.Utf8(realm) }, () => RespondHttpAuth(id, false, null, null));
            _onGeoPrompt = (u, id, origin) => RaiseRequest(GeolocationRequested,
                new GeolocationEventArgs { GeoId = id, Origin = Interop.Utf8(origin) }, () => RespondGeolocation(id, false));
            _onPermission = (u, id, origin, wa, wv) => RaiseRequest(MediaPermissionRequested,
                new MediaPermissionEventArgs { PermId = id, Origin = Interop.Utf8(origin), WantAudio = wa != 0, WantVideo = wv != 0 }, () => RespondMediaPermission(id, false));
            _onFileChooser = (u, id, mode, accept) => RaiseRequest(FileChooserRequested,
                new FileChooserEventArgs { ChooserId = id, Mode = (FileChooserMode)mode, AcceptTypes = Interop.Utf8(accept) }, () => RespondFileChooser(id, Array.Empty<string>()));
            // A批 thunk (事件空即丢弃, 装配无副作用; on_download_updated 故意 Zero 见字段注释)。
            _onConsoleMessage = (u, lv, msg, src, ln) =>
                ConsoleMessage?.Invoke(lv, Interop.Utf8(msg), Interop.Utf8(src), ln);
            _onCertError = (u, id, url, err, pem) => RaiseRequest(CertificateErrorOccurred,
                new CertificateErrorEventArgs { CertErrorId = id, Url = Interop.Utf8(url), CertError = err, Pem = Interop.Utf8(pem) }, () => RespondCertificateError(id, false));
            _onCloseWindow = u => CloseRequested?.Invoke();
            _onPopupShow = (u, s) => PopupShow?.Invoke(s != 0);
            _onPopupSize = (u, x, y, w, h) => PopupSize?.Invoke(x, y, w, h);
            _onPopupPaint = (u, px, w, h, dx, dy, dw, dh) => PopupPaint?.Invoke(this,
                new PaintEventArgs { Pixels = px, Width = w, Height = h, DirtyX = dx, DirtyY = dy, DirtyW = dw, DirtyH = dh });
            // MINOR6 (FB-P024): 同步回调 — 宿主置 Handled=true 即返 1 抑制内核原生菜单 (未订阅/异常=0 走内核默认)。
            _onContextMenu = (u, mt, link, src, sel, page, x, y, ed) =>
            {
                var h = ContextMenuRequested;
                if (h == null) return 0;
                var args = new ContextMenuEventArgs
                {
                    MediaType = (ContextMenuMediaType)mt,
                    LinkUrl = Interop.Utf8(link), SrcUrl = Interop.Utf8(src),
                    SelectionText = Interop.Utf8(sel), PageUrl = Interop.Utf8(page),
                    X = x, Y = y, IsEditable = ed != 0,
                };
                try { h(this, args); } catch { return 0; }
                return args.Handled ? 1 : 0;
            };
            // MINOR6 (FB-P026): 通知权限请求 (事件空即不抛, 内核未收应答默认拒绝)。
            _onNotificationPerm = (u, id, origin) => RaiseRequest(NotificationPermissionRequested,
                new NotificationPermissionEventArgs { NotifId = id, Origin = Interop.Utf8(origin) }, () => RespondNotificationPermission(id, false));

            var osr = new Interop.OsrSink
            {
                on_paint = Marshal.GetFunctionPointerForDelegate(_onPaint),
                on_cursor = Marshal.GetFunctionPointerForDelegate(_onCursor),   // FB-P016
                // A批 popup widget (FB-P018: 字段缺失曾致内核读栈垃圾当函数指针 → 野跳崩溃)。
                on_popup_show = Marshal.GetFunctionPointerForDelegate(_onPopupShow),
                on_popup_size = Marshal.GetFunctionPointerForDelegate(_onPopupSize),
                on_popup_paint = Marshal.GetFunctionPointerForDelegate(_onPopupPaint),
            };
            // should_intercept_request: 始终装配 — 每请求回调, ShouldInterceptRequest 为 null 时
            // thunk 返 Zero=放行, 零副作用。on_download_start: 仅 InterceptDownloads 时装配 —
            // 内核"注册即取消"语义, 误装配会取消所有下载, 故必须创建期门控。
            // _ex 与旧回调**都装**: 内核优先 _ex (带 resource_type); 老 DLL 若忽略 _ex,
            // 仍能经旧字段拦截, 只是没有资源类型 —— 两个 thunk 同源, 不存在双重处置。
            var cbs = new Interop.WebViewCallbacks
            {
                on_page_finished = Marshal.GetFunctionPointerForDelegate(_onPageFinished),
                on_received_error = Marshal.GetFunctionPointerForDelegate(_onError),
                should_intercept_request = Marshal.GetFunctionPointerForDelegate(_onIntercept),
                should_intercept_request_ex = Marshal.GetFunctionPointerForDelegate(_onInterceptEx),   // #34
                on_render_process_gone = Marshal.GetFunctionPointerForDelegate(_onRenderGone),   // FB-P017
            };
            if (opts.InterceptDownloads)
                cbs.on_download_start = Marshal.GetFunctionPointerForDelegate(_onDownload);
            // FB-P003: 这批回调无副作用 (事件为空即不抛),总装配。
            cbs.on_title = Marshal.GetFunctionPointerForDelegate(_onTitle);
            cbs.on_progress = Marshal.GetFunctionPointerForDelegate(_onProgress);
            cbs.on_ssl_state_changed = Marshal.GetFunctionPointerForDelegate(_onSslState);   // FB-P025
            cbs.on_find_result = Marshal.GetFunctionPointerForDelegate(_onFindResult);
            cbs.on_js_alert = Marshal.GetFunctionPointerForDelegate(_onJsAlert);
            cbs.on_js_confirm = Marshal.GetFunctionPointerForDelegate(_onJsConfirm);
            cbs.on_js_prompt = Marshal.GetFunctionPointerForDelegate(_onJsPrompt);
            cbs.on_js_before_unload = Marshal.GetFunctionPointerForDelegate(_onJsBeforeUnload);
            cbs.on_show_fullscreen = Marshal.GetFunctionPointerForDelegate(_onShowFullscreen);
            cbs.on_hide_fullscreen = Marshal.GetFunctionPointerForDelegate(_onHideFullscreen);
            cbs.on_picture_in_picture_request = Marshal.GetFunctionPointerForDelegate(_onPictureInPictureRequest);
            cbs.on_presentation_changed = Marshal.GetFunctionPointerForDelegate(_onPresentationChanged);
            cbs.on_browser_command = Marshal.GetFunctionPointerForDelegate(_onBrowserCommand);
            cbs.on_open_new_window = Marshal.GetFunctionPointerForDelegate(_onNewWindow);
            cbs.should_override_url_loading = Marshal.GetFunctionPointerForDelegate(_onShouldOverride);
            // FB-P013 第三批: 装配权限/认证/文件选择回调 (struct 字段已在 capi ABI, 仅填指针)。
            cbs.on_http_auth = Marshal.GetFunctionPointerForDelegate(_onHttpAuth);
            cbs.on_geolocation_prompt = Marshal.GetFunctionPointerForDelegate(_onGeoPrompt);
            cbs.on_permission_request = Marshal.GetFunctionPointerForDelegate(_onPermission);
            cbs.on_show_file_chooser = Marshal.GetFunctionPointerForDelegate(_onFileChooser);
            // A批: console/证书错误/关窗 (FB-P018 同步)。on_download_updated 留 Zero (语义开关, 见字段注释)。
            cbs.on_console_message = Marshal.GetFunctionPointerForDelegate(_onConsoleMessage);
            cbs.on_certificate_error = Marshal.GetFunctionPointerForDelegate(_onCertError);
            cbs.on_close_window = Marshal.GetFunctionPointerForDelegate(_onCloseWindow);
            // MINOR6 (FB-P024/P026): 右键菜单 / 通知权限 (事件空即无副作用, 总装配)。
            // ⚠ on_dialog_reset(#29)/on_http_auth_ex(#30) PC 暂不订阅, 留 Zero (占位字段维持偏移)。
            cbs.on_context_menu = Marshal.GetFunctionPointerForDelegate(_onContextMenu);
            cbs.on_notification_permission_requested = Marshal.GetFunctionPointerForDelegate(_onNotificationPerm);
            // MINOR8 (FB-P039): 页面 favicon 候选列表 (事件空即无副作用, 总装配)。
            cbs.on_favicon_urls = Marshal.GetFunctionPointerForDelegate(_onFaviconUrls);
            var cfg = new Interop.WebViewConfig
            {
                width = opts.Width,
                height = opts.Height,
                partition_name = opts.PartitionName,
                off_the_record = opts.OffTheRecord ? 1 : 0,
                proxy_url = opts.ProxyUrl,
            };
            // KI-001 (FB-P018 根治): 优先 size-checked 变体 — 传本 wrapper 编译期 sizeof,
            // 新内核只复制 caller_size 内的完整字段，截断字段直接拒绝；缺尾字段置零 → wrapper 不同步也只
            // 少订阅新回调, 永不野跳崩 (断 FB-P012/P016/P018 一族)。旧 dll(MINOR<2)无此导出 →
            // EntryPointNotFoundException 回退完整信任版 (维持原行为)。
            if (opts.PendingContentsToken is string pendingToken)
            {
                if (!long.TryParse(pendingToken, System.Globalization.NumberStyles.None,
                    System.Globalization.CultureInfo.InvariantCulture, out long token) || token <= 0)
                    throw new ArgumentException("Pending contents token must be a positive int64.", nameof(opts));
                // Adoption creates the ArupaWebView itself and attaches the original
                // WebContents. Do not create/reload a replacement: that loses opener/POST.
                int r = Interop.arupa_webview_adopt_pending(kernel, token, in cfg, in cbs, in osr, out _handle);
                if (r != 0)
                    throw new InvalidOperationException($"arupa_webview_adopt_pending failed: {r}");
            }
            else
            {
                try
                {
                    int r = Interop.arupa_webview_create_checked(kernel,
                        in cfg, (nuint)Marshal.SizeOf<Interop.WebViewConfig>(),
                        in cbs, (nuint)Marshal.SizeOf<Interop.WebViewCallbacks>(),
                        in osr, (nuint)Marshal.SizeOf<Interop.OsrSink>(),
                        out _handle);
                    if (r != 0)
                        throw new InvalidOperationException($"arupa_webview_create_checked failed: {r}");
                }
                catch (EntryPointNotFoundException)
                {
                    int r = Interop.arupa_webview_create(kernel, in cfg, in cbs, in osr, out _handle);
                    if (r != 0)
                        throw new InvalidOperationException($"arupa_webview_create failed: {r}");
                }
            }
            // 装配了 PiP 回调 = 宿主接管画中画 → 让网页显示 PiP 入口。保持默认关闭时
            // document.pictureInPictureEnabled=false, 请求根本不会到内核 (也就不会回调)。
            EnableHostPictureInPicture();
            if (Interop.arupa_kernel_abi_minor() >= 27)
                Interop.arupa_webview_set_new_contents_callback(_handle,
                    Marshal.GetFunctionPointerForDelegate(_onNewContents), IntPtr.Zero);
        }

        private void OnPaintThunk(IntPtr u, IntPtr px, int w, int h, int dx, int dy, int dw, int dh)
            => Paint?.Invoke(this, new PaintEventArgs
            {
                Pixels = px, Width = w, Height = h,
                DirtyX = dx, DirtyY = dy, DirtyW = dw, DirtyH = dh,   // FB-P009: 透出脏矩形 (原丢弃)
            });
        private void OnPageFinishedThunk(IntPtr u, IntPtr url)
        {
            // 跨进程导航 (含 about:blank→自定义 scheme 内部 prime) 后 CDP 的 addBinding 状态 + Runtime
            //   executionContextCreated 事件流不随新渲染进程续命 → 桥往返不回 (bindingCalled 不触发) +
            //   origin 门控失据 (_ctxOrigin 空)。executionContextCreated 事件本身也不流过来 (故不能靠它触发),
            //   但本 on_page_finished 是内核原生回调, 新进程导航后必触发 → 在此重 enable+addBinding 让桥续命。
            if (_bridgeInfra) _ = ReaddBridgeBindingAsync();
            PageFinished?.Invoke(this, Interop.Utf8(url));
        }
        private void OnErrorThunk(IntPtr u, IntPtr url, int code, IntPtr desc)
            => ReceivedError?.Invoke(Interop.Utf8(url), code, Interop.Utf8(desc));

        // FB-P002 C2/B2: 资源拦截 thunk (IO 线程, 同步返回)。返 Zero=放行; 否则返回内核要拷走的
        // ArupaInterceptResponse* (单槽缓冲, 下次回调/Dispose 释放上次)。
        private IntPtr _interceptMime, _interceptCharset, _interceptHeaders, _interceptBody;
        private IntPtr _interceptSetReqHdr, _interceptRemoveReqHdr, _interceptSetRespHdr, _interceptRemoveRespHdr;

        // 老签名 (无 resource_type): 仅在内核不支持 _ex 时用到。
        private IntPtr OnInterceptThunk(IntPtr u, IntPtr url, IntPtr method, int isMainFrame, IntPtr headers)
            => InterceptThunkCore(url, method, IntPtr.Zero, isMainFrame, headers);

        // #34: 内核优先走这条 (arupa_intercept_loader.cc:484), 因此资源类型只在 _ex 上才拿得到。
        private IntPtr OnInterceptExThunk(IntPtr u, IntPtr url, IntPtr method, IntPtr resourceType,
                                          int isMainFrame, IntPtr headers)
            => InterceptThunkCore(url, method, resourceType, isMainFrame, headers);

        private IntPtr InterceptThunkCore(IntPtr url, IntPtr method, IntPtr resourceType, int isMainFrame, IntPtr headers)
        {
            var h = ShouldInterceptRequest;
            if (h == null) return IntPtr.Zero;
            InterceptResponse? resp;
            try
            {
                resp = h(new InterceptRequest
                {
                    Url = Interop.Utf8(url) ?? "",
                    Method = Interop.Utf8(method) ?? "GET",
                    ResourceType = Interop.Utf8(resourceType) ?? "",
                    IsForMainFrame = isMainFrame != 0,
                    Headers = Interop.Utf8(headers),
                });
            }
            catch { return IntPtr.Zero; }   // 宿主回调抛 → 放行
            if (resp == null) return IntPtr.Zero;

            lock (_interceptLock)
            {
                FreeLastInterceptResp();    // 内核已拷走上次, 现可释放
                _interceptMime = Marshal.StringToCoTaskMemUTF8(resp.MimeType ?? "text/html");
                _interceptCharset = resp.Charset != null ? Marshal.StringToCoTaskMemUTF8(resp.Charset) : IntPtr.Zero;
                _interceptHeaders = resp.Headers != null ? Marshal.StringToCoTaskMemUTF8(resp.Headers) : IntPtr.Zero;
                int bodyLen = resp.Body?.Length ?? 0;
                if (bodyLen > 0)
                {
                    _interceptBody = Marshal.AllocCoTaskMem(bodyLen);
                    Marshal.Copy(resp.Body!, 0, _interceptBody, bodyLen);
                }
                // 空串按"不改"处理: 内核只判非 null 指针, 给空串会走一趟空解析。
                _interceptSetReqHdr = StrOrZero(resp.SetRequestHeaders);
                _interceptRemoveReqHdr = StrOrZero(resp.RemoveRequestHeaders);
                _interceptSetRespHdr = StrOrZero(resp.SetResponseHeaders);
                _interceptRemoveRespHdr = StrOrZero(resp.RemoveResponseHeaders);
                var native = new Interop.InterceptResponseNative
                {
                    status_code = resp.StatusCode,
                    mime_type = _interceptMime,
                    charset = _interceptCharset,
                    headers = _interceptHeaders,
                    body = _interceptBody,
                    body_len = (IntPtr)bodyLen,
                    action = (int)resp.Action,
                    set_request_headers = _interceptSetReqHdr,
                    remove_request_headers = _interceptRemoveReqHdr,
                    set_response_headers = _interceptSetRespHdr,
                    remove_response_headers = _interceptRemoveRespHdr,
                };
                _lastInterceptResp = Marshal.AllocCoTaskMem(Marshal.SizeOf<Interop.InterceptResponseNative>());
                Marshal.StructureToPtr(native, _lastInterceptResp, false);
                return _lastInterceptResp;
            }
        }

        private static IntPtr StrOrZero(string? s)
            => string.IsNullOrEmpty(s) ? IntPtr.Zero : Marshal.StringToCoTaskMemUTF8(s);

        private void FreeLastInterceptResp()
        {
            void Free(ref IntPtr p) { if (p != IntPtr.Zero) { Marshal.FreeCoTaskMem(p); p = IntPtr.Zero; } }
            Free(ref _lastInterceptResp);
            Free(ref _interceptMime); Free(ref _interceptCharset);
            Free(ref _interceptHeaders); Free(ref _interceptBody);
            Free(ref _interceptSetReqHdr); Free(ref _interceptRemoveReqHdr);
            Free(ref _interceptSetRespHdr); Free(ref _interceptRemoveRespHdr);
        }

        // FB-P002 B4: 下载 thunk (内核已取消该下载, 此处仅通知宿主接管路由)。
        private void OnDownloadThunk(IntPtr u, IntPtr url, IntPtr mime, IntPtr name,
                                     long len, IntPtr referer)
            => DownloadStarted?.Invoke(this, new DownloadStartedEventArgs
            {
                Url = Interop.Utf8(url),
                Mime = Interop.Utf8(mime),
                SuggestedName = Interop.Utf8(name),
                ContentLength = len,
                Referer = Interop.Utf8(referer),
            });

        [Obsolete("Set ArupaWebViewOptions.PendingContentsToken when creating a view instead.")]
        public void AdoptPendingContents(string token)
            => throw new NotSupportedException("Pending contents must be adopted when creating a view.");
        public async Task<bool> RequestCloseAsync()
        {
            NativeEvalRequests.Request? request;
            lock (_evalLifetime)
            {
                if (_handle == IntPtr.Zero) return true;
                request = _evalRequests.TryStart();
                if (request == null) return false;
                Interop.arupa_webview_request_close(_handle, NativeEvalRequests.CallbackPointer, request.User);
            }
            return await request.WaitAsync(31000).ConfigureAwait(false) == "true";
        }

        /// <summary>在宿主创建的专用 frontend view 中打开当前页的 Chromium DevTools。
        /// 宿主负责显示、输入、缩放和关闭此 view。需要 devtools_resources.pak 和 ABI 1.29 能力。</summary>
        public void OpenDevTools(ArupaWebView frontend)
        {
            ArgumentNullException.ThrowIfNull(frontend);
            if (_handle == IntPtr.Zero || frontend._handle == IntPtr.Zero)
                throw new ObjectDisposedException(nameof(ArupaWebView));
            if (Interop.arupa_kernel_supports("devtools.frontend.v1") != 1)
                throw new NotSupportedException("The kernel does not support devtools.frontend.v1.");
            int result = Interop.arupa_webview_open_devtools(_handle, frontend._handle);
            if (result != 0)
                throw new InvalidOperationException($"arupa_webview_open_devtools failed: {result}");
        }

        /// <summary>解除本 DevTools frontend 的调试连接并转到 about:blank，保留宿主 view。</summary>
        public void CloseDevTools()
        {
            if (_handle == IntPtr.Zero) throw new ObjectDisposedException(nameof(ArupaWebView));
            if (Interop.arupa_kernel_supports("devtools.frontend.v1") != 1)
                throw new NotSupportedException("The kernel does not support devtools.frontend.v1.");
            Interop.arupa_webview_close_devtools(_handle);
        }

        public void LoadUrl(string url) => Interop.arupa_webview_load_url(_handle, url);
        public void Reload() => Interop.arupa_webview_reload(_handle);
        // FB-P008: 硬刷新 (绕过缓存, ReloadType::BYPASSING_CACHE, 对标 CefSharp Reload(ignoreCache:true))。
        public void ReloadIgnoreCache() => Interop.arupa_webview_reload_ignore_cache(_handle);
        public void GoBack() => Interop.arupa_webview_go_back(_handle);
        public void GoForward() => Interop.arupa_webview_go_forward(_handle);
        public string? Url => Interop.TakeOwned(Interop.arupa_webview_get_url(_handle));
        public string? Title => Interop.TakeOwned(Interop.arupa_webview_get_title(_handle));
        public void Resize(int w, int h) => Interop.arupa_webview_resize(_handle, w, h);
        /// <summary>Mac OSR content view's actual screen rectangle in DIP.
        /// Re-send when the host window moves or the view layout changes.</summary>
        public void SetScreenRect(int x, int y, int width, int height)
        {
            if (_handle == IntPtr.Zero) throw new ObjectDisposedException(nameof(ArupaWebView));
            if (Interop.arupa_kernel_supports("mac.osr.screen_rect.v1") != 1) return;
            Interop.arupa_webview_set_screen_rect(_handle, x, y, width, height);
        }
        /// <summary>宿主或系统退出全屏时调用；异步退出当前页面的 HTML 全屏。
        /// FullscreenChanged(false) 表示内核状态已退出，DOM 通过 fullscreenchange 确认。
        /// 需要支持 fullscreen.host_exit 的内核。</summary>
        public void ExitFullscreen()
        {
            if (_handle == IntPtr.Zero)
                throw new ObjectDisposedException(nameof(ArupaWebView));
            if (Interop.arupa_kernel_supports("fullscreen.host_exit") != 1)
                throw new NotSupportedException("The kernel does not support fullscreen.host_exit.");
            Interop.arupa_webview_exit_fullscreen(_handle);
        }

        /// <summary>宿主接管画中画 (MINOR24 pip.host)。开启后网页 requestPictureInPicture() 会以
        /// PictureInPictureRequested 抛给宿主 (内核仍按 kNotSupported 拒绝该会话 — 它没有原生 PiP 窗)。
        /// 老内核无此能力时静默保持默认关闭。</summary>
        public void SetHostPictureInPictureEnabled(bool enabled)
        {
            if (_handle == IntPtr.Zero)
                throw new ObjectDisposedException(nameof(ArupaWebView));
            try
            {
                if (Interop.arupa_kernel_supports("pip.host") != 1) return;
                Interop.arupa_webview_set_picture_in_picture_host_enabled(_handle, enabled ? 1 : 0);
            }
            catch (EntryPointNotFoundException) { }   // 旧 dll: 能力不存在
            catch (DllNotFoundException) { }
        }
        /// <summary>Report the host window result for a presentation request (0 failed, 1 shown, 2 closed).</summary>
        public void ReportPictureInPicture(ulong requestSequence, int result)
        {
            if (_handle == IntPtr.Zero) throw new ObjectDisposedException(nameof(ArupaWebView));
            if (requestSequence == 0 || result < 0 || result > 2) return;
            try
            {
                if (Interop.arupa_kernel_supports("pip.host_result.v1") == 1)
                    Interop.arupa_webview_report_picture_in_picture(_handle, requestSequence, result);
            }
            catch (EntryPointNotFoundException) { }
        }

        private void EnableHostPictureInPicture()
        {
            try { SetHostPictureInPictureEnabled(true); }
            catch (ObjectDisposedException) { }
        }

        // ── 统一呈现通道: 旧内核路径归一 ──
        // 老内核 (无 presentation.v1) 不认识 on_presentation_changed, 只会调旧回调; 这里把同一次变化
        // 也送上统一通道, 于是宿主只订阅 PresentationChanged 一个事件就能同时覆盖新旧内核。不会双发:
        // 新内核只回调 _onPresentationChanged, 旧内核只回调下面两个 helper。
        private long _presentationSeq;
        private ulong NextPresentationSeq()
            => (ulong)System.Threading.Interlocked.Increment(ref _presentationSeq);
        private void RaiseLegacyFullscreen(bool on)
        {
            FullscreenChanged?.Invoke(on);
            PresentationChanged?.Invoke(
                PresentationModeFullscreen,
                on ? PresentationReasonPageRequest : PresentationReasonPageExit,
                NextPresentationSeq());
        }
        private void RaiseLegacyPictureInPicture(string? url)
        {
            PictureInPictureRequested?.Invoke(url);
            PresentationChanged?.Invoke(
                PresentationModePictureInPicture,
                PresentationReasonPageRequest,
                NextPresentationSeq());
        }

        /// <summary>统一呈现通道的反向出口 (MINOR25 presentation.v1): 宿主关掉某个呈现模式后通知内核清状态。
        /// mode=1 全屏 → 内核 ExitFullscreen (清指针/键盘锁 + 同步 renderer, 并回发一次退出通知);
        /// mode=2 画中画 → 内核只清"宿主已接管"标记 (它没有 PiP 窗可关)。老内核静默 no-op。</summary>
        public void ExitPresentation(int mode)
        {
            if (_handle == IntPtr.Zero)
                throw new ObjectDisposedException(nameof(ArupaWebView));
            try
            {
                if (Interop.arupa_kernel_supports("presentation.v1") != 1) return;
                Interop.arupa_webview_exit_presentation(_handle, mode);
            }
            catch (EntryPointNotFoundException) { }   // 旧 dll: 能力不存在
            catch (DllNotFoundException) { }
        }
        // FB-P014: 后台 tab 停渲染。active=false → WasHidden + 停 OSR capturer (页面 visibilityState=hidden
        //   顺带停 rAF, 内核不再投 on_paint); active=true → WasShown + Restart (复用 FB-P005 retarget)。
        public void SetRenderActive(bool active) => Interop.arupa_webview_set_render_active(_handle, active ? 1 : 0);
        public string? GetImeState() => Interop.TakeOwned(Interop.arupa_webview_get_ime_state(_handle));
        public bool UpdateIme(string text, int selection, bool commit, long document)
        {
            if (_handle == IntPtr.Zero || document < 0 || text == null || text.Length > 32768) return false;
            // The C ABI is (view, action, UTF-8 text, selectionStart, selectionEnd), returning void.
            // It queues the edit; true means submitted, not a native status code.
            int caret = Math.Clamp(selection, 0, text.Length);
            Interop.arupa_webview_update_ime(_handle, commit ? 1 : 0, text, caret, caret);
            return true;
        }
        public string? GetNetworkSettings()
            => Interop.TakeOwned(Interop.arupa_webview_get_network_settings(_handle));

        public void SetProxy(string socks5Url) { Interop.arupa_webview_set_proxy(_handle, socks5Url); ThrowRouteError(); }

        // ── FB-P013 护城河: 传输路由 / 延迟代理 / 待生效身份 / 诊断 ────────────────
        /// <summary>延迟代理: 下次导航生效 (身份/出口轮换用; 区别于即时 SetProxy, 即时后写覆盖)。</summary>
        public void SetPendingProxy(string socks5Url) => Interop.arupa_webview_set_pending_proxy(_handle, socks5Url);
        /// <summary>上一次 <see cref="InjectIsolatedJsAsync"/> 在**同步入口**上拿到的失败说明。
        /// 拿到 `null` 结果时读它, 就能分清是"越界/参数不对"还是"脚本真的返回了 null"。
        /// null = 那次同步入口没报错(结果为 null 就是脚本自己返回的)。</summary>
        public string? LastInjectError { get; private set; }

        /// <summary>
        /// 上一次隔离世界注入的 last_error **原始值**(未经陈旧值过滤)。
        /// 🔴 给**判据**用: <see cref="LastInjectError"/> 那层过滤是为兜住内核 FB-P084 而加的,
        ///    它会把"成功调用却留着旧错"这件事抹平 —— 判据要是也读那个值, 就永远测不出内核修没修。
        ///    业务面读 LastInjectError(安全), 判据读这个(有检出能力)。
        /// </summary>
        public string? LastInjectErrorRaw { get; private set; }

        /// <summary>
        /// MV3 Phase 1: 在**隔离世界** world_id∈[1,11] 执行 JS, 取 JSON 结果 (超时/失败 null)。
        ///
        /// 隔离世界与主世界(0, 内核 15 维反指纹 JS 所在)物理隔离: 独立 JS 上下文、共享 DOM,
        /// 读不到也改不到主世界全局。MV3 content script 就注在这儿。
        /// ⚠ world_id 越界 (不在 [1,11]) 内核 no-op —— 不崩, 但也**不会回调**, 故本包装靠超时收场。
        /// ⚠ 每插件占一个 world 的生命周期, 分配/复用由 App 层加载器负责 (browser 侧每帧最多 ~11 个)。
        /// </summary>
        /// <remarks>⚠ 2026-08-26 修: 本段说明原先与 <c>SetTransportRoute</c> 和
        /// <see cref="LastInjectError"/> 的 summary **三段连着写在同一处**(中间没有代码)
        /// ⇒ 三个 summary 全挂在了 <see cref="LastInjectError"/> 上, 而本方法与
        /// <c>SetTransportRoute</c> **各自一句注释都没有**。
        /// 🔴 本方法是插件线的核心入口(隔离世界注入), IDE 里悬停看到的却是别人的说明。</remarks>
        public async System.Threading.Tasks.Task<string?> InjectIsolatedJsAsync(
            int worldId, string script, int timeoutMs = 5000)
        {
            NativeEvalRequests.Request? request = null;
            try
            {
                lock (_evalLifetime)
                {
                    LastInjectError = LastInjectErrorRaw = null;
                    if (_handle == IntPtr.Zero || timeoutMs <= 0) return null;
                    request = _evalRequests.TryStart();
                    if (request == null) return null;
                    Interop.arupa_webview_inject_isolated_js(
                        _handle, worldId, script, NativeEvalRequests.CallbackPointer, request.User);
                    // Native last_error is thread-local: read before the first await.
                    // Preserve raw evidence and the legacy stale-world error filter.
                    var errJson = ArupaKernel.LastErrorJson();
                    LastInjectErrorRaw = errJson;
                    LastInjectError =
                        errJson != null && errJson.Contains("\"world_id=" + worldId + "\"", StringComparison.Ordinal)
                            ? errJson : null;
                }
                return await request.WaitAsync(timeoutMs).ConfigureAwait(false);
            }
            catch { return null; }
            finally { request?.Cancel(); }
        }

        /// <summary>
        /// 在**主世界**(world 0)执行 JS, 取 JSON 结果 (超时/失败返回 null)。
        ///
        /// 🔴 与 <see cref="InjectIsolatedJsAsync"/> 的分工, **别选错**:
        /// · 本方法 = 主世界 —— 页面自己的 JS 上下文。读得到 <c>document.title</c>、
        ///   写得到 <c>window.*</c> 且**页面里的脚本看得见**。
        /// · <see cref="InjectIsolatedJsAsync"/> = 隔离世界 —— 独立上下文、共享 DOM,
        ///   页面里的脚本**看不见**你写的全局。MV3 content script 注在那儿。
        ///
        /// ⚠ 什么时候必须用本方法: 往扩展页面里装 <c>nomad-plugin-runtime.js</c> 那类
        /// "**要让页面里的脚本够得着**"的东西。用隔离世界注入的话, 插件自己的代码
        /// 看不到 <c>window.nomad</c>, 等于没注入。
        ///
        /// ⚠ 已知约束(2026-08-26, 对接侧实测): <c>nomad-plugin-runtime.js</c> 是 **ESM**
        /// (末尾有 <c>export {…}</c>) ⇒ **直接把它整份丢进来是语法错误**, 必须当模块加载
        /// (桌面走 <c>nomad://</c> 供应 + <c>&lt;script type="module"&gt;</c> 引)。
        /// 本方法负责的是"在主世界跑一段 JS", 不负责模块加载。
        /// </summary>
        /// <remarks>
        /// 🔴 **这个包装此前一直缺**(2026-08-26 补): C ABI <c>arupa_webview_eval_js</c> 有、
        /// <c>Interop.cs</c> 也声明了, 但门面**没往外给** ⇒ 对接侧只能看到隔离世界那个,
        /// 拿它做主世界的事必然不成。同轮还发现文档里凭记忆写了一个**根本不存在**的
        /// <c>EvaluateJavaScriptAsync</c>。⇒ 缺口与幻觉是一对: 门面里没有的东西, 文档里会长出来。
        /// </remarks>
        /// <param name="script">要执行的 JS。返回值经内核序列化为 JSON 文本。</param>
        /// <param name="timeoutMs">等回调的上限; 超时返回 null(与"脚本真的返回 null"同形, 这一点与隔离世界那条一致)。</param>
        public async System.Threading.Tasks.Task<string?> EvalJsForDocumentAsync(ulong document, string script, int timeoutMs = 5000)
        {
            NativeEvalRequests.Request? request = null;
            try
            {
                lock (_evalLifetime)
                {
                    if (_handle == IntPtr.Zero || document == 0 || timeoutMs <= 0 || Interop.arupa_kernel_supports("pip.host_result.v1") != 1) return null;
                    request = _evalRequests.TryStart();
                    if (request == null) return null;
                    Interop.arupa_webview_eval_js_for_document(_handle, document, script, NativeEvalRequests.CallbackPointer, request.User);
                }
                return await request.WaitAsync(timeoutMs).ConfigureAwait(false);
            }
            catch { return null; }
            finally { request?.Cancel(); }
        }

        public async System.Threading.Tasks.Task<string?> EvalJsAsync(
            string script, int timeoutMs = 5000)
        {
            NativeEvalRequests.Request? request = null;
            try
            {
                lock (_evalLifetime)
                {
                    if (_handle == IntPtr.Zero || timeoutMs <= 0) return null;
                    request = _evalRequests.TryStart();
                    if (request == null) return null;
                    Interop.arupa_webview_eval_js(
                        _handle, script, NativeEvalRequests.CallbackPointer, request.User);
                }
                return await request.WaitAsync(timeoutMs).ConfigureAwait(false);
            }
            catch { return null; }
            finally { request?.Cancel(); }
        }

        /// <summary>split-tunnel 传输路由 (名单内走 socks / 名单外直连)。传 typed 或裸 JSON。</summary>
        public void SetTransportRoute(string routeJson) { Interop.arupa_webview_set_transport_route(_handle, routeJson); ThrowRouteError(); }
        private static void ThrowRouteError()
        {
            var error = ArupaKernel.LastErrorJson();
            if (!string.IsNullOrEmpty(error)) throw new InvalidOperationException(error);
        }
        public void SetTransportRoute(ArupaTransportRoute route) => SetTransportRoute(route.ToJson());

        /// <summary>**扩展发起的**设出口路由 —— 与 <see cref="SetTransportRoute(string)"/>
        /// 是<b>两个入口</b>。
        /// <para>🔴 为什么分两个而不是加个参数: **两种意图的默认值相反** ——
        /// <c>SetTransportRoute</c> = 宿主自己的意图(设置界面 / 策略), 默认可信;
        /// 本方法 = 扩展发起的意图, 默认<b>要用户确认过</b>才作数。
        /// 合成一个入口的话默认值只能选一个, 而<b>选错的那一侧就是静默放行</b>。</para>
        /// <para>返回码(**每一档给各自的码, 别折成 bool** —— 你要据此决定下一步):
        /// <c>0</c> 判过并**已应用** ·
        /// <c>1</c> 清单没声明 <c>proxy</c> ⇒ 去看清单(**弹框也没用**) ·
        /// <c>2</c> 未签名包要更细的作用域 ⇒ 拒 ·
        /// <c>3</c> 🔴 <b>要用户二次确认, 而你没断言过</b> ⇒ 去弹框, 用户同意后调
        /// <see cref="ArupaBrowser.SetExtensionRouteConsent"/> 再来一次 ·
        /// <c>4</c> 认不出的作用域 · <c>5</c> routeJson 坏 ·
        /// <c>6</c> 内核没登记这个扩展(<b>不是</b>"它没权限") ·
        /// <c>-1</c> 这个内核件没有这个导出。</para>
        /// <para>⚠ <b>拒的时候一条路由都不会被应用</b> —— 上一份配置原样保留,
        /// <b>不会退回直连</b>。</para></summary>
        public int SetTransportRouteForExtension(string extensionId, string routeJson)
        {
            try
            {
                return Interop.arupa_webview_set_transport_route_for_extension(
                    _handle, extensionId, routeJson);
            }
            catch (EntryPointNotFoundException)
            {
                // 🔴 -1 而不是某个拒的码: "这个 dll 没有这个能力"与"它拒了这一次"
                //   是两个问题。折成同一个码的话, 宿主会去弹一个没用的确认框。
                return -1;
            }
        }

        /// <summary>把**这个标签页的编号**告诉内核。
        /// <para>🔴 <c>chrome.tabs.sendMessage(tabId, …)</c> 靠它才投得出去:
        /// 那个整数是<b>宿主的命名空间</b>(<c>tabs.query</c> / <c>action.onClicked</c>
        /// 答出去的都是它), 而内核认识的只有 view —— 不声明就没法把两者对上。</para>
        /// <para>⚠ <b>建 view 的时候调一次就够</b>: 那个号在标签页对象构造时就定、
        /// 此后不变。传负数 = 解绑。</para>
        /// <para>⚠ 内核**不猜** —— 没声明过的号一律投递失败并留日志,
        /// 不会退化成"只有一个 view 那就是它"。</para>
        /// <para>⚠ 旧 dll 上没有这个导出 ⇒ 返回 <c>false</c>(这次声明没生效,
        /// <c>tabs.sendMessage</c> 会投不出去)。</para></summary>
        public bool SetTabId(int tabId)
        {
            try
            {
                Interop.arupa_webview_set_tab_id(_handle, tabId);
                return true;
            }
            catch (EntryPointNotFoundException)
            {
                return false;
            }
        }

        /// <summary>待生效指纹身份 (下次导航激活 C++ 深档反指纹层)。传 typed 或裸 JSON。</summary>
        public void SetPendingIdentity(string identityJson) => Interop.arupa_webview_set_pending_identity(_handle, identityJson);
        public void SetPendingIdentity(ArupaIdentity identity) => SetPendingIdentity(identity.ToJson());
        /// <summary>诊断 (schema "arupa-diag/1"; full=ARUPA_DIAG_FULL 仅本地调试严禁落盘, 默认脱敏)。
        /// 含 identity.applied / fingerprint.intended 等, 供验证身份是否真生效。</summary>
        public string? GetDiagnostics(bool full = false)
            => Interop.TakeOwned(Interop.arupa_webview_get_diagnostics(_handle, full ? 1 : 0));

        // ── FB-P013 第二批: 配置/状态/数据 ────────────────────────────────────────
        /// <summary>加载 HTML 字符串 (错误页/about; baseUrl 空时 origin 为 about:blank)。</summary>
        public void LoadData(string html, string? baseUrl = null)
            => Interop.arupa_webview_load_data(_handle, html, baseUrl);
        /// <summary>web 偏好 (force_dark/load_images/... 仅 schema (a) 5 键)。传 typed 或裸 JSON。</summary>
        public void SetWebPrefs(string prefsJson) => Interop.arupa_webview_set_web_prefs(_handle, prefsJson);
        public void SetWebPrefs(ArupaWebPrefs prefs) => SetWebPrefs(prefs.ToJson());
        /// <summary>序列化本 view 导航/会话状态 (内核黑盒格式; 关 tab 恢复用)。null=失败。</summary>
        public byte[]? SaveState()
        {
            IntPtr p = Interop.arupa_webview_save_state(_handle, out IntPtr len);
            if (p == IntPtr.Zero) return null;
            try
            {
                long length = len.ToInt64();
                if (length <= 0 || length > 16 * 1024 * 1024) return null;
                var buf = new byte[(int)length];
                Marshal.Copy(p, buf, 0, buf.Length);
                return buf;
            }
            finally { Interop.arupa_free(p); }
        }
        /// <summary>恢复至多 16 MiB 的状态。v2 须同 Chromium 版本；v1 只迁移 URL/标题，不恢复旧 PageState。</summary>
        public bool RestoreState(byte[] data)
        {
            if (data == null || data.Length == 0 || data.Length > 16 * 1024 * 1024) return false;
            return Interop.arupa_webview_restore_state(_handle, data, (IntPtr)data.Length) != 0;
        }
        /// <summary>同步落盘 cookie (退出前调, 防丢)。</summary>
        public void FlushCookies() => Interop.arupa_webview_flush_cookies(_handle);
        /// <summary>订阅 WebContents 导航快照；旧内核返回 false。</summary>
        public bool ObserveNavigationState()
        {
            if (_handle == IntPtr.Zero || Interop.arupa_kernel_supports("navigation.snapshot.v1") != 1) return false;
            Interop.arupa_webview_set_navigation_state_callback(_handle,
                Marshal.GetFunctionPointerForDelegate(_onNavigationState), IntPtr.Zero);
            return true;
        }

        /// <summary>取指定 url 的 cookie ("; 分隔 name=value")。</summary>
        public Task<string?> GetCookiesAsync(string url)
        {
            lock (_evalLifetime)
            {
                if (_handle == IntPtr.Zero) return Task.FromResult<string?>(null);
                var request = _evalRequests.TryStart();
                if (request == null) return Task.FromResult<string?>(null);
                try { Interop.arupa_webview_get_cookies(_handle, url, NativeEvalRequests.CallbackPointer, request.User); }
                catch { request.Cancel(); throw; }
                return request.WaitAsync(8000);
            }
        }

        // ── FB-P003: 导航/状态/查找/UA 高层 wiring (capi 已有) ─────────────────────
        public void Stop() => Interop.arupa_webview_stop(_handle);
        public void ClearHistory() => Interop.arupa_webview_clear_history(_handle);
        public bool CanGoBack => Interop.arupa_webview_can_go_back(_handle) != 0;
        public bool CanGoForward => Interop.arupa_webview_can_go_forward(_handle) != 0;
        public bool IsLoading => Interop.arupa_webview_is_loading(_handle) != 0;
        public void SetUserAgent(string ua) => Interop.arupa_webview_set_user_agent(_handle, ua);
        public void SendWheel(int x, int y, int dx, int dy)
            => Interop.arupa_webview_send_wheel(_handle, x, y, dx, dy);
        public void FindAll(string text) => Interop.arupa_webview_find_all(_handle, text);
        public void FindNext(bool forward = true) => Interop.arupa_webview_find_next(_handle, forward ? 1 : 0);
        public void ClearMatches() => Interop.arupa_webview_clear_matches(_handle);
        // FB-P003 第二批: 缩放 (1.0=100%, 1.5=150%) / 标签静音。
        public double ZoomFactor
        {
            get => Interop.arupa_webview_get_zoom(_handle);
            set => Interop.arupa_webview_set_zoom(_handle, value);
        }
        public bool AudioMuted
        {
            get => Interop.arupa_webview_is_audio_muted(_handle) != 0;
            set => Interop.arupa_webview_set_audio_muted(_handle, value ? 1 : 0);
        }
        // FB-P008③/FB-A008: 打印真矢量 PDF (原生 //components/printing PdfPrintJob)。
        // ⚠ 弃用旧 CDP Page.printToPDF — 它在**非 headless** chromium 下不可用 (报 "Printing is not
        //   available")，恒失败 = 用户报"打印PDF未实现"真因。改走原生 capi arupa_webview_print_to_pdf。
        // optionsJson 可空(默认 A4 纵向 100%): {landscape,scale,print_background,paper_width/height(英寸),
        //   margin_*,page_ranges:"1-3,5"}。返 PDF 字节; null=失败。
        public async Task<byte[]?> PrintToPdfAsync(string? optionsJson = null)
        {
            var request = _evalRequests.TryStart();
            if (request == null) return null;
            try
            {
                Interop.arupa_webview_print_to_pdf(_handle, optionsJson,
                    NativeEvalRequests.PdfCallbackPointer, request.User);
                var encoded = await request.WaitAsync(30000).ConfigureAwait(false);
                return encoded == null ? null : Convert.FromBase64String(encoded);
            }
            catch { request.Cancel(); return null; }
        }
        // FB-P003 第二批: 导航历史列表 (CDP Page.getNavigationHistory, 无需新增 capi)。返原始 JSON。
        public Task<string> GetNavigationHistoryAsync()
            => SendCdpAsync("Page.getNavigationHistory");
        // 回应 JS 对话框 (JsDialog 事件后调): accept=确认/取消; promptText 仅 Prompt。
        public void RespondJsDialog(int dialogId, bool accept, string? promptText = null)
            => Interop.arupa_webview_respond_js_dialog(_handle, dialogId, accept ? 1 : 0, promptText);

        // ── FB-P013 第三批: 权限/认证/文件选择应答 (对应事件后调) ──────────────────
        /// <summary>回应 HTTP 认证 (HttpAuthRequested 后): proceed=true 提交 user/pwd; false=取消。</summary>
        public void RespondHttpAuth(int authId, bool proceed, string? user = null, string? password = null)
            => Interop.arupa_webview_respond_http_auth(_handle, authId, proceed ? 1 : 0, user, password);
        /// <summary>回应地理位置授权 (GeolocationRequested 后)。</summary>
        public void RespondGeolocation(int geoId, bool granted)
            => Interop.arupa_webview_respond_geolocation(_handle, geoId, granted ? 1 : 0);
        /// <summary>A批: 回应证书错误 (CertificateErrorOccurred 后)。proceed=true 无视错误继续;
        /// false/不应答 = 取消 (内核安全默认)。</summary>
        public void RespondCertificateError(int certErrorId, bool proceed)
            => Interop.arupa_webview_respond_certificate_error(_handle, certErrorId, proceed ? 1 : 0);
        /// <summary>回应媒体(摄像头/麦克风)授权 (MediaPermissionRequested 后; audio/video 一把授权)。</summary>
        public void RespondMediaPermission(int permId, bool granted)
            => Interop.arupa_webview_respond_media_permission(_handle, permId, granted ? 1 : 0);
        /// <summary>回应文件选择 (FileChooserRequested 后): paths=选中文件; 取消传 null/空数组。</summary>
        public void RespondFileChooser(int chooserId, string[]? paths)
            => Interop.arupa_webview_on_file_chooser_result(_handle, chooserId, paths, paths?.Length ?? 0);

        /// <summary>FB-P026: 回应 Web Notification 权限请求 (NotificationPermissionRequested 后)。granted=true 允许。</summary>
        public void RespondNotificationPermission(int notifId, bool granted)
            => Interop.arupa_webview_respond_notification_permission(_handle, notifId, granted ? 1 : 0);

        /// <summary>FB-P025 诊断: 取原生 getter 的原始 JSON(不解析), 看 has_cert/字段实况。</summary>
        public string? GetCertificateInfoRaw()
            => Interop.TakeOwned(Interop.arupa_webview_get_certificate_info(_handle));

        /// <summary>FB-P025: 取当前主框架 TLS 证书详情 (供地址栏安全面板"查看证书")。
        /// 非 HTTPS / 内部页 / 未导航 / 无证书 → 返 null。封送内核 UI 线程同步取。</summary>
        /// <remarks>⚠ 2026-08-26 修: 本段原先与下面 <see cref="GetCertificateInfoRaw"/> 的
        /// summary **连着写在同一处** ⇒ 两段都挂在了那个方法上, 而本方法一句注释都没有。
        /// 两者语义**不同**(本方法解析成对象; Raw 那个不解析、给诊断看字段实况),
        /// 所以悬停看到的说明是**错的那一份**。同族第 6 个实例, 由闸 <c>C57</c> 抓出。</remarks>
        public CertificateInfo? GetCertificateInfo()
        {
            string? json = Interop.TakeOwned(Interop.arupa_webview_get_certificate_info(_handle));
            if (string.IsNullOrEmpty(json)) return null;
            try
            {
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;
                if (root.TryGetProperty("has_cert", out var hc) && !hc.GetBoolean()) return null;
                string? Str(string k) => root.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
                // ⚠ 内核把 valid_from/valid_to 输出成浮点(如 1780263552.0) → GetInt64() 会抛 → 整体返 null
                //   (FB-P025 真机仍空的真因)。兼容: 先 TryGetInt64, 失败回退 GetDouble 取整。
                long Num(string k) => root.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number
                    ? (v.TryGetInt64(out var lv) ? lv : (long)v.GetDouble()) : 0;
                var san = new List<string>();
                if (root.TryGetProperty("san", out var sanArr) && sanArr.ValueKind == JsonValueKind.Array)
                    foreach (var e in sanArr.EnumerateArray())
                        if (e.ValueKind == JsonValueKind.String && e.GetString() is { } s) san.Add(s);
                return new CertificateInfo
                {
                    Subject = Str("subject"), Issuer = Str("issuer"),
                    ValidFrom = Num("valid_from"), ValidTo = Num("valid_to"), San = san,
                    Protocol = Str("protocol"), Cipher = Str("cipher"),
                    KeyExchange = Str("key_exchange"), Pem = Str("pem"),
                };
            }
            catch { return null; }
        }

        private void RaiseRequest<T>(EventHandler<T>? handler, T args, Action fallback)
        {
            try { if (handler != null) { handler(this, args); return; } }
            catch { /* Never unwind through a native callback. */ }
            fallback();
        }

        private void RaiseJsDialog(JsDialogKind kind, int id, IntPtr url, IntPtr msg, IntPtr dv)
            => RaiseRequest(JsDialog, new JsDialogEventArgs
            {
                DialogId = id, Kind = kind, Url = Interop.Utf8(url),
                Message = Interop.Utf8(msg), DefaultValue = Interop.Utf8(dv),
            }, () => RespondJsDialog(id, kind == JsDialogKind.Alert, null));

        // ── FB-P002 B5/B3: cookie 写 / 枚举 / 按域删 / 清空 / 第三方拦截 ────────────
        // 写一条 cookie 到指定 url 的 origin (导入/恢复用; cookieLine 如 "k=v; Path=/")。
        public void SetCookie(string url, string cookieLine)
            => Interop.arupa_webview_set_cookie(_handle, url, cookieLine);
        // 清空本 partition 全部 cookie。
        public void RemoveAllCookies() => Interop.arupa_webview_remove_all_cookies(_handle);
        // 枚举全部 cookie (导出/清理用); 每条一行 "name=value; Domain=d; Path=p" (\n 分隔)。
        public Task<string?> GetAllCookiesAsync()
        {
            lock (_evalLifetime)
            {
                if (_handle == IntPtr.Zero) return Task.FromResult<string?>(null);
                var request = _evalRequests.TryStart();
                if (request == null) return Task.FromResult<string?>(null);
                try { Interop.arupa_webview_get_all_cookies(_handle, NativeEvalRequests.CallbackPointer, request.User); }
                catch { request.Cancel(); throw; }
                return request.WaitAsync(8000);
            }
        }
        // 按域删除 cookie (urlOrDomain = "example.com" 或完整 URL)。
        public void DeleteCookies(string urlOrDomain)
            => Interop.arupa_webview_delete_cookies(_handle, urlOrDomain);
        // 第三方 cookie 拦截开关 (Chromium CookieControlsMode)。
        public void SetBlockThirdPartyCookies(bool block)
            => Interop.arupa_webview_set_block_third_party_cookies(_handle, block ? 1 : 0);

        // 输入 (OSR 宿主转发): type 鼠标 0=move/1=down/2=up; 键盘 0=rawkeydown/1=keyup/2=char。
        public void SendMouse(int type, int x, int y, int button = 0, int modifiers = 0)
            => Interop.arupa_webview_send_mouse(_handle, type, x, y, button, modifiers);
        public void SendKey(int type, int windowsKeyCode, int modifiers = 0, string? text = null)
            => Interop.arupa_webview_send_key(_handle, type, windowsKeyCode, modifiers, text);
        public void SetFocus(bool focused) => Interop.arupa_webview_set_focus(_handle, focused ? 1 : 0);

        // CDP: 进程内 DevTools 协议 (专业自动化驱动)。
        private int _cdpId;
        private bool _cdpAttached;
        private readonly ConcurrentDictionary<int, TaskCompletionSource<string>> _cdpPending = new();
        // FB-P002 C1: document-start 脚本注册表 key→CDP identifier (供 clear)。
        private readonly Dictionary<string, string> _dsScripts = new();

        public void CdpAttach()
        {
            if (_cdpAttached) return;
            _onCdp = OnCdpThunk;
            // 显式 GCHandle 保活;Marshal.GetFunctionPointerForDelegate 不负责 keep。
            if (_onCdpHandle.IsAllocated) _onCdpHandle.Free();
            _onCdpHandle = GCHandle.Alloc(_onCdp);
            Interop.arupa_webview_cdp_attach(_handle,
                Marshal.GetFunctionPointerForDelegate(_onCdp), IntPtr.Zero);
            _cdpAttached = true;
        }
        public void CdpSend(string jsonCommand) => Interop.arupa_webview_cdp_send(_handle, jsonCommand);
        // ⚠ 必须复位 _cdpAttached, 否则下次 SendCdpAsync/CdpAttach 因 guard 跳过 → 永远无法重 attach。
        public void CdpDetach()
        {
            Interop.arupa_webview_cdp_detach(_handle);
            _cdpAttached = false;
            // 释放 GCHandle 代理根,让委托 lazy 回收;为空时 Free 抛 InvalidOperationException。
            if (_onCdpHandle.IsAllocated)
            {
                _onCdpHandle.Free();
                _onCdp = null;
            }
        }

        // 发一条 CDP 命令并 await 其响应的 result (按 id 关联)。供 wrapper 上层封装高层 API。
        public Task<string> SendCdpAsync(string method, string? paramsJson = null,
                                         int timeoutMs = 8000)
        {
            if (_handle == IntPtr.Zero) throw new ObjectDisposedException(nameof(ArupaWebView));
            if (_cdpPending.Count >= 128) throw new InvalidOperationException("cdp_queue_full");
            if (timeoutMs <= 0 || timeoutMs > 120000) throw new ArgumentOutOfRangeException(nameof(timeoutMs));
            CdpAttach();
            int id = Interlocked.Increment(ref _cdpId);
            var tcs = new TaskCompletionSource<string>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            _cdpPending[id] = tcs;
            string cmd = paramsJson == null
                ? $"{{\"id\":{id},\"method\":\"{method}\"}}"
                : $"{{\"id\":{id},\"method\":\"{method}\",\"params\":{paramsJson}}}";
            Interop.arupa_webview_cdp_send(_handle, cmd);
            // 超时清理 (避免泄漏)。
            _ = Task.Delay(timeoutMs).ContinueWith(_ =>
            {
                if (_cdpPending.TryRemove(id, out var t))
                    t.TrySetException(new TimeoutException($"CDP {method} 超时"));
            });
            return tcs.Task;
        }

        // ── FB-P002 C1: document-start 脚本注入 (反指纹+所有注入模块命脉) ──────────
        // 注册一段在每帧 V8 context 创建/document_start (早于页面脚本) 自动注入的脚本, 按 key 可增删,
        // 幂等。底层走 CDP Page.addScriptToEvaluateOnNewDocument (已 runtime-verified document-start)。
        // PC 侧 IContentScriptRegistry 的 Arupa 后端调本 API 即可 (6 applier + 注册表零改)。
        public async Task RegisterDocumentStartScriptAsync(string key, string js)
        {
            CdpAttach();
            await SendCdpAsync("Page.enable").ConfigureAwait(false);
            await ClearDocumentStartScriptAsync(key).ConfigureAwait(false);  // 幂等: 先清同 key
            string p = "{\"source\":" + JsonSerializer.Serialize(js) + "}";
            string result = await SendCdpAsync("Page.addScriptToEvaluateOnNewDocument", p)
                .ConfigureAwait(false);
            using var doc = JsonDocument.Parse(result);
            if (doc.RootElement.TryGetProperty("identifier", out var idf))
                _dsScripts[key] = idf.GetString() ?? "";
        }

        public async Task ClearDocumentStartScriptAsync(string key)
        {
            if (!_dsScripts.TryGetValue(key, out var ident) || string.IsNullOrEmpty(ident))
                return;
            _dsScripts.Remove(key);
            await SendCdpAsync("Page.removeScriptToEvaluateOnNewDocument",
                "{\"identifier\":" + JsonSerializer.Serialize(ident) + "}").ConfigureAwait(false);
        }

        // ── FB-P002 B1: 异步 JS↔宿主桥 (CDP addBinding + document-start shim + Promise) ──
        // PC 33 个 JsBridge: JS 调 window.<Name>.<method>(args) 返 Promise; 宿主 BridgeHandler
        // 异步处理 (带 frame origin 门控) 后回灌 Promise。底层 Runtime.addBinding(已验证) +
        // document-start shim (复用 C1) + Runtime.evaluate resolve。无需 mojo/重编内核。
        private readonly ConcurrentDictionary<int, string> _ctxOrigin = new();
        private readonly object _bridgeLock = new();
        private bool _bridgeInfra;
        public Func<BridgeCallArgs, Task<string>>? BridgeHandler { get; set; }

        private const string kBridgeShim = @"
(function(){
  if (window.__arupaBridgeInit) return; window.__arupaBridgeInit=true;
  var pending={}, nextId=1;
  window.__arupaBridgeResolve=function(callId,resultJson){
    var p=pending[callId]; if(!p) return; delete pending[callId];
    try{ p.resolve(JSON.parse(resultJson)); }catch(e){ p.resolve(resultJson); }
  };
  window.__arupaRegisterBridge=function(name){
    if(window[name]) return;
    window[name]=new Proxy({},{get:function(t,method){
      if(typeof method!=='string') return undefined;
      // ⚠ await proxy 时 JS 会探测 .then: 若桥 proxy 对任意属性都返函数, then 也成函数 →
      //   proxy 被当 thenable → await 调 proxy.then(res,rej) (当桥调用 name.then 发给 C#, 永不
      //   resolve) → 整个 await 挂死。WebUI bindRaw 正是 `await bindRaw(name)` (返 proxy)。
      //   故 then/catch/finally + Symbol 探测键一律返 undefined, 让 proxy 不是 thenable。
      if(method==='then'||method==='catch'||method==='finally') return undefined;
      return function(){
        var args=Array.prototype.slice.call(arguments);
        var callId=nextId++;
        var pr=new Promise(function(res,rej){pending[callId]={resolve:res,reject:rej};});
        try{ __arupaBridge(JSON.stringify({callId:callId,name:name,method:method,args:args})); }
        catch(e){ delete pending[callId]; return Promise.reject(e); }
        return pr;
      };
    }});
  };
})();";

        // 跨进程导航后重新注册 binding (+ 补 enable, 让 executionContextCreated 续报 origin 供门控)。
        //   只补 binding/enable, 不重注册各桥名 (那走 document-start shim, 随文档自动续命)。
        private async Task ReaddBridgeBindingAsync()
        {
            try
            {
                await SendCdpAsync("Runtime.enable").ConfigureAwait(false);
                await SendCdpAsync("Runtime.addBinding", "{\"name\":\"__arupaBridge\"}").ConfigureAwait(false);
            }
            catch { /* 同进程重复 add 返 error 或超时 → 吞掉, 不影响 */ }
        }

        private async Task EnsureBridgeInfraAsync()
        {
            if (_bridgeInfra) return;
            // ⚠ 不在此置 _bridgeInfra=true: CDP DevToolsAgentHost 在 cdp_attach 后首条命令偶发
            //   未就绪 → Runtime.enable 超时。原先提前置位 → 一旦超时整个桥设施死且永不重试
            //   (启动时桥全挂的根因)。改: 仅全部成功才置位, 首条命令带重试。
            CdpAttach();
            await SendCdpWithRetryAsync("Runtime.enable", null, attempts: 4, perTimeoutMs: 2500)
                .ConfigureAwait(false);
            await SendCdpAsync("Runtime.addBinding", "{\"name\":\"__arupaBridge\"}")
                .ConfigureAwait(false);
            await RegisterDocumentStartScriptAsync("__arupa_bridge_shim", kBridgeShim)
                .ConfigureAwait(false);
            // 当前已加载文档: 立即注入 shim (定义 __arupaRegisterBridge/__arupaBridgeResolve)。
            //   与 RegisterBridgeAsync 的"当前文档 + 后续文档"双注册对称 (见 1104-1107)。
            //   ⚠ 漏此一行的后果: 冷 tab 首次导航时 AttachAsync 在本页 page_finished 才跑, shim 只
            //   注册成 document-start (仅对后续文档生效) → 当前文档无 __arupaRegisterBridge →
            //   RegisterBridgeAsync 的当前文档 eval `if(window.__arupaRegisterBridge)...` 守卫失败 →
            //   命名桥不注入当前文档 → WebUI bindRaw 取不到桥 → 设置(皮肤/语言)只改本页 DOM、外壳不同步。
            //   kBridgeShim 自身幂等 (if(window.__arupaBridgeInit)return), 重复注入无害。
            await SendCdpAsync("Runtime.evaluate",
                "{\"expression\":" + JsonSerializer.Serialize(kBridgeShim) + "}")
                .ConfigureAwait(false);
            _bridgeInfra = true;
        }

        /// <summary>CDP DevToolsAgentHost attach 后首条命令偶发未就绪 → 短超时重试若干次, 让桥设施稳。</summary>
        private async Task SendCdpWithRetryAsync(string method, string? paramsJson, int attempts, int perTimeoutMs)
        {
            for (int i = 0; ; i++)
            {
                try { await SendCdpAsync(method, paramsJson, perTimeoutMs).ConfigureAwait(false); return; }
                catch (TimeoutException) when (i < attempts - 1)
                { await Task.Delay(300).ConfigureAwait(false); }
            }
        }

        // 注册一个桥名 (PC 的 AuthBridge/ModuleBridge/...)。当前文档 + 后续文档都生效。
        public async Task RegisterBridgeAsync(string name)
        {
            await EnsureBridgeInfraAsync().ConfigureAwait(false);
            string regJs = "if(window.__arupaRegisterBridge)window.__arupaRegisterBridge(" +
                           JsonSerializer.Serialize(name) + ");";
            // 后续文档: document-start 注册 (在 shim 之后跑)。
            await RegisterDocumentStartScriptAsync("__arupa_bridge_" + name, regJs)
                .ConfigureAwait(false);
            // 当前已加载文档: 立即注册。
            await SendCdpAsync("Runtime.evaluate",
                "{\"expression\":" + JsonSerializer.Serialize(regJs) + "}")
                .ConfigureAwait(false);
        }

        private async Task HandleBridgeCallAsync(string payload, int ctxId)
        {
            try
            {
                using var doc = JsonDocument.Parse(payload);
                var r = doc.RootElement;
                int callId = r.GetProperty("callId").GetInt32();
                string name = r.TryGetProperty("name", out var n) ? (n.GetString() ?? "") : "";
                string method = r.TryGetProperty("method", out var m) ? (m.GetString() ?? "") : "";
                string argsJson = r.TryGetProperty("args", out var a) ? a.GetRawText() : "[]";
                _ctxOrigin.TryGetValue(ctxId, out var origin);

                string result = "null";
                var h = BridgeHandler;
                if (h != null)
                {
                    result = await h(new BridgeCallArgs
                    {
                        Name = name, Method = method, ArgsJson = argsJson, FrameOrigin = origin
                    }).ConfigureAwait(false) ?? "null";
                }
                // 在调用方 context resolve 对应 Promise。
                string js = "window.__arupaBridgeResolve(" + callId + "," +
                            JsonSerializer.Serialize(result) + ");";
                await SendCdpAsync("Runtime.evaluate",
                    "{\"expression\":" + JsonSerializer.Serialize(js) + ",\"contextId\":" +
                    ctxId + "}").ConfigureAwait(false);
            }
            catch { /* 桥调用解析/resolve 失败 → 忽略 (Promise 留 pending, 由超时或页面卸载清) */ }
        }

        private void OnCdpThunk(IntPtr u, IntPtr json)
        {
            string? msg = Interop.Utf8(json);
            if (msg == null) return;
            // 响应 (含 id) → 关联 pending; 事件 (无 id) → 桥/上下文处理后抛 CdpMessage。
            try
            {
                using var doc = JsonDocument.Parse(msg);
                var root = doc.RootElement;
                if (root.TryGetProperty("id", out var idEl) &&
                    idEl.TryGetInt32(out int id) && _cdpPending.TryRemove(id, out var tcs))
                {
                    if (root.TryGetProperty("result", out var res))
                        tcs.TrySetResult(res.GetRawText());
                    else if (root.TryGetProperty("error", out var err))
                        tcs.TrySetException(new InvalidOperationException("CDP error: " + err.GetRawText()));
                    else tcs.TrySetResult("{}");
                    return;
                }
                // FB-P002 B1: 事件驱动 JS 桥。
                if (root.TryGetProperty("method", out var mEl))
                {
                    string method = mEl.GetString() ?? "";
                    if (method == "Runtime.executionContextCreated" &&
                        root.TryGetProperty("params", out var p1) &&
                        p1.TryGetProperty("context", out var ctx))
                    {
                        int cid = ctx.GetProperty("id").GetInt32();
                        string origin = ctx.TryGetProperty("origin", out var o) ? (o.GetString() ?? "") : "";
                        _ctxOrigin[cid] = origin;
                    }
                    else if (method == "Runtime.executionContextDestroyed" &&
                             root.TryGetProperty("params", out var p2) &&
                             p2.TryGetProperty("executionContextId", out var dce))
                    {
                        _ctxOrigin.TryRemove(dce.GetInt32(), out _);
                    }
                    else if (method == "Runtime.bindingCalled" &&
                             root.TryGetProperty("params", out var bp) &&
                             bp.TryGetProperty("name", out var bn) &&
                             bn.GetString() == "__arupaBridge")
                    {
                        int cid = bp.TryGetProperty("executionContextId", out var ce) ? ce.GetInt32() : 0;
                        string payload = bp.TryGetProperty("payload", out var pl) ? (pl.GetString() ?? "") : "";
                        _ = HandleBridgeCallAsync(payload, cid);
                        return;
                    }
                }
            }
            catch { /* 非 JSON / 解析失败 → 当事件抛 */ }
            CdpMessage?.Invoke(msg);
        }

        public void Dispose()
        {
            IntPtr handle;
            lock (_evalLifetime)
            {
                handle = _handle;
                _handle = IntPtr.Zero;
                _evalRequests.Dispose();
                foreach (var id in _cdpPending.Keys)
                    if (_cdpPending.TryRemove(id, out var pending))
                        pending.TrySetException(new ObjectDisposedException(nameof(ArupaWebView)));
            }
            // Native teardown may call back into managed code; do not hold the
            // submission lock across it. The two eval entry points now see zero.
            if (handle == IntPtr.Zero)
            {
                // A concurrent Dispose already owns teardown and buffer cleanup.
                // 兜底: 同时释放 GCHandle。IsAllocated == false 时 short-circuit,不会 double-free。
                if (_onCdpHandle.IsAllocated)
                {
                    _onCdpHandle.Free();
                    _onCdp = null;
                }
                GC.SuppressFinalize(this);
                return;
            }
            Interop.arupa_webview_destroy(handle);
            lock (_interceptLock) FreeLastInterceptResp();
            // ⚠️ 必须在 native teardown 之后释放 GCHandle;否则 Kernel 最后一帧 OnCdpMessage
            //   会调到正在收回的 function pointer 与 Clr FailFast 同根。
            if (_onCdpHandle.IsAllocated)
            {
                _onCdpHandle.Free();
                _onCdp = null;
            }
            GC.SuppressFinalize(this);
        }
        ~ArupaWebView() => Dispose();
    }
}
