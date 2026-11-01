"""词库联网更新：从 GitHub 拉最新的雾凇拼音词库。

关于代理：
  `urllib` 默认读 Windows 的系统代理设置（注册表的 ProxyEnable/ProxyServer），
  所以**挂全局代理就会自动跟着走**，不需要在程序里单独配。
  只有在想指定某个代理时才把地址填进设置，形如 `http://127.0.0.1:7890`。

关于下载源：
  实测这台机器走 GitHub 直连下大文件会中途断流（weasel 安装包只拿到 9.97/12.43 MB，
  NSIS 直接报完整性错误）。所以默认先走 `gh-proxy` 镜像，失败再回退直连。
  两个都失败才报错，报错信息里带上真实异常，别让人对着"更新失败"猜。
"""

from __future__ import annotations

import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import rime_setup

# 下载源：先镜像、后直连
SOURCES = [
    ("镜像加速",
     "https://gh-proxy.com/https://github.com/iDvel/rime-ice/archive/refs/heads/main.zip"),
    ("GitHub 直连",
     "https://codeload.github.com/iDvel/rime-ice/zip/refs/heads/main"),
]

# 只拉这个文件的头部几 KB 来读版本号，不用下整个词库
VERSION_URLS = [
    "https://gh-proxy.com/https://raw.githubusercontent.com/iDvel/rime-ice/main/rime_ice.dict.yaml",
    "https://raw.githubusercontent.com/iDvel/rime-ice/main/rime_ice.dict.yaml",
]

# 这些目录是仓库自带的，铺到用户目录没意义
SKIP_DIRS = {".github", "others", "build", ".git"}


def _opener(proxy: str = ""):
    """带代理的 opener。proxy 为空就用系统代理（urllib 的默认行为）。"""
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _get(url: str, proxy: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _opener(proxy).open(req, timeout=timeout) as r:
        return r.read()


def latest_version(proxy: str = "", timeout: int = 25) -> tuple[str, str]:
    """问一下线上最新版本号。返回 (版本, 错误)。"""
    err = "连不上 GitHub"
    for url in VERSION_URLS:
        try:
            head = _get(url, proxy, timeout)[:4096].decode("utf-8", "ignore")
            m = re.search(r"^version:\s*['\"]?([^'\"\n]+)", head, re.M)
            if m:
                return m.group(1).strip(), ""
        except Exception as e:      # 网络错误、超时、404 都可能是正常的
            err = f"{type(e).__name__}: {e}"
    return "", err


def download(proxy: str = "", on_progress=None,
             timeout: int = 1800) -> tuple[Path | None, str]:
    """下载词库 zip。返回 (文件, 说明)。

    下载完必须校验 zip 中央目录是否完整 —— 断流时会拿到一个"看起来有大小、
    其实解不开"的文件，这一步不查，后面解压时才会炸，错误信息还看不懂。
    """
    err = "下载失败"
    for name, url in SOURCES:
        tmp = Path(tempfile.gettempdir()) / "rime-ice.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _opener(proxy).open(req, timeout=timeout) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
            with zipfile.ZipFile(tmp) as z:
                bad = z.testzip()
                if bad is not None:
                    raise ValueError(f"压缩包损坏于 {bad}")
            return tmp, f"来自{name}，{done / 1024 / 1024:.1f} MB"
        except Exception as e:
            err = f"{name}：{type(e).__name__}: {e}"
    return None, err


def install_zip(zip_path: Path, keep_user_phrases: bool = True) -> tuple[bool, str]:
    """把下好的词库铺到用户目录并重新编译。

    `keep_user_phrases`：`custom_phrase.txt` 是用户自己加短语的地方，
    仓库里也带了一份。默认不覆盖已经存在的，免得把用户加的词冲掉。
    """
    dst = rime_setup.user_rime_dir()
    dst.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.gettempdir()) / "rime-ice-x"
    shutil.rmtree(work, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(work)
    except Exception as e:
        return False, f"解压失败: {e}"

    subs = [p for p in work.iterdir() if p.is_dir()]
    src = subs[0] if subs else work
    copied = 0
    skipped_phrase = False
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        target = dst / rel
        if keep_user_phrases and rel.name == "custom_phrase.txt" and target.exists():
            skipped_phrase = True
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied += 1
        except OSError:
            pass
    shutil.rmtree(work, ignore_errors=True)

    # 配置和样式是程序管的，覆盖完要重写一遍，否则用户的自定义会被冲掉
    rime_setup.write_custom()

    # default.yaml 也被 zip 覆盖回雾凇原版，它的 config_version 是横杠格式
    # （'2026-02-06'）。Rime 只认点分格式，解析失败按 0 算，0 < 共享目录的
    # 0.40 → 用户配置整体被忽略，编译出来变成 luna_pinyin 那套，雾凇拼音
    # 会消失且不报任何错。deploy_ice() 一直有这一步，升级这条路漏了：
    # 点一次「词库更新」，输入法当场变回默认方案。
    inst = rime_setup.rime_install_dir()
    if inst is not None:
        rime_setup._fix_config_version(dst, inst)

    ok, msg = rime_setup.trigger_deploy()
    note = f"已铺 {copied} 个文件"
    if skipped_phrase:
        note += "（保留了你自己的 custom_phrase.txt）"
    if not ok:
        return False, f"{note}，但编译失败：{msg}"
    return True, f"{note}。{msg}"


def update(proxy: str = "", on_progress=None) -> tuple[bool, str]:
    """一条龙：下载 → 铺词库 → 编译。"""
    zp, msg = download(proxy, on_progress)
    if zp is None:
        return False, msg
    return install_zip(zp)


if __name__ == "__main__":
    v, e = latest_version()
    print("线上版本:", v or f"取不到（{e}）")
    print("已装版本:", rime_setup.installed_ice_version())
    print("内置版本:", rime_setup.bundled_ice_version())
