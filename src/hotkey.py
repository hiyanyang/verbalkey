"""全局热键。用 Win32 低级键盘钩子，可区分左右 Alt，不需要管理员权限。"""

from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, CFUNCTYPE, c_int, c_long, c_ulong, wintypes

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

DOWN = (WM_KEYDOWN, WM_SYSKEYDOWN)
UP = (WM_KEYUP, WM_SYSKEYUP)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.CallNextHookEx.restype = c_long
# 必须声明 argtypes：l_param 在 64 位下是 LONG_PTR，
# 不声明的话 ctypes 会按 32 位 int 传，每次按键都抛 OverflowError，
# 钩子链就断了。
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.argtypes = [POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", POINTER(c_ulong)),
    ]


HOOKPROC = CFUNCTYPE(c_long, c_int, wintypes.WPARAM, wintypes.LPARAM)


class HotKey:
    """按住说话热键。on_press / on_release 在钩子线程触发，必须尽快返回。"""

    def __init__(self, vk: int, on_press, on_release, swallow: bool = True,
                 on_error=None):
        self.vk = vk
        self.on_press = on_press
        self.on_release = on_release
        self.swallow = swallow
        # 钩子装不上时回调（安全软件拦截、系统限制）。没有这条的话
        # 用户按热键毫无反应，也不知道为什么 —— 打包后 stdout 是空的
        self.on_error = on_error
        self._hook = None
        self._thread = None
        self._thread_id = None
        self._cb_ref = None
        self._down = False
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def set_vk(self, vk: int) -> None:
        # 如果正按着旧键，先把「松开」补出去：不然录音状态会永远卡在
        # busy=True，之后按什么键都没反应
        if self._down:
            self._down = False
            self._safe(self.on_release)
        self.vk = vk

    def _callback(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code >= 0:
            try:
                kb = ctypes.cast(l_param, POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == self.vk:
                    if w_param in DOWN:
                        if not self._down:
                            self._down = True
                            self._safe(self.on_press)
                        return 1 if self.swallow else 0
                    if w_param in UP:
                        if self._down:
                            self._down = False
                            self._safe(self.on_release)
                        return 1 if self.swallow else 0
            except Exception:
                pass
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._cb_ref = HOOKPROC(self._callback)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._cb_ref, None, 0
        )
        if not self._hook:
            self._running = False
            err = ctypes.get_last_error()
            if self.on_error:
                try:
                    self.on_error(f"热键注册失败（错误码 {err}）")
                except Exception:
                    pass
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
