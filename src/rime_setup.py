"""Rime 拼音输入法的安装与部署。

为什么不能只把文件摆进去就用：Windows 的输入法是 TSF 组件，引擎必须注册到
系统（装在 `C:\\Program Files\\Rime`），注册表要有 GUID、系统要能加载 DLL。
所以引擎这一步绕不开安装程序。

但**词库和配置是纯数据**，可以完全由本程序管理，不必让用户自己折腾 Rime：

  1. 引擎没装 → 跑随包分发的安装包装一次（要 UAC，一次性）
  2. 引擎装好 → 把随包分发的雾凇拼音铺到 `%APPDATA%\\Rime`，改三个配置
  3. 触发「重新部署」，Rime 编译词库（1–2 分钟），之后就能用

这样用户的体感是「装了一个 Verbalkey，输入法也就有了」。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import winreg
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

# Rime 引擎的默认安装位置。TSF 要求 DLL 在固定路径下，换地方注册不上。
RIME_ROOT_DIRS = [
    Path(r"C:\Program Files\Rime"),
    Path(r"C:\Program Files (x86)\Rime"),
]

# 新版小狼毫（0.17.x）装成 Rime\weasel-<版本>\ 的子目录结构，
# 旧版是直接把文件摊在 Rime\ 根下。两种都认。
_DEPLOYER_NAMES = ("WeaselDeployer.exe", "rime_deployer.exe")

# 随包分发的资源：assets/rime/ 下
#   installer/weasel-*.exe   引擎安装包
#   ice/…                    雾凇拼音全套（解压后的形态）
BUNDLE_RIME = "rime"


def app_dir() -> Path:
    from .config import APP_DIR
    return APP_DIR


def user_rime_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Rime"


def rime_install_dir() -> Path | None:
    """找到已安装的 Rime 引擎目录。没装返回 None。

    新版小狼毫的实际结构是 `C:\\Program Files\\Rime\\weasel-0.17.4\\`，
    引擎文件（rime.dll、WeaselServer.exe）在这一层，不在 Rime 根下。
    共享数据（data/）也在这一层，部署时要把它当 shared_dir 传。
    """
    for root in RIME_ROOT_DIRS:
        if not root.is_dir():
            continue
        subs = sorted(p for p in root.glob("weasel-*") if p.is_dir())
        if subs:
            return subs[-1]
        # 旧版：文件直接摊在根下
        if (root / "rime.dll").exists() or (root / "Weasel.exe").exists():
            return root
    return None


def rime_registered() -> bool:
    """查一下 TSF 文本服务，确认系统认这个输入法。"""
    try:
        from . import rime_register
        return rime_register.is_registered()
    except Exception:
        return False


def _bundle_root() -> Path:
    """随包资源的实际位置。

    PyInstaller 的 onedir 模式把 datas 放进 `_internal/`，所以打包后资源在
    `程序目录/_internal/assets/rime/`；源码跑的时候在 `项目目录/assets/rime/`。
    两个位置都找一下。
    """
    app = app_dir()
    for p in (app / "_internal" / "assets" / BUNDLE_RIME,
              app / "assets" / BUNDLE_RIME):
        if p.is_dir():
            return p
    return app / "assets" / BUNDLE_RIME


def bundled_installer() -> Path | None:
    d = _bundle_root() / "installer"
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.exe")):
        return p
    return None


def bundled_ice() -> Path | None:
    """随包分发的雾凇拼音（解压后的形态，含 rime_ice.schema.yaml）。"""
    d = _bundle_root() / "ice"
    if d.is_dir() and (d / "rime_ice.schema.yaml").exists():
        return d
    return None


def install_engine() -> tuple[bool, str]:
    """装 Rime 引擎。返回 (是否成功, 提示信息)。

    NSIS 静默安装参数 `/S`。需要管理员权限，所以用 ShellExecute 的
    `runas` 动词拉起 —— 会出现一次 UAC 弹窗，用户点「是」即可。
    """
    exe = bundled_installer()
    if exe is None:
        return False, "安装包不在程序目录里"
    if rime_install_dir() is not None:
        return True, "已经装过了"
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(exe), "/S", None, 0)
        # ShellExecuteW 返回 > 32 表示成功拉起（不代表安装完成）
        if rc <= 32:
            return False, "没能提权启动安装程序（可能取消了 UAC）"
        return True, "安装程序已启动，等它跑完"
    except Exception as e:
        return False, f"启动安装程序失败: {e}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parse_version(s: str) -> tuple[int, ...]:
    nums = [int(p) for p in re.findall(r"\d+", s or "")]
    while len(nums) < 2:
        nums.append(0)
    return tuple(nums[:4])


def _bump_version(s: str) -> str:
    """在原版本号基础上加一，格式保持 Rime 认得的点分形式。

    要保留前导零：`2020.01.01` 应该是 `2020.01.02`，不能变成 `2020.1.2`。
    """
    parts = re.split(r"[._-]", s or "")
    nums: list[int] = []
    for p in parts:
        m = re.search(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 2:
        nums.append(0)
    nums[-1] += 1
    out: list[str] = []
    for i, n in enumerate(nums):
        seg = parts[i] if i < len(parts) else ""
        m2 = re.search(r"\d+", seg)
        width = len(m2.group()) if m2 else 0
        out.append(str(n).zfill(width) if width else str(n))
    return ".".join(out)


def _fix_config_version(user_dir: Path, shared_dir: Path) -> str:
    """把用户 default.yaml 的 config_version 提到比共享目录大。

    这个坑很隐蔽：雾凇拼音自带的 default.yaml 写的是
    `config_version: '2026-02-06'`，但 Rime 的版本解析只认点分形式
    （1.2.3 / 2020.01.01），横杠格式它解析失败、按 0 算。
    共享目录的 default.yaml 是 `0.40`，0 < 0.40 → **用户配置被整体忽略**，
    编译出来的全是共享目录里的默认方案（luna_pinyin 那套），
    雾凇拼音根本不会出现，而且没有任何报错。

    所以这里统一把用户版本改成比共享大的点分数字。
    """
    shared_yaml = shared_dir / "data" / "default.yaml"
    if not shared_yaml.exists():
        shared_yaml = shared_dir / "default.yaml"
    user_yaml = user_dir / "default.yaml"
    if not (shared_yaml.exists() and user_yaml.exists()):
        return ""

    def read_ver(p: Path) -> str:
        m = re.search(r"^config_version:\s*['\"]?([^'\"\n]+)",
                      p.read_text(encoding="utf-8", errors="ignore"),
                      re.M)
        return m.group(1).strip() if m else "0"

    shared_ver = read_ver(shared_yaml)
    user_ver = read_ver(user_yaml)
    # 横杠格式 Rime 解析不出来，等同于 0，必须改
    need = ("-" in user_ver) or (_parse_version(user_ver) <= _parse_version(shared_ver))
    if not need:
        return ""
    new_ver = _bump_version(shared_ver)
    text = user_yaml.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"^config_version:.*$", f"config_version: '{new_ver}'",
                  text, count=1, flags=re.M)
    user_yaml.write_text(text, encoding="utf-8")
    return f"config_version {user_ver} → {new_ver}"


def deploy_ice() -> tuple[bool, str]:
    """把雾凇拼音铺到用户目录并改配置。"""
    src = bundled_ice()
    if src is None:
        return False, "词库不在程序目录里"
    dst = user_rime_dir()
    dst.mkdir(parents=True, exist_ok=True)

    # 整体覆盖：词库文件多，shutil.copytree 遇到已存在会报错，
    # 逐个文件覆盖（dirs_exist_ok=True）更省事
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except Exception as e:
        return False, f"铺词库失败: {e}"

    # 用户的个性化配置（方案 / 候选窗 / 符号）全走 custom.yaml
    write_custom()

    inst = rime_install_dir()
    if inst is not None:
        _fix_config_version(dst, inst)

    return True, "词库已铺好"


# 输入方案：显示名 → schema 文件名（雾凇拼音自带）
SCHEMAS = {
    "全拼": "rime_ice",
    "小鹤双拼": "double_pinyin_flypy",
    "自然码双拼": "double_pinyin",
    "微软双拼": "double_pinyin_mspy",
    "搜狗双拼": "double_pinyin_sogou",
    "智能ABC双拼": "double_pinyin_abc",
    "拼音加加双拼": "double_pinyin_jiajia",
    "紫光双拼": "double_pinyin_ziguang",
}


def read_ime_settings() -> dict:
    """拼音输入法的用户设置（存 config.json 的 rime 段）。"""
    from .config import CONFIG
    d = CONFIG.get("rime") or {}
    return {
        "schema": d.get("schema") or "全拼",
        "page_size": int(d.get("page_size") or 9),
        "horizontal": bool(d.get("horizontal", True)),
        "font_point": int(d.get("font_point") or 14),
        "emoji": bool(d.get("emoji", True)),
        "traditional": bool(d.get("traditional") or False),
    }


def _split_patch(text: str) -> tuple[list[str], list[str], list[str]]:
    """把 custom.yaml 拆成 patch 段之前 / 之中 / 之后三段。

    只认我们实际用到的结构：顶层 `patch:`，其下两空格起头的 key。
    解析器写这么窄是故意的 —— 为了改这种几行的文件去引一个 YAML 库，
    等于给打包再加一个依赖和一堆边界情况。
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "patch:":
            start = i
            break
    if start is None:
        return [], [], lines
    head = lines[: start + 1]
    body: list[str] = []
    tail: list[str] = []
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "" or ln.startswith("  ") or ln.lstrip().startswith("#"):
            body.append(ln)
            i += 1
            continue
        tail = lines[i:]
        break
    return head, body, tail


