"""Process helpers shared by the tests."""
import os
import os.path as op
import sys


def pid_alive(pid):
    """Whether process `pid` is running (zombies are not)."""
    if sys.platform == "win32":
        # Never use os.kill(pid, 0) on Windows: it terminates the process
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        ]
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/status") as f:
            return "\nState:\tZ" not in f.read()
    except FileNotFoundError:
        if op.isdir("/proc/self"):
            return False
    # No procfs (macOS)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True
