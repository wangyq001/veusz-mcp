"""Read Windows desktop identity without switching desktops or changing input."""

import os


def desktop_context():
    result = {"window_station": None, "thread_desktop": None,
              "input_desktop": None, "desktop_is_interactive": False,
              "desktop_error": None}
    if os.name != "nt":
        result["desktop_error"] = "Desktop verification requires Windows"
        return result

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL

    def name(handle):
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        needed = wintypes.DWORD()
        user32.GetUserObjectInformationW(handle, 2, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(
            needed.value // ctypes.sizeof(ctypes.c_wchar) + 1)
        if not user32.GetUserObjectInformationW(
                handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    try:
        result["window_station"] = name(user32.GetProcessWindowStation())
        result["thread_desktop"] = name(
            user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
        input_handle = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not input_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result["input_desktop"] = name(input_handle)
        finally:
            user32.CloseDesktop(input_handle)
        result["desktop_is_interactive"] = (
            result["window_station"].casefold() == "winsta0" and
            result["thread_desktop"].casefold() == result["input_desktop"].casefold())
    except OSError as exc:
        result["desktop_error"] = str(exc)
    return result
