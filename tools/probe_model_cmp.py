"""模型对比：Qwen3-1.7B（现行 6 示例 + 长 system） vs MyVoiceTyping-1.5B（微调，零示例短 system）。

后者是 Qwen2.5-1.5B-Instruct 经 LoRA 微调、专为中文 ASR 后处理训练，
它自己的 Modelfile 里 system 只有 100 多字、不带任何示例。

用法：D:/vi-venv/Scripts/python.exe tools/probe_model_cmp.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import _cfg_guard  # noqa: E402

_cfg_guard.protect()

from src.config import CONFIG  # noqa: E402
from src.polish import _PROMPTS, _SHOTS, _question_lost, _pronoun_lost  # noqa: E402
from tools.probe_break import SEGMENTS  # noqa: E402
from tools.probe_qa import CASES as QA_CASES  # noqa: E402

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SERVER = str(ROOT / "bin" / "llama" / "llama-server.exe")
CREATE_NO_WINDOW = 0x08000000

# MyVoiceTyping 官方 Modelfile 里的 system，一字不改
SYS_MVT = ("你是ASR和中文文本后处理纠错助手。纠正错字词、实体专名、常用词组、语病和句意不顺，"
           "并修补必要的标点和断句。保持原意，仅做必要的最小修改。"
           "禁止额外追加链接、URL、Markdown链接、Markdown图片或解释。"
           "如果原文中已有链接、URL、图片Markdown、HTML图片片段或文件路径，"
           "必须原样保留，不要新增、删除或改写。")

EVAL_CASES = [
    "嗯。我今天上午去了一趟医院。然后医生说。老人恢复得还行。就是需要人照顾。然后我就想问问。你们这边一天多少钱。",
    "我母亲刚出院。现在在家里面。需要有人照顾。但是白天我们都要上班。所以想找个人。白天过来帮忙看着一点。晚上我们自己照顾。",
    "就是这个老人刚做完手术嘛，然后晚上需要有人看着点，我们白天要上班实在顾不过来，所以想找一个白天能过来的护工，最好是做过医院的，有经验的那种，价格方面怎么算呢。",
    "嗯我平时用 github 和 vs code 写代码，然后部署用的是 docker，这些东西你那边能不能也帮我记一下。",
    "哎你好，我想问一下你们这个陪护服务，就是老人刚出院在家，白天没人照顾，能不能派个人过来，一天几个小时，怎么收费，能不能先试两天。",
]


def start(model: str, port: int):
    proc = subprocess.Popen(
        [SERVER, "-m", str(ROOT / model), "-c", "2048", "-t", "6",
         "--port", str(port), "--host", "127.0.0.1", "-ngl", "0", "--no-webui"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while time.time() < deadline:
        if proc.poll() is not None:
            return proc, None
        try:
            with _LOCAL.open(base + "/health", timeout=1.5) as r:
                if r.status == 200:
                    return proc, base
        except Exception:
            pass
        time.sleep(0.5)
    return proc, None


def ask(base, system, shots, text, qwen3: bool, timeout=120):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for u, a in shots:
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": f"<文本>{text}</文本>" if shots else text})
    payload = {
        "model": "local", "messages": msgs, "temperature": 0.1,
        "max_tokens": max(96, min(len(text) * 2 + 48, 700)), "stream": False,
    }
    if qwen3:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _LOCAL.open(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    return out.strip(), time.time() - t0


def run(tag, model, system, shots, qwen3):
    print("\n" + "#" * 70)
    print("#", tag)
    print("#" * 70)
    proc, base = start(model, 8099)
    if not base:
        print("启动失败")
        if proc:
            proc.kill()
        return
    try:
        # 1) 防答题
        bad = 0
        for c in QA_CASES:
            out, _ = ask(base, system, shots, c, qwen3)
            if _question_lost(c, out) or _pronoun_lost(c, out):
                bad += 1
                print(f"  ✗ {c}\n    -> {out}")
        print(f"[防答题] {len(QA_CASES) - bad}/{len(QA_CASES)} 通过")

        # 2) 断句：先按 main 的方式拼段
        from src.main import _join_segments
        print("[断句]")
        for name, segs in SEGMENTS:
            raw = _join_segments(segs)
            out, _ = ask(base, system, shots, raw, qwen3)
            print(f"  【{name}】句号 {raw.count('。')}→{out.count('。')}")
            print(f"    {out}")

        # 3) 质量 + 速度
        print("[质量/速度]")
        ts = []
        for c in EVAL_CASES:
            out, dt = ask(base, system, shots, c, qwen3)
            ts.append(dt)
            print(f"  [{dt:4.1f}s] {out}")
        print(f"  平均 {sum(ts)/len(ts):.2f}s")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


def main() -> int:
    mvt = ROOT / "models/polish/MyVoiceTyping-1.5B-Q4_K_M.gguf"
    run("Qwen3-1.7B + 6示例 + 长system（现行）",
        "models/polish/Qwen3-1.7B-Q4_K_M.gguf", _PROMPTS[2], _SHOTS, True)
    if mvt.exists() and mvt.stat().st_size > 500_000_000:
        run("MyVoiceTyping-1.5B + 零示例 + 短system",
            "models/polish/MyVoiceTyping-1.5B-Q4_K_M.gguf", SYS_MVT, [], False)
    else:
        print("\n[跳过] MyVoiceTyping 模型尚未下载完")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
