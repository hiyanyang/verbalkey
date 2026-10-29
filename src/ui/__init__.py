"""UI 包。在这里做一件全局的事：换掉 Qt 的字体渲染引擎。

Qt6 在 Windows 上默认走 DirectWrite，它不认 `QFont.NoSubpixelAntialias`
这类逐字体的渲染开关（实测四种组合的像素数完全一样）。切回 GDI 引擎后
这些开关才生效。

现在的用法是**反向的**：`InkLabel` 用它拿到系统 ClearType 子像素渲染
（`PreferAntialias`），也就是微信输入法、Chromium 用的那套 —— 边缘锐利，
横向分辨率是灰度抗锯齿的三倍。中间试过改成灰度，中文笔画会明显发虚。
细节见 README「浮窗文字为什么要这么处理」。

必须赶在 QApplication 创建之前设置，所以放在包初始化里，
凡是 `import src.ui.*` 的地方都会先走到这里。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "windows:nodirectwrite")