def _patch_items(body: list[str]) -> list[tuple[str | None, list[str]]]:
    """把 patch 段切成 (key, 该 key 的所有行)。key=None 表示空行/注释。

    值可以是多行块（比如 schema_list 下面的 `- schema: xxx`），
    这些续行要跟着上一个 key 走，不能当成新 key。
    """
    items: list[tuple[str | None, list[str]]] = []
    for ln in body:
        if ln.strip() == "":
            items.append((None, [ln]))
            continue
        m = re.match(r'^  ("?)([^":]+)\1:\s*(.*)$', ln)
        if m and not ln.startswith("    "):
            items.append((m.group(2), [ln]))
        elif items and items[-1][0] is not None:
            items[-1][1].append(ln)
        else:
            items.append((None, [ln]))
    return items


def _merge_patch(path: Path, updates: dict[str, str | list[str]]) -> None:
    """把 updates 合并进现有 custom.yaml，别的 key 原样保留。

    以前这里是整份重写：只要手工往里加过东西（比如 schema/icon），
    下一次设置页一改就全被抹掉 —— 2026-10-29 任务栏图标就是这样
    退回默认的。现在只动 updates 里列出的 key，其余行一个字节不碰。
    """
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    head, body, tail = _split_patch(text)
    if not head:
        if text.strip():
            # 有内容却找不到 `patch:`：格式不认识。先原样备份，再整份重写。
            # 绝不能把原文当 tail 拼到新内容后面 —— 那会生成两个 patch 段，
            # Rime 直接读不了（自测时真撞到过）。
            try:
                path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
            except OSError:
                pass
        head, body = ["patch:"], []
    left = dict(updates)
    out: list[str] = []
    for key, lines in _patch_items(body):
        if key is not None and key in left:
            val = left.pop(key)
            if isinstance(val, list):
                out.append(f'  "{key}":')
                out.extend(val)
            else:
                out.append(f'  "{key}": {val}')
        else:
            out.extend(lines)
    for key, val in left.items():
        if isinstance(val, list):
            out.append(f'  "{key}":')
            out.extend(val)
        else:
            out.append(f'  "{key}": {val}')
    while out and out[-1].strip() == "":
        out.pop()
    lines_out = head + out
    if tail:
        lines_out += [""] + tail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines_out).rstrip("\n") + "\n", encoding="utf-8")


