"""用 Win32 自动操作小狼毫的「安装选项」窗口，完成输入法注册。

WeaselSetup.exe 是个 GUI 工具，带 /i /s 参数也照样弹窗等人点。
这里直接找到窗口里的控件发 BM_CLICK，不依赖鼠标坐标。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

u = ctypes.windll.user32
# 64 位下不声明 argtypes 会把指针截断，SendMessageW 直接崩
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM]
u.SendMessageW.restype = wintypes.LPARAM
u.GetDlgCtrlID.argtypes = [wintypes.HWND]
u.GetDlgCtrlID.restype = ctypes.c_int
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.EnumChildWindows.argtypes = [wintypes.HWND,
                               ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                  wintypes.LPARAM),
                               wintypes.LPARAM]
u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                             wintypes.LPARAM),
                          wintypes.LPARAM]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                        ctypes.POINTER(wintypes.DWORD)]

BM_GETCHECK, BM_CLICK = 0x00F0, 0x00F5
WEASEL = r"C:\Program Files\Rime\weasel-0.17.4\WeaselSetup.exe"


def windows_of(pid: int) -> list:
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        q = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(q))
        if q.value == pid:
            out.append(h)
        return True

    u.EnumWindows(cb, 0)
    return out


def children(top):
    res = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        res[u.GetDlgCtrlID(h)] = h
        return True

    u.EnumChildWindows(top, cb, 0)
    return res


def main() -> int:
    try:
        proc = subprocess.Popen([WEASEL])
    except Exception as e:
        print("启动失败:", e)
        return 1
    time.sleep(4)

    clicked = False
    for top in windows_of(proc.pid):
        ctrls = children(top)
        if 1000 not in ctrls:
            continue
        h = ctrls[1000]
        state = u.SendMessageW(h, BM_GETCHECK, 0, 0)
        print(f"「中文（简体，中国大陆）」勾选状态 = {state}")
        if state != 1:
            u.SendMessageW(h, BM_CLICK, 0, 0)
            clicked = True
            time.sleep(4)
            state2 = u.SendMessageW(h, BM_GETCHECK, 0, 0)
            print(f"点击后状态 = {state2}")
        else:
            print("已经注册过了")
        # 关掉窗口
        u.PostMessageW(top, 0x0010, 0, 0)   # WM_CLOSE
        break

    if not clicked:
        print("没有找到可点的控件")
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
