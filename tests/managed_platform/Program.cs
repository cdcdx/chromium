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
    }
    Check(ArupaKernel.ProcessState == ArupaKernelState.Stopped, "managed shutdown completes");
    return 0;
});