def _verify_custom(schema_file: str, s: dict, icon_wanted: bool) -> list[str]:
    """写完立刻回读，确认关键字段真的在。返回问题列表，空列表=通过。

    为什么要这一步：以前只看「写函数没报错」就当成功了，结果配置文件
    被别的地方覆盖、或模板漏了一段，界面没有任何提示，只能等用户发现。
    """
    dst = user_rime_dir()

    def has(p: Path, needle: str) -> bool:
        try:
            # 写出去的 key 都带双引号（"style/color_scheme"），查的时候先剥掉，
            # 否则校验会把自己刚写的配置误报成"没写进去"
            return needle in p.read_text(encoding="utf-8", errors="ignore").replace('"', "")
        except OSError:
            return False

    problems: list[str] = []
    if not has(dst / "default.custom.yaml", f"- schema: {schema_file}"):
        problems.append("方案列表没写进去")
    weasel = dst / "weasel.custom.yaml"
    if not has(weasel, "style/color_scheme: verbalkey"):
        problems.append("候选窗配色没写进去")
    if not has(weasel, f"font_point: {s['font_point']}"):
        problems.append("候选窗字号没写进去")
    if icon_wanted and not has(dst / "rime_ice.custom.yaml", "schema/icon"):
        problems.append("图标配置没写进去")
    # build/weasel.yaml 只有部署过才存在，不存在不算错
    built = dst / "build" / "weasel.yaml"
    if built.exists() and not has(built, "color_scheme: verbalkey"):
        problems.append("build/weasel.yaml 里没有配色（可能需要重新部署）")
    return problems


