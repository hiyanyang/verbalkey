"""测「后置修正词表能加多少」：正收益 vs 误伤面 vs 耗时。

三个实验：
  A. 正收益 —— 人工构造的同音错听文本，看 4/8/16/32/48 个词各能修好几处。
     关键点：构造时**只给错听写法，不给程序任何"错→对"的对应关系**，
     能修好就证明后置修正不依赖已知错法。
  B. 误伤面 —— 拿真实终稿（本来就是正确文本）当输入跑修正，任何改动都是误伤。
     这是唯一客观的指标，不靠我主观判断"这句改坏了"。
  C. 耗时 —— 词表变长后的成本。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from src import hotwords

CORPUS = Path("dist/Verbalkey/data/hotwords/corpus.jsonl")

# 48 个词，前 4 个是业务核心词，按顺序取前 n 个分档
POOL = [
    "压疮", "鼻饲", "翻身扣背", "陪护",
    "褥疮", "吸痰", "尿管", "胃管",
    "术后", "康复", "出院", "家属",
    "护理员", "护工", "月嫂", "护理费",
    "气垫床", "纸尿裤", "经口喂", "翻身拍背",
    "住院", "医嘱", "病历", "血压",
    "血糖", "体温", "心率", "输液",
    "换药", "伤口", "消毒", "康复训练",
    "喂饭", "擦身", "洗头", "剪指甲",
    "生命体征", "听诊器", "打针", "隔离衣",
    "github", "markdown", "skill", "python",
    "docker", "vscode", "excel", "notion",
]


def load_texts() -> list[str]:
    if not CORPUS.exists():
        return []
    out = []
    for line in CORPUS.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = (rec.get("f") or "").strip()
        if len(t) >= 8:
            out.append(t)
    # 去重，保持顺序
    return list(dict.fromkeys(out))


# ---- A. 正收益：真实同音错听（同音同调 / 英文近形），只给错写，不给映射
WRONG = [
    ("鼻四", "鼻饲"), ("陪互", "陪护"), ("翻身扣被", "翻身扣背"),
    ("入疮", "褥疮"), ("吸谈", "吸痰"), ("尿馆", "尿管"),
    ("喂管", "胃管"), ("数后", "术后"), ("康夫", "康复"),
    ("初院", "出院"), ("护里员", "护理员"),
    ("get hub", "github"), ("githup", "github"), ("mark down", "markdown"),
]
# 对照：不同音 / 不同调，应该修不了（说明边界在哪）
HARD = [
    ("压仓", "压疮"), ("轮胎", "它"), ("拉到他上面", "拉到车上面"),
]


def exp_a():
    print("=" * 70)
    print("A. 正收益（只给错听写法，程序不知道任何对应关系）")
    print("=" * 70)
    for n in (4, 8, 16, 32, 48):
        ws = POOL[:n]
        fixed = []
        for wrong, right in WRONG:
            out, hits = hotwords.phonic_fix(wrong, ws)
            if out != wrong:
                fixed.append(f"{wrong}→{out}")
        print(f"  {n:2d} 个词：修好 {len(fixed):2d}/{len(WRONG)}"
              f"   {('、'.join(fixed))[:100]}")
    print()
    print("  修不了的类型（对照）：")
    ws = POOL[:48]
    for wrong, right in HARD:
        out, _ = hotwords.phonic_fix(wrong, ws)
        print(f"    {wrong!r:16} → {out!r:16} {'改了' if out != wrong else '没改'}")


# ---- B. 误伤面：真实终稿本来就是对的，任何改动都是误伤
def exp_b(texts: list[str]):
    print()
    print("=" * 70)
    print(f"B. 误伤面（{len(texts)} 条真实终稿，输入本身正确，改动即误伤）")
    print("=" * 70)
    for n in (4, 8, 16, 32, 48):
        ws = POOL[:n]
        bad = 0
        chars = 0
        samples = []
        for t in texts:
            out, hits = hotwords.phonic_fix(t, ws)
            chars += len(t)
            if out != t:
                bad += 1
                if len(samples) < 3:
                    samples.append(hits)
        rate = bad / max(1, len(texts)) * 100
        print(f"  {n:2d} 个词：{bad:3d}/{len(texts)} 条被改动（{rate:.1f}%），共 {chars} 字")
        for s in samples:
            print(f"        样例：{s}")


# ---- C. 耗时
def exp_c(texts: list[str]):
    print()
    print("=" * 70)
    print("C. 耗时（每条真实终稿跑一次，取平均）")
    print("=" * 70)
    body = "".join(texts[:40])
    for n in (4, 8, 16, 32, 48):
        ws = POOL[:n]
        t0 = time.perf_counter()
        for t in texts[:40]:
            hotwords.phonic_fix(t, ws)
        dt = (time.perf_counter() - t0) / min(40, len(texts)) * 1000
        print(f"  {n:2d} 个词：{dt:6.2f} ms/条（平均 {len(body)//max(1,min(40,len(texts)))} 字）")


if __name__ == "__main__":
    exp_a()
    texts = load_texts()
    exp_b(texts)
    exp_c(texts)
