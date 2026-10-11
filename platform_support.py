"""Small standard-library platform adapters; Windows needs no Unix utilities."""
from __future__ import annotations

import contextlib
import os
import subprocess
import time

WINDOWS = os.name == "nt"


def shell_quote(value: str) -> str:
    """Quote one literal PowerShell argument (never interpolate its contents)."""
    return "'" + value.replace("'", "''") + "'"


@contextlib.contextmanager
def exclusive_lock(path):
    with path.open("a+b") as lock:
        if WINDOWS:
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if not lock.tell():
                lock.write(b"\0")
                lock.flush()
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (13, 36):
                        raise
                    time.sleep(.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def detached_options():
    if WINDOWS:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


if WINDOWS:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    def process_details(pid):
        """Return creation time and command line, or None for dead/inaccessible PIDs."""
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            size = wintypes.ULONG()
            ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if ntdll.NtQueryInformationProcess(handle, 60, buffer, size.value, ctypes.byref(size)) < 0:
                return None
            value = UnicodeString.from_buffer(buffer)
            command = ctypes.wstring_at(value.Buffer, value.Length // 2)
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return {"created": created, "command": command}
        finally:
            kernel.CloseHandle(handle)

    def terminate_process(pid, created):
        handle = kernel.OpenProcess(0x101001, False, pid)
        if not handle:
            return
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            actual_created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if actual_created != created:
                return  # The PID was reused after the caller's ownership check.
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.WaitForSingleObject(handle, 5000)
        finally:
            kernel.CloseHandle(handle)

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = kernel.Process32FirstW.argtypes

    def process_parents():
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            result = []
            while present:
                result.append((entry.th32ProcessID, entry.th32ParentProcessID))
                present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            return result
        finally:
            kernel.CloseHandle(snapshot)

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE

    class KeyEvent(ctypes.Structure):
        _fields_ = [("key_down", wintypes.BOOL), ("repeat", wintypes.WORD),
                    ("virtual_key", wintypes.WORD), ("scan_code", wintypes.WORD),
                    ("character", wintypes.WCHAR), ("controls", wintypes.DWORD)]

    class InputEvent(ctypes.Union):
        _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]

    class InputRecord(ctypes.Structure):
        _fields_ = [("event_type", wintypes.WORD), ("event", InputEvent)]

    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(InputRecord),
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    def console_keys(keys):
        """Queue keyboard events in this process's console, without redirecting I/O."""
        handle = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            events = []
            for virtual_key, character in keys:
                for pressed in (True, False):
                    record = InputRecord()
                    record.event_type = 1
                    record.event.key = KeyEvent(pressed, 1, virtual_key, 0, character, 0)
                    events.append(record)
            records = (InputRecord * len(events))(*events)
            written = wintypes.DWORD()
            if not kernel.WriteConsoleInputW(handle, records, len(events), ctypes.byref(written)):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(events):
                raise OSError("Incomplete console input write")
        finally:
            kernel.CloseHandle(handle)

    @contextlib.contextmanager
    def process_job():
        """Own the supervisor and children atomically, including at process creation.

        Keep this context open until the background command or interactive shell ends:
        closing it also terminates the supervisor itself.
        """
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
            yield job
        finally:
            kernel.CloseHandle(job)
