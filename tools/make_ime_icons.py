"""生成小狼毫方案图标（透明背景的纯文字），供 schema/icon 使用。

小狼毫 0.17 支持在方案里指定 icon / ascii_icon，指向用户目录下的 .ico。
用它就能把任务栏那个「白底方块 + 中/A」换成和微软拼音一样的光秃秃文字，
而且完全不用碰 System32 里的 weasel.dll。

为什么自己写 ICO：
PIL 的 ICO 保存是把一张大图缩下去，16px 会糊。任务栏按 100%/150%/200%
分别取 16/24/32 像素，每个尺寸都要单独按该字号渲染才锐利。
"""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZES = (16, 20, 24, 32, 40, 48, 64)
# 任务栏那个位置跟系统文字挨着，粗体会显得比旁边一圈图标都重。
# 雅黑 Light 最贴近「细体文字」的观感；真机太淡就换成 Regular。
FONT = "C:/Windows/Fonts/msyhl.ttc"       # Microsoft YaHei Light
FALLBACK = "C:/Windows/Fonts/msyh.ttc"    # Regular


def render(ch: str, size: int) -> Image.Image:
    """把一个字渲染成 size×size 的透明图，并让它尽量占满画布。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 按字号试排版：中文方块字的实际墨迹比字号小一圈，所以要放大到贴边
    best = None
    for px in range(int(size * 0.95), int(size * 1.25) + 1):
        try:
            font = ImageFont.truetype(FONT, px)
        except OSError:
            font = ImageFont.truetype(FALLBACK, px)
        box = draw.textbbox((0, 0), ch, font=font)
        w, h = box[2] - box[0], box[3] - box[1]
        if w <= size - 1 and h <= size - 1:
            best = (font, box, w, h)
        else:
            break
    if best is None:
        font = ImageFont.truetype(FONT, max(8, int(size * 0.8)))
        box = draw.textbbox((0, 0), ch, font=font)
        best = (font, box, box[2] - box[0], box[3] - box[1])

    font, box, w, h = best
    draw.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]),
              ch, font=font, fill=(255, 255, 255, 255))
    return img


def to_dib(im: Image.Image) -> bytes:
    """RGBA → ICO 里的 32bpp DIB（自下而上 BGRA + AND 掩码）。"""
    w, h = im.size
    px = im.load()
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    pixels = b"".join(rows)

    # AND 掩码：32bpp 用 alpha，掩码全 0 即可。每行按 4 字节对齐
    mask_row = ((w + 31) // 32) * 4
    mask = b"\x00" * (mask_row * h)

    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    return header + pixels + mask


def write_ico(path: Path, ch: str) -> None:
    frames = []
    for s in SIZES:
        frames.append((s, to_dib(render(ch, s))))

    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                           len(data), offset)
        offset += len(data)
    for _, data in frames:
        out += data
    path.write_bytes(bytes(out))


def main() -> None:
    base = Path(__file__).resolve().parent.parent
    outdir = base / "assets" / "ime"
    outdir.mkdir(parents=True, exist_ok=True)
    for name, ch in (("vk_zh.ico", "中"), ("vk_en.ico", "英")):
        p = outdir / name
        write_ico(p, ch)
        im = Image.open(p)
        print(f"{name}: {p.stat().st_size} 字节，回读 {im.size}，字号 {ch}")
        # 预览：贴在任务栏那种橙色上，看是不是像光秃秃的文字
        s = 64
        prev = Image.new("RGB", (s * 6, s), (234, 138, 26))
        x = 0
        for size in (16, 24, 32, 40, 48, 64):
            g = render(ch, size)
            prev.paste(g, (x + (64 - size) // 2, (64 - size) // 2), g)
            x += 64
        prev.save(base / "logs" / f"preview_{name}.png")


if __name__ == "__main__":
    main()
