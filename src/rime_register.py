"""Rime 注册进 Windows 的 TSF（Text Services Framework）—— 修复模式。

先说清楚谁负责注册：**小狼毫的安装器自己就写全了**。跑完 `/S` 静默安装后，
注册表里长这样（HKLM，需要管理员）：

    HKLM\\SOFTWARE\\Classes\\CLSID\\{TIP}
        (默认) = "Weasel"
        [InprocServer32]
            (默认) = "C:\\WINDOWS\\system32\\weasel.dll"
            ThreadingModel = "Apartment"

    HKLM\\SOFTWARE\\Microsoft\\CTF\\TIP\\{TIP}
        [Category]
            [Category]\\{分类 GUID}\\{TIP}
            [Item]\\{TIP}\\{分类 GUID}
        [LanguageProfile]
            {5 种语言}\\{PROFILE}
                Description = "小狼毫"
                IconFile   = "C:\\WINDOWS\\system32\\weasel.dll"
                IconIndex  = 0
                Enable     = 1（只有 0x00000804 简体是 1，其余 0）

注意 **没有** `HKLM\\...\\CTF\\Assemblies\\{TIP}`：安装器不写这张「语言 → 输入法」
映射表，输入法能不能出现在切换列表里，靠的是安装器往
`Get-WinUserLanguageList` 里加的那条 `0804:{TIP}{PROFILE}` 记录。

本模块只做两件事：
  1. `is_registered()` —— 判断系统认不认这个输入法
  2. `repair()`       —— 安装器跑了但注册没写成功时，把**同一套结构**补进 HKCU

补写 HKCU 不需要管理员权限，所以「安装 / 更新」按钮全程可以做到不弹 UAC。

踩过的坑：早先这里只写了一个残缺的 TIP 键（没有 Category、没有 InprocServer32），
结果跟 HKLM 里安装器写的完整键打架，TSF 枚举时行为不确定。现在严格照抄
安装器的结构，一处不多一处不少。
"""

from __future__ import annotations

import subprocess
import winreg
from pathlib import Path

# TIP 的 CLSID 既是 TSF 文本服务 ID，也是 COM 类 ID —— 同一个值。
TIP_CLSID = r"{A3F4CDED-B1E9-41EE-9CA6-7B4D0DE6CB0A}"
# 语言 profile 的 CLSID
PROFILE_CLSID = r"{3D02CAB6-2B8E-4781-BA20-1C9267529467}"
NULL_GUID = r"{00000000-0000-0000-0000-000000000000}"

# 安装器写的 16 个分类 GUID，逐个照抄
CATEGORY_GUIDS = [
    r"{046B8C80-1647-40F7-9B21-B93B81AABC1B}",
    r"{13A016DF-560B-46CD-947A-4C3AF1E0E35D}",
    r"{24AF3031-852D-40A2-BC09-8992898CE722}",
    r"{25504FB4-7BAB-4BC1-9C69-CF81890F0EF5}",
    r"{34745C63-B2F0-4784-8B67-5E12C8701A31}",
    r"{364215D9-75BC-11D7-A6EF-00065B84435C}",
    r"{364215DA-75BC-11D7-A6EF-00065B84435C}",
    r"{49D2F9CE-1F5E-11D7-A6D3-00065B84435C}",
    r"{49D2F9CF-1F5E-11D7-A6D3-00065B84435C}",
    r"{534C48C1-0607-4098-A521-4FC899C73E90}",
    r"{565FB8D8-6BD4-4CA1-B223-0F2CCB8F4F96}",
    r"{7C6A82AE-B0D7-4F14-A745-14F28B009D61}",
    r"{85F9794B-4D19-40D8-8864-4E747371A66D}",
    r"{9B7BE3A9-E8AB-4D47-A8FE-254FA423436D}",
    r"{B95F181B-EA4C-4AF1-8056-7C321ABBB091}",
    r"{CCF05DD7-4A87-11D7-A6E2-00065B84435C}",
]

# 安装器建的 5 个语言 profile。只有简体中文（0x00000804）默认启用。
PROFILE_LANGS = {
    "0x00000404": 0,   # 繁体中文
    "0x00000804": 1,   # 简体中文 ← 唯一启用的
    "0x00000c04": 0,   # 香港
    "0x00001004": 0,   # 新加坡
    "0x00001404": 0,   # 澳门
}

CTF = r"SOFTWARE\Microsoft\CTF"
TIP_PATH = CTF + r"\TIP"
ASM_PATH = CTF + r"\Assemblies\0x00000804"
CLASSES_CLSID = r"SOFTWARE\Classes\CLSID"
KEYBOARD_LAYOUT = 0x08040804      # (langid << 16) | layout id
DESCRIPTION = "小狼毫"

# 语言列表里那条记录，格式是 0804:{TIP}{PROFILE}（连写，不能用 :: ）
TIP_STRING = f"0804:{TIP_CLSID}{PROFILE_CLSID}"


def _write(root, path, values=(), default=None):
    k = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
    try:
        if default is not None:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, default)
        for name, val in values:
            kind = winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ
            winreg.SetValueEx(k, name, 0, kind, val)
    finally:
        winreg.CloseKey(k)


def _exists(root, path) -> bool:
    try:
        winreg.OpenKey(root, path).CloseKey()
        return True
    except OSError:
        return False


