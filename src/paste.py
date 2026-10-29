"""把文本写进当前焦点输入框。三级降级：Ctrl+V → WM_PASTE → 只留剪贴板。"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 不声明 restype 的话，ctypes 默认按 32 位 int 返回，64 位下 HANDLE/HGLOBAL
# 会被截断成半个指针 —— 表现就是剪贴板 API 静默失败，文本根本没写进去。
# 这一类 64 位截断坑必须逐个显式声明。
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.c_bool
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

user32.OpenClipboard.restype = ctypes.c_bool
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.CloseClipboard.restype = ctypes.c_bool
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.EmptyClipboard.restype = ctypes.c_bool
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetFocus.restype = ctypes.c_void_p
user32.SendMessageW.restype = ctypes.c_void_p
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p]
user32.SendInput.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_PASTE = 0x0302

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_CONTROL = 0x11
VK_V = 0x56


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ki", KEYBDINPUT),
                ("padding", ctypes.c_ubyte * 8)]


def _get_clipboard(retries: int = 5) -> str | None:
    for _ in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            p = kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    return None


def _set_clipboard(text: str, retries: int = 8) -> bool:
    """写入剪贴板。别的程序可能正占着剪贴板，所以要重试几次。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    for i in range(retries):
        if not user32.OpenClipboard(None):
            time.sleep(0.05)
            continue
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                time.sleep(0.05)
                continue
            p = kernel32.GlobalLock(h)
            if not p:
                # 拿不到指针就得自己释放，否则这块内存永远挂在进程上
                kernel32.GlobalFree(h)
                time.sleep(0.05)
                continue
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if user32.SetClipboardData(CF_UNICODETEXT, h):
                # 成功后所有权归系统，不能再动这个句柄
                return True
            kernel32.GlobalFree(h)
        finally:
            user32.CloseClipboard()
        time.sleep(0.05)
    return False


def _send_ctrl_v() -> bool:
    extra = ctypes.pointer(ctypes.c_ulong(0))
    down = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_CONTROL, 0, 0, 0, extra), (0,) * 8)
    v_down = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_V, 0, 0, 0, extra), (0,) * 8)
    v_up = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_V, 0, KEYEVENTF_KEYUP, 0, extra), (0,) * 8)
    up = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, extra), (0,) * 8)
    arr = (INPUT * 4)(down, v_down, v_up, up)
    return user32.SendInput(4, ctypes.byref(arr), ctypes.sizeof(INPUT)) == 4


def _send_wm_paste() -> bool:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    target = user32.GetFocus() or hwnd
    return bool(user32.SendMessageW(target, WM_PASTE, 0, 0))


def backspace(count: int) -> None:
    if count <= 0:
        return
    extra = ctypes.pointer(ctypes.c_ulong(0))
    for _ in range(count):
        down = INPUT(INPUT_KEYBOARD, KEYBDINPUT(0x08, 0, 0, 0, extra), (0,) * 8)
        up = INPUT(INPUT_KEYBOARD, KEYBDINPUT(0x08, 0, KEYEVENTF_KEYUP, 0, extra), (0,) * 8)
        arr = (INPUT * 2)(down, up)
        user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(INPUT))


def replace_last(old_text: str, new_text: str, mode: str = "auto",
                 restore: bool = True, hwnd: int = 0) -> str:
    """退格删掉刚上屏的原文，再粘整理版。富文本编辑器里可能误删，默认不自动调用。"""
    refocus(hwnd)
    backspace(len(old_text))
    return paste_text(new_text, mode=mode, restore=restore, hwnd=hwnd)


def get_foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def refocus(hwnd: int) -> None:
    """把焦点还给按下热键时那个窗口。浮窗和托盘都可能把焦点带走。"""
    if not hwnd or int(user32.GetForegroundWindow() or 0) == hwnd:
        return
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    time.sleep(0.05)


def paste_text(text: str, mode: str = "auto", restore: bool = True,
               hwnd: int = 0) -> str:
    """返回 'sent' / 'clipboard' / 'failed'。

    粘贴没成功时**故意不恢复剪贴板** —— 把识别结果留在里面，
    用户至少还能自己按 Ctrl+V，而不是什么都没有。
    """
    if not text:
        return "failed"

    old = _get_clipboard() if restore else None
    if not _set_clipboard(text):
        return "failed"

    refocus(hwnd)

    sent = False
    if mode in ("auto", "clipboard"):
        time.sleep(0.05)
        sent = _send_ctrl_v()
        time.sleep(0.08)
    if not sent and mode in ("auto", "message"):
        sent = _send_wm_paste()

    # 等目标程序把剪贴板读完再恢复，否则会覆盖成旧内容
    if sent and restore and old is not None:
        time.sleep(0.3)
        _set_clipboard(old)

    return "sent" if sent else "clipboard"