def write_custom() -> list[str]:
    """按用户设置更新三个 custom.yaml。**这是 Rime 用户目录的唯一写入口。**

    以前的写法是整份重写，谁写谁覆盖；现在改成读-改-合并：
    只改这个函数认识的 key，文件里其它内容（包括手工加的）全部保留。
    返回校验问题列表（空=正常），调用方可以显示给用户。
    """
    s = read_ime_settings()
    dst = user_rime_dir()
    schema_file = SCHEMAS.get(s["schema"], "rime_ice")
    # 标签、注释字号跟着正文字号递减，别低于可读下限
    label_px = max(10, s["font_point"] - 1)
    comment_px = max(9, s["font_point"] - 2)

    # 1) default.custom.yaml：方案列表 + 每页候选数
    _merge_patch(dst / "default.custom.yaml", {
        "schema_list": [f"    - schema: {schema_file}"],
        "menu/page_size": str(s["page_size"]),
    })

    # 2) weasel.custom.yaml：候选窗样式，对齐语音浮窗的视觉
    #    颜色是 0xBBGGRR（Weasel 的格式，注意是 BGR 不是 RGB）：
    #      PINK #FF2D55 → 0x552DFF      INK  #1D1D1F → 0x1F1D1D
    #      MUTED #86868B → 0x8B8686     LINE #EDEDF0 → 0xF0EDED
    _merge_patch(dst / "weasel.custom.yaml", {
        "style/color_scheme": "verbalkey",
        "style/font_face": '"Microsoft YaHei UI, Segoe UI Emoji"',
        "style/label_font_face": '"Microsoft YaHei UI"',
        "style/comment_font_face": '"Microsoft YaHei UI"',
        "style/font_point": str(s["font_point"]),
        "style/label_font_point": str(label_px),
        "style/comment_font_point": str(comment_px),
        "style/horizontal": "true" if s["horizontal"] else "false",
        # 圆角、留白、阴影 —— 数值照语音浮窗的观感调
        "style/layout/corner_radius": "12",
        "style/layout/round_corner": "8",
        "style/layout/border_width": "1",
        "style/layout/margin_x": "12",
        "style/layout/margin_y": "8",
        "style/layout/candidate_spacing": "18",
        "style/layout/hilite_padding": "6",
        "style/layout/hilite_spacing": "6",
        "style/layout/shadow_radius": "8",
        "style/layout/shadow_offset_x": "0",
        "style/layout/shadow_offset_y": "3",
        # 配色：白底 + 深灰字 + 粉色高亮块
        "preset_color_schemes/verbalkey/name": "Verbalkey",
        "preset_color_schemes/verbalkey/back_color": "0xFFFFFF",
        "preset_color_schemes/verbalkey/border_color": "0xF0EDED",
        "preset_color_schemes/verbalkey/text_color": "0x1F1D1D",
        "preset_color_schemes/verbalkey/label_color": "0x8B8686",
        "preset_color_schemes/verbalkey/comment_text_color": "0x8B8686",
        "preset_color_schemes/verbalkey/candidate_text_color": "0x1F1D1D",
        "preset_color_schemes/verbalkey/hilited_text_color": "0x1F1D1D",
        "preset_color_schemes/verbalkey/hilited_back_color": "0xFFFFFF",
        "preset_color_schemes/verbalkey/hilited_candidate_back_color": "0x552DFF",
        "preset_color_schemes/verbalkey/hilited_candidate_text_color": "0xFFFFFF",
        "preset_color_schemes/verbalkey/hilited_candidate_label_color": "0xFFFFFF",
        "preset_color_schemes/verbalkey/hilited_comment_text_color": "0xFFFFFF",
        "preset_color_schemes/verbalkey/shadow_color": "0x28000000",
    })

    # 3) rime_ice.custom.yaml：emoji / 繁简 / 句号样式
    #    switches 的下标照 rime_ice.schema.yaml：2=traditionalization 3=emoji
    #    任务栏那个「中」「英」是 weasel.dll 编进 TSF 的，不读注册表 IconFile，
    #    只能靠 schema/icon 换。图标文件在就写；不在就保留原值（不删），
    #    免得哪次手滑把文件删了、这里又把配置一起清掉。
    updates: dict[str, str | list[str]] = {
        "switches/@2/reset": "1" if s["traditional"] else "0",
        "switches/@3/reset": "1" if s["emoji"] else "0",
        "punctuator/full_shape/。": '"."',
        "punctuator/half_shape/。": '"."',
    }
    icon_wanted = (dst / "vk_zh.ico").exists() and (dst / "vk_en.ico").exists()
    if icon_wanted:
        updates["schema/icon"] = "vk_zh.ico"
        updates["schema/ascii_icon"] = "vk_en.ico"
    _merge_patch(dst / "rime_ice.custom.yaml", updates)

    # custom.yaml 只是"声明"，小狼毫实际读的是 build/weasel.yaml，
    # 那个文件只有它自己的 GUI 部署器才会重建 —— 所以直接改一份进去
    apply_weasel_style()
    return _verify_custom(schema_file, s, icon_wanted)

    # custom.yaml 只是"声明"，小狼毫实际读的是 build/weasel.yaml，
    # 那个文件只有它自己的 GUI 部署器才会重建 —— 所以直接写一份进去
    apply_weasel_style()


