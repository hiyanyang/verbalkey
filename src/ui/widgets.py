"""自己画的 iOS 风格控件。

为什么不用 QCheckBox / QRadioButton + 样式表：
一旦给 ::indicator 写了背景色，Qt 就用样式表自己画这个方块，
不再调用原生绘制，结果是「实心粉色方块」——勾和圆点全没了。
想画出 iOS 的开关和勾，只能自己画。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property, QAbstractAnimation, QEasingCurve, QLineF, QPoint, QPointF,
    QPropertyAnimation, QRectF, Qt, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QFrame, QSizePolicy, QSlider,
    QSpinBox, QStyleOptionComboBox, QWidget,
)

PINK = "#FF2D55"
INK = "#1D1D1F"
MUTED = "#86868B"
TRACK_OFF = "#E9E9EA"
LINE = "#EDEDF0"


def _qcolor(c: str) -> QColor:
    return QColor(c)


class Switch(QAbstractButton):
    """iOS 开关。42×26，滑动有动画。"""

    WIDTH = 42
    HEIGHT = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._offset = 0.0          # 0 = 关，1 = 开
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.toggled.connect(self._animate)

    # 必须用 QtCore.Property：Python 内置的 property() 不是 Qt 属性，
    # QPropertyAnimation 驱动不了，动画会一动不动。
    def getOffset(self) -> float:
        return self._offset

    def setOffset(self, v: float) -> None:
        self._offset = v
        self.update()

    offset = Property(float, getOffset, setOffset)

    def _animate(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(self.WIDTH, self.HEIGHT)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        if not self.isEnabled():
            p.setOpacity(0.35)     # 置灰：和被禁用项一起的说明文字仍然可读

        # 按实际矩形画，不写死尺寸，缩放时也不会画歪
        track = QRectF(self.rect())
        W, H = track.width(), track.height()
        on = self.isChecked()
        t = self._offset
        # 动画没在跑时就直接用目标值，避免状态显示不对
        if self._anim.state() != QAbstractAnimation.Running:
            t = 1.0 if on else 0.0

        # 轨道：关是浅灰，开是粉，中间按进度插值
        c_off = _qcolor(TRACK_OFF)
        c_on = _qcolor(PINK)
        col = QColor(
            int(c_off.red() + (c_on.red() - c_off.red()) * t),
            int(c_off.green() + (c_on.green() - c_off.green()) * t),
            int(c_off.blue() + (c_on.blue() - c_off.blue()) * t),
        )
        path = QPainterPath()
        path.addRoundedRect(track, H / 2, H / 2)
        p.fillPath(path, col)

        # 旋钮
        pad = max(2.0, H * 0.1)
        d = H - pad * 2
        x = pad + (W - pad * 2 - d) * t
        knob = QRectF(x, pad, d, d)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawEllipse(knob)
        p.end()


class OptionRow(QAbstractButton):
    """iOS 设置里的勾选行：标题 + 副标题，选中时右侧一个粉色勾。

    放在同一个父控件里会自动互斥（autoExclusive）。
    """

    HEIGHT_PAD = 10

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._checked_col = PINK
        self._draw_sep = True

    def setShowSeparator(self, on: bool):
        self._draw_sep = on

    def sizeHint(self):
        from PySide6.QtCore import QSize
        h = 22 + self.HEIGHT_PAD * 2
        if self._subtitle:
            h += 17
        return QSize(240, h)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not self.isEnabled():
            # 禁用态：整行淡下去，否则用户会以为还能点
            p.setOpacity(0.35)
        r = self.rect()
        hovered = self.underMouse()

        if hovered and not self.isChecked():
            p.fillRect(r, QColor("#FAFAFC"))

        f = QFont()
        f.setPointSize(10)
        fm_sub = None
        text_h = 22
        if self._subtitle:
            text_h = 22 + 17
        y0 = (r.height() - text_h) / 2

        # 标题
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QColor(INK))
        p.drawText(QRectF(2, y0, r.width() - 34, 22),
                   Qt.AlignLeft | Qt.AlignVCenter, self._title)

        if self._subtitle:
            f.setPointSize(8)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(2, y0 + 22, r.width() - 34, 17),
                       Qt.AlignLeft | Qt.AlignVCenter, self._subtitle)

        # 选中勾
        if self.isChecked():
            cx = r.width() - 18.0
            cy = r.height() / 2.0
            pen = QPen(QColor(self._checked_col))
            pen.setWidthF(2.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(cx - 7, cy - 0.5), QPointF(cx - 2.5, cy + 4.5))
            p.drawLine(QPointF(cx - 2.5, cy + 4.5), QPointF(cx + 7, cy - 5.5))

        if self._draw_sep:
            p.setPen(QPen(QColor(LINE), 1))
            p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
        p.end()


class OptionGroup(QWidget):
    """装 OptionRow 的容器，负责互斥和最后一行不画分隔线。"""

    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QVBoxLayout
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._rows: list[OptionRow] = []

    def add(self, title: str, subtitle: str = "") -> OptionRow:
        row = OptionRow(title, subtitle, self)
        row.toggled.connect(lambda on, i=len(self._rows):
                            on and self.changed.emit(i))
        self._rows.append(row)
        self._lay.addWidget(row)
        self._refresh_sep()
        return row

    def _refresh_sep(self):
        for i, r in enumerate(self._rows):
            r.setShowSeparator(i != len(self._rows) - 1)

    def setCurrentIndex(self, i: int):
        if 0 <= i < len(self._rows):
            self._rows[i].setChecked(True)


def hairline() -> QWidget:
    """1px 分隔线。"""
    from PySide6.QtWidgets import QFrame
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"background:{LINE}; border:none; max-height:1px;")
    return f


class _ComboPopup(QFrame):
    """下拉候选面板。

    不用 QComboBox 自带的弹出层：那层外面还套了一个直角方框
    （QComboBoxPrivateContainer），样式表改不掉，圆角面板边上总会露出一条。
    所以整个弹出层自己画：圆角白底 + 阴影 + 选中项右侧打勾。
    """

    ROW = 34
    PAD = 6
    MARGIN = 10     # 留给阴影的空间，不然阴影会被窗口边界裁掉

    def __init__(self, combo):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.combo = combo
        self.hover = -1
        self.setMouseTracking(True)

    def _row_at(self, y: float) -> int:
        i = int((y - self.MARGIN - self.PAD) // self.ROW)
        n = self.combo.count()
        return i if 0 <= i < n else -1

    def sizeHint(self):
        from PySide6.QtCore import QSize
        fm = QFontMetrics(self.font())
        w = max(fm.horizontalAdvance(self.combo.itemText(i))
                for i in range(self.combo.count())) if self.combo.count() else 80
        return QSize(min(max(w + 56, self.combo.width()), 460) + self.MARGIN * 2,
                     self.combo.count() * self.ROW + self.PAD * 2 + self.MARGIN * 2)

    def show_at(self, combo):
        self.resize(self.sizeHint())
        # 让面板本体和控件左对齐，阴影则溢出到外面去
        pos = combo.mapToGlobal(QPoint(-self.MARGIN, combo.height() + 4 - self.MARGIN))
        # 贴近屏幕右边时往左挪，别弹出屏幕外
        from PySide6.QtWidgets import QApplication
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            body_w = self.width() - self.MARGIN * 2
            if pos.x() + self.MARGIN + body_w > g.right():
                pos.setX(g.right() - body_w - self.MARGIN)
            if pos.y() + self.height() > g.bottom():
                pos.setY(combo.mapToGlobal(QPoint(0, 0)).y()
                         - self.height() + self.MARGIN - 4)
        self.move(pos)
        self.hover = self.combo.currentIndex()
        self.show()

    def mouseMoveEvent(self, e):
        i = self._row_at(e.position().y())
        if i != self.hover:
            self.hover = i
            self.update()

    def mousePressEvent(self, e):
        i = self._row_at(e.position().y())
        if i >= 0:
            self.combo.setCurrentIndex(i)
        self.close()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.close()
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)

        # 假阴影：由外向内叠十层极淡的圆角实心块，越靠里叠得越多，
        # 就成了一个连续渐变的灰雾。画边框会留下同心线，填充不会。
        # （给透明顶层窗口挂 QGraphicsDropShadowEffect 在 Windows 上不可靠）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 4))
        for k in range(self.MARGIN - 1, -1, -1):
            f = float(k)
            p.drawRoundedRect(r.adjusted(-f, -f * 0.7, f, f * 1.1),
                              10 + f, 10 + f)

        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        p.setPen(QPen(QColor(0, 0, 0, 30), 1))
        p.setBrush(QColor("white"))
        p.drawPath(path)

        fm = QFontMetrics(self.font())
        for i in range(self.combo.count()):
            y = m + self.PAD + i * self.ROW
            if i == self.hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(PINK).lighter(190))
                p.drawRoundedRect(r.x() + 4, y + 2, r.width() - 8,
                                  self.ROW - 4, 6, 6)
            sel = i == self.combo.currentIndex()
            p.setPen(QColor(PINK if sel else INK))
            txt = fm.elidedText(self.combo.itemText(i), Qt.ElideRight,
                                int(r.width() - 56))
            p.drawText(QRectF(r.x() + 12, y, r.width() - 56, self.ROW),
                       Qt.AlignLeft | Qt.AlignVCenter, txt)
            if sel:
                pen = QPen(QColor(PINK))
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                cx, cy = r.right() - 20, y + self.ROW / 2
                p.drawLine(QLineF(cx - 5, cy, cx - 1, cy + 4))
                p.drawLine(QLineF(cx - 1, cy + 4, cx + 5, cy - 4))
        p.end()


class Spin(QWidget):
    """自己画的数字框。

    不用 QSpinBox 有两个原因：
      1. Fusion 的步进箭头只有几个像素，看起来就是两个小点；
      2. 给它写样式表会被整块接管（箭头直接消失），而继承它再 paintEvent
         又会被内部的 lineEdit 子控件盖住，画了也看不见。
    所以整个控件自己实现：圆角边框 + 数值 + 上下 V 形箭头，箭头区可点可滚。
    """

    valueChanged = Signal(int)

    HEIGHT = 34
    PAD_LEFT = 11
    ARROW_ZONE = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min, self._max, self._step = 0, 99, 1
        self._value = 0
        self._suffix = ""
        self._hover_up = self._hover_down = False
        self.setMinimumHeight(self.HEIGHT)
        self.setMinimumWidth(120)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_InputMethodEnabled, False)

    # --- 和 QSpinBox 对齐的接口 ---

    def setRange(self, lo: int, hi: int):
        self._min, self._max = lo, hi
        self.setValue(self._value)

    def setSingleStep(self, step: int):
        self._step = max(1, step)

    def setSuffix(self, suffix: str):
        self._suffix = suffix
        self.update()

    def value(self) -> int:
        return self._value

    def setValue(self, v: int):
        v = max(self._min, min(self._max, int(v)))
        if v != self._value:
            self._value = v
            self.valueChanged.emit(v)
        self.update()

    def stepBy(self, n: int):
        self.setValue(self._value + n * self._step)

    def stepUp(self):
        self.stepBy(1)

    def stepDown(self):
        self.stepBy(-1)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(f"{self._max}{self._suffix}") + 70
        return QSize(max(w, 120), self.HEIGHT)

    # --- 交互 ---

    def _up_rect(self):
        x = self.width() - self.ARROW_ZONE
        return QRectF(x, 1, self.ARROW_ZONE - 1, self.height() / 2 - 1)

    def _down_rect(self):
        x = self.width() - self.ARROW_ZONE
        return QRectF(x, self.height() / 2, self.ARROW_ZONE - 1,
                      self.height() / 2 - 1)

    def mouseMoveEvent(self, e):
        self._hover_up = self._up_rect().contains(QPointF(e.position()))
        self._hover_down = self._down_rect().contains(QPointF(e.position()))
        self.update()

    def leaveEvent(self, e):
        self._hover_up = self._hover_down = False
        self.update()

    def mousePressEvent(self, e):
        p = QPointF(e.position())
        if self._up_rect().contains(p):
            self.stepUp()
        elif self._down_rect().contains(p):
            self.stepDown()
        else:
            self.setFocus()

    def wheelEvent(self, e):
        self.stepBy(-1 if e.angleDelta().y() < 0 else 1)
        e.accept()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Up:
            self.stepUp()
        elif k == Qt.Key_Down:
            self.stepDown()
        elif k == Qt.Key_PageUp:
            self.stepBy(10)
        elif k == Qt.Key_PageDown:
            self.stepBy(-10)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, QColor("white"))
        active = self.hasFocus() or self._hover_up or self._hover_down
        p.setPen(QPen(QColor(PINK if active else "#D2D2D7"), 1))
        p.drawPath(path)

        trect = QRectF(self.PAD_LEFT, 0,
                       self.width() - self.PAD_LEFT - self.ARROW_ZONE,
                       self.height())
        fm = QFontMetrics(self.font())
        txt = fm.elidedText(f"{self._value}{self._suffix}",
                            Qt.ElideRight, int(trect.width()))
        p.setPen(QColor(INK))
        p.drawText(trect, Qt.AlignLeft | Qt.AlignVCenter, txt)

        x0 = self.width() - self.ARROW_ZONE + 5
        x1 = self.width() - 8
        mid = self.height() / 2.0
        pen = QPen()
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)

        pen.setColor(QColor(LINE if not (self._hover_up or self._hover_down)
                            else "#E0E0E5"))
        p.setPen(pen)
        p.drawLine(QLineF(x0, mid - 0.5, x1 - 1, mid - 0.5))

        pen.setColor(QColor(PINK if self._hover_up else MUTED))
        p.setPen(pen)
        cy = mid / 2.0 + 1.0
        p.drawLine(QLineF(x0 + 2, cy + 2.5, (x0 + x1) / 2, cy - 2.5))
        p.drawLine(QLineF((x0 + x1) / 2, cy - 2.5, x1 - 2, cy + 2.5))

        pen.setColor(QColor(PINK if self._hover_down else MUTED))
        p.setPen(pen)
        cy = mid + mid / 2.0 - 1.0
        p.drawLine(QLineF(x0 + 2, cy - 2.5, (x0 + x1) / 2, cy + 2.5))
        p.drawLine(QLineF((x0 + x1) / 2, cy + 2.5, x1 - 2, cy - 2.5))
        p.end()


class Combo(QWidget):
    """自己画的下拉框，连弹出面板都是自己的。

    不继承 QComboBox 有三个原因，全是踩过的坑：
      1. 样式表一接管，Fusion 的默认箭头就没了；
      2. 用 ::down-arrow 的 border 三角画箭头，Qt 会画成灰方块；
      3. 弹出层外面套着一层直角方框（QComboBoxPrivateContainer），
         样式表改不掉，圆角面板边上总会露出一条直角边。
    """

    currentIndexChanged = Signal(int)
    currentTextChanged = Signal(str)

    HEIGHT = 34
    PAD_LEFT = 11
    ARROW_ZONE = 28
    MIN_WIDTH = 200   # 统一宽度，否则不同选项长度会让各页的下拉框参差不齐

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[str, object]] = []
        self._index = -1
        self._hover = False
        self._popup = None
        self.setMinimumHeight(self.HEIGHT)
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)

    # --- 接口对齐 QComboBox ---

    def addItem(self, text: str, data=None):
        self._items.append((text, data))
        if self._index < 0:
            self._index = 0
        self.update()

    def addItems(self, texts):
        for t in texts:
            self.addItem(t)

    def clear(self):
        self._items = []
        self._index = -1
        self.update()

    def count(self) -> int:
        return len(self._items)

    def itemText(self, i: int) -> str:
        return self._items[i][0] if 0 <= i < len(self._items) else ""

    def itemData(self, i: int):
        return self._items[i][1] if 0 <= i < len(self._items) else None

    def currentIndex(self) -> int:
        return self._index

    def currentText(self) -> str:
        return self.itemText(self._index)

    def currentData(self):
        return self.itemData(self._index)

    def findData(self, value) -> int:
        for i, (_t, d) in enumerate(self._items):
            if d == value:
                return i
        return -1

    def findText(self, text: str) -> int:
        for i, (t, _d) in enumerate(self._items):
            if t == text:
                return i
        return -1

    def setCurrentIndex(self, i: int):
        i = max(-1, min(i, len(self._items) - 1))
        if i == self._index:
            return
        self._index = i
        self.update()
        self.currentIndexChanged.emit(i)
        self.currentTextChanged.emit(self.currentText())

    def setCurrentText(self, text: str):
        i = self.findText(text)
        if i >= 0:
            self.setCurrentIndex(i)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        fm = QFontMetrics(self.font())
        w = max((fm.horizontalAdvance(t) for t, _ in self._items), default=80)
        return QSize(w + self.ARROW_ZONE + self.PAD_LEFT + 14, self.HEIGHT)

    # --- 交互 ---

    def _open(self):
        if not self._items:
            return
        if self._popup is None:
            self._popup = _ComboPopup(self)
        self._popup.show_at(self)

    def mousePressEvent(self, _e):
        self.setFocus()
        self._open()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._open()
        elif k == Qt.Key_Up:
            self.setCurrentIndex(max(0, self._index - 1))
        elif k == Qt.Key_Down:
            self.setCurrentIndex(min(len(self._items) - 1, self._index + 1))
        else:
            super().keyPressEvent(e)

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, QColor("white"))

        active = (self._hover or self.hasFocus()
                  or (self._popup is not None and self._popup.isVisible()))
        p.setPen(QPen(QColor(PINK if active else "#D2D2D7"), 1))
        p.drawPath(path)

        trect = QRectF(self.PAD_LEFT, 0,
                       self.width() - self.PAD_LEFT - self.ARROW_ZONE,
                       self.height())
        fm = QFontMetrics(self.font())
        txt = fm.elidedText(self.currentText(), Qt.ElideRight, int(trect.width()))
        p.setPen(QColor(INK))
        p.drawText(trect, Qt.AlignLeft | Qt.AlignVCenter, txt)

        cx = self.width() - self.ARROW_ZONE / 2 - 1.0
        cy = self.height() / 2.0 - 1.0
        pen = QPen(QColor(MUTED))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawLine(QLineF(cx - 4, cy - 2, cx, cy + 2))
        p.drawLine(QLineF(cx, cy + 2, cx + 4, cy - 2))
        p.end()


class Slider(QSlider):
    """自己画的滑杆。

    QSlider 没有子控件，paintEvent 不会被盖住，拖动和键盘也都还由 Qt 管，
    所以只要自己画就够了。用样式表画手柄时，手柄会被滑杆自身的矩形裁掉一半。
    """

    TRACK = 6
    KNOB = 26

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setOrientation(Qt.Horizontal)
        self.setMinimumHeight(self.KNOB + 8)
        self.setCursor(Qt.PointingHandCursor)

    def _knob_x(self) -> float:
        lo, hi = self.minimum(), self.maximum()
        t = (self.value() - lo) / max(1, hi - lo)
        return self.KNOB / 2 + (self.width() - self.KNOB) * t

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cy = self.height() / 2.0
        x0, x1 = self.KNOB / 2, self.width() - self.KNOB / 2
        kx = self._knob_x()

        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#E5E5EA"))
        p.drawRoundedRect(x0, cy - self.TRACK / 2, x1 - x0,
                          self.TRACK, self.TRACK / 2, self.TRACK / 2)
        p.setBrush(QColor(PINK))
        p.drawRoundedRect(x0, cy - self.TRACK / 2, max(0.0, kx - x0),
                          self.TRACK, self.TRACK / 2, self.TRACK / 2)

        p.setBrush(QColor("white"))
        p.setPen(QPen(QColor(0, 0, 0, 45), 1))
        p.drawEllipse(kx - self.KNOB / 2, cy - self.KNOB / 2,
                      self.KNOB, self.KNOB)
        p.end()
