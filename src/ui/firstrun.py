"""首次运行引导：用户点一次确认，剩下的自己下完、装好。

**为什么非要有这个窗口**（不是「README 写清楚就行」）：

有个本地 AI 应用的真实复盘——403 MB 安装包，首次启动静默下 1.9 GB 模型，
界面只显示一句「Warming up」。用户对着不动的界面等了 549 秒，以为是卡死，
2,738 个访客、零激活。他们的结论是：要么把大文件打进包，要么把下载做成
**有进度、有速度、有剩余时间**的明面流程，绝不能让用户猜。

我们这个项目选后者：模型 0.9–2 GB，打进包会让下载体积翻四倍，
而且国内下 GitHub Release 比下 ModelScope 慢得多。所以包做小，
首次运行时把这个窗口摆出来，让用户按一下「开始下载」。

设计上的三条硬要求：
1. **每一行都要有体积**，而且只算「真的还缺的部分」——已下过就别显示 938 MB 吓人。
2. **下载中必须有速度、剩余时间、当前文件**。速度是 0 也要显示（说明在连）。
3. **随时能关掉**。关掉程序照样能启动，只是没模型用不了；下次再问。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QVBoxLayout,
)

from .. import fetch as F
from .widgets import INK, MUTED, PINK

BG = "#F5F5F7"

QSS = f"""
QDialog {{ background: {BG}; }}
QLabel {{ color: {INK}; font-size: 14px; font-family: "Microsoft YaHei UI", "微软雅黑"; }}
QLabel#title {{ font-size: 17px; }}
QLabel#sub {{ color: {MUTED}; font-size: 13px; }}
QLabel#sum {{ color: {PINK}; font-size: 14px; }}
QFrame#box {{ background: white; border: 1px solid #E5E5EA; border-radius: 12px; }}
QCheckBox {{ font-size: 14px; spacing: 8px; }}
QProgressBar {{
    border: none; border-radius: 4px; background: #E5E5EA;
    height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {PINK}; border-radius: 4px; }}
