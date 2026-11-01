"""测修正词表规模对耗时的影响，判断要不要设上限、要不要做淘汰。

只测成本，不测效果 —— 效果在 probe_fix_scale.py 里。
这里用随机汉字词（几乎不可能与真实文本同音），所以命中数≈0，
量到的是纯「扫描成本」，也就是最坏情况下的固定开销。
"""
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from src import hotwords

CORPUS = Path("dist/Verbalkey/data/hotwords/corpus.jsonl")

CN = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产"
      "种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业"
      "本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利"
      "比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常"
      "文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西"
      "被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什"
      "认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名"
      "确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿"
      "须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率")
EN = "abcdefghijklmnopqrstuvwxyz"


def load_texts():
    out = []
    for line in CORPUS.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = (rec.get("f") or "").strip()
        if len(t) >= 8:
            out.append(t)
    return list(dict.fromkeys(out))[:40]


def gen_cn(n, rng):
    return ["".join(rng.choice(CN) for _ in range(rng.choice([2, 2, 2, 3, 3, 4])))
            for _ in range(n)]


def gen_en(n, rng):
    return ["".join(rng.choice(EN) for _ in range(rng.choice([4, 5, 6, 7, 8])))
            for _ in range(n)]


def bench(texts, words):
    hotwords.phonic_fix(texts[0], words)          # 预热（拼音缓存等）
    t0 = time.perf_counter()
    for t in texts:
        hotwords.phonic_fix(t, words)
    return (time.perf_counter() - t0) / len(texts) * 1000


def main():
    texts = load_texts()
    avg = sum(len(t) for t in texts) / len(texts)
    print(f"输入：{len(texts)} 条真实终稿，平均 {avg:.0f} 字\n")

    rng = random.Random(42)
    sizes = [0, 3, 10, 50, 100, 200, 300, 500]
    cn_all = gen_cn(500, rng)
    en_all = gen_en(300, rng)

    print(f"{'词数':>6} | {'纯中文':>10} | {'纯英文':>10} | {'中英各半':>10}")
    print("-" * 48)
    rows = []
    for n in sizes:
        cn = cn_all[:n]
        en = en_all[:min(n, 300)]
        mix = cn_all[:n // 2] + en_all[:n // 2]
        a, b, c = bench(texts, cn), bench(texts, en), bench(texts, mix)
        rows.append((n, a, b, c))
        print(f"{n:>6} | {a:>8.2f}ms | {b:>8.2f}ms | {c:>8.2f}ms")

    print()
    d = {r[0]: r for r in rows}
    for hi, lo in ((500, 100), (300, 50), (100, 3)):
        per = (d[hi][3] - d[lo][3]) / (hi - lo)
        print(f"中英各半 {lo}→{hi} 个词：每加一个词 +{per*1000:.1f} 微秒，"
              f"共 {d[hi][3]-d[lo][3]:+.2f} ms")


if __name__ == "__main__":
    main()
