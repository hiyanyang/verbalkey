"""说话时浮窗。实时显示转写文字，松键后显示整理结果与替换提示。

关于文字渲染，踩过的坑按顺序记在这里：

1. 最初开 WA_TranslucentBackground 做半透明圆角，系统直接关掉 ClearType，
   字发虚。改不透明窗口后拿到 ClearType —— 但子像素渲染会在笔画两侧留
   橙蓝彩边，中文横竖笔画密，看着全是毛刺。
2. 关子像素要换 GDI 字体引擎（见 src/ui/__init__.py），Qt6 默认的
   DirectWrite 不认 NoSubpixelAntialias。关掉后彩边 0%，但只剩灰度抗锯齿。
3. 灰度抗锯齿的固有问题是**中间灰多**（实测 22%）。中文一个竖画跨两个
   像素时会是 50%+50% 的灰，对齐时才是纯黑 —— 同一行里深浅不一就是这么来的。
   Qt 没有 gamma 开关，只能自己来：InkLabel 把文字画到离屏图，按 gamma
   曲线把中间调压深，再贴回窗口。字形和排版仍是 QLabel 自己算的，不受影响。

圆角由系统 DWM 裁（Win11），拿不到再用 1-bit 遮罩兜底。
"""

from __future__ import annotations

import ctypes
import math
import os
from ctypes import byref, c_int, wintypes

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QFontMetricsF, QImage, QPainter,
    QPainterPath, QPen, QRegion, QTextLayout, QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

BG = QColor(255, 255, 255)
INK = "#1D1D1F"
MUTED = "#86868B"
PINK = "#FF2D55"

CORNER_R = 8      # 与 Win11 系统圆角一致，边框弧线才能和裁切吻合

_INK = QColor(INK)
_LUT_CACHE: dict[float, bytes] = {}


def ink_lut(gamma: float) -> bytes:
    """覆盖率 → 覆盖率**gamma 的查色表。

    gamma < 1 时中间调被压深：50% 覆盖的灰边变成 70%，笔画看着就实。
    纯黑（29,29,31）和纯白都不动，所以不会改变字色和背景。
    """
    key = round(gamma, 3)
    if key in _LUT_CACHE:
        return _LUT_CACHE[key]
    span = 255 - _INK.red()
    table = []
    for v in range(256):
        c = min(1.0, (255 - v) / span)
        table.append(max(0, min(255, int(round(255 - (c ** gamma) * span)))))
    table[255] = 255          # 背景与 alpha 通道必须原样
    data = bytes(table)
    _LUT_CACHE[key] = data
    return data


def _stem_darken(img: QImage, strength: float) -> None:
    """把笔画向外胀 1 个物理像素，对应 DirectWrite 的 enhancedContrast。

    白底黑字，用 Multiply 叠加四向偏移副本 = 取邻域最暗 = 形态学膨胀。
    strength 是叠加比例，不是膨胀圈数。
    """
    src = img.copy()
    p = QPainter(img)
    p.setCompositionMode(QPainter.CompositionMode_Multiply)
    p.setOpacity(max(0.0, min(1.0, strength)))
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        p.drawImage(dx, dy, src)
    p.end()