# 候选窗配色（Weasel 用 0xBBGGRR，是 BGR 不是 RGB）
#   PINK #FF2D55 → 0x552DFF    INK #1D1D1F → 0x1F1D1D
#   MUTED #86868B → 0x8B8686   LINE #EDEDF0 → 0xF0EDED
_WEASEL_COLORS = {
    "name": "Verbalkey",
    "author": "Verbalkey",
    "back_color": "0xFFFFFF",
    "border_color": "0xF0EDED",
    "text_color": "0x1F1D1D",
    "label_color": "0x8B8686",
    "comment_text_color": "0x8B8686",
    "candidate_text_color": "0x1F1D1D",
    "hilited_text_color": "0x1F1D1D",
    "hilited_back_color": "0xFFFFFF",
    "hilited_candidate_back_color": "0x552DFF",
    "hilited_candidate_text_color": "0xFFFFFF",
    "hilited_candidate_label_color": "0xFFFFFF",
    "hilited_comment_text_color": "0xFFFFFF",
    "shadow_color": "0x28000000",
}


def apply_weasel_style() -> bool:
    """把候选窗样式直接写进 `build/weasel.yaml`。

    为什么不只靠 `weasel.custom.yaml`：

      `weasel.yaml` 是**小狼毫前端**的配置，不是 Rime 核心配置。
      实测：跑完 `rime_deployer --build`，`build/weasel.yaml` 的时间戳和
      内容一个字节都没动 —— librime 的部署器根本不碰这个文件。
      小狼毫自己的 `WeaselDeployer.exe` 会重建它，但那是 GUI 程序，
      带 `/deploy` 也照样弹窗等人点，没法自动化。

      所以这里直接把样式写进小狼毫运行时真正读的那份（build 目录），
      重启 WeaselServer 就生效。`weasel.custom.yaml` 也照写一份，
      将来用户要是自己点「重新部署」，结果一样。
    """
    p = user_rime_dir() / "build" / "weasel.yaml"
    if not p.exists():
        return False
    s = read_ime_settings()
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    def top(key: str, val) -> None:
        nonlocal text
        text = re.sub(rf"^  {key}:\s*.*$", f"  {key}: {val}",
                      text, count=1, flags=re.M)

    def layout(key: str, val) -> None:
        nonlocal text
        text = re.sub(rf"^    {key}:\s*.*$", f"    {key}: {val}",
                      text, count=1, flags=re.M)

    top("color_scheme", "verbalkey")
    top("font_face", '"Microsoft YaHei UI, Segoe UI Emoji"')
    top("label_font_face", '"Microsoft YaHei UI"')
    top("comment_font_face", '"Microsoft YaHei UI"')
    top("font_point", s["font_point"])
    top("label_font_point", max(10, s["font_point"] - 1))
    top("comment_font_point", max(9, s["font_point"] - 2))
    top("horizontal", "true" if s["horizontal"] else "false")
    # 圆角 / 留白 / 阴影，照语音浮窗的观感
    layout("corner_radius", 12)
    layout("round_corner", 8)
    layout("border_width", 1)
    layout("margin_x", 12)
    layout("margin_y", 8)
    layout("candidate_spacing", 18)
    layout("hilite_padding", 6)
    layout("hilite_spacing", 6)
    layout("shadow_radius", 8)
    layout("shadow_offset_x", 0)
    layout("shadow_offset_y", 3)

    # 配色方案：已经有 verbalkey 段就先剔掉，避免重复插入
    text = re.sub(r"^  verbalkey:\n(?:    [^\n]*\n)+", "", text, count=1, flags=re.M)
    block = "  verbalkey:\n" + "".join(
        f"    {k}: {v}\n" for k, v in _WEASEL_COLORS.items())
    text = re.sub(r"^preset_color_schemes:\n",
                  "preset_color_schemes:\n" + block,
                  text, count=1, flags=re.M)

    try:
        p.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def bundled_icon() -> Path | None:
    """随包分发的 app.ico，给系统托盘用。"""
    app = app_dir()
    for p in (app / "_internal" / "assets" / "app.ico",
              app / "assets" / "app.ico"):
        if p.exists():
            return p
    return None


