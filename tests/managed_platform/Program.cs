// Run from outside the kernel directory: dotnet ManagedPlatform.dll <kernel-dir> <temporary-profile>.
using Arupa;
using System.Runtime.InteropServices;
using System.Text.Json;

if (args.Length != 2) throw new ArgumentException("Expected kernel directory and disposable profile directory.");
string output = Path.GetFullPath(args[0]), profile = Path.GetFullPath(args[1]);
string library = OperatingSystem.IsWindows() ? "arupa_kernel.dll" :
    OperatingSystem.IsMacOS() ? "libarupa_kernel.dylib" : "libarupa_kernel.so";
NativeLibrary.SetDllImportResolver(typeof(ArupaKernel).Assembly, (name, assembly, flags) =>
    name == "arupa_kernel" ? NativeLibrary.Load(Path.Combine(output, library)) : IntPtr.Zero);

void Check(bool ok, string label)
{
    Console.WriteLine($"{(ok ? "PASS" : "FAIL")} {label}");
    if (!ok) Environment.Exit(1);
}

return ArupaKernel.RunMacHost(() => {
    Check(ArupaKernel.ProcessState == ArupaKernelState.NotStarted, "managed lifecycle state");
    using var caps = JsonDocument.Parse(ArupaKernel.GetCapabilitiesJson()!);
    Check(caps.RootElement.GetProperty("schema").GetInt32() == 1, "managed capability JSON ownership");
    using (var kernel = new ArupaKernel(new ArupaKernelOptions {
        UserDataDir = profile, PakDir = output, DeviceScaleFactor = 1, FingerprintHardening = false
    })) {
        using var view = kernel.CreateWebView(new ArupaWebViewOptions { Width = 640, Height = 360 });
        using var loaded = new ManualResetEventSlim();
        view.PageFinished += (_, _) => loaded.Set();
        view.LoadUrl("data:text/html,<input id=field><script>window.keys=[];onkeydown=e=>keys.push([e.key,e.code]);field.focus()</script>");
        Check(loaded.Wait(10000), "managed page loads");
        string? proxy = view.LookupProxyForUrlAsync("https://example.test/").GetAwaiter().GetResult();
        Check(!string.IsNullOrWhiteSpace(proxy), "managed native proxy lookup returns a PAC result");
        using (var frames = JsonDocument.Parse(view.ListFramesJson())) {
            Check(frames.RootElement.EnumerateArray().Any(frame =>
                frame.GetProperty("is_main").GetBoolean() && frame.GetProperty("live").GetBoolean()),
                "managed native frame list contains the live main frame");
        }
        using (var cdp = JsonDocument.Parse(view.SendCdpAsync("Runtime.evaluate",
            "{\"expression\":\"6 * 7\",\"returnByValue\":true}", 8000, false).GetAwaiter().GetResult())) {
            Check(cdp.RootElement.GetProperty("result").GetProperty("value").GetInt32() == 42,
                "managed four-parameter CDP calls the native session");
        }
        view.SetViewport(50, 80, 640, 360, 2, 30);
        view.SetFocus(true);
        view.SendKeyV2(0, 65, "KeyA", "é");
        view.SendKeyV2(1, 65, "KeyA", "é");
        bool received = false;
        for (int i = 0; i < 50 && !received; ++i) {
            received = view.EvalJsAsync("devicePixelRatio === 2 && innerWidth === 640 && keys.some(k => k[0]==='é' && k[1]==='KeyA')")
                .GetAwaiter().GetResult() == "true";
            if (!received) Thread.Sleep(100);
        }
        Check(received, "managed viewport and UTF-8 key ABI");
        // Select the legacy partition before the same about:blank CDP bootstrap
        // used by OOP hosts. The marker is only a path fixture, not a real DB.
        const string legacyId = "abcdefghijklmnopabcdefghijklmnop";
        Directory.CreateDirectory(Path.Combine(profile, "IndexedDB", $"chrome-extension_{legacyId}_0.indexeddb.leveldb"));
        using var migration = kernel.CreateWebView(new ArupaWebViewOptions {
            OffTheRecord = false, LegacyExtensionPartitionId = legacyId
        });
        using var legacyLoaded = new ManualResetEventSlim();
        migration.PageFinished += (_, _) => legacyLoaded.Set();
        migration.LoadUrl("about:blank");
        Check(legacyLoaded.Wait(10000), "legacy bootstrap document loads");
        migration.SendCdpAsync("Runtime.enable").GetAwaiter().GetResult();
        Check(Path.TrimEndingDirectorySeparator(migration.PartitionKey) == Path.TrimEndingDirectorySeparator(profile),
            "legacy view keeps the default partition through CDP bootstrap");
        using var privateView = kernel.CreateWebView(new ArupaWebViewOptions { OffTheRecord = true });
        Check(!privateView.UseLegacyExtensionPartition(legacyId), "native legacy selection rejects private views");
        string marker = Path.Combine(profile, "ExtensionPartitionMigration", legacyId + ".json");
        Directory.CreateDirectory(marker);
        File.WriteAllText(Path.Combine(marker, "occupied"), "keep");
        Check(!kernel.ForgetExtensionStorageMigration(legacyId), "forget reports migration marker deletion failure");
        using (var failedClear = JsonDocument.Parse(kernel.ClearExtensionStorage(legacyId)))
            Check(failedClear.RootElement.GetProperty("code").GetInt32() == 7,
                "cleanup reports migration marker deletion failure");
        Directory.Delete(marker, true);
        Check(kernel.ForgetExtensionStorageMigration(legacyId), "forget succeeds after marker failure is resolved");
    }
    Check(ArupaKernel.ProcessState == ArupaKernelState.Stopped, "managed shutdown completes");
    return 0;
});
