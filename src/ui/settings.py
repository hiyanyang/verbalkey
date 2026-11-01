"""设置界面。左侧分栏，右侧卡片式表单，改完即存。"""

from __future__ import annotations

import subprocess
import sys
import threading
import winreg
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from ..asr import scan_models
from ..config import (
    APP_NAME, CONFIG, LOGS_DIR, MODELS_DIR, VK, VK_CONFIRM, VK_CONFIRM_NAME, VK_NAME,
)
from .widgets import (
    INK, LINE, MUTED, PINK, Combo, OptionGroup, Slider, Spin, Switch,
)
from .brand import draw_icon, make_icon

BG = "#F5F5F7"
CARD_LINE = "#E5E5EA"

QSS = f"""
QWidget {{ color: {INK}; font-size: 14px; font-family: "Microsoft YaHei UI", "微软雅黑"; }}
QMainWindow, QWidget#page {{ background: {BG}; }}

QListWidget {{
    background: transparent; border: none; outline: none;
    padding: 10px 0;
}}
QListWidget::item {{
    padding: 10px 16px; margin: 2px 8px; border-radius: 9px; color: {INK};
}}
QListWidget::item:selected {{ background: {PINK}; color: white; }}
QListWidget::item:hover:!selected {{ background: #E8E8ED; }}

QFrame#card {{ background: white; border: 1px solid {CARD_LINE}; border-radius: 12px; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: white; border: 1px solid #D2D2D7; border-radius: 8px;
    padding: 6px 10px; min-height: 20px;
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus {{
    border: 1px solid {PINK};
}}
QLineEdit {{ selection-background-color: {PINK}; selection-color: white; }}

/* 下拉框由 widgets.Combo 自己画，这里只管弹出的列表 */
QComboBox {{ background: transparent; border: none; min-width: 190px; }}
QComboBox QAbstractItemView {{
    border: 1px solid #D2D2D7; border-radius: 10px; background: white;
    padding: 4px; outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 28px; padding: 4px 10px; border-radius: 6px;
}}
QComboBox QAbstractItemView::item:selected {{
    background: #FFF0F3; color: {PINK};
}}

/* 步进箭头交给 Fusion 画。别给 ::up-arrow 写样式：
   一旦接管，Qt 只画你给的东西，箭头就没了（下拉框踩过同样的坑）。

   但什么都不写也不行：内容一长（比如 "400 ms"），文字会挤到按钮区里，
   上下箭头被压成叠在一起。所以只给按钮留固定位置，箭头仍交给 Fusion 画。 */
QSpinBox {{
    padding: 5px 26px 5px 10px;
    min-height: 20px;
}}

QPushButton {{
    background: white; border: 1px solid #D2D2D7; border-radius: 8px;
    padding: 7px 16px; min-height: 16px;
}}
QPushButton:hover {{ background: #F2F2F7; }}
QPushButton:pressed {{ background: #E8E8ED; }}
QPushButton#primary {{ background: {PINK}; color: white; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #E02449; }}
QPushButton#link {{
    background: transparent; border: none; color: {PINK}; padding: 4px 0;
    text-align: left;
}}
QPushButton#link:hover {{ background: transparent; color: #E02449; }}

/* 滑杆不写样式：样式表画的手柄会被滑杆自身的矩形裁掉一半，
   widgets.Slider 整个自己画，这里的规则留着只会干扰。 */

QScrollArea {{ background: transparent; border: none; }}

/* 细滚动条，不抢视线。没有它时页面一长就会被压扁、底部卡片被切掉 */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(0, 0, 0, 0.16); border-radius: 4px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: rgba(0, 0, 0, 0.30); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def _shadow(w: QWidget):
    e = QGraphicsDropShadowEffect(w)
    e.setBlurRadius(18)
    e.setOffset(0, 1)
    e.setColor(QColor(0, 0, 0, 26))
    w.setGraphicsEffect(e)


class Card(QFrame):
    """白底圆角卡片，行之间自动插分隔线。"""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(18, 15, 18, 15)
        self._lay.setSpacing(0)
        _shadow(self)

        head = QVBoxLayout()
        head.setSpacing(3)
        t = QLabel(title)
        # GDI 字体引擎只有 400/700 两档，写 600 会被压成 700（更黑）。
        # 所以标题不靠字重、靠字号和颜色拉开层次。
        t.setStyleSheet("font-size:15px; font-weight:400; border:none;")
        head.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setStyleSheet(f"color:{MUTED}; font-size:13px; border:none;")
            s.setWordWrap(True)
            head.addWidget(s)
        self._lay.addLayout(head)
        self._lay.addSpacing(6)
        self._row_count = 0

    def _sep(self):
        if self._row_count > 0:
            f = QFrame()
            f.setFrameShape(QFrame.HLine)
            f.setStyleSheet(f"background:{LINE}; border:none; max-height:1px;")
            self._lay.addWidget(f)

    def add_row(self, label: str, widget, hint: str = ""):
        self._sep()
        self._row_count += 1
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 9, 0, 9)
        lay.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(2)
        lb = QLabel(label)
        lb.setStyleSheet("border:none;")
        left.addWidget(lb)
        if hint:
            h = QLabel(hint)
            h.setStyleSheet(f"color:{MUTED}; font-size:13px; border:none;")
            h.setWordWrap(True)
            left.addWidget(h)
        lay.addLayout(left, 1)
        lay.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)
        self._lay.addWidget(w)
        return w

    def add_widget(self, widget, alignment=Qt.AlignLeft, stretch=0):
        self._sep()
        self._row_count += 1
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 9, 0, 9)
        lay.addWidget(widget, stretch, alignment)
        self._lay.addWidget(box)
        return box

    def add_note(self, text: str):
        self._sep()
        self._row_count += 1
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 9, 0, 9)
        lb = QLabel(text)
        lb.setStyleSheet(f"color:{MUTED}; font-size:13px; border:none;")
        lb.setWordWrap(True)
        lay.addWidget(lb)
        self._lay.addWidget(box)
        return box

    def add_options(self, group: OptionGroup):
        self._sep()
        self._row_count += 1
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.addWidget(group)
        self._lay.addWidget(box)
        return box


class SettingsWindow(QMainWindow):

    # 拼音设置的编译、词库更新都在后台线程跑，完成信号弹回主线程
    _ime_done = Signal(bool, str)
    _ice_checked = Signal(str, str)
    _ice_progress = Signal(int)
    _ice_updated = Signal(bool, str)
    # 拼音安装现在是「先下 29 MB 组件再装引擎」，全程几十秒，
    # 必须放后台线程，否则界面直接冻住（用户会以为崩了）
    _rime_log = Signal(str)
    _rime_done = Signal(bool, str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self._ime_done.connect(self._on_ime_done)
        self._ice_checked.connect(self._on_ice_checked)
        self._ice_progress.connect(self._on_ice_progress)
        self._ice_updated.connect(self._on_ice_updated)
        self._rime_log.connect(self._on_rime_log)
        self._rime_done.connect(self._on_rime_installed)
        self.setWindowTitle(f"{APP_NAME} · 设置")
        self.setWindowIcon(make_icon())
        self.resize(780, 620)
        self.setStyleSheet(QSS)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(168)
        self.nav.setSpacing(0)
        self.stack = QStackedWidget()

        root.addWidget(self.nav)
        root.addWidget(self.stack, 1)

        # 构造界面期间不许写配置：控件初始化会 setChecked / setCurrentIndex，
        # 信号一旦连上就会把"当前值"当成"用户操作"存盘。实测踩过 ——
        # 打开一次设置窗口，整理强度就被改回默认值了。
        self._loading = True
        self._pages: list[QVBoxLayout] = []
        self._build_general()
        self._build_hotkey()
        self._build_asr()
        self._build_text()
        self._build_polish()
        self._build_rime()
        self._build_models()
        self._build_about()
        self._loading = False

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def showEvent(self, event):
        # 窗口是复用的，每次打开重新扫 models 目录，
        # 新拷进去的模型不用重启软件就能选到
        self._refresh_models()
        super().showEvent(event)

    def _save(self, section: str, key: str, value) -> None:
        """配置写入的唯一入口。加载界面期间直接丢弃。"""
        if getattr(self, "_loading", False):
            return
        CONFIG.set(section, key, value)

    def _page(self, name: str) -> QVBoxLayout:
        page = QWidget()
        page.setObjectName("page")
        page.setAttribute(Qt.WA_StyledBackground, True)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)
        lay.addStretch(1)

        # 外面套一层滚动区：卡片多的时候能滚，不会被硬压扁（压扁会把底部卡片切掉）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(page)

        self.stack.addWidget(scroll)
        self.nav.addItem(name)
        self._pages.append(lay)
        return lay

    def _put(self, lay: QVBoxLayout, card: Card):
        lay.insertWidget(lay.count() - 1, card)

    # ---------- 通用 ----------

    def _build_general(self):
        lay = self._page("通用")
        c = Card("启动与常驻")
        self.sw_autostart = Switch()
        self.sw_autostart.setChecked(bool(CONFIG.get("general", "autostart")))
        self.sw_autostart.toggled.connect(self._toggle_autostart)
        c.add_row("开机自动启动", self.sw_autostart,
                  "登录后自动在托盘启动，不用手动点")
        self._put(lay, c)

        c2 = Card("浮窗")
        self.cb_hud = Combo()
        self.cb_hud.addItems(["屏幕底部居中", "跟随鼠标位置"])
        self.cb_hud.setCurrentIndex(
            0 if CONFIG.get("general", "hud_position") == "bottom" else 1)
        self.cb_hud.currentIndexChanged.connect(
            lambda i: self._save("general", "hud_position",
                                 "bottom" if i == 0 else "cursor"))
        c2.add_row("显示位置", self.cb_hud,
                   "说话时显示已识别文字的小窗口")

        self.cb_font = Combo()
        self.cb_font.addItems(["小", "标准", "大"])
        px = int(CONFIG.get("general", "hud_font_px") or 18)
        self.cb_font.setCurrentIndex({16: 0, 18: 1, 20: 2}.get(px, 1))
        self.cb_font.currentIndexChanged.connect(self._change_hud_font)
        c2.add_row("浮窗字号", self.cb_font,
                   "正文字号。觉得一眼看不清就调大一档，改完立即生效")
        self._put(lay, c2)

        c3 = Card("上屏方式", "把文字写进当前光标所在的输入框。")
        self.cb_paste = Combo()
        self.cb_paste.addItems(["自动（先 Ctrl+V，失败再发消息）",
                                "仅模拟 Ctrl+V", "仅发送粘贴消息"])
        self.cb_paste.setCurrentIndex(
            {"auto": 0, "clipboard": 1, "message": 2}.get(
                CONFIG.get("paste", "mode"), 0))
        self.cb_paste.currentIndexChanged.connect(
            lambda i: self._save("paste", "mode",
                                 ["auto", "clipboard", "message"][i]))
        c3.add_row("写入方式", self.cb_paste)
        self.sw_restore = Switch()
        self.sw_restore.setChecked(bool(CONFIG.get("paste", "restore_clipboard")))
        self.sw_restore.toggled.connect(
            lambda on: self._save("paste", "restore_clipboard", on))
        c3.add_row("粘贴后恢复原剪贴板", self.sw_restore,
                   "关掉的话，剪贴板里会留着你刚说的内容")
        self._put(lay, c3)

    def _change_hud_font(self, i: int):
        self._save("general", "hud_font_px", [16, 18, 20][max(0, min(2, i))])
        hud = getattr(self.app, "hud", None)
        if hud is not None:
            hud.refresh_font()

    def _toggle_autostart(self, on):
        name = "Verbalkey"
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                winreg.KEY_SET_VALUE)
            if on:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    exe = sys.executable.replace("python.exe", "pythonw.exe")
                    script = str(Path(__file__).resolve().parent.parent / "main.py")
                    cmd = f'"{exe}" "{script}"'
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)
            else:
                for k in (name, "VoiceInput"):   # 顺手清掉改名前的旧值
                    try:
                        winreg.DeleteValue(key, k)
                    except FileNotFoundError:
                        pass
            winreg.CloseKey(key)
        except Exception:
            pass
        self._save("general", "autostart", on)

    # ---------- 热键 ----------

    def _build_hotkey(self):
        lay = self._page("热键")
        c = Card("触发方式", "改完立即生效，不用重启。")
        self.cb_vk = Combo()
        self.cb_vk.addItems(list(VK.keys()))
        self.cb_vk.setCurrentText(
            VK_NAME.get(int(CONFIG.get("hotkey", "vk")), "右 Alt"))
        self.cb_vk.currentTextChanged.connect(self._change_vk)
        c.add_row("按键", self.cb_vk)

        self.grp_mode = OptionGroup()
        self.grp_mode.add("按住说话", "按住不放录音，松开就识别上屏。说完即走，最顺手")
        self.grp_mode.add("按一下切换", "短按一下开始录音，再按一下结束。"
                                        "适合长段口述，手不用一直按着")
        self.grp_mode.setCurrentIndex(
            1 if CONFIG.get("hotkey", "mode") == "toggle" else 0)
        self.grp_mode.changed.connect(
            lambda i: self._save("hotkey", "mode", "toggle" if i else "hold"))
        c.add_options(self.grp_mode)
        c.add_note("已知限制：全屏程序和以管理员身份运行的窗口会拦掉全局热键，"
                   "这时先用普通窗口点一下再说话。\n"
                   "按住模式下单独轻按一下 Alt 会激活窗口菜单栏，按住不放不会。")
        self._put(lay, c)

        c2 = Card("整理结果确认",
                  "整理完先不覆盖原文，按下确认键才替换。这样不想要的时候还能留着原文。")
        self.cb_confirm = Combo()
        self.cb_confirm.addItems(list(VK_CONFIRM.keys()))
        self.cb_confirm.setCurrentText(
            VK_CONFIRM_NAME.get(int(CONFIG.get("hotkey", "confirm_vk")), "右 Ctrl"))
        self.cb_confirm.currentTextChanged.connect(self._change_confirm_vk)
        c2.add_row("确认键", self.cb_confirm)
        c2.add_note("整理完 45 秒内有效。浮窗上也有「用整理版」按钮，鼠标点一样。\n"
                    "这个键不会被吞掉，别的程序照常能用。")
        self.lb_conflict = QLabel("")
        self.lb_conflict.setStyleSheet("color:#FF3B30; font-size:13px; border:none;")
        self.lb_conflict.setWordWrap(True)
        self.lb_conflict.hide()
        c2.add_widget(self.lb_conflict, Qt.AlignLeft, 1)
        self._put(lay, c2)
        self._refresh_conflict()

    def _change_confirm_vk(self, text):
        self._save("hotkey", "confirm_vk", VK_CONFIRM.get(text, 0xA3))
        self._save("hotkey", "confirm_label", text)
        self._refresh_conflict()
        if self.app:
            self.app.reload_hotkey()

    def _refresh_conflict(self):
        same = (int(CONFIG.get("hotkey", "confirm_vk") or 0xA3)
                == int(CONFIG.get("hotkey", "vk")))
        if same:
            self.lb_conflict.setText(
                "确认键和录音键是同一个键，会互相打架。换一个吧。")
            self.lb_conflict.show()
        else:
            self.lb_conflict.hide()

    def _change_vk(self, text):
        vk = VK.get(text, 0xA5)
        self._save("hotkey", "vk", vk)
        self._save("hotkey", "label", text)
        if hasattr(self, "lb_conflict"):
            self._refresh_conflict()
        if self.app:
            self.app.reload_hotkey()

    # ---------- 语音与识别 ----------

    def _build_asr(self):
        lay = self._page("语音与识别")
        c = Card("麦克风")
        self.cb_dev = Combo()
        self.cb_dev.addItem("系统默认", None)
        try:
            from ..audio import Recorder
            for idx, name in Recorder.list_devices():
                self.cb_dev.addItem(name, idx)
        except Exception:
            pass
        i = self.cb_dev.findData(CONFIG.get("audio", "device"))
        self.cb_dev.setCurrentIndex(i if i >= 0 else 0)
        self.cb_dev.currentIndexChanged.connect(
            lambda: self._save("audio", "device", self.cb_dev.currentData()))
        c.add_row("输入设备", self.cb_dev)
        self._put(lay, c)

        c2 = Card("断句", "已按模型特性调好，不需要动。")
        c2.add_note(
            "这里的值不是随便定的：识别模型是整段解码，每次只看到它面前"
            "这一段音频，片段越短越容易把同音字听错。实测同一段话，"
            "切成 1.5 秒的小段错字率 43.8%，切成 5 秒只有 12.3%。\n\n"
            "所以现在的做法是：说完停顿 0.5 秒才断句；不到 3 秒的半句"
            "先攒着，跟下一句一起识别；一口气不停则每 6 秒先出一次字。")
        self._put(lay, c2)

        c3 = Card("识别", "线程越多越快，超过物理核心数反而变慢。")
        self.sp_threads = Spin()
        self.sp_threads.setRange(1, 12)
        self.sp_threads.setFixedWidth(110)
        self.sp_threads.setValue(int(CONFIG.get("asr", "num_threads")))
        self.sp_threads.valueChanged.connect(
            lambda v: self._save("asr", "num_threads", v))
        c3.add_row("推理线程", self.sp_threads)
        self._put(lay, c3)

    # ---------- 文本 ----------

    def _build_text(self):
        lay = self._page("文本")
        c = Card("基础修正")
        self.sw_t2s = Switch()
        self.sw_t2s.setChecked(bool(CONFIG.get("text", "to_simplified")))
        self.sw_t2s.toggled.connect(
            lambda on: self._save("text", "to_simplified", on))
        c.add_row("繁体转简体", self.sw_t2s,
                  "Qwen3-ASR 常把「开放」输出成「開放」，建议开启")
        self._put(lay, c)

        c2 = Card("替换词表", "识别结果里固定替换。一行一条，格式：原文=替换后")
        self.txt_rep = QPlainTextEdit()
        self.txt_rep.setPlaceholderText("三十六=36")
        self.txt_rep.setFixedHeight(96)
        table = CONFIG.get("text", "replacements") or {}
        self.txt_rep.setPlainText(
            "\n".join(f"{k}={v}" for k, v in table.items()))
        self.txt_rep.textChanged.connect(self._save_replacements)
        c2.add_widget(self.txt_rep, Qt.AlignLeft, 1)
        self._put(lay, c2)

        c3 = Card("热词", "默认不带热词。只有你填了，才会在识别时优先匹配这些词"
                          "（最多 8 个，逗号分隔）。填了要按下面按钮重载识别模型。")
        self.ed_hw = QLineEdit(str(CONFIG.get("asr", "hotwords") or ""))
        self.ed_hw.setPlaceholderText("可选：最常被听错的那两三个词")
        self.ed_hw.textChanged.connect(lambda t: self._save("asr", "hotwords", t))
        c3.add_widget(self.ed_hw, Qt.AlignLeft, 1)

        try:
            from .. import hotwords as _hw
            st = _hw.stats()
            note = f"当前生效 {len(st['words'])} 个"
            if st.get("learned"):
                note += f"，已从改正里学到 {st['learned']} 条"
            if st["chars"]:
                note += f"，已积累 {st['chars']} 字语料（仅本机）"
            lbl = QLabel(note)
            lbl.setStyleSheet(f"color:{MUTED};font-size:13px;")
            c3.add_widget(lbl)
        except Exception:
            pass

        btn = QPushButton("重载识别模型")
        btn.setObjectName("primary")
        btn.clicked.connect(lambda: self.app.reload_asr() if self.app else None)
        c3.add_widget(btn)
        self._put(lay, c3)

    def _save_replacements(self):
        out = {}
        for line in self.txt_rep.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        self._save("text", "replacements", out)

    # ---------- 整理 ----------

    def _build_polish(self):
        lay = self._page("口述整理")
        c = Card("整理强度", "「去口水词」走本地规则，毫秒级；"
                            "「书面化」要跑本地模型，约一到两秒。")
        self.grp_level = OptionGroup()
        self.grp_level.add("关闭", "只用识别原文，一字不改")
        self.grp_level.add("去口水词", "只删口水词和结巴重复，不改断句")
        self.grp_level.add("书面化", "补标点、合并碎句，转成能直接发出去的文字")
        self.grp_level.setCurrentIndex(int(CONFIG.get("polish", "level")))
        self.grp_level.changed.connect(self._change_level)
        c.add_options(self.grp_level)
        self._put(lay, c)

        # 「整理完怎么给」原来做成两个开关，但它们的区别只在中间过程，
        # 最终结果都是「不用按确认键就得到整理版」—— 用户切来切去觉得没区别。
        # 改成三选一，一条流水线三种走法，语义才不重叠。
        c_out = Card("整理完怎么给你",
                     "说明：松手后都要先整段重解一遍（把分段边界丢的字补回来），"
                     "这段空档怎么处理由下面决定。「书面化」还要再多花一两秒跑模型。")
        self.grp_out = OptionGroup()
        self.grp_out.add("先写原文，我按确认键替换",
                         "松键立刻出字；整理完浮窗提示，按确认键换成整理版")
        self.grp_out.add("先写原文，整理完自动替换",
                         "字先出来，一两秒后自动变成整理版，不用按键")
        self.grp_out.add("等整理好，一次写进去",
                         "松键后要等一两秒才出字，但过程中不闪、不替换")
        self.grp_out.setCurrentIndex(self._out_mode_index())
        self.grp_out.changed.connect(self._change_out_mode)
        # 去口水词这一档也有整段重解的空档，同一个选项同样生效，不能灰掉
        self.grp_out.setEnabled(int(CONFIG.get("polish", "level")) >= 1)
        c_out.add_options(self.grp_out)
        self._put(lay, c_out)

        c2 = Card("本地模型", "用 llama.cpp 跑 GGUF，完全离线。"
                              "松键后才启动，空闲自动退出。")
        self.ed_bin = QLineEdit(str(CONFIG.get("polish", "server_bin") or ""))
        self.ed_bin.setPlaceholderText("llama-server.exe 的完整路径")
        self.ed_bin.setCursorPosition(0)     # 显示开头，别一上来只看到文件名尾巴
        self.ed_bin.textChanged.connect(
            lambda t: self._save("polish", "server_bin", t))
        pick_bin = QPushButton("选择…")
        pick_bin.clicked.connect(self._pick_bin)
        c2.add_row("llama-server", pick_bin)
        c2.add_widget(self.ed_bin, Qt.AlignLeft, 1)

        self.ed_model = QLineEdit(str(CONFIG.get("polish", "model_file") or ""))
        self.ed_model.setPlaceholderText("整理模型 .gguf 的完整路径")
        self.ed_model.setCursorPosition(0)
        self.ed_model.textChanged.connect(
            lambda t: self._save("polish", "model_file", t))
        pick_model = QPushButton("选择…")
        pick_model.clicked.connect(self._pick_model)
        c2.add_row("模型文件", pick_model)
        c2.add_widget(self.ed_model, Qt.AlignLeft, 1)

        self.sw_keep = Switch()
        self.sw_keep.setChecked(bool(CONFIG.get("polish", "keep_alive")))
        self.sw_keep.toggled.connect(self._toggle_keep_alive)
        c2.add_row("整理模型常驻内存", self.sw_keep,
                   "开着就不用每次重新加载，省掉约 4.5 秒冷启动；"
                   "代价是常驻约 2.3 GB 内存")

        self.sp_idle = Spin()
        self.sp_idle.setRange(1, 120)
        self.sp_idle.setSuffix(" 分钟")
        self.sp_idle.setFixedWidth(110)
        self.sp_idle.setValue(int(CONFIG.get("polish", "idle_unload_min")))
        self.sp_idle.setEnabled(not self.sw_keep.isChecked())
        self.sp_idle.valueChanged.connect(
            lambda v: (self._save("polish", "idle_unload_min", v),
                       self.app.apply_polish_config() if self.app else None))
        c2.add_row("空闲多久卸载", self.sp_idle, "不用的时候把内存还回去")
        self._put(lay, c2)

    def _toggle_keep_alive(self, on: bool):
        self._save("polish", "keep_alive", on)
        self.sp_idle.setEnabled(not on)
        if self.app:
            self.app.apply_polish_config()

    def _out_mode_index(self) -> int:
        """把两个配置字段反推成三选一的下标。"""
        if CONFIG.get("polish", "paste_mode") == "polished":
            return 2
        return 1 if CONFIG.get("polish", "auto_replace") else 0

    def _change_out_mode(self, i: int):
        self._save("polish", "paste_mode", "polished" if i == 2 else "raw")
        self._save("polish", "auto_replace", i == 1)

    def _change_level(self, i: int):
        self._save("polish", "level", i)
        # 关闭这一档不跑重解，没有「等结果」的空档，三个输出方式都没意义；
        # 去口水词和书面化都有重解空档，选项要能选。
        if hasattr(self, "grp_out"):
            self.grp_out.setEnabled(i >= 1)

    def _pick_bin(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 llama-server.exe",
                                           "", "可执行文件 (*.exe)")
        if p:
            self.ed_bin.setText(p)

    def _pick_model(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择整理模型",
                                           "", "GGUF 模型 (*.gguf)")
        if p:
            self.ed_model.setText(p)

    # ---------- 拼音输入法 ----------

    def _build_rime(self):
        from .. import rime_setup

        lay = self._page("拼音输入法")
        s = rime_setup.read_ime_settings()

        # 卡 1：状态
        c = Card("拼音输入法（Verbalkey）",
                 "拼音和语音同属一个程序。引擎是系统级组件，"
                 "词库和配置由本程序托管，装好即用。")
        self.lbl_rime = QLabel("")
        self.lbl_rime.setWordWrap(True)
        # 两行文字 + 折行余量，不给最小高度时 QLabel 按一行算，第二行会被裁掉
        self.lbl_rime.setMinimumHeight(46)
        self.lbl_rime.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        c.add_widget(self.lbl_rime, Qt.AlignLeft, 1)

        # 这个按钮只在「引擎没装」或「词库缺失」时出现 —— 它是修复入口，不是日常设置。
        # 别的输入法也没有「恢复出厂词库」这种选项，因为正常用不到。
        self.btn_rime = QPushButton("安装拼音输入法")
        self.btn_rime.clicked.connect(self._install_rime)
        self.row_rime_btn = c.add_row(
            "需要安装", self.btn_rime,
            "引擎或词库缺失时才会出现这一行，装好即自动隐藏")
        self._put(lay, c)

        # 卡 2：输入方案
        c2 = Card("输入方案", "全拼或双拼。切换后点下方「应用」重新编译。")
        self.cb_schema = Combo()
        self.cb_schema.addItems(list(rime_setup.SCHEMAS.keys()))
        if s["schema"] in rime_setup.SCHEMAS:
            self.cb_schema.setCurrentText(s["schema"])
        self.cb_schema.currentTextChanged.connect(self._save_ime)
        c2.add_row("方案", self.cb_schema, "全拼最通用；双拼适合长期大量打字")
        self._put(lay, c2)

        # 卡 3：候选窗
        c3 = Card("候选窗", "打字时弹出的选字框。改完点下方「应用」。")
        self.sp_cand = Spin()
        self.sp_cand.setRange(3, 10)
        self.sp_cand.setSuffix(" 个")
        self.sp_cand.setFixedWidth(110)
        self.sp_cand.setValue(s["page_size"])
        self.sp_cand.valueChanged.connect(self._save_ime)
        c3.add_row("每页候选数", self.sp_cand, "按数字键选字，一页能看几个")

        self.sw_horiz = Switch()
        self.sw_horiz.setChecked(s["horizontal"])
        self.sw_horiz.toggled.connect(self._save_ime)
        c3.add_row("候选横排", self.sw_horiz, "竖排是传统输入法的样式")

        self.sp_ime_font = Spin()
        self.sp_ime_font.setRange(10, 24)
        self.sp_ime_font.setSuffix(" px")
        self.sp_ime_font.setFixedWidth(110)
        self.sp_ime_font.setValue(s["font_point"])
        self.sp_ime_font.valueChanged.connect(self._save_ime)
        c3.add_row("候选字号", self.sp_ime_font, "看不清就调大")

        self.sw_emoji = Switch()
        self.sw_emoji.setChecked(s["emoji"])
        self.sw_emoji.toggled.connect(self._save_ime)
        c3.add_row("Emoji", self.sw_emoji, "候选里带 😄 这类表情")

        self.sw_trad = Switch()
        self.sw_trad.setChecked(s["traditional"])
        self.sw_trad.toggled.connect(self._save_ime)
        c3.add_row("繁体输出", self.sw_trad, "打出来是繁体字")
        self._put(lay, c3)

        # 卡 3.5：中英文切换键
        # 小狼毫默认只绑了左 Shift，右 Shift 是 noop —— 按了完全没反应，
        # 很多人以为「Shift 切不了中英文」就是这个原因。
        c3b = Card("中英文切换",
                   "单按一下 Shift 就切中英文。正在打拼音时按，"
                   "会把已经打出来的字母当英文上屏，再切到英文。")
        self.sw_shift_l = Switch()
        self.sw_shift_l.setChecked(s["shift_left"])
        self.sw_shift_l.toggled.connect(self._save_ime)
        c3b.add_row("左 Shift", self.sw_shift_l, "键盘左边那个 Shift，单按一下切中英文")

        self.sw_shift_r = Switch()
        self.sw_shift_r.setChecked(s["shift_right"])
        self.sw_shift_r.toggled.connect(self._save_ime)
        c3b.add_row("右 Shift", self.sw_shift_r,
                    "关掉它，「右 Shift + 字母」就还是打大写")
        self._put(lay, c3b)

        # 卡 3.6：符号输入速查
        # 雾凇拼音一直自带这个功能（symbols_v.yaml），但没有任何界面告诉用户怎么打，
        # 用户只会觉得「没有这个功能」。这里只做速查表，不写任何配置。
        # 示例现场从本机文件读，用户自己往里加条目也能显示出来。
        c3c = Card("符号输入",
                   "打 v 加下面的码，候选里直接出符号。"
                   "例：vcheng → ×，vsx → 整组数学符号。")
        for code, name, sample, total in rime_setup.symbol_groups():
            cnt = QLabel(f"共 {total} 个" if total else "")
            cnt.setStyleSheet(f"color:{MUTED}; font-size:13px; border:none;")
            c3c.add_row(f"{code}　{name}", cnt, sample)
        c3c.add_note("打 vhelp 看全部类别。右侧数量是现场从本机 symbols_v.yaml 统计的；"
                     "不标数量说明没读到那个文件，符号表可能被删或改坏了。")
        self._put(lay, c3c)

        # 卡 4：生效
        c4 = Card("生效", "Rime 的配置改完要重新编译一次（约 10 秒）。")
        self.btn_ime_apply = QPushButton("应用并重新编译")
        self.btn_ime_apply.clicked.connect(self._apply_ime)
        c4.add_row("部署", self.btn_ime_apply, "把上面的设置编译进输入法")
        self.lbl_ime_pending = QLabel("")
        self.lbl_ime_pending.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        c4.add_widget(self.lbl_ime_pending, Qt.AlignLeft, 1)
        self._put(lay, c4)

        # 卡 5：词库更新
        c5 = Card("词库更新",
                  "从 GitHub 拉最新的拼音词库。挂全局代理就行，程序会跟着系统代理走；"
                  "想指定别的代理再填下面那一栏。")
        self.lbl_ice_ver = QLabel("")
        self.lbl_ice_ver.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        c5.add_row("当前版本", self.lbl_ice_ver, "")
        self.lbl_online = QLabel("还没检查")
        self.lbl_online.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        btn_chk = QPushButton("检查更新")
        btn_chk.clicked.connect(self._check_ice)
        c5.add_row("线上最新", btn_chk, "")
        c5.add_widget(self.lbl_online, Qt.AlignLeft, 1)

        self.ed_proxy = QLineEdit(str(CONFIG.get("rime", "proxy") or ""))
        self.ed_proxy.setPlaceholderText("留空＝跟随系统代理")
        self.ed_proxy.setFixedWidth(240)
        self.ed_proxy.setCursorPosition(0)
        self.ed_proxy.textChanged.connect(
            lambda t: self._save("rime", "proxy", t.strip()))
        c5.add_row("代理", self.ed_proxy,
                   "挂全局代理就不用填。要指定别的代理写 http://127.0.0.1:7890")

        self.btn_ice_upd = QPushButton("下载并安装")
        self.btn_ice_upd.clicked.connect(self._update_ice)
        c5.add_row("更新", self.btn_ice_upd,
                   "先走镜像加速，失败自动改直连 GitHub")
        self.lbl_ice_state = QLabel("")
        self.lbl_ice_state.setWordWrap(True)
        self.lbl_ice_state.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        c5.add_widget(self.lbl_ice_state, Qt.AlignLeft, 1)
        self._put(lay, c5)

        self._refresh_rime_status()

    def _save_ime(self, *_):
        """拼音设置改动：立即写入配置和 custom.yaml，但编译要点「应用」。"""
        from .. import rime_setup
        from ..config import CONFIG
        if not hasattr(self, "sp_cand"):
            return      # 构造期间首批信号，控件还没建全
        self._save("rime", "schema", self.cb_schema.currentText())
        self._save("rime", "page_size", self.sp_cand.value())
        self._save("rime", "horizontal", self.sw_horiz.isChecked())
        self._save("rime", "font_point", self.sp_ime_font.value())
        self._save("rime", "emoji", self.sw_emoji.isChecked())
        self._save("rime", "traditional", self.sw_trad.isChecked())
        if hasattr(self, "sw_shift_l"):
            self._save("rime", "shift_left", self.sw_shift_l.isChecked())
            self._save("rime", "shift_right", self.sw_shift_r.isChecked())
        problems: list[str] = []
        try:
            problems = rime_setup.write_custom() or []
        except Exception as e:
            problems = [f"写配置失败：{e}"]
        if problems:
            # 校验没过就明说。以前这里无论成败都显示「已保存」，
            # 图标被抹掉那次就是被这句骗过去的。
            self.lbl_ime_pending.setText("配置有问题：" + "；".join(problems))
            self.lbl_ime_pending.setStyleSheet("color:#D70015;")
        else:
            self.lbl_ime_pending.setText("设置已保存，点「应用并重新编译」生效")
            self.lbl_ime_pending.setStyleSheet(f"color: {MUTED}; font-size: 13px;")

    def _apply_ime(self):
        from .. import rime_setup
        self.btn_ime_apply.setEnabled(False)
        self.btn_ime_apply.setText("正在编译…（约 10 秒）")
        self.lbl_ime_pending.setText("")

        def work():
            ok, msg = rime_setup.trigger_deploy()
            # 部署器不会重建 build/weasel.yaml，部署完再写一次样式
            rime_setup.apply_weasel_style()
            rime_setup.rebrand()      # 顺手确保名字图标是对的（内部会重启服务）
            self._ime_done.emit(ok, msg)

        threading.Thread(target=work, daemon=True,
                         name="rime-apply").start()

    def _on_ime_done(self, ok: bool, msg: str):
        self.btn_ime_apply.setEnabled(True)
        self.btn_ime_apply.setText("应用并重新编译")
        if ok:
            self.lbl_ime_pending.setText("已生效，切换一次输入法即可用")
        else:
            QMessageBox.warning(self, "拼音输入法", f"编译失败: {msg}")

    def _refresh_rime_status(self):
        try:
            from .. import rime_setup
            st = rime_setup.status()
        except Exception as e:
            self.lbl_rime.setText(f"读状态失败: {e}")
            return
        if not st["installed"]:
            txt = ("拼音输入法还没装。点下面的按钮会自动装好"
                   "（Windows 会弹一次「是否允许此应用更改设备」，选「是」）。")
        elif not st["has_ice"]:
            txt = ("引擎装好了，但词库文件不见了。"
                   "点下面的按钮重新铺词库，约 10 秒。")
        else:
            # 不显示完整路径：QLabel 换行会在任意字符处断，把路径切断很难看。
            # 用户真正关心的是「能不能用」和「词库是哪一版」。
            eng = st["install_dir"].rsplit("\\", 1)[-1]      # weasel-0.17.4
            ice = st.get("ice_version") or "内置"
            txt = (f"已就绪。Win+空格 切到 Verbalkey 就能打拼音。\n"
                   f"引擎 {eng} · 词库 {ice}")
        self.lbl_rime.setText(txt)
        # 一切正常就把安装行收起来，别让"修复按钮"看起来像日常要点的东西
        if hasattr(self, "row_rime_btn"):
            self.row_rime_btn.setVisible(
                not (st["installed"] and st["has_ice"]))
        if hasattr(self, "lbl_ice_ver"):
            cur = st.get("ice_version") or "未安装"
            bun = st.get("bundle_ice_version") or "—"
            self.lbl_ice_ver.setText(f"已装 {cur}　程序内置 {bun}")

    # ---------- 词库联网更新 ----------

    def _check_ice(self):
        from .. import rime_update
        from ..config import CONFIG
        self.lbl_online.setText("正在检查…")
        proxy = str(CONFIG.get("rime", "proxy") or "")

        def work():
            v, e = rime_update.latest_version(proxy)
            self._ice_checked.emit(v if v else f"取不到（{e}）", e)

        threading.Thread(target=work, daemon=True, name="ice-check").start()

    def _on_ice_checked(self, ver: str, err: str):
        if err:
            self.lbl_online.setText(f"检查失败：{ver}")
            return
        from .. import rime_setup
        cur = rime_setup.installed_ice_version()
        if ver == cur:
            self.lbl_online.setText(f"{ver}（已是最新）")
        else:
            self.lbl_online.setText(f"{ver}（有新版本，当前 {cur}）")

    def _update_ice(self):
        from .. import rime_update
        from ..config import CONFIG
        self.btn_ice_upd.setEnabled(False)
        self.btn_ice_upd.setText("下载中…")
        self.lbl_ice_state.setText("正在下载词库（约 50 MB），失败会自动换源重试")
        proxy = str(CONFIG.get("rime", "proxy") or "")

        def progress(done: int, total: int):
            if total:
                self._ice_progress.emit(done * 100 // total)

        def work():
            ok, msg = rime_update.update(proxy, progress)
            self._ice_updated.emit(ok, msg)

        threading.Thread(target=work, daemon=True, name="ice-update").start()

    def _on_ice_progress(self, pct: int):
        self.btn_ice_upd.setText(f"下载中 {pct}%")

    def _on_ice_updated(self, ok: bool, msg: str):
        self.btn_ice_upd.setEnabled(True)
        self.btn_ice_upd.setText("下载并安装")
        if ok:
            self.lbl_ice_state.setText(msg)
            self._refresh_rime_status()
        else:
            self.lbl_ice_state.setText(f"更新失败：{msg}")

    def _install_rime(self):
        """一键安装：缺组件先下（约 29 MB），再装引擎（一次 UAC），最后铺词库。"""
        if getattr(self, "_rime_busy", False):
            return
        self._rime_busy = True
        self.btn_rime.setEnabled(False)
        self.btn_rime.setText("准备中…")
        self.lbl_rime.setText("正在检查并下载组件…")
        self.lbl_ime_pending.setText("")

        def log(msg: str):
            self._rime_log.emit(msg)

        def work():
            ok, msg = False, ""
            try:
                from .. import rime_setup
                ok, msg = rime_setup.setup_all(on_log=log)
            except Exception as e:
                ok, msg = False, f"出错了: {e}"
            self._rime_done.emit(ok, msg)

        threading.Thread(target=work, daemon=True, name="rime-install").start()

    def _on_rime_log(self, msg: str):
        self.lbl_rime.setText(msg)
        self.btn_rime.setText("安装中…")

    def _on_rime_installed(self, ok: bool, msg: str):
        self._rime_busy = False
        self.btn_rime.setEnabled(True)
        self.btn_rime.setText("安装拼音输入法")
        self._refresh_rime_status()
        if ok:
            self.lbl_rime.setText(msg)
            QMessageBox.information(self, "拼音输入法", msg)
        else:
            self.lbl_rime.setText(msg)
            QMessageBox.warning(self, "拼音输入法", msg)

    # ---------- 识别模型 ----------

    def _build_models(self):
        lay = self._page("识别模型")
        c = Card("已安装", "把 sherpa-onnx 模型目录整个放进 models 文件夹即可被发现。")
        self.cb_model = Combo()
        self.cb_model.setMinimumWidth(340)
        self.cb_model.currentIndexChanged.connect(self._pick_asr_model)
        c.add_row("当前模型", self.cb_model)
        btn_open = QPushButton("打开模型目录")
        btn_open.clicked.connect(
            lambda: QDesktopServices.openUrl(f"file:///{MODELS_DIR}"))
        c.add_widget(btn_open)
        self._put(lay, c)

        c2 = Card("换模型")
        c2.add_note(
            "1. 到 sherpa-onnx 的 asr-models 发布页下载需要的模型包\n"
            "2. 解压后整个文件夹放进 models 目录\n"
            "3. 文件夹里需要有 conv_frontend.onnx、encoder.onnx"
            "（或 encoder.int8.onnx）、decoder.onnx（或 decoder.int8.onnx）和词表\n"
            "4. 回到上面选中它，点下面的按钮重载")
        btn_dl = QPushButton("打开模型下载页")
        btn_dl.setObjectName("link")
        btn_dl.clicked.connect(lambda: QDesktopServices.openUrl(
            "https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models"))
        c2.add_widget(btn_dl)
        btn_reload = QPushButton("重载识别模型")
        btn_reload.setObjectName("primary")
        btn_reload.clicked.connect(
            lambda: self.app.reload_asr() if self.app else None)
        c2.add_widget(btn_reload)
        self._put(lay, c2)

    def _refresh_models(self):
        cur = self.cb_model.currentData()
        self.cb_model.blockSignals(True)
        self.cb_model.clear()
        for m in scan_models(MODELS_DIR):
            self.cb_model.addItem(
                f"{m.name}  ({m.size_bytes()/1048576:.0f} MB)", str(m.dir))
        if self.cb_model.count() == 0:
            self.cb_model.addItem("（未找到模型）", "")
        saved = str(MODELS_DIR / str(CONFIG.get("asr", "model_id") or ""))
        i = self.cb_model.findData(saved)
        if i < 0 and cur:
            i = self.cb_model.findData(cur)
        if i >= 0:
            self.cb_model.setCurrentIndex(i)
        self.cb_model.blockSignals(False)

    def _pick_asr_model(self):
        d = self.cb_model.currentData()
        if not d:
            return
        self._save("asr", "model_id", Path(d).name)
        if self.app:
            self.app.reload_asr()

    # ---------- 关于 ----------

    def _build_about(self):
        lay = self._page("关于")
        c = Card(APP_NAME, "本地跑的语音输入。按住热键说话，松开就写进光标。")

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(14)
        ico = QLabel()
        ico.setFixedSize(64, 64)
        ico.setPixmap(draw_icon(64))
        ico.setScaledContents(True)
        ico.setStyleSheet("border:none; background:transparent;")
        hl.addWidget(ico, 0, Qt.AlignTop)
        hl.addStretch(1)
        c.add_widget(head, Qt.AlignLeft, 1)

        c.add_note(
            "识别：Qwen3-ASR-0.6B int8（sherpa-onnx）\n"
            "分句：Silero VAD\n"
            "上屏：剪贴板 + 模拟粘贴\n"
            "整理：llama.cpp 本地 GGUF\n\n"
            "全程离线，录音不离开本机。")
        self._put(lay, c)

        foot = QLabel("开发者：heyyorkyan@gmail.com")
        foot.setAlignment(Qt.AlignCenter)
        foot.setStyleSheet(f"color:{MUTED}; font-size:13px; border:none;")
        lay.insertWidget(lay.count() - 1, foot)