QPlainTextEdit {{
    background: #FBFBFD; border: 1px solid #E5E5EA; border-radius: 8px;
    color: {MUTED}; font-size: 12px; padding: 6px;
}}
QPushButton {{
    background: white; border: 1px solid #D2D2D7; border-radius: 8px;
    padding: 8px 18px; font-size: 14px; color: {INK};
}}
QPushButton:hover {{ border-color: #B8B8BE; }}
QPushButton#primary {{
    background: {PINK}; border: none; color: white;
}}
QPushButton#primary:hover {{ background: #E6274B; }}
QPushButton:disabled {{ color: #A9A9AE; border-color: #E5E5EA; background: #F2F2F5; }}
"""

# 三个可选项。required 的不给取消勾选 —— 没它程序没法用
OPTIONS = [
    {
        "groups": ["vad", "asr"],
        "title": "语音识别（必需）",
        "desc": "本地识别模型 + 语音活动检测，装完按住右 Alt 就能说话上屏",
        "required": True,
        "checked": True,
    },
    {
        "groups": ["polish", "llama"],
        "title": "书面化整理（可选）",
        "desc": "把口语整理成通顺的书面语。不装也能用「去口水词」，那档是规则实现",
        "required": False,
        "checked": True,
    },
    {
        "groups": ["pinyin"],
        "title": "拼音输入法（可选）",
        "desc": "小狼毫引擎 + 雾凇拼音，打字和语音共用一个中/英图标。"
                "会自动下载并装好，装引擎那一步系统会要一次授权",
        "required": False,
        "checked": False,
        "install": True,     # 除了下载，还要跑安装流程（rime_setup.setup_all）
    },
]


def _missing_items(groups: list[str]) -> list[dict]:
    return [a for a in F.select(groups) if F.status(a) != "ok"]


def pinyin_installed() -> bool:
    """拼音**装没装**（不是文件在不在）。"""
    try:
        from .. import rime_setup
        return bool(rime_setup.status()["installed"])
    except Exception:
        return False


class _Worker(QThread):
    """下载在子线程跑，界面只接收信号 —— 否则进度条根本不刷新。"""

    log = Signal(str)
    progress = Signal(int, int, float)   # 已下, 总数, 速度
    phase = Signal(str)                  # 进入安装阶段（此时没有进度可言）
    done = Signal(list, str)             # 失败项, 附加说明（拼音装失败时的原因）

    def __init__(self, items: list[dict], pinyin: bool = False):
        super().__init__()
        self.items = items
        self.pinyin = pinyin
        self._stop = False

    def cancel(self) -> None:
        self._stop = True

    def run(self) -> None:
        failed = F.fetch(
            self.items,
            on_log=self.log.emit,
            on_progress=lambda d, t, s: self.progress.emit(d, t, s),
            stop=lambda: self._stop,
        )
        note = ""
        # 拼音不是「下完就能用」：还要装引擎、注册、铺词库、编译。
        # 这些以前留在设置页让用户自己点，等于白下。现在这里一次做完。
        # setup_all 内部会自己补齐拼音文件（ensure_bundle），所以上面
        # 的 items 里故意不含 pinyin 组，避免同一批文件下两遍。
        if not failed and self.pinyin and not self._stop:
            self.phase.emit("正在安装拼音输入法…（会弹一次系统授权框，点「是」）")
            try:
                from .. import rime_setup
                ok, msg = rime_setup.setup_all(
                    on_log=self.log.emit,
                    on_progress=lambda d, t, s: self.progress.emit(d, t, s),
                    stop=lambda: self._stop,
                )
            except Exception as e:
                ok, msg = False, f"安装拼音输入法出错：{e}"
            self.log.emit(msg)
            if not ok:
                note = msg
        self.done.emit(failed, note)


class FirstRunDialog(QDialog):
    """返回 accepted 表示「该装的都装好了」，调用方据此决定要不要重载模型。"""

    def __init__(self, parent=None, title: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Verbalkey 首次设置" if title is None else title)
        self.setStyleSheet(QSS)
        self.setMinimumWidth(600)
        self.worker: _Worker | None = None
        self._rows: list[tuple[dict, QCheckBox, QLabel]] = []
        self._built = False
        self._build()
        self._refresh_sum()

    # ---------- 界面 ----------

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(14)

        t = QLabel("先把要用的东西下下来")
        t.setObjectName("title")
        t.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay.addWidget(t)

        sub = QLabel(
            "全部从官方源下载，装完就能离线使用。"
            "断网了没关系，下次打开这个窗口会接着下（不会从头来）。")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        sub.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay.addWidget(sub)

        box = QFrame()
        box.setObjectName("box")
        box_lay = QVBoxLayout(box)
        box_lay.setContentsMargins(16, 14, 16, 14)
        box_lay.setSpacing(12)

        for opt in OPTIONS:
            items = _missing_items(opt["groups"])
            mb = F.estimate_mb(items)
            # 拼音的「还需要做点什么」不一定等于「还有文件要下」：文件下完了
            # 但引擎没装，用户还是用不了，这一行必须还能勾、还能点。
            need = mb > 0 or (bool(opt.get("install")) and not pinyin_installed())
            row = QHBoxLayout()
            cb = QCheckBox(opt["title"])
            # 拼音有个特殊情况：文件已经下过（用户上次选过它）、但引擎没装完。
            # 这时候不能按「默认不勾」处理 —— 用户已经表达过意图，而且窗口
            # 会显示「待安装」却默认不勾，点「开始使用」就直接跳过，
            # 表现就是「明明下过却一直用不了拼音」。所以这种情况默认勾上。
            half_done = bool(opt.get("install")) and need and mb <= 0
            cb.setChecked((bool(opt["checked"]) and need) or half_done)
            cb.setEnabled(not opt["required"] and need)
            cb.stateChanged.connect(lambda _=0: self._refresh_sum())
            right = QLabel(self._row_text(opt, mb))
            right.setObjectName("sub")
            right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            right.setMinimumWidth(90)
            row.addWidget(cb)
            row.addStretch(1)
            row.addWidget(right)
            box_lay.addLayout(row)

            d = QLabel(opt["desc"])
            d.setObjectName("sub")
            d.setWordWrap(True)
            d.setContentsMargins(26, 0, 0, 0)
            box_lay.addWidget(d)
            box_lay.addSpacing(2)
            self._rows.append((opt, cb, right))

        self.lbl_sum = QLabel("")
        self.lbl_sum.setObjectName("sum")
        box_lay.addWidget(self.lbl_sum)
        lay.addWidget(box)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.lbl_state = QLabel("")
        self.lbl_state.setObjectName("sub")
        self.lbl_state.setVisible(False)
        lay.addWidget(self.lbl_state)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(104)
        self.log.setVisible(False)
        lay.addWidget(self.log)

        lay.addStretch(1)

        btns = QHBoxLayout()
        self.btn_later = QPushButton("稍后再说")
        self.btn_later.clicked.connect(self._on_later)
        self.btn_go = QPushButton("开始下载")
        self.btn_go.setObjectName("primary")
        self.btn_go.clicked.connect(self._on_go)
        btns.addStretch(1)
        btns.addWidget(self.btn_later)
        btns.addWidget(self.btn_go)
        lay.addLayout(btns)

    # ---------- 逻辑 ----------

    @staticmethod
    def _row_text(opt: dict, mb: float) -> str:
        if mb > 0:
            return f"{mb:,.0f} MB"
        # 拼音：文件齐了 ≠ 能用了，引擎没装还得装
        if opt.get("install") and not pinyin_installed():
            return "待安装"
        return "已就绪"

    def _refresh_rows(self) -> None:
        """下载结束后把每行的「体积 / 待安装 / 已就绪」重新算一遍。"""
        for opt, cb, right in self._rows:
            mb = F.estimate_mb(_missing_items(opt["groups"]))
            right.setText(self._row_text(opt, mb))
            need = mb > 0 or (bool(opt.get("install")) and not pinyin_installed())
            cb.setEnabled(not opt["required"] and need)

    def _selected_groups(self) -> list[str]:
        out: list[str] = []
        for opt, cb, _ in self._rows:
            if cb.isChecked():
                out.extend(opt["groups"])
        return out

    def _wants_pinyin(self) -> bool:
        for opt, cb, _ in self._rows:
            if opt.get("install"):
                return cb.isChecked()
        return False

    def _selected_items(self) -> list[dict]:
        """要下的文件（不含拼音组 —— 它由 setup_all 连带下载，见 _Worker.run）。"""
        items: list[dict] = []
        seen = set()
        for opt, cb, _ in self._rows:
            if not cb.isChecked() or opt.get("install"):
                continue
            for a in _missing_items(opt["groups"]):
                key = str(a["dest"])
                if key not in seen:
                    seen.add(key)
                    items.append(a)
        return items

    def _refresh_sum(self) -> None:
        items = self._selected_items()
        mb = F.estimate_mb(items)
        py = self._wants_pinyin() and not pinyin_installed()
        tail = "，再装拼音输入法" if py else ""
        if mb <= 0 and not py:
            self.lbl_sum.setText("都装好了，不用下任何东西")
            self.btn_go.setText("开始使用")
            self.btn_go.setEnabled(True)
        elif mb <= 0:
            self.lbl_sum.setText("拼音输入法还没装，点下面的按钮装一下")
            self.btn_go.setText("开始安装")
            self.btn_go.setEnabled(True)
        elif mb >= 1024:
            self.lbl_sum.setText(f"需要下载 {len(items)} 个文件，"
                                 f"合计约 {mb/1024:.2f} GB{tail}")
        elif mb >= 1:
            self.lbl_sum.setText(f"需要下载 {len(items)} 个文件，"
                                 f"合计约 {mb:,.0f} MB{tail}")
        else:
            self.lbl_sum.setText(f"需要下载 {len(items)} 个文件，合计不到 1 MB{tail}")
        if py:
            self.btn_go.setText("开始下载并安装")

    def _on_go(self) -> None:
        items = self._selected_items()
        py = self._wants_pinyin() and not pinyin_installed()
        if not items and not py:
            self.accept()
            return
        for _, cb, _ in self._rows:
            cb.setEnabled(False)
        self.btn_later.setEnabled(False)
        self.btn_go.setEnabled(False)
        self.btn_go.setText("正在下载…" if items else "正在安装…")
        self.bar.setVisible(True)
        self.lbl_state.setVisible(True)
        self.log.setVisible(True)
        self.bar.setValue(0)
        self.bar.setRange(0, 0)          # 还没拿到大小，先转圈
        self.log.clear()
        self._total_mb = F.estimate_mb(items)

        self.worker = _Worker(items, py)
        self.worker.log.connect(self.log.appendPlainText)
        self.worker.progress.connect(self._on_progress)
        self.worker.phase.connect(self._on_phase)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_phase(self, text: str) -> None:
        """进入安装阶段：没有百分比可言，进度条转圈，文字说清在干什么。"""
        self.bar.setRange(0, 0)
        self.lbl_state.setText(text)

    def _on_progress(self, done: int, total: int, speed: float) -> None:
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
            eta = (total - done) / speed if speed > 0 else 0
            self.lbl_state.setText(
                f"{done/1048576:.0f} / {total/1048576:.0f} MB"
                f"　{F.human(speed)}/s"
                + (f"　还剩约 {int(eta//60)} 分 {int(eta%60)} 秒" if eta > 0 else ""))
        else:
            self.bar.setRange(0, 0)
            self.lbl_state.setText(
                f"已下载 {done/1048576:.0f} MB　{F.human(speed)}/s")

    def _on_done(self, failed: list, note: str = "") -> None:
        self.worker = None
        self._refresh_rows()
        if failed:
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            self.lbl_state.setText(
                f"{len(failed)} 项没下完：{'、'.join(failed)}。"
                "点「重试」会接着下，已下的不会重来")
            self.btn_go.setText("重试")
            self.btn_go.setEnabled(True)
            self.btn_later.setEnabled(True)
            self.btn_later.setText("以后再说")
            for _, cb, _ in self._rows:
                cb.setEnabled(True)
            # 重试时重新挑（缺什么下什么）
            try:
                self.btn_go.clicked.disconnect()
            except Exception:
                pass
            self.btn_go.clicked.connect(self._on_go)
            return

        if note:
            # 文件都在，栽在安装这一步（多半是 UAC 被关了或者编译失败）
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            self.lbl_state.setText(f"拼音输入法没装成：{note}")
            self.btn_go.setText("再试一次")
            self.btn_go.setEnabled(True)
            self.btn_later.setEnabled(True)
            self.btn_later.setText("先不用拼音")
            try:
                self.btn_go.clicked.disconnect()
            except Exception:
                pass
            self.btn_go.clicked.connect(self._on_go)
            return

        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        done_py = pinyin_installed()
        self.lbl_state.setText(
            "全部完成，现在可以用了。"
            "按 Win+空格 切到 Verbalkey 就能打拼音" if done_py
            else "全部完成，现在可以用了")
        self.btn_go.setText("开始使用")
        self.btn_go.setEnabled(True)
        self.btn_later.setVisible(False)
        try:
            self.btn_go.clicked.disconnect()
        except Exception:
            pass
        self.btn_go.clicked.connect(self.accept)

    def _on_later(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(3000)
            self.worker = None
        self.reject()

    def closeEvent(self, e) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(3000)
            self.worker = None
        super().closeEvent(e)


def prompt_if_needed(parent=None) -> bool:
    """缺必需模型时弹引导。返回 True 表示用户下完了（调用方应重载模型）。"""
    if not _missing_items(["vad", "asr"]):
        return False
    dlg = FirstRunDialog(parent)
    return dlg.exec() == QDialog.Accepted
