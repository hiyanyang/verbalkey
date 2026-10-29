"""对比整理 prompt 方案：零样本 vs few-shot 对话示例。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.config import CONFIG  # noqa: E402
from src.polish import PolishEngine, _PROMPTS  # noqa: E402

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

TEXTS = [
    "嗯我今天上午去了一趟医院然后医生说老人恢复得还行就是需要人照顾然后我就想问问你们这边一天多少钱",
    "我母亲刚出院嘛现在在家里面需要有人照顾但是白天我们都要上班所以想找个人白天过来帮忙看着一点晚上我们自己照顾",
    "嗯那个我想问一下咱们这个陪护服务一天是管多少钱然后是不是可以按小时算",
    "就是这个老人刚做完手术嘛然后晚上需要有人看着点我们白天要上班实在顾不过来",
    "嗯我这边是做陪护的主要是派护理员上门照顾刚出院的老人很多家属第一次找我们最关心的就是人靠不靠得住",
]

SYS_B = (
    "你把语音转写的口述整理成通顺的书面中文，只输出正文。"
    "删掉「嗯、啊、那个、就是说」这类口头语。"
    "按意思断句：一件事说完用句号，不要一条逗号到底。"
    "把三五个字的碎句合并成完整句子。不改意思，不加内容。"
)

SYS_C = (
    "你把口述转写整理成通顺的书面中文，只输出正文，不加解释。"
    "规则：删掉嗯、啊、那个、就是说这类口头语和不必要的「然后」；"
    "一个意思说完用句号，同一意思里的短句用逗号连起来，不要出现三五个字的碎句；"
    "不改原意，不添加原文没有的信息。"
)

FEWSHOT = [
    ("嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
     "我今天上午去了一趟医院，医生说恢复得还行，就是需要人照顾。"),
    ("我母亲刚出院现在在家需要有人照顾但是白天我们都要上班",
     "我母亲刚出院，现在在家需要有人照顾。但白天我们都要上班。"),
    ("我想问一下陪护一天多少钱然后能不能按小时算",
     "我想问一下陪护一天多少钱，能不能按小时算？"),
    ("嗯我们这个服务主要是照顾刚出院的老人家属最关心人靠不靠得住",
     "我们这个服务主要是照顾刚出院的老人，家属最关心人靠不靠得住。"),
]


def call(messages, max_tokens=256, temperature=0.15):
    body = json.dumps({
        "model": "local",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions",
                                data=body,
                                headers={"Content-Type": "application/json"})
    t0 = time.time()
    with _LOCAL.open(req, timeout=180) as r:
        d = json.loads(r.read().decode("utf-8"))
    return time.time() - t0, d["choices"][0]["message"]["content"].strip()


def msgs_a(text):
    return [{"role": "system", "content": _PROMPTS[2]},
            {"role": "user", "content": text}]


FEWSHOT_C = [
    ("嗯我今天上午去了一趟医院然后医生说恢复得还行就是需要人照顾",
     "我今天上午去了一趟医院，医生说恢复得还行，就是需要人照顾。"),
    ("我母亲刚出院现在在家需要有人照顾但是白天我们都要上班实在顾不过来",
     "我母亲刚出院，需要有人在家照顾，但白天我们都要上班，实在顾不过来。"),
    ("我想问一下陪护一天多少钱然后能不能按小时算",
     "我想问一下陪护一天多少钱，能不能按小时算？"),
    ("嗯我们这个服务主要是照顾刚出院的老人家属最关心人靠不靠得住",
     "我们这个服务主要是照顾刚出院的老人，家属最关心人靠不靠得住。"),
]


def msgs_c(text):
    m = [{"role": "system", "content": SYS_C}]
    for u, a in FEWSHOT_C:
        m.append({"role": "user", "content": u})
        m.append({"role": "assistant", "content": a})
    m.append({"role": "user", "content": text})
    return m


def msgs_b(text):
    m = [{"role": "system", "content": SYS_B}]
    for u, a in FEWSHOT:
        m.append({"role": "user", "content": u})
        m.append({"role": "assistant", "content": a})
    m.append({"role": "user", "content": text})
    return m


def main():
    eng = PolishEngine()
    eng.configure(str(CONFIG.get("polish", "server_bin")),
                  str(CONFIG.get("polish", "model_file")), 8080, 0)
    ok, err = eng.ensure()
    if not ok:
        print("启动失败", err)
        return 1

    for name, fn in (("B few-shot 对话", msgs_b), ("C few-shot + 合并要求", msgs_c)):
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        total = 0.0
        for i, t in enumerate(TEXTS, 1):
            fn(t)  # 第一次预热，把前缀编码进 KV
            dt, out = call(fn(t))
            total += dt
            print(f"--- {i}  {dt:.2f}s ---")
            print("原:", t)
            print("整:", out)
        print(f"\n合计 {total:.2f}s，平均 {total / len(TEXTS):.2f}s")
    eng.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
