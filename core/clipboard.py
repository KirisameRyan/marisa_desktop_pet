import ctypes


def get_clipboard_text(max_len: int = 2000):
    """读取 Windows 剪贴板文本。
    返回 (text, kind)：kind ∈ {"text", "empty", "other"}。
    用 ctypes 直读 Win32 API，可安全地在 QThread 中调用（不依赖 Qt）。
    注意显式声明 64 位指针 restype，否则句柄/指针被截断导致访问违例。"""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = ctypes.c_bool
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool

        if not user32.OpenClipboard(None):
            return "", "empty"
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return "", "other"
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return "", "other"
            size = kernel32.GlobalSize(handle)
            if not size:
                return "", "empty"
            size = min(size, (max_len + 1) * 2)
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return "", "other"
            try:
                buf = ctypes.create_string_buffer(size)
                ctypes.memmove(buf, ptr, size)
                raw = buf.raw
            finally:
                kernel32.GlobalUnlock(handle)
            text = raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0].strip()
            if not text:
                return "", "empty"
            return text[:max_len], "text"
        finally:
            user32.CloseClipboard()
    except Exception as e:
        print(f"[clipboard] error: {e}")
        return "", "other"
