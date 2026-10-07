"""Conservative default parallelism for large Chromium C++ compilation units."""
from __future__ import annotations

import ctypes
import os
import platform

GIB = 1024 ** 3
MEMORY_PER_JOB = 2 * GIB
RESERVE_MINIMUM = 1.5 * GIB


def physical_memory():
    """Return physical RAM in bytes, or None when the host query fails."""
    try:
        system = platform.system()
        if system == 'Windows':
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                            *[(name, ctypes.c_ulonglong) for name in
                              ('total_physical', 'available_physical', 'total_page', 'available_page',
                               'total_virtual', 'available_virtual', 'available_extended')]]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.total_physical or None
        if system == 'Darwin':
            value = ctypes.c_uint64()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            if libc.sysctlbyname(b'hw.memsize', ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
                return None
            return value.value or None
        size = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
        return size if size > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


def cpu_capacity():
    count = os.cpu_count() or 1
    try:
        count = min(count, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    return max(1, count)


def jobs_for_resources(cpus, memory):
    # Reserve at least 1.5 GiB or 20% for the OS/linker; budget 2 GiB per compiler.
    # This is a heuristic, not a per-process memory limit.
    if not memory or memory <= 0:
        return 1
    reserve = max(RESERVE_MINIMUM, (memory + 4) // 5)
    return max(1, min(max(1, cpus), (memory - reserve) // MEMORY_PER_JOB))


def automatic_jobs():
    cpus, memory = cpu_capacity(), physical_memory()
    jobs = jobs_for_resources(cpus, memory)
    reason = (f'CPU 逻辑核心={cpus}，内存={memory / GIB:.1f} GiB，'
              f'每任务预算 {MEMORY_PER_JOB / GIB:.0f} GiB，预留至少 {RESERVE_MINIMUM / GIB:.1f} GiB 或总内存 20%'
              ) if memory else f'CPU 逻辑核心={cpus}，无法读取内存，保守使用 1 个任务'
    return jobs, reason
