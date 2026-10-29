"""生成 assets/app.ico。多尺寸打包进同一个 ico，任务栏/资源管理器里都清楚。

用 Qt 画（PNG 编码），再按 ICO 格式把各尺寸拼起来 ——
QImage.save("ico") 只写一张，16px 时会糊。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QByteArray  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from src.ui.brand import draw_icon  # noqa: E402

OUT = ROOT / "assets" / "app.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(size: int) -> bytes:
    pm = draw_icon(size)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def build_ico() -> bytes:
    blobs = [(s, png_bytes(s)) for s in SIZES]
    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, data = b"", b""
    for s, blob in blobs:
        # 宽高 256 在 ico 目录项里记作 0
        w = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32,
                               len(blob), offset + len(data))
        data += blob
    return head + entries + data


def main() -> int:
    app = QGuiApplication([])   # QPixmap 需要 GUI 应用上下文
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_ico())
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节，{len(SIZES)} 个尺寸）")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