# 小狼毫的卸载登记项。Windows 的「安装的应用」列表就是读这里。
# 它自己写的是「小狼毫输入法 / 式恕堂」，跟语音输入法摆在一起像两个不相干的软件，
# 所以统一改成 Verbalkey 的名字和图标 —— 用户体感就是一套东西。
# 卸载入口保留（仍指向小狼毫自己的卸载器），不能删，否则用户想卸都没地方点。
UNINSTALL_NAMES = ("Weasel",)
UNINSTALL_BASES = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def rebrand_uninstall(icon_file: str) -> bool:
    """把「小狼毫输入法」在应用列表里改名为 Verbalkey。要管理员权限（写 HKLM）。"""
    from .config import APP_NAME
    hit = False
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in UNINSTALL_BASES:
            for name in UNINSTALL_NAMES:
                path = base + "\\" + name
                try:
                    k = winreg.OpenKey(root, path, 0, winreg.KEY_WRITE)
                except OSError:
                    continue
                try:
                    winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ,
                                      f"{APP_NAME} 拼音输入法")
                    winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, icon_file)
                    winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, APP_NAME)
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(k)
                hit = True
    return hit


def rebrand() -> tuple[bool, str]:
    """把输入法在系统里的名字和图标换成 Verbalkey。

    任务栏右下角的输入法指示器、Win+空格 的切换列表，显示的名字和图标
    都来自注册表 LanguageProfile 里的 Description / IconFile。
    小狼毫默认写的是「小狼毫 + System32\\weasel.dll 的图标」。

    HKLM 要管理员（安装器写的那份），HKCU 不用——两处都写，
    哪边成了算哪边。改完要重启 WeaselServer 才刷新。
    """
    from . import rime_register as rr
    from .config import APP_NAME
    # Windows 从 TSF 取图标时只认 PE 文件（exe/dll），给单独的 .ico 会加载失败、
    # 退回显示「中」「拼」这种文字标记。所以打包后直接指向自己的 exe。
    exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
    if exe is not None and exe.exists():
        icon_file = str(exe)
    else:
        ico = bundled_icon()
        if ico is None:
            return False, "找不到程序图标"
        icon_file = str(ico)
    done = 0
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for lang in rr.PROFILE_LANGS:
            path = (rr.TIP_PATH + "\\" + rr.TIP_CLSID
                    + r"\LanguageProfile\\" + lang + "\\" + rr.PROFILE_CLSID)
            try:
                k = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
                try:
                    winreg.SetValueEx(k, "Description", 0, winreg.REG_SZ, APP_NAME)
                    winreg.SetValueEx(k, "IconFile", 0, winreg.REG_SZ, icon_file)
                    winreg.SetValueEx(k, "IconIndex", 0, winreg.REG_DWORD, 0)
                finally:
                    winreg.CloseKey(k)
                done += 1
            except OSError:
                pass
    if done == 0:
        return False, "改不了注册表里的输入法信息"
    # 应用列表里的名字/图标一起换，别让它跟语音输入法看着像两个软件
    rebrand_uninstall(icon_file)
    # 图标和名字被 WeaselServer 缓存，重启才刷新
    restart_service()
    return True, "输入法已更名为 Verbalkey"


