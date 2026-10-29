"""诊断：150% 缩放下 Qt 到底在哪个分辨率上栅格化字形。

核心疑问：18px 逻辑字号在 DPR=1.5 的窗口上是
  (a) 直接以 27 物理像素栅格化（hinting 对齐物理像素，锐利）
  (b) 先以 18 像素栅格化再放大 1.5 倍（hinting 失效，笔画半像素化）
把两种画法渲染的同一句话逐像素对比，就能判定。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import src.ui  # noqa: F401,E402

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / "logs" / "font"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = "我这边是做陪护的，主要派护理员"


def make_font(px: int, weight=QFont.Normal) -> QFont:
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.NoSubpixelAntialias)
    return f


def render(logical_w: int, logical_h: int, dpr: float, font_px: int) -> QImage:
    """按指定 DPR 渲染，返回物理像素图。"""
    img = QImage(int(logical_w * dpr), int(logical_h * dpr),
                 QImage.Format_RGB32)
    img.setDevicePixelRatio(dpr)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.setFont(make_font(font_px))
    p.setPen(QColor("#1D1D1F"))
    p.drawText(QRect(0, 0, logical_w, logical_h),
               Qt.AlignLeft | Qt.AlignVCenter, TEXT)
    p.end()
    return img


def ink_stats(img: QImage, tag: str):
    """统计灰度分布：越集中在深色端，笔画越"实"。"""
    levels = {}
    total = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            v = (c.red() + c.green() + c.blue()) // 3
            if v < 250:
                levels[v // 32] = levels.get(v // 32, 0) + 1
                total += 1
    solid = sum(n for k, n in levels.items() if k <= 1)      # 灰度 < 64
    mid = sum(n for k, n in levels.items() if 2 <= k <= 5)   # 64..191
    faint = sum(n for k, n in levels.items() if k >= 6)      # 192..255
    print(f"{tag:28s} 墨迹 {total:5d}  实心 {100 * solid / max(1, total):5.1f}%"
          f"  中间 {100 * mid / max(1, total):5.1f}%"
          f"  浅边 {100 * faint / max(1, total):5.1f}%")


def save(img: QImage, name: str, zoom: int = 4):
    img.scaled(img.width() * zoom, img.height() * zoom,
               Qt.IgnoreAspectRatio, Qt.FastTransformation).save(str(OUT / name))


def main():
    app = QApplication.instance() or QApplication([])
    scr = app.primaryScreen()
    dpr = scr.devicePixelRatio()
    print(f"屏幕 DPR = {dpr}\n")

    # 逻辑尺寸 400x40，物理 600x60；两组画布物理尺寸必须一致才可比
    a = render(400, 40, dpr, 18)                  # 18 逻辑字号 @ DPR 1.5
    b = render(400 * dpr, 40 * dpr, 1.0, 18 * dpr)  # 27 物理字号 @ DPR 1.0

    print(f"A 18px@DPR{dpr}: 物理 {a.width()}x{a.height()}")
    print(f"B 27px@DPR1.0 : 物理 {b.width()}x{b.height()}\n")
    ink_stats(a, "A 18px + DPR 缩放")
    ink_stats(b, "B 27px 原生物理像素")

    save(a, "diag_A_18px_dpr.png")
    save(b, "diag_B_27px_native.png")

    # 逐像素差异：如果两图完全相同，说明 Qt 直接在物理分辨率栅格化
    diff = 0
    w = min(a.width(), b.width())
    h = min(a.height(), b.height())
    for y in range(h):
        for x in range(w):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            if abs(ca.red() - cb.red()) > 8:
                diff += 1
    print(f"\n两图像素差异 {diff} / {w * h} "
          f"({100.0 * diff / (w * h):.1f}%)")
    print("→ 差异接近 0 = 直接在物理分辨率栅格化；差异大 = 先按逻辑分辨率再缩放")


if __name__ == "__main__":
    main()
