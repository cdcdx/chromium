// Arupa 内核 C ABI P/Invoke 互操作层 (低层, 内部用)。
// 对应 chrome/browser/arupa_desktop/public/arupa_kernel_capi.h (ABI_MAJOR=1)。
using System;
using System.Runtime.InteropServices;

namespace Arupa
{
    internal static class Interop
    {
        // 库式嵌入: arupa_kernel.dll (component build 下还需同目录 508 个依赖 dll + arupa_render.exe
        // + content_shell.pak/icudtl.dat/v8_context_snapshot.bin)。宿主须把工作目录或 PATH
        // 指向 out/arupa-desktop (或 release monolithic 后单 dll)。
        public const string Dll = "arupa_kernel";
        public const int ABI_MAJOR = 1;

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_kernel_abi_minor();

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern IntPtr arupa_webview_get_ime_state(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_webview_update_ime(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string text, int selection, int commit, ulong document);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_webview_open_devtools(IntPtr inspected, IntPtr frontend);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern void arupa_webview_close_devtools(IntPtr frontend);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        internal delegate int OnBrowserCommandNative(IntPtr user, int command);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern void arupa_webview_set_new_contents_callback(IntPtr view, IntPtr callback, IntPtr user);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_webview_adopt_pending(IntPtr view, [MarshalAs(UnmanagedType.LPUTF8Str)] string token);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern void arupa_webview_request_close(IntPtr view, IntPtr callback, IntPtr user);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern IntPtr arupa_webview_get_network_settings(IntPtr view);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_kernel_supports([MarshalAs(UnmanagedType.LPUTF8Str)] string feature);

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        internal delegate int HostMainCb(IntPtr user);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        internal static extern int arupa_kernel_run_mac_host(HostMainCb callback, IntPtr user);


        [StructLayout(LayoutKind.Sequential)]
        public struct KernelConfig
        {
            public int abi_major;
            [MarshalAs(UnmanagedType.LPUTF8Str)] public string? user_data_dir;
            [MarshalAs(UnmanagedType.LPUTF8Str)] public string? locale;
            [MarshalAs(UnmanagedType.LPUTF8Str)] public string? pak_dir;
            public int enable_fingerprint_hardening;
            // FB-P011: OSR 设备缩放。0/1 = 不强制(dpr=1); 1.5 = 150% DPI 物理出帧+锐利。
            // ⚠ 必须有此字段: 内核 ArupaKernelConfig 含 double device_scale_factor, 缺了 →
            //   内核按更大 sizeof 读越界栈垃圾 → SetForceDeviceScaleFactor(garbage) →
            //   第二个 OSR view compositor scale=0 DCHECK FATAL 崩 (FB-P012)。
            public double device_scale_factor;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct WebViewConfig
        {
            public int width;
            public int height;
            [MarshalAs(UnmanagedType.LPUTF8Str)] public string? partition_name;
            public int off_the_record;
            [MarshalAs(UnmanagedType.LPUTF8Str)] public string? proxy_url;
        }

        // OSR sink: 函数指针表。
        // ⚠ ABI: 严格镜像 capi.h ArupaOsrSink (on_paint, on_cursor, on_popup_show/size/paint)。
        //   字段缺失 = 内核按更大 sizeof 读, 越过本结构读栈垃圾当函数指针 → 野跳崩溃 (FB-P012/P018 类)。
        //   FB-P016 加 on_cursor (3caca24); A批 (2026-06-11) 加 popup widget 三回调 (FB-P018 真因之一)。
        [StructLayout(LayoutKind.Sequential)]
        public struct OsrSink
        {
            public IntPtr on_paint;        // OnPaintNative
            public IntPtr on_cursor;       // OnCursorNative (FB-P016)
            public IntPtr on_popup_show;   // OnIntNative: 1=显示 0=隐藏 (A批 popup widget: <select>/autocomplete)
            public IntPtr on_popup_size;   // OnPopupSizeNative: popup 在 view 坐标系的矩形
            public IntPtr on_popup_paint;  // OnPaintNative: popup 帧 (语义同 on_paint, 属 popup widget)
        }

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnPaintNative(IntPtr user, IntPtr pixels, int w, int h,
                                           int dx, int dy, int dw, int dh);
        // FB-P016: 页面光标变化 (cursor_type = ui::mojom::CursorType: 0=kPointer/2=kHand/3=kIBeam/...)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnCursorNative(IntPtr user, int cursorType);
        // A批: popup widget 位置/尺寸。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnPopupSizeNative(IntPtr user, int x, int y, int w, int h);

        // ── ArupaWebViewCallbacks: 全部字段都是指针 (1 void* + 28 fn ptr)。 ──────────
        // x64 下每字段 8 字节, 故用 29 个 IntPtr 精确匹配 C 布局 (顺序严格对应 capi.h)。
        // 只填需要的回调 (GetFunctionPointerForDelegate), 其余 IntPtr.Zero。
        // ⚠ ABI 铁律: capi.h 该结构一变, 这里必须同步 (字段缺失 = 内核越界读栈垃圾当函数
        //   指针 → 野跳崩溃, FB-P012/P018 两次教训)。A批 (2026-06-11) 尾部加 4 回调。
        [StructLayout(LayoutKind.Sequential)]
        public struct WebViewCallbacks
        {
            public IntPtr user;                       // 0  void*
            public IntPtr on_page_started;            // 1
            public IntPtr on_page_finished;           // 2
            public IntPtr on_progress;                // 3
            public IntPtr on_title;                   // 4
            public IntPtr on_received_icon;           // 5
            public IntPtr on_received_error;          // 6
            public IntPtr on_render_process_gone;     // 7
            public IntPtr on_ssl_state_changed;       // 8
            public IntPtr should_override_url_loading;// 9
            public IntPtr should_intercept_request;   // 10
            public IntPtr on_js_alert;                // 11
            public IntPtr on_js_confirm;              // 12
            public IntPtr on_js_prompt;               // 13
            public IntPtr on_js_before_unload;        // 14
            public IntPtr on_http_auth;               // 15
            public IntPtr on_geolocation_prompt;      // 16
            public IntPtr on_permission_request;      // 17
            public IntPtr on_download_start;          // 18
            public IntPtr on_find_result;             // 19
            public IntPtr on_show_file_chooser;       // 20
            public IntPtr on_show_fullscreen;         // 21
            public IntPtr on_hide_fullscreen;         // 22
            public IntPtr on_open_new_window;         // 23
            public IntPtr on_js_bridge_call;          // 24
            // ── A批新增 (2026-06-11, capi.h 尾部加性扩展; FB-P018 修复同步) ──
            public IntPtr on_console_message;         // 25 OnConsoleMessageNative
            public IntPtr on_certificate_error;       // 26 OnCertificateErrorNative
            public IntPtr on_download_updated;        // 27 OnDownloadUpdatedNative ⚠注册即改变下载语义(内核放行自下载), 默认不装配
            public IntPtr on_close_window;            // 28 OnVoidNative (window.close; 内核不自动毁 view)
            // ── C批 (capi MINOR3/4 尾部加性扩展; PC 侧暂不订阅, 占位维持后续字段偏移正确) ──
            public IntPtr on_dialog_reset;            // 29 (导航回收挂起 JS 对话框通知, PC 暂留 Zero)
            public IntPtr on_http_auth_ex;            // 30 (HTTP/代理认证增强超集, PC 走旧 on_http_auth, 暂留 Zero)
            // ── MINOR6 (2026-06-13, FB-P024+P026; capi.h 尾部加性扩展) ──
            public IntPtr on_context_menu;            // 31 OnContextMenuNative (FB-P024 网页右键菜单接缝)
            public IntPtr on_notification_permission_requested; // 32 OnNotificationPermNative (FB-P026 通知权限请求)
            // ── MINOR8 (2026-06-19, FB-P039; capi.h 尾部加性扩展) ──
            public IntPtr on_favicon_urls;            // 33 OnFaviconUrlsNative (页面声明 favicon 完整候选列表 JSON, 宿主按 size 挑最佳)
            // capi.h:242-246 的 should_intercept_request_ex (#34)。**已装配** —— 内核优先取它,
            //   宿主才拿得到 resource_type (旧 should_intercept_request 仅在其为 null 时兜底)。
            //   ⚠ 2026-09-20 前这里是恒填 IntPtr.Zero 的占位, 于是 PC 侧 DNR 资源类型恒 Unknown、
            //   头改写拿不到 action/header 通道。字段本身必须存在 (缺了其后所有字段整体前移一位 →
            //   内核按更大 sizeof 读取时错位野跳, FB-P012/P018 教训)。
            public IntPtr should_intercept_request_ex; // 34 ShouldInterceptExNative
            // ── MINOR24 (pip.host; capi.h 尾部加性扩展) ──
            public IntPtr on_picture_in_picture_request; // 35 OnStringNative (网页 requestPictureInPicture; 内核无原生 PiP 窗 → 交宿主呈现)
            // ── MINOR25 (presentation.v1; capi.h 尾部加性扩展) ──
            public IntPtr on_presentation_changed;       // 36 OnPresentationChangedNative (统一呈现通道: 全屏 + 画中画)
            public IntPtr on_browser_command;            // 37 OnBrowserCommandNative (ABI 1.29)
        }

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_navigation_state_callback(
            IntPtr view, IntPtr callback, IntPtr user);

        // 常用回调签名 (string 回调统一 (void* user, const char*))。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnStringNative(IntPtr user, IntPtr str);  // url/title 等
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnFaviconUrlsNative(IntPtr user, IntPtr pageUrl, IntPtr iconsJson);  // 33 FB-P039 favicon 候选列表
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnErrorNative(IntPtr user, IntPtr url, int code, IntPtr desc);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnIntNative(IntPtr user, int v);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnCdpNative(IntPtr user, IntPtr json);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnEvalNative(IntPtr user, IntPtr jsonResult);

        // FB-P002 C2/B2: should_intercept_request (#10, IO 线程!)。返回 ArupaInterceptResponse*
        // (IntPtr.Zero=放行)。内核在回调返回后同步拷走 struct + 其指向的内存, 故调用方须保证返回
        // 的内存存活到回调返回 (高层 thunk 用单槽缓冲: 下次回调/Dispose 才释放上次)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate IntPtr ShouldInterceptNative(IntPtr user, IntPtr url, IntPtr method,
                                                     int isForMainFrame, IntPtr headers);
        // capi.h:242-246 (#34, 在 ABI 1.16 冻结前缀内): 同类回调 + resource_type。
        //   内核**优先**取 _ex (arupa_intercept_loader.cc:484), 旧回调只在 _ex 为 null 时兜底。
        //   resourceType = Chromium 资源类型名: "main_frame"/"sub_frame"/"stylesheet"/"script"/
        //   "image"/"xmlhttprequest" … (见 tests/network/arupa_restype_test.cc 的断言表)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate IntPtr ShouldInterceptExNative(IntPtr user, IntPtr url, IntPtr method,
                                                       IntPtr resourceType, int isForMainFrame,
                                                       IntPtr headers);
        // FB-P002 B4: on_download_start (#18)。注册即拦截原生下载 (内核取消, 宿主接管路由)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnDownloadNative(IntPtr user, IntPtr url, IntPtr mime,
                                              IntPtr suggestedName, long contentLength, IntPtr referer);
        // FB-P002 B5: cookie 枚举回调 (cookies = \n 分隔 "name=value; Domain=d; Path=p")。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void ArupaCookiesCb(IntPtr user, IntPtr cookies);