def restart_service() -> bool:
    """重启 WeaselServer（配置或品牌改动后刷新）。"""
    import subprocess as sp
    inst = rime_install_dir()
    if inst is None:
        return False
    try:
        sp.run(["taskkill", "/IM", "WeaselServer.exe", "/F"],
               capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    return start_service()


def bundled_deployer() -> Path | None:
    """随包分发的 librime 官方部署器（rime_deployer.exe + rime.dll）。"""
    d = _bundle_root() / "deployer"
    if (d / "rime_deployer.exe").exists():
        return d
    return None


def trigger_deploy() -> tuple[bool, str]:
    """让 Rime 重新编译词库。词库大，要 1–2 分钟。

    用随包分发的 librime 官方部署器，两个坑都踩过：
      1. 参数形式是 `--build <用户目录> <共享目录>`，多传一个就报
         `invalid arguments`，少传则静默无效。
      2. `rime.dll` 必须和 `rime_deployer.exe` 在同一目录，否则它秒退、
         什么都不输出（连错误都没有）。
    小狼毫自带的 `WeaselDeployer.exe` 是 GUI 版，会挂在那儿等人点，不适合自动化。
    """
    inst = rime_install_dir()
    if inst is None:
        return False, "Rime 还没装"
    user = user_rime_dir()
    user.mkdir(parents=True, exist_ok=True)
    log = user / "rime.log"

    dep = bundled_deployer()
    if dep is not None:
        exe = dep / "rime_deployer.exe"
        cwd = dep
    else:
        exe = None
        for name in _DEPLOYER_NAMES:
            p = inst / name
            if p.exists():
                exe = p
                break
        if exe is None:
            return False, "找不到部署程序"
        cwd = inst

    cmd = [str(exe), "--build", str(user), str(inst)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                cwd=str(cwd), creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        return False, f"启动部署失败: {e}"
    # 词库大，编译要一两分钟。给个上限，超时不当失败（可能已完成）
    try:
        out, _ = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        return True, "正在编译词库，1–2 分钟"
    text = (out or b"").decode("utf-8", "ignore").strip()
    if "invalid arguments" in text:
        return False, "部署器参数不对"
    if proc.returncode not in (0, None):
        return False, f"部署失败（退出码 {proc.returncode}）{text[:80]}"
    return True, "词库编译完成"


def _read_version(p: Path, key: str = "version") -> str:
    """从 yaml 里抠一个版本号出来（没有就返回空）。"""
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(rf"^{key}:\s*['\"]?([^'\"\n]+)", text, re.M)
    return m.group(1).strip() if m else ""


def bundled_ice_version() -> str:
    """随包分发的词库版本（雾凇 rime_ice.dict.yaml 的 version）。"""
    src = bundled_ice()
    return _read_version(src / "rime_ice.dict.yaml") if src else ""


def installed_ice_version() -> str:
    """已经铺到用户目录的词库版本。"""
    return _read_version(user_rime_dir() / "rime_ice.dict.yaml")


def status() -> dict:
    """当前状态，给设置页显示。"""
    inst = rime_install_dir()
    user = user_rime_dir()
    has_ice = (user / "rime_ice.schema.yaml").exists()
    return {
        "installed": inst is not None,
        "install_dir": str(inst) if inst else "",
        "user_dir": str(user),
        "has_ice": has_ice,
        "ice_version": installed_ice_version(),
        "bundle_ice_version": bundled_ice_version(),
        "bundle_installer": bundled_installer() is not None,
        "bundle_ice": bundled_ice() is not None,
    }


def service_running() -> bool:
    """WeaselServer 是不是**真的**在跑。

    不能只看进程名。进程退出后 tasklist 里会残留一条记录（32 KB、0 线程），
    名字还在，于是 `service_running()` 返回 True、`start_service()` 直接
    return True 而不去启动 —— 真正的服务永远起不来，表现就是拼音突然打
    不了字，而日志里还写着「已拉起 Rime 服务」。

    所以按工作集内存判断：真服务占几十 MB，残留记录只有几十 KB。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WeaselServer.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        text = (out.stdout or b"").decode("gbk", "ignore")
        # 内存值带千分位逗号（"28,352 K"），按逗号 split 会被切成两段。
        # 内存是这行唯一以 K 结尾的字段，直接从行尾取最稳。
        for line in text.splitlines():
            m = re.search(r'"([\d,]+)\s*K"\s*$', line.strip())
            if not m:
                continue
            try:
                if int(m.group(1).replace(",", "")) > 1024:   # 1 MB 以上才算真在跑
                    return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


def start_service() -> bool:
    """拉起 WeaselServer（输入法后台服务）。已经在跑就不重复起。"""
    inst = rime_install_dir()
    if inst is None:
        return False
    exe = inst / "WeaselServer.exe"
    if not exe.exists():
        return False
    if service_running():
        return True
    try:
        subprocess.Popen([str(exe)], cwd=str(inst),
                         creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def setup_all() -> tuple[bool, str]:
    """一键搞定：注册 → 铺词库 → 编译 → 启动服务。"""
    if rime_install_dir() is None:
        ok, msg = install_engine()
        if not ok:
            return False, msg
        return True, "安装程序已启动，装完后重新点一次即可完成配置"

    # 注册进系统。安装器其实已经写好了，绝大部分情况下这里什么都不用做；
    # 只有安装器跑了但注册没写成功时，才把同一套结构补进 HKCU（不需要管理员）。
    try:
        from . import rime_register
        dll = None
        inst_dir = rime_install_dir()
        for cand in ([inst_dir / "weasel.dll"] if inst_dir else []) + \
                    [Path(r"C:\Windows\System32\weasel.dll")]:
            if cand.exists():
                dll = cand
                break
        ok, msg = rime_register.ensure(dll)
        if not ok:
            return False, msg
    except Exception as e:
        return False, f"注册输入法失败: {e}"

    ok, msg = deploy_ice()
    if not ok:
        return False, msg
    ok, msg = trigger_deploy()
    if not ok:
        return False, msg
    # 名字和图标换成 Verbalkey（内部会重启 WeaselServer 刷新缓存）
    rebrand()
    return True, "已装好。按 Win+空格 切到 Verbalkey 就能打拼音"


if __name__ == "__main__":
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))
