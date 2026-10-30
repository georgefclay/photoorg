"""Resident memory of this process, for the Sync tab and the log.

Low memory is the usual killer on this laptop (CLAUDE.md, Cleanup (c)), and
a process killed for it leaves no traceback — so the figure is shown while a
job runs and written beside every stage line. No psutil: one Win32 call, or
/proc on anything else.
"""
from __future__ import annotations

import os
import sys


def rss_bytes() -> int | None:
    """Working set in bytes, or None when it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi")
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                            ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        with open(f"/proc/{os.getpid()}/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:  # noqa: BLE001 — a missing figure must never break a job
        return None


def rss_text() -> str:
    b = rss_bytes()
    return "memory ?" if b is None else f"memory {b / (1024 * 1024):,.0f} MB"