        // FB-P008③/FB-A008: print_to_pdf 回调 (pdf 仅回调期有效, 同步拷走; ok=0 失败 pdf=NULL)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void ArupaPdfCb(IntPtr user, int ok, IntPtr pdf, IntPtr len);

        // ── FB-P003: 高层 wrapper wiring 回调 ─────────────────────────────────────
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnVoidNative(IntPtr user);                                   // fullscreen show/hide
        // MINOR25 presentation.v1: 统一呈现通道 (全屏 + 画中画共用一条出口)。
        //   mode: 0=none / 1=fullscreen / 2=pip       reason: 0=page请求 / 1=page退出 / 2=宿主退出 / 3=视图销毁
        //   seq: 单调递增, 异步宿主据此丢弃过期事件。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnPresentationChangedNative(IntPtr user, int mode, int reason, ulong seq);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnFindResultNative(IntPtr user, int active, int num, int done);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnJsDialogNative(IntPtr user, int dialogId, IntPtr url, IntPtr message);  // alert/confirm
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnJsPromptNative(IntPtr user, int dialogId, IntPtr url, IntPtr message, IntPtr defValue);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnJsBeforeUnloadNative(IntPtr user, int dialogId, IntPtr url);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate int ShouldOverrideNative(IntPtr user, IntPtr url, int isMainFrame, int isRedirect, int hasGesture);
        // ── FB-P013 第三批: 权限/认证/文件选择回调 (capi.h:123-137; ⚠ struct 字段已在, 仅装配) ──
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnHttpAuthNative(IntPtr user, int authId, IntPtr host, IntPtr realm);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnGeoPromptNative(IntPtr user, int geoId, IntPtr origin);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnPermissionNative(IntPtr user, int permId, IntPtr origin, int wantAudio, int wantVideo);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnFileChooserNative(IntPtr user, int chooserId, int mode, IntPtr acceptTypes);
        // ── A批: console / 证书错误 / 下载进度回调 (capi.h 尾部 4 项; FB-P018 同步) ──
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnConsoleMessageNative(IntPtr user, int level, IntPtr message,
                                                    IntPtr sourceId, int lineNumber);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnCertificateErrorNative(IntPtr user, int certErrorId, IntPtr url,
                                                      int certError, IntPtr pem);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnDownloadUpdatedNative(IntPtr user, int downloadId, IntPtr url,
                                                     int state, long received, long total,
                                                     int percent, IntPtr fullPath);
        // ── MINOR6 (FB-P024/P026) 回调签名 ──
        // FB-P024: 网页右键菜单接缝 (同步)。返回 1 = 宿主已接管 → 抑制内核原生菜单; 0 = 内核默认。
        //   mediaType: 0=none 1=image 2=video 3=audio 4=canvas 5=file 6=plugin。x/y 视口坐标 (OSR 物理由宿主按 dsf 换算)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate int OnContextMenuNative(IntPtr user, int mediaType, IntPtr linkUrl,
                                                IntPtr srcUrl, IntPtr selectionText, IntPtr pageUrl,
                                                int x, int y, int isEditable);
        // FB-P026: Web Notification 权限请求 (异步, 经 arupa_webview_respond_notification_permission 应答)。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void OnNotificationPermNative(IntPtr user, int notifId, IntPtr origin);

        // ArupaInterceptResponse (capi.h:278-292)。x64: int(0) + pad(4) → 指针从 offset 8 起;
        //   body_len(40) 之后 int action(48) + pad(4) → 4 个改头指针 56/64/72/80, sizeof = 88。
        //   ⚠ 这是内核**读**的 struct, 短一个字段就是越界读宿主内存 —— PC 的
        //   NomadBrowser.Browser.Tests/ArupaSdkAbiTests.cs 直接钉死 size=88 与这四个偏移。
        // 语义 (arupa_intercept_loader.cc:493): action == 1 → 只改头、请求继续走网络 (经
        //   ArupaHeaderRewriteClient); 其余值 → 用本 struct 合成响应替换 (status_code/mime/
        //   charset/headers/body), 即旧行为。四个改头字段按 \n 分隔, set 为 "K: V"、remove 为 "K"
        //   (行尾 \r 与空格会被内核裁掉)。
        [StructLayout(LayoutKind.Sequential)]
        public struct InterceptResponseNative
        {
            public int status_code;     // 0
            public IntPtr mime_type;    // 8
            public IntPtr charset;      // 16
            public IntPtr headers;      // 24
            public IntPtr body;         // 32
            public IntPtr body_len;     // 40 (size_t)
            public int action;          // 48 (capi.h:287)
            public IntPtr set_request_headers;     // 56 (capi.h:288)
            public IntPtr remove_request_headers;  // 64 (capi.h:289)
            public IntPtr set_response_headers;    // 72 (capi.h:290)
            public IntPtr remove_response_headers; // 80 (capi.h:291)
        }

        // ── 生命周期 ────────────────────────────────────────────────────────────
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_create(in KernelConfig cfg, out IntPtr kernel);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_destroy(IntPtr kernel);

        // ── 插件加载 (FB-P020, ABI MINOR 5): 签名门控 native 传输引擎 (happyview/fastview)
        // 进程级。load 含阻塞 IO (内核已封送 MayBlock 线程, 但本 P/Invoke 同步阻塞) →
        // 宿主请在后台线程调。outInfoJson = arupa_free 释放, PtrToStringUTF8 解
        // {plugin_id, socks_endpoint, managed_hosts:[]} → 宿主据 socks_endpoint+managed_hosts
        // 调 arupa_webview_set_transport_route 把 view 路由进插件。MINOR 5 dll 才有此导出。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_plugin_root(IntPtr kernel,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string dir);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_load_plugin(IntPtr kernel,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId, out IntPtr outInfoJson);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_revoke_plugin(IntPtr kernel);

        // ── MV3 扩展根 (ABI MINOR 11, 2026-08-25 FB-P089) ────────────────────────
        // 🔴 与上面的 set_plugin_root **是两个东西, 别传同一个目录**:
        //     set_plugin_root    = 传输插件根 (签名 bundle, <id>.arupa-plugin/)
        //     set_extension_root = MV3 扩展根 (解包后的扩展目录)
        //   传混了的后果见 FB-A050: 扩展被拿去传输目录里找, 零下发, 且不报错。
        // 什么时候调都行 —— 根设进来时若已有 ready 的 renderer, 内核会回头补发。
        // MINOR 11 dll 才有此导出 (旧 dll 上 P/Invoke 会抛 EntryPointNotFoundException,
        // 门面那侧已 try/catch, 见 ArupaBrowser.SetExtensionRoot)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_extension_root(IntPtr kernel,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string dir);
        // 回显内核真正记住的扩展根 (UTF-8; 未设 = 空串, 永不 NULL)。
        // ⚠ 返回的是内核 thread_local 缓冲的指针 —— **不要**用 LPUTF8Str 做返回值 marshal
        //   (那会让运行时以为该指针归它管并尝试 free, 内核给的这块不是 CoTaskMemAlloc 出来的)。
        //   取 IntPtr 自己 PtrToStringUTF8, 见 ArupaBrowser.GetExtensionRoot。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_kernel_get_extension_root(IntPtr kernel);

        // 单个扩展启停 (ABI MINOR 12, FB-A052)。enabled=0 立刻卸载并从此跳过; =1 放行并补发。
        // ⚠ extensionId 是 MV3 那个 32 字符 id, **不是目录名** (manifest 有 key 时 id 从 key 派生)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_extension_enabled(IntPtr kernel,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId, int enabled);

        // 重扫扩展根并补发给已 ready 的 renderer (ABI MINOR 12)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_rescan_extensions(IntPtr kernel);

        // 列出内核已经认识的扩展 (JSON 数组; caller arupa_free)。
        // 🔴 启停要的是 MV3 那个 32 字符 id, **不是目录名** —— 拿这个取权威 id。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_kernel_list_extensions(IntPtr kernel);

        // 这个扩展的 background service worker 起没起 (ABI MINOR 13, FB-P090)。
        // 返回 1=注册上了 · 0=有 SW 但没起来 · 2=清单里本来没有 SW · -1=内核不认识这个 id。
        // 🔴 0 / 2 / -1 是**三件不同的事**, 别合成一个布尔 —— 合起来的话"内容脚本型
        //   扩展"与"SW 起不来"长得一模一样, 而 FB-P090 正是被这种静默逼着查了三轮。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_extension_sw_state(IntPtr kernel,
                                                                 string extensionId);

        // ── Action 点击派发 + activeTab (FB-P121, 2026-09-08) ────────────────
        //
        // 🔴 **卡在这一行后面的是 713 个真实商店包**(全语料 2848 的 25%): 它们有工具栏
        //   按钮、没有 default_popup、且带后台脚本 ⇒ **只能靠 `onClicked`**。
        //   C++ 与 C API 早就做完了, 差的一直只是这里的声明。
        //
        // ⚠ **这一族没有 `IntPtr kernel` 首参, 与上面几个不一样 —— 那是有意的, 不是漏了。**
        //   扩展注册表 / SW 宿主 / activeTab 授权在内核里是**进程级全局**
        //   (`LiveHosts()` / `RegisteredExtensions()` 都是 NoDestructor 静态),
        //   **一个进程一份, 不随 ArupaKernel 实例走** ⇒ 没有可传的实例句柄。
        //   同族全局导出还有 `list_extension_actions` / `fire_ext_event` /
        //   `revoke_active_tab_*` / `active_tab_grant_count`。
        //   而 `set_extension_root` / `list_extensions` / `extension_sw_state` 是
        //   **每实例**的(它们改的是那个 kernel 自己的状态), 所以带 kernel 首参。
        //   ⇒ 判断标准不是"看起来像不像", 是**这个状态属于进程还是属于实例**。
        //
        // 返回码 —— 🔴 **0 和 1 都不是失败, 别折叠成 bool**:
        //   0 DISPATCHED           onClicked 已送进该扩展的 SW
        //   1 OPEN_POPUP_INSTEAD   该扩展有 popup ⇒ **宿主去开 popup**;
        //                          按 MV3 语义**不派发 onClicked**(两样都做是错的)。
        //                          ⚠ 这一支**也已经授了 activeTab** —— popup 里的
        //                            脚本同样要读当前页。
        //   2 EXTENSION_NOT_FOUND  3 EXTENSION_DISABLED  4 ACTION_NOT_FOUND
        //   5 NO_USER_GESTURE      6 TAB_NOT_FOUND       7 SERVICE_WORKER_UNAVAILABLE
        //   ⚠ 把 1 折进"失败"是 FB-P116 那个错的同族(码 7「走替代」被折成"拒",
        //     结果权限被摘、SW 顶层崩)。**八个码逐个分支, 别求简。**
        //
        // tab_context_json:
        //   {"tabId":"tab-42","navigationId":"nav-108","url":"...","title":"...",
        //    "userGesture":true}
        //   ⚠ `userGesture` 缺省按**拒**处理; 内核不猜手势, **由宿主如实断言**。
        //   ⚠ `navigationId` = activeTab 绑定的 **document** 维度, **每次导航必须换新值**
        //     —— 不换的话旧授权会跟着用户从新闻站走到网银站。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_dispatch_action_click(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string tabContextJson);

        // ── 内核 → 扩展 方向的事件门面 (FB-P133, 2026-09-08) ─────────────────
        //
        // 🔴 **这个绑定此前不存在, 而上面那段注释里把它当例子列了出来** ——
        //   「同族全局导出还有 `list_extension_actions` / `fire_ext_event` / …」。
        //   ⇒ **它被写进了注释, 而没有被写进代码**, 且那句注释还让人以为它可用。
        //   对接侧的现象是 grep 不到、调不到, 于是最自然的结论是"内核还没做" ——
        //   而内核 dll 里这个导出一直在。
        //   📌 判断"这个能力有没有"要**分三层**: 内核实现了吗 / 导出了吗 /
        //     **包装绑了吗**。三层里任何一层缺, 现象都是"调不到", 而修的地方完全不同;
        //     **第三层最容易被漏, 因为它在另一个仓里**。(FB-P121 是同一族的第一次。)
        //
        // ⚠ 本函数**不管** `action.onClicked` —— 那条走 `dispatch_action_click`,
        //   它带**手势断言**与 popup 语义(有 popup 时按 MV3 不该派发 onClicked),
        //   本通用通道两样都没有。用错的表现是"点了没反应"或"popup 与 onClicked 同时来",
        //   两种都难查。
        //
        // `argsJson` 是 JSON **数组** = 监听器的实参列表
        //   (如 `tabs.onRemoved` 是 `[42, {"windowId":1,"isWindowClosing":false}]`)。
        //   空参传 `"[]"`, **不是** `null`、不是 `""`。
        //
        // 返回码 —— 🔴 **每一种都说得出原因, 别折成 bool**:
        //   0 成功
        //   1 `eventName` 不在**宿主可发的事件边界**内(或为空)。这条边界防的是宿主伪造
        //     `runtime.onInstalled` / `cookies.onChanged` 这类**内核自己该发**的事件
        //     —— 扩展分不出真假。边界 = `kHostOwnedPrefixes` ∪
        //     `kHostFireableEventPrefixes`(后者含 `notifications.` / `downloads.` /
        //     `tabGroups.`: 事件源头在宿主, 而它们的**函数刻意不给** ——
        //     **能发事件 ≠ 函数归宿主答**, 两件事分开判)。
        //   2 `argsJson` 不是合法 JSON 数组
        //   3 该扩展的 SW 现在不在(没起来/已停)。🔴 **不是"发了"** —— 宿主要如实处理,
        //     别当成成功; 这正是本项目反复吃亏的那一类静默。
        //   4 `extensionId` 为空
        // ⚠ **没有"广播"语义**: `extensionId` 为空回 4, 不是"发给所有扩展"。
        // 🔴 FB-P144 ③ (2026-09-08): 宿主断言"这个扩展是**用户自己加载的开发包**"。
        //
        //   产品 2026-09-08 拍板: `nativeMessaging` 按签名分档 ——
        //   我们签的给 · **用户自己加载的开发包也给(风险自担)** · 商店装的未签名包拒。
        //
        //   🔴 **内核不猜来源**: 它只看得到"有个目录里有个 manifest",
        //   看不出是用户拖进来的还是商店下下来的, 而那两者在信任模型里完全不同。
        //   ⇒ **宿主没调过这个函数 ⇒ 按 0 算(fail-closed)**。
        //   ⚠ 所以产品那条「用户自己加载的要能用」**要宿主配合才成立** ——
        //     宿主是唯一知道来源的那一方。不调 = 那一档永远走不通。
        //
        //   ⚠ 谎报一个 true 等于**把一个还没做的入口当成做了** —— 如实说 false
        //     不是保守, 是准确。
        // 🔴 产品 2026-09-09 拍板: **扩展改浏览器全局代理出口, 要用户二次确认。**
        //   宿主弹完框、用户点了同意之后调它如实断言。`granted` 非 0 = 已确认。
        //   ⚠ 传 0 是**撤销** —— 撤销要能表达, 否则"同意过一次"等于永久授权。
        //   🔴 内核**不猜用户点了什么**: 没调过 ⇒ 按没同意算(fail-closed)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_extension_route_consent(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId,
            int granted);

        // **扩展发起的**设出口路由 —— 与 `arupa_webview_set_transport_route`
        //   是**两个入口**(两种意图的默认值相反, 见 arupa_extension_route.h)。
        //   返回码: 0 应用了 · 1 清单没声明 proxy · 2 未签名要更细作用域 ·
        //           3 **要用户二次确认而宿主没断言过** · 4 认不出作用域 ·
        //           5 route_json 坏 · 6 内核没登记这个扩展。
        //   ⚠ **拒的时候一条路由都不会被应用**, 上一份配置原样保留。
        // 🔴 FB-P149: 把**宿主的标签页编号**声明给内核。
        //   `chrome.tabs.sendMessage(tabId, …)` 靠它才投得出去 —— 那个整数是
        //   宿主的命名空间(`tabs.query` 答出去的就是它), 内核认识的只有 view。
        //   ⚠ 建 view 时调一次即可(那个号此后不变); 传负数 = 解绑。
        //   ⚠ 没声明过的号一律投递失败, 内核**不猜**。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_tab_id(IntPtr view, int tabId);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_set_transport_route_for_extension(
            IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string routeJson);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_extension_user_loaded(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId,
            int userLoaded);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_fire_ext_event(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string eventName,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string argsJson);

        // 有 Action 的扩展清单 (JSON 数组; 返回的是 **thread_local** 指针 ⇒ 立刻拷走)。
        // ⚠ `popupPath` 为 null ⇒ 点击该派发 onClicked; 非 null ⇒ 只开 popup。
        // ⚠ 实测: **MV3 扩展都在里面**(上游给 MV3 一个默认 Action, 清单没写 action 键也一样),
        //   与 Chrome 一致。别读成"清单里写了 action 键的那些"。
        // 🔴 native 通道的准入 (2026-09-08 产品拍板)。
        //   0 签名包 · 1 未签名+**用户自己加载的开发包**(需同意) ·
        //   2 清单没声明 · 3 商店装的未签名包。**0 和 1 都不是拒, 别折成 bool。**
        // ⚠ `user_loaded` 由**宿主如实断言** —— 与 `userGesture` 同一条纪律:
        //   缺省按**没有**处理, 不按有。谎报 true 等于把"加载开发包"这个入口
        //   当成已经做了。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_native_check(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId,
            int declared,
            int userLoaded);

        // 🔴 这条权限**能看到用户的什么** —— 一句给普通用户看的中文。
        //   2026-09-08 产品拍板「说明隐私风险, 而不是拒绝」之后,
        //   **它是放行之后唯一还在的保护**。回 NULL = 这条没写说明,
        //   **不等于**"它没有隐私风险"。
        // ⚠ 参数是**权限名**, 不是判定码 —— 与上面几条刚好相反, 别传混。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_mv3_perm_privacy_note(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string permission);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_kernel_list_extension_actions();

        // activeTab 撤销 —— 🔴 **三个时机都要调, 少一个就是权限跟着用户跑**:
        //   ①导航(document 变了) ②标签页关闭 ③扩展被停用/卸载
        // ⚠ 内核**不监听**宿主的标签页生命周期, 它不知道你们什么时候导航。
        //   漏调不会报错, 但插件会在它不该有权限的页面上继续有权限。
        // ⚠ `navigationId` 必须与 `dispatch_action_click` 那次用的**同一个值** ——
        //   两处各自编一个字符串, 撤销就对不上(那正是"撤销了却没真撤"的形态)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_revoke_active_tab_document(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string tabId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string navigationId);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_revoke_active_tab_tab(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string tabId);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_revoke_active_tab_extension(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string extensionId);

        // 现有 activeTab 授权条数 —— 给判据用: **反测要能看见"撤销之后真的少了一条"**。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_active_tab_grant_count();

        // FB-P034: 全局/进程级 DoH (MINOR 7 dll 才有此导出)。mode: 0=off(系统DNS)
        // 1=automatic(尽力升级DoH失败回落) 2=secure(强制DoH失败不回落); dohTemplate =
        // DoH URL 模板 (如 "https://dns.alidns.com/dns-query"), off 时忽略。
        // 返回 ArupaResult (0=OK, 4=INVALID_ARG 模板非法/secure 缺模板)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_kernel_set_doh(IntPtr kernel, int mode,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string dohTemplate);

        // spare renderer 预热 (内核 06-24 dll 才有此导出; 内核侧 SpareRenderProcessHostManager::Get().WarmupSpare):
        //   kernel_create 成功后调一次 → 内核预起一个空闲渲染进程 (arupa_render.exe), 首次导航直接领用,
        //   省进程 fork/exec + V8 初始化。返回 ArupaResult (0=OK)。失败非致命 (内核会按需起渲染进程)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_warmup_renderer(IntPtr kernel);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_create(IntPtr kernel, in WebViewConfig cfg,
            in WebViewCallbacks cbs, in OsrSink osr, out IntPtr view);
        // KI-001 (FB-P018 根治): size-checked 变体。每个 struct 传本 wrapper 编译期 sizeof,
        // 内核只信任 min(传入, 内核 sizeof) 字节 → wrapper 不同步只会少订阅新回调, 永不野跳崩
        // (断 FB-P012/P016/P018 一族)。MINOR 2 dll 才有此导出; 旧 dll 走 arupa_webview_create 兜底。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_create_checked(IntPtr kernel,
            in WebViewConfig cfg, nuint cfgSize,
            in WebViewCallbacks cbs, nuint cbsSize,
            in OsrSink osr, nuint osrSize,
            out IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_destroy(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_free(IntPtr p);

        /// <summary>本线程最近一次失败的结构化说明 (arupa_free 释放; NULL = 本线程没记录过失败)。
        /// ⚠ **thread_local** —— 谁调的谁读, 且只在紧接着的失败之后读才有意义 (capi.h §错误通道)。</summary>
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_last_error_json();   // arupa_free

        // ── 导航 / 状态 ─────────────────────────────────────────────────────────
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_load_url(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string url);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_reload(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_reload_ignore_cache(IntPtr view);   // FB-P008
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_render_active(IntPtr view, int active);   // FB-P014: 后台 view 停渲染: 硬刷新 (BYPASSING_CACHE)
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_go_back(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_go_forward(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_webview_get_url(IntPtr view);    // arupa_free
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_webview_get_title(IntPtr view);  // arupa_free
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_resize(IntPtr view, int w, int h);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_screen_rect(IntPtr view, int x, int y, int w, int h);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_exit_fullscreen(IntPtr view);
        // MINOR24 / pip.host: 宿主请求转发开关；与 Blink DOM 能力探测及原生会话支持分开。
        // ⚠ 老内核无此导出: 调用前必须 arupa_kernel_supports("pip.host") == 1, 否则 EntryPointNotFound。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_picture_in_picture_host_enabled(IntPtr view, int enabled);
        // MINOR25 / presentation.v1: 统一呈现通道的反向出口 (宿主关闭某呈现模式 → 通知内核清状态)。
        // mode: 1=全屏 (走内核 ExitFullscreen: 清指针/键盘锁 + 同步 renderer) / 2=画中画 (仅清"宿主接管"标记)。
        // ⚠ 老内核无此导出: 调用前必须 arupa_kernel_supports("presentation.v1") == 1, 否则 EntryPointNotFound。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_exit_presentation(IntPtr view, int mode);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_report_picture_in_picture(IntPtr view, ulong requestSequence, int result);
        // ── FB-P003: 导航/状态/查找/UA (capi 已有, 高层 wiring) ───────────────────
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_stop(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_clear_history(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_can_go_back(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_can_go_forward(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_is_loading(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_user_agent(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string ua);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_find_all(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string text);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_find_next(IntPtr view, int forward);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_clear_matches(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_js_dialog(IntPtr view, int dialogId,
            int accept, [MarshalAs(UnmanagedType.LPUTF8Str)] string? promptText);
        // FB-P003 第二批: 缩放 / 静音。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_zoom(IntPtr view, double zoomFactor);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern double arupa_webview_get_zoom(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_audio_muted(IntPtr view, int mute);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_is_audio_muted(IntPtr view);

        // ── 输入 ────────────────────────────────────────────────────────────────
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_send_mouse(IntPtr view, int type, int x,
            int y, int button, int modifiers);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_send_key(IntPtr view, int type,
            int windowsKeyCode, int modifiers, [MarshalAs(UnmanagedType.LPUTF8Str)] string? text);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_send_wheel(IntPtr view, int x, int y,
            int dx, int dy);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_focus(IntPtr view, int focused);

        // ── eval / 代理 / CDP ───────────────────────────────────────────────────
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void EvalCbNative(IntPtr user, IntPtr jsonResult);   // ArupaEvalCb (capi.h:682)
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_eval_js_for_document(IntPtr view, ulong document,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string script, IntPtr callback, IntPtr user);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_eval_js(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string script, IntPtr cb, IntPtr user);
        // ── MV3 Phase 1 keystone: 隔离世界注入 (capi.h:692) ─────────────────────
        // ⚠ 方案A(同上方 FB-P013 那批): 本条 P/Invoke 由对接侧(桌面端会话)按 FB-P062 草拟,
        //   属内核 Interop owner 域, 待内核 review 确认 ABI/语义。新增独立函数, 非 struct 改动
        //   → 零 FB-P012 式漂移风险。
        // world_id 合法段 [1,11] (主世界=0 是内核反指纹 JS 所在, 插件永远不能进); 越界内核 no-op。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_inject_isolated_js(IntPtr view, int worldId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string script, IntPtr cb, IntPtr user);
        // 页面内注入的域名粒度授权 (capi.h:719/727) —— 判定在内核, 宿主只按返回码决定"拒/弹/放行"。
        // 返回值见 ARUPA_INJECT_* (0=ALLOW 1=未声明 2=未授权 3=受保护scheme 4=被代理域 5=<all_urls>未授权)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_inject_check(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string url,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string declaredCsv,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string grantedCsv,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string managedHostsCsv);
        // MV3 Phase 4: manifest permission → 内核 capability 安全映射 (capi.h:752/755)。
        // 运行时 (nomad-plugin-runtime.js) 的 host.guard.mapPermission 就吃这个判定。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_mv3_map_permission(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string permission);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_mv3_permission_capability(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string permission);   // 静态串, 不 free
        // 判 7 时"替代顶不到哪儿" (2026-09-06 内核加, **而 .NET 门面 09-07 才补上** ——
        // 中间这段时间对接文档里写着 `ArupaMv3.SubstitutionGap(perm)`, 而它并不存在)。
        // 🔴 与 permission_capability 是**一对**: 那个答"替代走哪条能力", 这个答缺口。
        //   只给前者的后果实测过: 对接侧当成全量等价 ⇒ 不提示 ⇒ 覆盖不到的那半静默失效。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_mv3_substitution_gap(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string permission);   // 静态串, 不 free
        // 判定码 → 名字 (capi.h, 2026-09-04)。**静态串, 不 free**;
        // 🔴 认不出的码回 **NULL** —— 不是回 "DENY_UNKNOWN"(那会与码 4 撞名, 分不出
        //   "码是 4"和"这个码我不认识")。⇒ 这里用 LPUTF8Str 返回, null 会 marshal 成 null string,
        //   调用方**必须判空**, 拿到 null 按拒处理并明说"本版本不认识这个码", **不许猜成放行**。
        // ⚠ 旧内核没有这个导出 ⇒ 调它会 EntryPointNotFoundException。采用条件是
        //   **生产基线含它**(153.0.8010.12+2 起), 不是"我这台机器上的 current 有了"。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        [return: MarshalAs(UnmanagedType.LPUTF8Str)]
        public static extern string arupa_mv3_verdict_name(int verdict);

        // ── 判定码 → 两个**不同的问题** (2026-09-07) ────────────────────────
        // ① 这条能力能不能用  ② 这插件装不装得上。两者**只在码 8 上不同**。
        // 🔴 别自己按码写 if —— 与 verdict_name 同一条理由: 别让每一端各抄一份表。
        //   安卓那边就是自己写的, 而它对 7 和 8 一律回 false, 两个问题各答错一半。
        // ⚠ 认不出的码回 0(deny-by-default)。旧内核没有这两个导出 ⇒
        //   调它会 EntryPointNotFoundException, 采用条件是生产基线含它。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_mv3_perm_usable(int verdict);

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_mv3_plugin_installable(int verdict);
        // ── 出口路由的**档位**护栏 (capi.h:735, 2026-08-19 新增) ────────────────
        // 回答"这个插件能不能拿到**这一档作用域**的出口路由": 签名包四档全给; 未签名只给
        // global 且首次要用户点头一次, 请求更细的作用域一律拒。
        // 🔴 is_signed **不是入参** —— 内核自己查 active 签名包。做成入参等于把信任交给调用方。
        // 返回值见 ARUPA_ROUTE_*(0=ALLOW 1=需同意一次 2=更细作用域要签名 3=清单没声明 4=兜底拒)。
        // ⚠ 方案A(同上): 本条 P/Invoke 由对接侧(桌面端会话)按 capi.h 草拟, 属内核 Interop owner
        //   域, 待 review。新增独立函数, 非 struct 改动 → 零 FB-P012 式漂移风险。
        // ⚠ **2026-08-19 的交付件里还没有这个导出** (内核当天才落地)。调用方要按"探测不到就
        //   如实降级"处理, 别直接调 —— 老 dll 上会抛 EntryPointNotFoundException。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_route_check(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string scope,
            int declared);
        // ── FB-P104: 扩展 API 转宿主 (capi.h 的「扩展 API 转宿主」那一节) ────────
        //
        // 🔴 `chrome.tabs` / `action` / `contextMenus` / `sidePanel` / `permissions`
        //   的语义**内核答不出来**(哪些标签页存在、侧栏怎么渲、右键菜单挂哪 ——
        //   都在宿主这一侧)。内核只做一件事: **把调用如实转出来**, 并保证一定有答复。
        //
        // ⚠ **回调必须被钉住** —— 它活得比这次调用长(注册一次, 之后每次插件调 API 都会进来)。
        //   局部委托会被 GC 掉, 然后内核回调时野跳。门面里用 static 字段保活, 见 ArupaBrowser.cs。
        // ⚠ 三个 IntPtr 都是**回调期内有效**的 UTF-8 指针, 要用就 `PtrToStringUTF8` 拷走。
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        public delegate void ArupaExtApiCb(IntPtr user, int requestId,
                                           IntPtr extensionId, IntPtr name,
                                           IntPtr argsJson);
        // 🔴 FB-P154: 权限项的**一份真相源**(名字/判定/能力映射/给用户看的那句话)。
        //   ⚠ 回的是**拥有权**字符串, 用 `Interop.TakeOwned` 接。
        //   ⚠ `note` 为 null = **内核没有这句话**, 不是"这条权限无害"。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_kernel_get_permission_descriptions();

        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_set_ext_api_handler(ArupaExtApiCb? cb,
                                                                   IntPtr user);
        // 二选一: resultJson 非空 = 成功(**回调参数数组**的 JSON); error 非空 = 失败。
        // ⚠ 不答的话插件那边永远 pending —— 内核有 10 秒超时兜底, 但那是兜底不是设计。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_kernel_respond_ext_api(
            int requestId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string? resultJson,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string? error);
        // ── MV3 声明式网络规则**护栏** (capi.h:731) ─────────────────────────────
        // 是护栏不是规则引擎本体: 拦住会碰代理流量或内核页的规则。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_mv3_check_declarative_rule(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string urlPattern,
            int actionType,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string managedHostsCsv);
        // ── MV3 Phase 3: per-plugin 隔离存储 (capi.h:738/743/744/747/749) ────────
        // 🔴 get / get_all 返回的指针是 **thread_local**, 只在"同线程下次 get 之前"有效
        //   ⇒ 这里一律声明成 IntPtr, 由门面**立刻拷成托管 string**。
        //   声明成 string 让默认 marshaller 拷也行, 但那样就看不出这条约定了 ——
        //   而下一个人照着改成"存起来待会儿用"就会读到已被覆盖的缓冲区。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_plugin_storage_get(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string key);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_plugin_storage_get_all(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_plugin_storage_set(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string key,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string valueJson);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_plugin_storage_remove(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string key);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_plugin_storage_clear(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string pluginId);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_inject_needs_reconsent(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string oldDeclaredCsv,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string newDeclaredCsv,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string grantedCsv);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_proxy(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string socks5Url);
        // ── FB-P013 护城河: 传输路由 / 延迟代理 / 待生效身份 / 诊断 (capi.h:264-270,223) ──
        // ⚠ 方案A: 本 4 条 P/Invoke 由对接侧(App 会话)按 FB-P013 草拟, 属内核 Interop owner 域,
        //   待内核 review 确认 ABI/语义。均为新增独立函数(非 struct 改动) → 零 FB-P012 式漂移风险。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_pending_proxy(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string socks5Url);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_transport_route(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string routeJson);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_pending_identity(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string identityJson);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_webview_get_diagnostics(IntPtr view, int flags);   // arupa_free
        // ── FB-P013 第二批: 配置/状态/数据 (capi.h:182,226-234,245,250; ⚠内核 Interop owner 域待 review) ──
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_load_data(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string html,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string? baseUrl);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_web_prefs(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string prefsJson);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_webview_save_state(IntPtr view, out IntPtr outLen);   // arupa_free
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern int arupa_webview_restore_state(IntPtr view, byte[] data, IntPtr len);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_flush_cookies(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_get_cookies(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string url, IntPtr cb, IntPtr user);
        // FB-P008③/FB-A008: 当前页打成真矢量 PDF (//components/printing PdfPrintJob, 非 CDP/位图)。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_print_to_pdf(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string? optionsJson, IntPtr cb, IntPtr user);
        // ── FB-P013 第三批: 权限/认证/文件选择应答 (capi.h:275-284; ⚠内核 Interop owner 域待 review) ──
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_http_auth(IntPtr view, int authId,
            int proceed, [MarshalAs(UnmanagedType.LPUTF8Str)] string? user,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string? password);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_geolocation(IntPtr view, int geoId, int granted);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_media_permission(IntPtr view, int permId, int granted);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_on_file_chooser_result(IntPtr view, int chooserId,
            [MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.LPUTF8Str)] string[]? paths,
            int pathCount);
        // ── A批: 证书错误决策 + 下载控制 (配 on_certificate_error / on_download_updated) ──
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_certificate_error(IntPtr view,
            int certErrorId, int proceed);   // proceed!=0 无视错误继续; =0/不应答 = 取消 (安全默认)
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_download_control(IntPtr view,
            int downloadId, int action);     // 0=cancel 1=pause 2=resume
        // ── MINOR6 (FB-P025/P026) ──
        // FB-P025: 当前页 TLS 证书详情 JSON (schema "arupa-cert/1"); 返 NULL=无证书; caller arupa_free。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern IntPtr arupa_webview_get_certificate_info(IntPtr view);   // arupa_free
        // FB-P026: 通知权限应答 (配 on_notification_permission_requested)。granted!=0=允许。
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_respond_notification_permission(IntPtr view,
            int notifId, int granted);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_cdp_attach(IntPtr view, IntPtr onMessage, IntPtr user);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_cdp_send(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string jsonCommand);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_cdp_detach(IntPtr view);

        // ── Cookie (FB-P002 B5/B3) ───────────────────────────────────────────────
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_cookie(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string url,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string cookieLine);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_remove_all_cookies(IntPtr view);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_get_all_cookies(IntPtr view, IntPtr cb, IntPtr user);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_delete_cookies(IntPtr view,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string urlOrDomain);
        [DllImport(Dll, CallingConvention = CallingConvention.Cdecl)]
        public static extern void arupa_webview_set_block_third_party_cookies(IntPtr view, int block);

        public static string? Utf8(IntPtr p) => p == IntPtr.Zero ? null : Marshal.PtrToStringUTF8(p);
        public static string? TakeOwned(IntPtr p)
        {
            if (p == IntPtr.Zero) return null;
            string? s = Marshal.PtrToStringUTF8(p);
            arupa_free(p);
            return s;
        }
    }
}
