using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text.Json;
using Arupa;

NativeLibrary.SetDllImportResolver(typeof(ArupaWebView).Assembly, (name, _, _) =>
    name is "arupa_kernel" or "compat_shim" ? NativeLibrary.Load(Path.GetFullPath(args[0])) : IntPtr.Zero);
Probe.LegacyOffset((int)Marshal.OffsetOf<Interop.WebViewCallbacks>(nameof(Interop.WebViewCallbacks.on_open_new_window)));
int passed=0;
void Check(bool ok,string name) { if(!ok) throw new Exception(name); ++passed; Console.WriteLine("PASS " + name); }
async Task Throws<T>(Func<Task> action,string name) where T:Exception {
    try { await action(); } catch(T) { Check(true,name);return; } throw new Exception(name);
}
using var view = new ArupaWebView((IntPtr)1,new());
IntPtr Handle(ArupaWebView v) => (IntPtr)typeof(ArupaWebView).GetField("_handle",BindingFlags.NonPublic|BindingFlags.Instance)!.GetValue(v)!;
Check(typeof(ArupaWebView).GetMethod("SendCdpAsync",[typeof(string),typeof(string),typeof(int)]) is not null,"three-argument binary signature");
Check(typeof(ArupaWebView).GetMethod("SendCdpAsync",[typeof(string),typeof(string),typeof(int),typeof(bool)]) is not null,"four-argument binary signature");
Probe.Mode(1);
Check((await view.SendCdpAsync("Runtime.enable")).Contains("true") && Probe.Sends()==2,"default rebound retries once");
Probe.Mode(1);
Check((await view.SendCdpAsync("Runtime.enable",null,1000)).Contains("true") && Probe.Sends()==2,"three-argument forwards to recovery");
Probe.Mode(1);
await Throws<InvalidOperationException>(()=>view.SendCdpAsync("Runtime.evaluate",retryOnRebound:false),"opt-out reports rebound");
Check(Probe.Sends()==1,"opt-out sends once");
Probe.Mode(2);
await Throws<InvalidOperationException>(()=>view.SendCdpAsync("Runtime.enable"),"repeated rebound propagates error");
Check(Probe.Sends()==2,"repeated rebound is bounded");
Probe.Mode(3);
await Throws<InvalidOperationException>(()=>view.SendCdpAsync("Runtime.enable"),"ordinary error propagates");
Check(Probe.Sends()==1,"ordinary error not retried");
Probe.Mode(4);
await Throws<TimeoutException>(()=>view.SendCdpAsync("Runtime.enable",timeoutMs:25),"unanswered CDP times out");
Probe.Mode(0);
Check((await view.SendCdpAsync("Runtime.enable")).Contains("true"),"timed-out request does not poison next command");
Probe.ProxyMode(0);
Check(await view.LookupProxyForUrlAsync("https://example.test")=="PROXY real.test:8080","actual proxy result preserved");
Probe.ProxyMode(1);
Check(await view.LookupProxyForUrlAsync("https://example.test") is null,"network error returns null");
Probe.ProxyMode(2);
Check(await view.LookupProxyForUrlAsync("https://example.test",25) is null,"proxy timeout finishes");
Probe.LateProxy();
Check(true,"late callback ignores freed request and invalid result pointer");
using(var disposed=new ArupaWebView((IntPtr)1,new())) {
    var lookup=disposed.LookupProxyForUrlAsync("https://example.test"); disposed.Dispose();
    Check(await lookup is null,"dispose resolves outstanding proxy request"); Probe.LateProxy();
}
int freed=Probe.Freed();
using(var frames=JsonDocument.Parse(view.ListFramesJson())) Check(frames.RootElement[0].GetProperty("frame_id").GetInt32()==7,"native frame data preserved");
Check(Probe.Freed()==freed+1,"native frame JSON freed exactly once");
int legacy=0, adopted=0;
view.NewWindowRequested += _=>legacy++;
Probe.Open(Handle(view)); Check(legacy==1,"no subscriber retains legacy popup");
Action<string> onNew=json=>{adopted++;using var d=JsonDocument.Parse(json);Check(d.RootElement.GetProperty("token").GetString()=="123","token serialized without precision loss");};
view.NewContentsRequested += onNew;
Probe.Open(Handle(view));Check(adopted==1&&legacy==1,"subscribed popup uses adoption only");
Check(ArupaWebView.DiscardPendingContents("123")&&!ArupaWebView.DiscardPendingContents("123"),"rejection is one-shot");
Probe.Open(Handle(view));
using(var child=new ArupaWebView((IntPtr)1,new(){PendingContentsToken="123"})) Check(!ArupaWebView.DiscardPendingContents("123"),"adoption consumes token");
view.NewContentsRequested -= onNew;
Probe.Open(Handle(view));Check(legacy==2,"unsubscribe restores legacy route");
var kernel=(ArupaKernel)RuntimeHelpers.GetUninitializedObject(typeof(ArupaKernel));
freed=Probe.Freed();
Check(kernel.GetExtensionStorageState("id").Contains("needs_merge")&&kernel.GetExtensionPartitionKey("id")=="/actual/partition"&&kernel.ClearExtensionStorage("id").Contains("0"),"migration facade returns native state and cleanup result");
Check(Probe.Freed()==freed+3,"all migration owned strings released");
Check(kernel.ForgetExtensionStorageMigration("id"),"merge completion can re-evaluate state");
int before=Probe.LegacySelected();
using(var migration=new ArupaWebView((IntPtr)1,new(){LegacyExtensionPartitionId="id",OffTheRecord=false})) Check(Probe.LegacySelected()==before+1,"migration partition selected during construction");
try { using var rejected=new ArupaWebView((IntPtr)1,new(){LegacyExtensionPartitionId="missing",OffTheRecord=false});throw new Exception("missing legacy accepted"); }
catch(InvalidOperationException) { Check(true,"missing legacy partition rejects construction"); }
try { using var rejected=new ArupaWebView((IntPtr)1,new(){LegacyExtensionPartitionId="id",OffTheRecord=true});throw new Exception("private legacy accepted"); }
catch(ArgumentException) { Check(true,"private view cannot select the persistent legacy partition"); }
Console.WriteLine($"PASS {passed} managed ABI/compatibility checks");

static class Probe {
 const string Dll="compat_shim";
 [DllImport(Dll,EntryPoint="probe_mode")] internal static extern void Mode(int value);
 [DllImport(Dll,EntryPoint="probe_sends")] internal static extern int Sends();
 [DllImport(Dll,EntryPoint="probe_freed")] internal static extern int Freed();
 [DllImport(Dll,EntryPoint="probe_legacy_offset")] internal static extern void LegacyOffset(int value);
 [DllImport(Dll,EntryPoint="probe_proxy_mode")] internal static extern void ProxyMode(int value);
 [DllImport(Dll,EntryPoint="probe_late_proxy")] internal static extern void LateProxy();
 [DllImport(Dll,EntryPoint="probe_open")] internal static extern void Open(IntPtr view);
 [DllImport(Dll,EntryPoint="probe_legacy_selected")] internal static extern int LegacySelected();
}