def _read(root, path, name):
    try:
        k = winreg.OpenKey(root, path)
    except OSError:
        return None
    try:
        return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None
    finally:
        winreg.CloseKey(k)


def _hives():
    return ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
            (winreg.HKEY_CURRENT_USER, "HKCU"))


def is_registered() -> bool:
    """系统认不认这个输入法。

    两个条件都要满足：
      1. COM 类注册了（InprocServer32 指向 weasel.dll）
      2. 简体中文的 profile 是启用状态
    """
    dll_ok = False
    profile_ok = False
    for root, _ in _hives():
        if _read(root, CLASSES_CLSID + "\\" + TIP_CLSID + r"\InprocServer32",
                 "") is not None:
            dll_ok = True
        v = _read(root, TIP_PATH + "\\" + TIP_CLSID
                  + r"\LanguageProfile\0x00000804\\" + PROFILE_CLSID, "Enable")
        if v is not None and int(v) == 1:
            profile_ok = True
    return dll_ok and profile_ok


def repair(weasel_dll: Path) -> tuple[bool, str]:
    """把安装器那套注册结构补进 HKCU。

    写 HKCU 不需要管理员权限。只在 `is_registered()` 为假时才需要调用。
    """
    dll = str(weasel_dll)
    try:
        # 1) COM 类
        _write(winreg.HKEY_CURRENT_USER,
               CLASSES_CLSID + "\\" + TIP_CLSID,
               default="Weasel")
        _write(winreg.HKEY_CURRENT_USER,
               CLASSES_CLSID + "\\" + TIP_CLSID + r"\InprocServer32",
               values=[("ThreadingModel", "Apartment")],
               default=dll)

        # 2) TIP：分类树（Category\Category 和 Category\Item 两个方向）
        base = TIP_PATH + "\\" + TIP_CLSID
        for cat in CATEGORY_GUIDS:
            _write(winreg.HKEY_CURRENT_USER,
                   base + r"\Category\Category\\" + cat + "\\" + TIP_CLSID)
            _write(winreg.HKEY_CURRENT_USER,
                   base + r"\Category\Item\\" + TIP_CLSID + "\\" + cat)

        # 3) 语言 profile
        for lang, enabled in PROFILE_LANGS.items():
            _write(winreg.HKEY_CURRENT_USER,
                   base + r"\LanguageProfile\\" + lang + "\\" + PROFILE_CLSID,
                   values=[("Description", DESCRIPTION),
                           ("IconFile", dll),
                           ("IconIndex", 0),
                           ("Enable", enabled)])
    except OSError as e:
        return False, f"写注册表失败: {e}"

    if not is_registered():
        return False, "注册信息写入后仍检测不到，可能缺权限"
    return True, "注册信息已补齐"


def ensure_in_language_list() -> tuple[bool, str]:
    """确保输入法出现在 Win+空格 的切换列表里。

    格式是 `0804:{TIP}{PROFILE}` —— 连写。之前误写成 `{TIP}::{PROFILE}`，
    配合 `Set-WinUserLanguageList -Force` 会把用户原有的输入法列表清空。
    """
    ps = (
        "$tip = '" + TIP_STRING + "';"
        "$list = Get-WinUserLanguageList;"
        "$hit = $false;"
        "foreach ($l in $list) { if ($l.InputMethodTips -contains $tip) { $hit = $true } };"
        "if ($hit) { 'ALREADY' } else {"
        "  $zh = $list | Where-Object { $_.LanguageTag -like 'zh*' } | Select-Object -First 1;"
        "  if ($null -eq $zh) { $zh = New-WinUserLanguageList -Language 'zh-CN'; $list = @($zh) };"
        "  $zh.InputMethodTips.Add($tip);"
        "  Set-WinUserLanguageList $list -Force;"
        "  'ADDED' }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=60,
        )
    except Exception as e:
        return False, f"调用系统接口失败: {e}"
    text = (out.stdout or b"").decode("utf-8", "ignore")
    if "ADDED" in text:
        return True, "已加入输入法切换列表"
    if "ALREADY" in text:
        return True, "已在输入法切换列表里"
    return False, "没能加入输入法切换列表"


def ensure(weasel_dll: Path | None = None) -> tuple[bool, str]:
    """保证输入法注册到位。已经注册好就直接返回。"""
    if is_registered():
        return True, "已注册"
    if weasel_dll is None:
        return False, "输入法还没注册，也找不到 weasel.dll"
    ok, msg = repair(weasel_dll)
    if not ok:
        return False, msg
    ensure_in_language_list()
    return True, "注册信息已补齐"


def unregister(verbose: bool = True) -> tuple[bool, str]:
    """从 HKCU 摘掉我们补写的部分（不动安装器写在 HKLM 的）。"""
    def del_tree(path):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                               winreg.KEY_READ | winreg.KEY_WRITE)
        except OSError:
            return
        subs = []
        i = 0
        while True:
            try:
                subs.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
        winreg.CloseKey(k)
        for s in subs:
            del_tree(path + "\\" + s)
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, path,
                               winreg.KEY_WRITE, 0)
            if verbose:
                print("已删除", path)
        except OSError:
            pass

    del_tree(TIP_PATH + "\\" + TIP_CLSID)
    del_tree(CLASSES_CLSID + "\\" + TIP_CLSID)
    return True, "已注销"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "off":
        unregister()
    else:
        print("is_registered =", is_registered())
        print("TIP_STRING =", TIP_STRING)
