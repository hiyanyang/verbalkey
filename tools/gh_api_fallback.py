"""github.com 主域被代理拦掉时，用 REST API 把本地提交推上去。

为什么需要这个：本机 git 走沙箱代理（http://127.0.0.1:8692），代理对
`github.com:443` 的 CONNECT 隧道返回 502，但 `api.github.com` 和
`uploads.github.com` 完全正常。结果是 `git push` 一次都过不去，发
Release 却毫无障碍。

脚本用 Git Data API 复现一次 push：blob → tree → commit → ref。

**刻意对齐** author / committer / tree / message，让远端算出来的 commit sha
与本地完全相同。不这样做会得到「内容一样、sha 不同」的分叉，下次 push
就要 reset --hard，白丢历史对齐。

只支持线性推进：本地 HEAD 必须是远端 main 的直接后继。

用法：
    python tools/gh_api_fallback.py --dry-run     # 只生成对象并比对 sha，不动 ref
    python tools/gh_api_fallback.py --apply       # 比对一致才更新 ref 和 tag

token 取自 `git credential fill`，不读也不写任何配置文件。
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER, REPO = "hiyanyang", "verbalkey"
API = "https://api.github.com"


def _token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"], cwd=str(ROOT), input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", errors="ignore")
    for line in (out.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("拿不到 GitHub token（git credential fill 没返回 password）")


TOK = ""


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOK}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:500]
        raise SystemExit(f"API {method} {path} → {e.code}\n{detail}")


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{r.stderr.decode(errors='ignore')}")
    return r.stdout.decode("utf-8", errors="replace")


def main() -> int:
    global TOK
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="比对一致后更新 ref 和 tag")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tag", default="", help="顺带创建这个 tag")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        print("要么 --dry-run，要么 --apply")
        return 1

    TOK = _token()
    head = git("rev-parse", "HEAD").strip()
    parent = git("rev-parse", "HEAD~1").strip()
    # git 规范化后的 message 以一个 \n 结尾。全删掉会让 commit 对象差几个字节，
    # 算出来的 sha 就跟本地对不上（实测 tree 一致、commit 不一致就是这个）。
    msg = git("log", "-1", "--format=%B").rstrip("\n") + "\n"

    print(f"本地 HEAD  {head[:12]}")
    remote_main = api(f"/repos/{OWNER}/{REPO}/git/ref/heads/main")["object"]["sha"]
    print(f"远端 main  {remote_main[:12]}")
    if remote_main != parent:
        raise SystemExit(
            f"不是线性推进：远端 main 应为本地 HEAD 的父提交 {parent[:12]}，"
            f"实际 {remote_main[:12]}。改用别的方式对齐。")

    # 改了哪些文件：用 diff-tree 而非 status，拿的是「这个提交」的内容
    files = [f for f in git("diff-tree", "--no-commit-id", "--name-only",
                            "-r", head).splitlines() if f.strip()]
    print(f"本次提交涉及 {len(files)} 个文件")

    entries = []
    for path in files:
        raw = subprocess.run(["git", "show", f"{head}:{path}"], cwd=str(ROOT),
                             capture_output=True).stdout
        # mode：100644 普通文件 / 100755 可执行 / 120000 符号链接
        mode = git("ls-tree", head, "--", path).split()[0]
        blob = api(f"/repos/{OWNER}/{REPO}/git/blobs", "POST",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print(f"  blob {path:24} {blob['sha'][:12]}  {len(raw)} 字节")

    base_tree = api(f"/repos/{OWNER}/{REPO}/git/commits/{parent}")["tree"]["sha"]
    tree = api(f"/repos/{OWNER}/{REPO}/git/trees", "POST",
               {"base_tree": base_tree, "tree": entries})
    print(f"tree  {tree['sha'][:12]}（本地 {git('rev-parse', head + '^{tree}').strip()[:12]}）")

    # 对齐提交者信息，让 sha 能复现
    an = git("log", "-1", "--format=%an").strip()
    ae = git("log", "-1", "--format=%ae").strip()
    ad = git("log", "-1", "--format=%aI").strip()
    cn = git("log", "-1", "--format=%cn").strip()
    ce = git("log", "-1", "--format=%ce").strip()
    cd = git("log", "-1", "--format=%cI").strip()
    made = api(f"/repos/{OWNER}/{REPO}/git/commits", "POST", {
        "message": msg, "tree": tree["sha"], "parents": [parent],
        "author": {"name": an, "email": ae, "date": ad},
        "committer": {"name": cn, "email": ce, "date": cd},
    })
    new_sha = made["sha"]
    same = new_sha == head
    print(f"远端 commit {new_sha[:12]}  与本地{'一致 ✅（不会分叉）' if same else '不一致 ❌'}")

    if not same:
        print("\n树或提交元信息没对齐，--apply 不会执行。")
        print("可以接受分叉的话，之后在本地跑 git fetch && git reset --hard origin/main。")
        return 2
    if args.dry_run:
        print("\n(dry-run，未改动远端 ref)")
        return 0

    api(f"/repos/{OWNER}/{REPO}/git/refs/heads/main", "PATCH",
        {"sha": new_sha, "force": False})
    print("main 已更新")
    if args.tag:
        api(f"/repos/{OWNER}/{REPO}/git/refs", "POST",
            {"ref": f"refs/tags/{args.tag}", "sha": new_sha})
        print(f"tag {args.tag} 已创建")
    return 0


if __name__ == "__main__":
    sys.exit(main())