class InkLabel(QWidget):
    """对比度增强的文本标签。

    Qt 的灰度抗锯齿会留大量中间灰（实测 22%）：一个竖画跨两个像素时是
    50%+50% 的灰，压在像素格上才是纯黑 —— 同一行里深浅不一就是这么来的。
    Qt 没有 gamma 开关，所以这里自己画：

      1. 用 QTextLayout 排好版，按物理像素画到离屏图；
      2. 对整张图做 LUT，把中间调压深（纯黑纯白不动）；
      3. 贴回窗口。

    不能沿用 QLabel.paintEvent：在 paintEvent 内部调 render() 会递归，
    Qt 直接拒绝，画出来是空白。
    """

    GAMMA = float(os.environ.get("VERBALKEY_INK_GAMMA", "0.48"))
    # 对应 DirectWrite 的 enhancedContrast（笔画加粗 / stem darkening）。
    # 微信输入法用 Direct2D + DirectWrite 画候选词，这个参数是它字"实"的
    # 主要原因之一：细笔画会被轻微膨胀，边缘不再是一片浅灰。
    STEM = float(os.environ.get("VERBALKEY_INK_STEM", "0.0"))
    # 子像素渲染开关。True = 走系统 ClearType（和微信输入法、Chromium 一致），
    # 边缘锐利但带极窄彩边；False = 灰度 + gamma 压深，无彩边但边缘只能是灰的。
    SUBPIXEL = os.environ.get("VERBALKEY_INK_GRAY", "") != "1"

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        # 固化到实例：类属性是给实验脚本临时改的默认值
        self._stem = self.STEM
        self._gamma = self.GAMMA
        self._text = text
        self._color = QColor(INK)
        self._opt = QTextOption()
        self._opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred,
                         QSizePolicy.Policy.Fixed)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    # --- 兼容 QLabel 的接口，调用方不用改 ---
    def text(self) -> str:
        return self._text

    def setText(self, t: str):
        t = t or ""
        if t == self._text:
            return
        self._text = t
        self.updateGeometry()
        self.update()

    def setInkColor(self, color):
        self._color = QColor(color)
        self.update()

    def setStem(self, value: float):
        """单独控制笔画加粗强度。小字号要弱一些，否则笔画会粘。"""
        self._stem = max(0.0, min(1.0, float(value)))
        self.update()

    def setWordWrap(self, on: bool):
        self._opt.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
            if on else QTextOption.WrapMode.NoWrap)

    def setTextInteractionFlags(self, _flags):
        pass          # 浮窗文字不需要选中

    # --- 排版 ---
    def _font(self, dpr: float, subpixel: bool) -> QFont:
        """按渲染模式造字体。

        子像素模式用**逻辑字号**交给 Qt，它会按设备像素比栅格化，
        这样才能拿到 ClearType；灰度模式自己乘 dpr，画到离屏位图上。
        """
        f = QFont(self.font())
        px = f.pixelSize()
        if px <= 0:
            px = max(1, int(round(QFontMetrics(f).height() / 1.2)))
        if subpixel:
            f.setPixelSize(int(px))
            # 必须清掉 NoSubpixelAntialias，否则拿不到子像素
            f.setStyleStrategy(QFont.PreferAntialias)
        else:
            f.setPixelSize(max(1, int(round(px * dpr))))
            f.setStyleStrategy(QFont.NoSubpixelAntialias)
        return f

    def _do_layout(self, width: float, font: QFont):
        lay = QTextLayout(self._text, font)
        lay.setTextOption(self._opt)
        lay.beginLayout()
        y = 0.0
        while True:
            line = lay.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0.0, y))
            y += line.height()
        lay.endLayout()
        return lay, y

    def heightForWidth(self, w: int) -> int:
        fm = QFontMetricsF(self.font())
        if not self._text:
            return int(math.ceil(fm.height())) + 2
        if self.SUBPIXEL:
            _, total = self._do_layout(max(1, w), self._font(1.0, True))
            return int(math.ceil(total)) + 4
        dpr = self.devicePixelRatioF()
        _, total = self._do_layout(max(1, int(round(w * dpr))),
                                   self._font(dpr, False))
        # +4 是给下伸部（j/q/g 之类的下摆）和基线取整留的余量，
        # 不留会把最后一行切掉一条边
        return int(math.ceil(total / dpr)) + 4

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self._text or "  ")
        return QSize(min(w, 4000), self.heightForWidth(max(1, w)))

    # --- 绘制 ---
    def paintEvent(self, _e):
        if self.SUBPIXEL:
            self._paint_subpixel()
        else:
            self._paint_gray()

    def _paint_subpixel(self):
        """子像素渲染：直接用系统 ClearType 画到窗口。

        这是微信输入法（Direct2D + DirectWrite）、Chromium 应用、系统 UI
        共同的做法。汉字竖画能落在 1/3 像素的位置上，所以边缘锐利；
        代价是笔画两侧有极窄的橙蓝彩边 —— 那是精度的副产品，
        正常观看距离下会被人眼融合成更高的亮度对比。

        注意：**不能先画到 QImage 再贴**，离屏位图拿不到子像素，
        γ 校正和加粗也就用不上了。这条路是纯粹的「照抄系统」。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(self._color)
        lay, total = self._do_layout(self.width(), self._font(1.0, True))
        p.translate(0.0, max(0.0, round((self.height() - total) / 2.0)))
        lay.draw(p, QPointF(0.0, 0.0))
        p.end()

    def _paint_gray(self):
        """灰度 + gamma 压深 + 可选加粗。没有彩边，但边缘只能是灰的。"""
        dpr = self.devicePixelRatioF()
        W = max(1, int(round(self.width() * dpr)))
        H = max(1, int(round(self.height() * dpr)))

        img = QImage(W, H, QImage.Format_RGB32)
        img.fill(BG)

        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(self._color)
        lay, total = self._do_layout(W, self._font(dpr, False))
        # 垂直居中；取整到物理像素，避免半像素相位
        p.translate(0.0, max(0.0, round((H - total) / 2.0)))
        lay.draw(p, QPointF(0.0, 0.0))
        p.end()

        buf = img.bits()
        buf[:] = bytes(buf).translate(ink_lut(self._gamma))

        if self._stem > 0:
            _stem_darken(img, self._stem)

        img.setDevicePixelRatio(dpr)
        q = QPainter(self)
        q.drawImage(0, 0, img)
        q.end()


def body_font_px() -> int:
    """浮窗正文字号，从设置里读。150% 缩放下 16/18/20 分别对应 24/27/30 物理像素。"""
    try:
        from ..config import CONFIG
        px = int(CONFIG.get("general", "hud_font_px") or 18)
    except Exception:
        px = 18
    return max(14, min(28, px))


def hud_font(px: int, weight: int = None) -> QFont:
    """浮窗专用字体：**不指定字体族，用系统默认**。

    QFont() 留空 family，Qt 会用系统 UI 字体（Windows 上是 Segoe UI，
    中文自动回退到微软雅黑），和微信输入法、系统对话框用的是同一套。
    以前写死 "Microsoft YaHei UI"，那是个更紧、更硬的界面变体，
    连续读整段话会显得生硬。

    字号仍然要取整数物理像素：系统 150% 缩放，15px = 22.5 物理像素，
    半个像素没法对齐，笔画会忽粗忽细。
    """
    f = QFont()
    f.setPixelSize(px)
    if weight is not None:
        f.setWeight(weight)
    f.setStyleStrategy(QFont.NoSubpixelAntialias)
    return f


# DwmSetWindowAttribute 的 DWMWA_WINDOW_CORNER_PREFERENCE
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


def _dwm_round_corners(hwnd: int) -> bool:
    """让系统把窗口四角切成圆角。成功返回 True。

    Win11 才支持；Win10 返回非 0，调用方会退回到自绘遮罩。
    """
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        val = c_int(DWMWCP_ROUND)
        hr = dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            byref(val), ctypes.sizeof(val))
        return hr == 0
    except Exception:
        return False


STATE = {
    "recording": ("录音中", "#FF2D55"),
    "recognizing": ("识别中", "#FF9500"),
    "polishing": ("整理中", "#AF52DE"),
    "done": ("已上屏", "#34C759"),
    "error": ("出错", "#FF3B30"),
}

ANIMATED = {"recording", "recognizing", "polishing"}


class PulseDot(QWidget):
    """状态指示点，做成呼吸灯：一涨一落 1.8 秒，跟静息呼吸差不多。

    之前是 0.44 秒转一圈，看起来像在抽搐；现在用余弦缓动，
    中心和光晕同步起伏，外圈再有一个慢慢扩散淡出的环。
    """

    SIZE = 20
    PERIOD_MS = 1800
    TICK_MS = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._color = QColor(PINK)
        self._t = 0.0
        self._animating = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)

    def _tick(self):
        self._t = (self._t + self.TICK_MS / self.PERIOD_MS) % 1.0
        self.update()

    def set_state(self, key: str, color: str):
        self._color = QColor(color)
        self._animating = key in ANIMATED
        if self._animating:
            if not self._timer.isActive():
                self._t = 0.0
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        cx = cy = self.SIZE / 2.0
        c = self._color
        # 余弦缓动：0 → 1 → 0，两头慢中间快，才像呼吸而不是闪
        breathe = 0.5 - 0.5 * math.cos(2 * math.pi * self._t)

        if self._animating:
            # 外圈：从核心慢慢扩散出去再淡掉
            r = 4.6 + self._t * 5.0
            a = int(80 * (1.0 - self._t) ** 2)
            if a > 0:
                p.setBrush(QColor(c.red(), c.green(), c.blue(), a))
                p.drawEllipse(QPointF(cx, cy), r, r)
            # 光晕：跟着呼吸一起胀缩
            hr = 5.6 + 3.6 * breathe
            ha = int(26 + 62 * breathe)
            p.setBrush(QColor(c.red(), c.green(), c.blue(), ha))
            p.drawEllipse(QPointF(cx, cy), hr, hr)

        # 核心圆点，呼吸时轻微胀缩
        cr = 4.3 + 0.9 * breathe
        p.setBrush(c)
        p.drawEllipse(QPointF(cx, cy), cr, cr)
        p.end()

    def hideEvent(self, _e):
        self._timer.stop()


class CloseButton(QWidget):
    """右上角的小 × 。自己画，避免样式表把图形吃掉。"""

    SIZE = 22
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        self.clicked.emit()
        e.accept()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 28))
            p.drawEllipse(0, 0, self.SIZE, self.SIZE)
        pen = QPen(QColor(MUTED))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        m = 7.0
        p.drawLine(m, m, self.SIZE - m, self.SIZE - m)
        p.drawLine(self.SIZE - m, m, m, self.SIZE - m)
        p.end()


class PillButton(QWidget):
    """胶囊小按钮。自己画：文字 + 圆角底，选中/悬停有反馈。"""

    clicked = Signal()

    def __init__(self, text: str, primary: bool = False, parent=None):
        super().__init__(parent)
        self.text = text
        self.primary = primary
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self.text) + 28, 28)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        self.clicked.emit()
        e.accept()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        if self.primary:
            p.fillPath(path, QColor(PINK))
        else:
            p.fillPath(path, QColor(0, 0, 0, 20 if self._hover else 12))
        p.setPen(QColor("white") if self.primary else QColor(INK))
        p.drawText(r, Qt.AlignCenter, self.text)
        if not self.primary and self._hover:
            p.setPen(QPen(QColor(0, 0, 0, 40), 1))
            p.drawPath(path)
        p.end()


class Hud(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAutoFillBackground(False)
        self._dwm_ok = False          # 系统圆角是否生效，不生效就用遮罩兜底
        self.setFont(hud_font(13))    # 按钮等子控件继承

        root = QVBoxLayout(self)
        root.setContentsMargins(self.PAD_X, 14, self.PAD_X, 16)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.dot = PulseDot()
        self.state = QLabel("录音中")
        self.state.setFont(hud_font(12))
        self.state.setStyleSheet(f"color:{MUTED};")
        head.addWidget(self.dot, 0, Qt.AlignVCenter)
        head.addWidget(self.state, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.btn_close = CloseButton()
        self.btn_close.clicked.connect(self.hide_hud)
        head.addWidget(self.btn_close, 0, Qt.AlignVCenter)
        root.addLayout(head)

        self.text = InkLabel("")
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text.setFont(hud_font(body_font_px()))
        self.text.setStyleSheet(f"color:{INK};")
        root.addWidget(self.text)

        self.hint = InkLabel("")
        self.hint.setWordWrap(True)
        self.hint.setFont(hud_font(12))
        self.hint.setInkColor(PINK)
        # 12px 笔画本来就细，加粗过头会糊成一团，强度减半
        self.hint.setStem(InkLabel.STEM * 0.6)
        self.hint.hide()
        root.addWidget(self.hint)

        # 整理结果的动作行，默认藏起来
        self.actions = QWidget()
        al = QHBoxLayout(self.actions)
        al.setContentsMargins(0, 2, 0, 0)
        al.setSpacing(8)
        self.btn_use = PillButton("用整理版", primary=True)
        self.btn_ignore = PillButton("忽略")
        self._use_conn = None
        self._ignore_conn = None
        al.addWidget(self.btn_use)
        al.addWidget(self.btn_ignore)
        al.addStretch(1)
        self.actions.hide()
        root.addWidget(self.actions)

        # 只用一个隐藏定时器：每次显示都先取消旧的。
        # 之前用 QTimer.singleShot，上一次 flash 的定时器会在新一次录音中途
        # 把浮窗关掉 —— 表现就是「说话时看不到实时识别的内容」。
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_hud)

        self.setFixedWidth(self.WIDTH)
        self.set_state("recording")

    # ---------- 绘制 ----------

    def _corners(self):
        """窗口圆角。系统 DWM 拿不到时退回 1-bit 遮罩（边上会有点锯齿，但不会露直角）。"""
        if self._dwm_ok:
            self.setMask(QRegion())
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), CORNER_R, CORNER_R)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def showEvent(self, e):
        if not self._dwm_ok:
            self._dwm_ok = _dwm_round_corners(int(self.winId()))
        self._corners()
        super().showEvent(e)

    def resizeEvent(self, e):
        self._corners()
        super().resizeEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        # 整块铺满：底色不透明，文字的 ClearType 才有意义；
        # 四角交给系统（或遮罩）裁掉。
        # 不用再画 1px 边框：Win11 的 DWM 自己会给窗口描一圈细边 + 投影，
        # 我们再描一道会叠成 2px 深灰，显得脏。
        p.fillRect(self.rect(), BG)
        p.end()

    # ---------- 状态 ----------

    def refresh_font(self):
        """设置里改了字号后调用，立即生效。"""
        self.text.setFont(hud_font(body_font_px()))
        self._fit()

    def _cancel_auto_hide(self):
        self._hide_timer.stop()

    def set_state(self, key: str):
        label, color = STATE.get(key, ("", PINK))
        self.state.setText(label)
        self.dot.set_state(key, color)

    def set_text(self, text: str):
        self.text.setText(text)
        self._fit()

    def set_hint(self, text: str):
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
        self._fit()

    def show_actions(self, on_use=None, on_ignore=None):
        """显示「用整理版 / 忽略」两个按钮。传 None 表示沿用上次的连接。"""
        # 用 connect 返回的连接对象来断开，直接 disconnect() 在没连过时会打警告
        if on_use is not None:
            if self._use_conn is not None:
                self.btn_use.clicked.disconnect(self._use_conn)
            self._use_conn = self.btn_use.clicked.connect(on_use)
        if on_ignore is not None:
            if self._ignore_conn is not None:
                self.btn_ignore.clicked.disconnect(self._ignore_conn)
            self._ignore_conn = self.btn_ignore.clicked.connect(on_ignore)
        self.actions.setVisible(True)
        self._fit()

    def hide_actions(self):
        self.actions.hide()
        self._fit()

    WIDTH = 600
    PAD_X = 22

    @staticmethod
    def _wrapped_h(label, width: int) -> int:
        """按给定宽度折行后，这段文字实际要占多高。

        不用 QLabel.heightForWidth：它会按字体行高给一个偏小的值，
        再配合 setFixedHeight 就把字的上下沿切掉了。
        """
        fm = label.fontMetrics()
        text = label.text() or " "
        rect = fm.boundingRect(QRect(0, 0, width, 10000),
                               Qt.TextWordWrap | Qt.AlignLeft, text)
        # 多留 8px：Qt 画中文时行高比 fontMetrics().height() 略大，
        # 卡着算会把字的上下沿切掉
        return max(fm.height(), rect.height()) + 8

    def _fit(self):
        w = self.WIDTH - self.PAD_X * 2
        self.text.setFixedWidth(w)
        th = self._wrapped_h(self.text, w)
        self.text.setFixedHeight(th)

        h = 14 + 22 + 8 + th

        if self.hint.isVisible():
            self.hint.setFixedWidth(w)
            hh = self._wrapped_h(self.hint, w)
            self.hint.setFixedHeight(hh)
            h += 8 + hh

        if self.actions.isVisible():
            h += 10 + 28

        h += 16
        self.setFixedHeight(max(76, min(h, 520)))
        self._place()

    def _place(self):
        """按设置定位：屏幕底部居中，或者跟着鼠标走。

        以前这里写死了底部居中，「通用 → 显示位置」选了「跟随光标」
        其实没生效 —— 设置项存在但没有实现。
        """
        from ..config import CONFIG

        if CONFIG.get("general", "hud_position") == "cursor":
            cur = QCursor.pos()
            screen = QApplication.screenAt(cur) or QApplication.primaryScreen()
            if not screen:
                return
            geo = screen.availableGeometry()
            x = cur.x() - self.width() // 2
            y = cur.y() + 28                    # 落在光标下方，别挡住正文
            x = max(geo.left() + 8,
                    min(x, geo.right() - self.width() - 8))
            if y + self.height() > geo.bottom():
                y = cur.y() - self.height() - 28   # 下方放不下就翻到上面
            y = max(geo.top() + 8, y)
            self.move(x, y)
            return

        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + geo.height() - self.height() - 90)

    def show_hud(self, state: str = "recording", text: str = ""):
        self.set_state(state)
        self.set_text(text)
        self.set_hint("")
        self.hide_actions()
        self._cancel_auto_hide()
        # 「跟随光标」每次都得重新定位：用户换了个窗口，位置就变了
        from ..config import CONFIG
        if not self.isVisible() or CONFIG.get("general", "hud_position") == "cursor":
            self._place()
        self.show()
        self.raise_()

    def hide_hud(self):
        self._cancel_auto_hide()
        self.dot.set_state("done", PINK)   # 顺手停掉呼吸动画
        self.hide()

    def flash(self, state: str, text: str, ms: int = 1800):
        self.show_hud(state, text)
        self._hide_timer.start(ms)
