using System;
using System.Runtime.InteropServices;

namespace Arupa
{
    public sealed partial class ArupaKernel
    {
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void WorkerReadinessCallback(IntPtr user, IntPtr json);

        private static readonly object WorkerReadinessGate = new();
        private static WorkerReadinessCallback? workerReadinessCallback;

        [DllImport(Interop.Dll, CallingConvention = CallingConvention.Cdecl)]
        private static extern void arupa_kernel_set_mivpn_worker_readiness_observer(
            WorkerReadinessCallback? observer, IntPtr user);

        [DllImport(Interop.Dll, CallingConvention = CallingConvention.Cdecl)]
        private static extern IntPtr arupa_kernel_get_mivpn_worker_readiness();

        /// <summary>
        /// Replaces the process-wide MiVPN worker observer. The callback runs on
        /// the kernel UI sequence; copy the record and marshal longer work.
        /// Returns false for an older kernel without the readiness exports.
        /// </summary>
        public static bool TrySetMivpnWorkerReadinessObserver(Action<string>? observer)
        {
            lock (WorkerReadinessGate)
            {
                WorkerReadinessCallback? next = observer is null ? null : (_, pointer) =>
                {
                    try
                    {
                        if (Marshal.PtrToStringUTF8(pointer) is { } json) observer(json);
                    }
                    catch (Exception error)
                    {
                        System.Diagnostics.Debug.WriteLine(error.Message);
                    }
                };
                var previous = workerReadinessCallback;
                try
                {
                    arupa_kernel_set_mivpn_worker_readiness_observer(next, IntPtr.Zero);
                    workerReadinessCallback = next;
                    return true;
                }
                catch (EntryPointNotFoundException) { return false; }
                catch (DllNotFoundException) { return false; }
                finally { GC.KeepAlive(previous); }
            }
        }

        /// <summary>
        /// Copies the latest readiness JSON record. A snapshot alone does not
        /// prove a fresh activation; consumers must match a new pending/ready
        /// cycle and its generation. Returns null when the export is absent.
        /// </summary>
        public static string? GetMivpnWorkerReadiness()
        {
            try { return Interop.TakeOwned(arupa_kernel_get_mivpn_worker_readiness()); }
            catch (EntryPointNotFoundException) { return null; }
            catch (DllNotFoundException) { return null; }
        }
    }
}
