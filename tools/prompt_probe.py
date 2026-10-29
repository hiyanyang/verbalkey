"""排查整理为什么没生效：对比几种 prompt 组织方式的实际输出。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import urllib.request  # noqa: E402

from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine, _PROMPTS  # noqa: E402

LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8080"

TEXT = ("嗯那个我想问一下咱们这个陪护服务一天是管多少钱然后是不是可以按小时算"
        "我母亲刚出院")


def chat(messages, temperature=0.1, max_tokens=256, extra=None):
    body = {
        "model": "local",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if extra:
        body.update(extra)
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with LOCAL.open(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    dt = time.time() - t0
    ch = d["choices"][0]
    return dt, ch["message"]["content"], ch.get("finish_reason"), d.get("usage", {})


def show(tag, dt, out, fin, usage):
    print(f"\n【{tag}】 {dt:.2f}s  finish={fin}  usage={usage}")
    print(f"  → {out!r}")


def main() -> int:
    eng = PolishEngine()
    eng.configure(str(CONFIG.get("polish", "server_bin")),
                  str(CONFIG.get("polish", "model_file")),
                  int(CONFIG.get("polish", "port")), 0)
    ok, err = eng.ensure()
    if not ok:
        print("启动失败:", err)
        return 1

    sysmsg = _PROMPTS[2].strip()
    think_off = {"chat_template_kwargs": {"enable_thinking": False}}

    show("A 现状：指令+原文塞进一条 user",
         *chat([{"role": "user", "content": _PROMPTS[2] + TEXT}], extra=think_off))

    show("B system=指令，user=原文",
         *chat([{"role": "system", "content": sysmsg},
                {"role": "user", "content": TEXT}], extra=think_off))

    show("C 同 B，温度 0.7",
         *chat([{"role": "system", "content": sysmsg},
                {"role": "user", "content": TEXT}], temperature=0.7, extra=think_off))

    show("D 同 B，不传 enable_thinking",
         *chat([{"role": "system", "content": sysmsg},
                {"role": "user", "content": TEXT}]))

    short_sys = "把这段口述整理成通顺的书面中文：删掉「嗯、那个、就是说」这类口头语，补上标点。只输出整理后的正文。"
    show("E 短指令 + system",
         *chat([{"role": "system", "content": short_sys},
                {"role": "user", "content": TEXT}], extra=think_off))

    eng.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
