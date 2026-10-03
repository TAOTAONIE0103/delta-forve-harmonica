# -*- coding: utf-8 -*-
"""三角洲口琴 · 自动按键器（PySide6 版）

界面：Qt 6 / PySide6，带音符可视化（钢琴卷帘）、速度调整、键位映射、播放控制、
全局快捷键、曲谱库、诊断与日志。
按键输出：Windows SendInput / PostMessage / 串口硬件键盘（见 harp_core.py）。
核心逻辑全部来自 harp_core.py，与 tkinter 版 harmonica_auto.py 共用。
"""

import json
import os
import queue
import sys
import time

from PySide6.QtCore import (Qt, QRectF, QTimer, Signal, QSize)
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QBrush, QAction,
                           QKeySequence, QTextCursor, QTextCharFormat)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QPlainTextEdit, QComboBox, QSlider, QSpinBox,
    QCheckBox, QDialog, QListWidget, QListWidgetItem, QMessageBox, QSplitter,
    QGroupBox, QProgressBar, QFileDialog, QLineEdit, QSizePolicy, QFrame,
    QScrollArea, QToolButton, QStatusBar, QTextEdit)

from harp_core import *                     # noqa: F401,F403
from harp_core import _trim                 # noqa: F401

C = {
    "bg": "#0f141b", "panel": "#182029", "field": "#0b1118", "line": "#2a3646",
    "fg": "#e6edf3", "dim": "#8d9bad", "accent": "#f0b429", "accent2": "#2dd4bf",
    "danger": "#f87171", "note": "#2f6f8f", "note_sel": "#f0b429",
    "sharp": "#b4762a", "flat": "#3f6ea8", "play": "#2dd4bf",
}

QSS = """
QWidget { background: %(bg)s; color: %(fg)s;
          font-family: "Microsoft YaHei UI","Segoe UI"; font-size: 13px; }
QGroupBox { border: 1px solid %(line)s; border-radius: 8px; margin-top: 14px;
            padding: 10px 8px 8px 8px; background: %(panel)s; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                   color: %(accent)s; }
QPushButton { background: #22303f; border: 1px solid #33455a; border-radius: 7px;
              padding: 6px 12px; }
QPushButton:hover { background: #2b3b4d; }
QPushButton:disabled { color: #5b6b7e; background: #1a222c; }
QPushButton#primary { background: %(accent)s; color: #1b1305; font-weight: bold;
                      border: 1px solid #f5c551; padding: 9px 16px; }
QPushButton#primary:hover { background: #ffc63f; }
QPushButton#danger { background: #332023; border-color: #5c3030; color: %(danger)s; }
QPlainTextEdit, QListWidget, QLineEdit { background: %(field)s; border: 1px solid %(line)s;
                                        border-radius: 6px; selection-background-color: #2b4a54; }
QComboBox { background: #22303f; border: 1px solid #33455a; border-radius: 6px;
            padding: 4px 8px; }
QComboBox QAbstractItemView { background: %(panel)s; border: 1px solid %(line)s;
                              selection-background-color: #2b4a54; }
QSlider::groove:horizontal { height: 5px; background: #2b3745; border-radius: 3px; }
QSlider::handle:horizontal { width: 15px; margin: -6px 0; border-radius: 7px;
                             background: %(accent)s; }
QProgressBar { background: %(field)s; border: 1px solid %(line)s; border-radius: 5px;
               text-align: center; height: 16px; }
QProgressBar::chunk { background: %(accent2)s; border-radius: 4px; }
QCheckBox { color: %(dim)s; }
QStatusBar { color: %(dim)s; }
QSplitter::handle { background: %(line)s; }
""" % C


# ============================================================================
# 钢琴卷帘：把曲谱画成 8 条音轨（对应口琴的 8 个物理键）
# ============================================================================
class PianoRoll(QWidget):
    noteClicked = Signal(int)

    HEADER_W = 52
    RULER_H = 18
    LANES = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens = []
        self.scheme = "shift"
        self.bar_beats = 0.0
        self.position = 0.0          # 当前播放位置（拍）
        self.total_beats = 0.0
        self.selected = -1
        self.px_per_beat = 54.0
        self.scroll_beat = 0.0
        self.setMinimumHeight(210)
        self.setMouseTracking(True)

    # ---- 数据 ----
    def lane_of(self, tok):
        if tok["type"] != "note":
            return -1
        acc = tok.get("accidental", 0)
        if self.scheme == "slide":
            return resolve_note(tok["degree"], acc)[0]
        d, _a = to_shift_form(tok["degree"], acc)
        return 7 if d == 8 else max(0, d - 1)

    def accent_of(self, tok):
        acc = tok.get("accidental", 0)
        if self.scheme == "slide":
            return resolve_note(tok["degree"], acc)[1]
        _d, a = to_shift_form(tok["degree"], acc)
        return a

    def layout_notes(self):
        """返回 [(起始拍, 时值, 音轨, 升贬, 记号序号, token), ...]

        小节线本身不占音符位，但会按「小节停顿」在时间轴上留出一段空白，
        这样卷帘和实际播放的时间轴是一致的。
        """
        out = []
        beat = 0.0
        for i, tok in enumerate(self.tokens):
            if tok["type"] == "bar":
                beat += self.bar_beats
                continue
            if tok["type"] == "rest":
                beat += tok["beats"]
                continue
            out.append((beat, tok["beats"], self.lane_of(tok), self.accent_of(tok),
                        i, tok))
            beat += tok["beats"]
        return out

    def set_tokens(self, tokens, scheme, bar_beats=0.0):
        self.tokens = list(tokens)
        self.scheme = scheme
        self.bar_beats = bar_beats
        self.total_beats = (sum(t["beats"] for t in self.tokens
                                if t["type"] in ("note", "rest"))
                            + len([t for t in self.tokens if t["type"] == "bar"])
                            * bar_beats)
        if self.selected >= len(self.tokens):
            self.selected = -1
        self.update()

    def set_position(self, beat, playing):
        self.position = beat
        if playing:
            visible = max(2.0, self.width() / self.px_per_beat)
            if beat < self.scroll_beat or beat > self.scroll_beat + visible * 0.66:
                self.scroll_beat = max(0.0, beat - visible * 0.33)
        self.update()

    def set_selected(self, idx):
        self.selected = idx
        self.update()

    # ---- 坐标换算 ----
    def x_of(self, beat):
        return self.HEADER_W + (beat - self.scroll_beat) * self.px_per_beat

    def beat_of(self, x):
        return (x - self.HEADER_W) / self.px_per_beat + self.scroll_beat

    def lane_rect(self, lane, height):
        lane_h = max(12.0, (height - self.RULER_H) / float(self.LANES))
        top = self.RULER_H + lane * lane_h
        return top, lane_h

    # ---- 绘制 ----
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(C["field"]))
        lane_h = max(12.0, (h - self.RULER_H) / float(self.LANES))

        # 音轨底纹 + 标签
        for lane in range(self.LANES):
            top = self.RULER_H + lane * lane_h
            if lane % 2 == 0:
                p.fillRect(QRectF(self.HEADER_W, top, w - self.HEADER_W, lane_h),
                           QColor("#0d151d"))
            p.setPen(QPen(QColor(C["line"]), 1))
            p.drawLine(0, int(top + lane_h), w, int(top + lane_h))
            letter = SCALE_KEYS[lane] if lane < len(SCALE_KEYS) else "?"
            degree = lane + 1
            p.setPen(QColor(C["fg"] if lane == 0 else C["dim"]))
            f = QFont("Consolas", 10)
            p.setFont(f)
            p.drawText(QRectF(0, top, self.HEADER_W - 6, lane_h),
                       Qt.AlignRight | Qt.AlignVCenter, "%s  %d" % (letter, degree))

        # 拍线 / 小节线
        start = int(self.scroll_beat)
        end = int(self.scroll_beat + w / self.px_per_beat) + 1
        for beat in range(max(0, start - 1), end + 1):
            x = self.x_of(beat)
            if x < self.HEADER_W - 2:
                continue
            bar = (beat % 4 == 0)
            p.setPen(QPen(QColor("#3a4a5e" if bar else "#1e2a36"), 1))
            p.drawLine(int(x), self.RULER_H, int(x), h)
            if bar:
                p.setPen(QColor(C["dim"]))
                p.setFont(QFont("Consolas", 8))
                p.drawText(QRectF(x + 2, 0, 40, self.RULER_H),
                           Qt.AlignLeft | Qt.AlignVCenter, "%d" % (beat // 4 + 1))

        # 音符
        for beat, beats, lane, accent, _idx, tok in self.layout_notes():
            x1 = self.x_of(beat)
            x2 = self.x_of(beat + beats)
            if x2 < self.HEADER_W or x1 > w:
                continue
            top, lh = self.lane_rect(lane, h)
            rect = QRectF(max(x1, self.HEADER_W) + 1, top + 2,
                          max(3.0, x2 - max(x1, self.HEADER_W) - 2), lh - 4)
            if accent > 0:
                fill = QColor(C["sharp"])
            elif accent < 0:
                fill = QColor(C["flat"])
            else:
                fill = QColor(C["note"])
            if self.position >= beat and self.position < beat + beats:
                fill = QColor(C["play"])
            if _idx == self.selected:
                p.setPen(QPen(QColor(C["note_sel"]), 2))
            else:
                p.setPen(QPen(QColor("#0a1017"), 1))
            p.setBrush(QBrush(fill))
            p.drawRoundedRect(rect, 4, 4)
            if accent > 0:
                p.setPen(QColor("#ffe6b0"))
                p.setFont(QFont("Consolas", 8, QFont.Bold))
                p.drawText(rect, Qt.AlignCenter, "#")
            elif accent < 0:
                p.setPen(QColor("#cfe3ff"))
                p.setFont(QFont("Consolas", 8, QFont.Bold))
                p.drawText(rect, Qt.AlignCenter, "b")

        # 播放头
        px = self.x_of(self.position)
        if self.HEADER_W <= px <= w:
            p.setPen(QPen(QColor(C["accent2"]), 2))
            p.drawLine(int(px), self.RULER_H - 2, int(px), h)

        # 空谱提示
        if not self.tokens:
            p.setPen(QColor(C["dim"]))
            p.setFont(QFont("Microsoft YaHei UI", 11))
            p.drawText(QRectF(self.HEADER_W, 0, w - self.HEADER_W, h),
                       Qt.AlignCenter, "在下面输入简谱，这里会显示出音符")

    # ---- 交互 ----
    def mousePressEvent(self, ev):
        beat = self.beat_of(ev.position().x())
        lane = int((ev.position().y() - self.RULER_H)
                   // max(12.0, (self.height() - self.RULER_H) / float(self.LANES)))
        for b, beats, ln, _acc, idx, _tok in self.layout_notes():
            if ln == lane and b <= beat <= b + beats:
                self.selected = idx
                self.noteClicked.emit(idx)
                self.update()
                return
        self.selected = -1
        self.noteClicked.emit(-1)
        self.update()

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            self.px_per_beat = max(12.0, min(220.0, self.px_per_beat * 1.1
                                             if ev.angleDelta().y() > 0
                                             else self.px_per_beat / 1.1))
        else:
            self.scroll_beat = max(0.0, self.scroll_beat
                                   - ev.angleDelta().y() / self.px_per_beat)
        self.update()


# ============================================================================
# 通用对话框
# ============================================================================
class TextDialog(QDialog):
    def __init__(self, parent, title, text, subtitle=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 480)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        if subtitle:
            lab = QLabel(subtitle)
            lab.setStyleSheet("color:%s" % C["dim"])
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.edit = QPlainTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setPlainText(text)
        self.edit.setFont(QFont("Consolas", 10))
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        btn = QPushButton("复制全部")
        btn.clicked.connect(lambda: QApplication.clipboard().setText(self.edit.toPlainText()))
        row.addWidget(btn)
        row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)


class LibraryDialog(QDialog):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("曲谱库")
        self.resize(560, 380)
        self.setStyleSheet(QSS)
        lay = QVBoxLayout(self)
        self.listw = QListWidget()
        lay.addWidget(self.listw)
        row = QHBoxLayout()
        for text, fn in (("载入", self.load), ("覆盖保存", self.overwrite),
                         ("重命名", self.rename), ("删除", self.delete),
                         ("导出全部", self.export_all)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        self.listw.clear()
        for item in self.app.scores:
            self.listw.addItem("%s    %d 音    %s"
                               % (item.get("name", "未命名"), item.get("count", 0),
                                  item.get("updated", "")))

    def _sel(self):
        r = self.listw.currentRow()
        return r if 0 <= r < len(self.app.scores) else None

    def load(self):
        i = self._sel()
        if i is None:
            return
        self.app.load_score(self.app.scores[i])
        self.accept()

    def overwrite(self):
        i = self._sel()
        if i is None:
            return
        self.app.name_edit.setText(self.app.scores[i].get("name", ""))
        self.app.save_current()
        self.refresh()

    def rename(self):
        from PySide6.QtWidgets import QInputDialog
        i = self._sel()
        if i is None:
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称：",
                                        text=self.app.scores[i].get("name", ""))
        if ok and name:
            self.app.scores[i]["name"] = name
            self.app.persist_scores()
            self.refresh()

    def delete(self):
        i = self._sel()
        if i is None:
            return
        item = self.app.scores[i]
        if QMessageBox.question(self, "删除", "确定删除「%s」？" % item.get("name", "")) \
                == QMessageBox.Yes:
            self.app.scores.pop(i)
            self.app.persist_scores()
            self.refresh()

    def export_all(self):
        if not self.app.scores:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出全部曲谱",
                                              "harmonica-scores.json", "JSON (*.json)")
        if path:
            save_json(path, self.app.scores)


# ============================================================================
# 主窗口
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(load_json(SETTINGS_FILE, {}))
        self.scores = load_json(SCORES_FILE, [])
        if not isinstance(self.scores, list):
            self.scores = []

        self.tokens = []
        self.errors = []
        self.ignored = []
        self._syncing = False
        self.selected = -1
        self.scheme = self.settings.get("scheme", "slide")
        self.target_hwnd = None
        self.player = None
        self.hotkeys = None
        self.q = queue.Queue()
        self.kb = Keyboard()
        self.ime = ImeGuard()
        self.hid = SerialHid()
        self.kb.hid = self.hid
        self.own_pid = kernel32.GetCurrentProcessId()
        self._progress_done = 0.0
        self._progress_total = 0.0

        self.setWindowTitle(APP_TITLE)
        # 初始尺寸按屏幕可用区域来，别超出屏幕（否则底部按钮点不到）
        scr = QApplication.primaryScreen().availableGeometry()
        self.resize(max(1000, min(1500, scr.width() - 40)),
                    max(640, min(900, scr.height() - 60)))
        self.setMinimumSize(940, 600)
        self.setStyleSheet(QSS)

        self._build()

        # 定时器要在 _apply_settings 之前建好（设置初始化时就会触发回调）
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(40)
        self.parse_timer = QTimer(self)
        self.parse_timer.setSingleShot(True)
        self.parse_timer.timeout.connect(self.reparse)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.timeout.connect(self._write_draft)

        self._apply_settings()
        self._load_draft()

        try:
            with open(os.path.join(APP_DIR, "run.log"), "w", encoding="utf-8") as fh:
                fh.write("=== Qt 版启动 %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:                                # noqa: BLE001
            pass
        self.log("程序启动，本程序%s"
                 % ("以管理员权限运行" if is_process_elevated(self.own_pid)
                    else "以普通权限运行"))

        if self.settings.get("hotkeys", True):
            self._start_hotkeys()
        self._push_to_roll()

    # ------------------------------------------------------------------ 布局
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(10)

        left = QSplitter(Qt.Vertical)
        roll_box = QGroupBox("音符可视化（钢琴卷帘 · 8 条音轨 = 口琴 8 个键）")
        rl = QVBoxLayout(roll_box)
        self.roll = PianoRoll()
        self.roll.noteClicked.connect(self.on_roll_click)
        rl.addWidget(self.roll)
        tip = QLabel("滚轮 = 横向滚动，Ctrl+滚轮 = 缩放；点音符 = 选中，"
                     "选中后可用下面「曲谱」面板底部的「编辑」按钮处理")
        tip.setStyleSheet("color:%s" % C["dim"])
        tip.setWordWrap(True)
        rl.addWidget(tip)
        left.addWidget(roll_box)

        edit_box = QGroupBox("曲谱（简谱，空格分隔音符）")
        el = QVBoxLayout(edit_box)
        # 工具按钮分两行，否则这一行会把左栏的最小宽度撑到 900 多，
        # 右栏就被挤出窗口了
        row = QHBoxLayout()
        row.addWidget(QLabel("曲名"))
        self.name_edit = QLineEdit()
        self.name_edit.setMaximumWidth(180)
        row.addWidget(self.name_edit)
        b = QPushButton("保存到曲谱库")
        b.clicked.connect(self.save_current)
        row.addWidget(b)
        b = QPushButton("曲谱库…")
        b.clicked.connect(lambda: LibraryDialog(self, self).exec())
        row.addWidget(b)
        row.addStretch(1)
        el.addLayout(row)

        row2 = QHBoxLayout()
        for text, fn in (("导入", self.import_file),
                         ("导出 TXT", self.export_txt),
                         ("导出 JSON", self.export_json),
                         ("载入示例…", self.load_sample_menu)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        el.addLayout(row2)

        self.text = QPlainTextEdit()
        self.text.setFont(QFont("Consolas", 12))
        self.text.setPlaceholderText(
            "在这里输入或粘贴简谱，例如：\n1 1 5 5 6 6 5:2 | 4 4 3 3 2 2 1:2\n"
            "8 或 1' = 高音 1（逗号键）；#4 升半音；b3 降半音；0 休止；- 延长一拍")
        self.text.textChanged.connect(self._on_text_changed)
        self.text.cursorPositionChanged.connect(self._on_text_cursor)
        el.addWidget(self.text)

        edit_row = QHBoxLayout()
        edit_label = QLabel("编辑")
        edit_label.setStyleSheet("color:%s" % C["accent2"])
        edit_row.addWidget(edit_label)
        for text, tip, fn in (("←", "选中上一个记号", lambda: self.move_sel(-1)),
                              ("→", "选中下一个记号", lambda: self.move_sel(1)),
                              ("删除", "删除选中的记号", self.delete_sel),
                              ("休止", "插入休止符 0", self.insert_rest),
                              ("|", "插入小节线", self.insert_bar),
                              ("×2", "选中记号的时值翻倍", lambda: self.scale_sel(2)),
                              ("÷2", "选中记号的时值减半", lambda: self.scale_sel(0.5)),
                              ("撤销", "撤销上一步编辑", self.undo)):
            bb = QPushButton(text)
            bb.setToolTip(tip)
            bb.clicked.connect(fn)
            edit_row.addWidget(bb)
        edit_row.addStretch(1)
        el.addLayout(edit_row)

        # 解析状态单独一行，免得和上面那排按钮一起把左栏撑宽
        status_row = QHBoxLayout()
        status_row.addStretch(1)
        self.parse_label = QLabel("—")
        status_row.addWidget(self.parse_label)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:%s" % C["dim"])
        status_row.addWidget(self.stats_label)
        el.addLayout(status_row)
        left.addWidget(edit_box)
        left.setSizes([330, 470])
        root.addWidget(left, 3)

        # 右侧控件多，直接放会让整个窗口被撑得比屏幕还高（底部按钮被顶到屏幕外），
        # 所以套一层滚动区：窗口高度可以小于内容高度。
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        right_scroll.setWidget(self._build_right())
        right_scroll.setStyleSheet("QScrollArea{background:transparent;border:0}")
        root.addWidget(right_scroll, 2)

    def _build_right(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # 演奏
        box = QGroupBox("演奏")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 启动演奏")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.start)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.stop)
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            row.addWidget(b)
        bl.addLayout(row)

        self.now_label = QLabel("—")
        self.now_label.setFont(QFont("Consolas", 16, QFont.Bold))
        self.now_label.setStyleSheet("color:%s" % C["accent"])
        bl.addWidget(self.now_label)
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setValue(0)
        self.prog.setFormat("%p%")
        bl.addWidget(self.prog)
        self.time_label = QLabel("0 / 0    0.0s / 0.0s")
        self.time_label.setStyleSheet("color:%s; font-family:Consolas" % C["dim"])
        bl.addWidget(self.time_label)

        g = QGridLayout()
        self.sl_interval, self.lab_interval = self._slider(
            g, 0, "音符间隔", 80, 3000, 10, self._on_interval)
        self.sl_hold, self.lab_hold = self._slider(
            g, 1, "按住时长", 10, 400, 5, self._on_hold)
        self.sl_count, self.lab_count = self._slider(
            g, 2, "启动倒计时", 0, 10, 1, self._on_count)
        self.sl_bar, self.lab_bar = self._slider(
            g, 3, "小节停顿", 0, 3000, 50, self._on_bar)
        bl.addLayout(g)
        self.bpm_label = QLabel("")
        self.bpm_label.setStyleSheet("color:%s" % C["dim"])
        bl.addWidget(self.bpm_label)
        self.hold_label = QLabel("")
        self.hold_label.setStyleSheet("color:%s; font-family:Consolas" % C["dim"])
        self.hold_label.setWordWrap(True)
        bl.addWidget(self.hold_label)
        self.bar_label = QLabel("")
        self.bar_label.setStyleSheet("color:%s; font-family:Consolas" % C["dim"])
        self.bar_label.setWordWrap(True)
        bl.addWidget(self.bar_label)

        r2 = QGridLayout()
        self.chk_lock = QCheckBox("锁定目标窗口")
        self.chk_min = QCheckBox("启动时最小化")
        self.chk_top = QCheckBox("窗口置顶")
        self.chk_hot = QCheckBox("全局热键 F8/F9/F12")
        # 两行两列，挤在一行会把右侧面板的最小宽度撑到 500
        r2.addWidget(self.chk_lock, 0, 0)
        r2.addWidget(self.chk_min, 0, 1)
        r2.addWidget(self.chk_top, 1, 0)
        r2.addWidget(self.chk_hot, 1, 1)
        bl.addLayout(r2)
        self.chk_top.toggled.connect(self._toggle_topmost)
        self.chk_hot.toggled.connect(self._toggle_hotkeys)
        lay.addWidget(box)

        # 键位与注入
        box2 = QGroupBox("键位映射与注入")
        b2 = QGridLayout(box2)
        b2.addWidget(QLabel("音符方案"), 0, 0)
        self.cmb_scheme = QComboBox()
        for _k, label in NOTE_SCHEMES:
            self.cmb_scheme.addItem(label)
        self.cmb_scheme.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_scheme.setMinimumContentsLength(10)
        self.cmb_scheme.currentIndexChanged.connect(self._on_scheme)
        b2.addWidget(self.cmb_scheme, 0, 1, 1, 2)
        b2.addWidget(QLabel("注入方式"), 1, 0)
        self.cmb_mode = QComboBox()
        for _k, label in INJECT_MODES:
            self.cmb_mode.addItem(label)
        self.cmb_mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_mode.setMinimumContentsLength(10)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)
        b2.addWidget(self.cmb_mode, 1, 1, 1, 2)
        b2.addWidget(QLabel("串口"), 2, 0)
        self.port_edit = QLineEdit(self.settings.get("port", "COM3"))
        self.port_edit.setMaximumWidth(90)
        b2.addWidget(self.port_edit, 2, 1)
        b = QPushButton("连接硬件")
        b.clicked.connect(self.connect_hid)
        b2.addWidget(b, 2, 2)

        b2.addWidget(QLabel("目标窗口"), 3, 0)
        self.target_label = QLabel("自动（当前前台窗口）")
        self.target_label.setStyleSheet("color:%s" % C["dim"])
        b2.addWidget(self.target_label, 3, 1)
        rowt = QHBoxLayout()
        bb = QPushButton("选择…")
        bb.clicked.connect(self.choose_target)
        rowt.addWidget(bb)
        bb = QPushButton("清除")
        bb.clicked.connect(self.clear_target)
        rowt.addWidget(bb)
        b2.addLayout(rowt, 3, 2)

        self.elev_label = QLabel("")
        self.elev_label.setWordWrap(True)
        b2.addWidget(self.elev_label, 4, 0, 1, 3)

        self.chk_slide_reset = QCheckBox("半音后切回自然档")
        self.chk_anchor = QCheckBox("推键前移动鼠标")
        self.chk_slide_reset.setToolTip(
            "只对「点一下切档」方式有效：半音吹完立刻点中键切回自然档。")
        self.chk_anchor.setToolTip(
            "鼠标点击只会落在光标所在的窗口上。\n"
            "勾上则在每次推键前把光标挪到目标窗口中心，确保点的是游戏。")

        b2.addWidget(QLabel("推键方式"), 4, 0)
        self.cmb_slide_mode = QComboBox()
        self.cmb_slide_mode.addItem("按住（推荐）")
        self.cmb_slide_mode.addItem("点一下切档")
        self.cmb_slide_mode.setToolTip(
            "按住：鼠标键按住的整段时间里音才是升高的，程序会在按键前按下鼠标、"
            "吹完再松开。\n点一下切档：点一次保持住，再点中键回自然档。")
        b2.addWidget(self.cmb_slide_mode, 4, 1, 1, 2)

        row_sl = QHBoxLayout()
        row_sl.addWidget(QLabel("鼠标提前"))
        self.spin_lead = QSpinBox()
        self.spin_lead.setRange(0, 300)
        self.spin_lead.setSingleStep(5)
        self.spin_lead.setSuffix(" ms")
        self.spin_lead.setToolTip("鼠标比按键早按下的时间，确保游戏先识别到推键。")
        row_sl.addWidget(self.spin_lead)
        row_sl.addWidget(QLabel("延后"))
        self.spin_tail = QSpinBox()
        self.spin_tail.setRange(0, 300)
        self.spin_tail.setSingleStep(5)
        self.spin_tail.setSuffix(" ms")
        self.spin_tail.setToolTip("按键松开后鼠标再按住的时间。")
        row_sl.addWidget(self.spin_tail)
        row_sl.addStretch(1)
        b2.addLayout(row_sl, 5, 0, 1, 3)

        slide_row = QHBoxLayout()
        slide_row.addWidget(self.chk_slide_reset)
        slide_row.addWidget(self.chk_anchor)
        b2.addLayout(slide_row, 6, 0, 1, 3)

        self.elev_label = QLabel("")
        self.elev_label.setWordWrap(True)
        b2.addWidget(self.elev_label, 7, 0, 1, 3)
        self.elev_btn = QPushButton("以管理员身份重启")
        self.elev_btn.clicked.connect(self.restart_admin)
        b2.addWidget(self.elev_btn, 8, 0, 1, 3)
        lay.addWidget(box2)

        # 工具
        box3 = QGroupBox("工具")
        b3 = QGridLayout(box3)
        for i, (text, fn) in enumerate((("🔍 注入自检", self.self_test),
                                        ("🎵 试吹一遍", self.test_play),
                                        ("🎼 半音测试", self.accidental_test),
                                        ("🩺 诊断", self.show_diagnostics),
                                        ("📜 日志", self.show_log),
                                        ("⌨ 松开所有键", self.release_keys),
                                        ("格式说明", self.show_help))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            b3.addWidget(b, i // 2, i % 2)
        lay.addWidget(box3)

        # 虚拟键盘
        box4 = QGroupBox("虚拟键盘（点一下会真的发一个按键）")
        b4 = QVBoxLayout(box4)
        self.kb_buttons = {}
        grid = QGridLayout()
        for i in range(8):
            letter = SCALE_KEYS[i]
            btn = QPushButton("%s\n%d" % (letter, i + 1))
            btn.setFont(QFont("Consolas", 11, QFont.Bold))
            btn.clicked.connect(lambda _=False, idx=i: self.on_vkey(idx))
            grid.addWidget(btn, 0, i)
            self.kb_buttons[i] = btn
        b4.addLayout(grid)
        lay.addWidget(box4)
        lay.addStretch(1)

        self.hint = QLabel("第一次用：先「注入自检」确认本机能送键，再「试吹一遍」到游戏里验证。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:%s" % C["dim"]) 
        lay.addWidget(self.hint)

        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("待机")
        self.status_label.setStyleSheet("color:%s; font-weight:bold" % C["accent2"])
        self.statusBar().addPermanentWidget(self.status_label)
        return panel

    def _slider(self, grid, row, title, lo, hi, step, cb):
        grid.addWidget(QLabel(title), row, 0)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setPageStep(step * 5)
        if cb:
            s.valueChanged.connect(cb)
        grid.addWidget(s, row, 1)
        lab = QLabel("")
        lab.setMinimumWidth(74)
        lab.setStyleSheet("font-family:Consolas")
        grid.addWidget(lab, row, 2)
        return s, lab

    # ------------------------------------------------------------- 设置同步
    def _apply_settings(self):
        s = self.settings
        self.name_edit.setText(s.get("draft_name", "小星星"))
        self.sl_interval.setValue(int(s.get("interval", 1000)))
        self.sl_hold.setValue(int(s.get("hold_ms", 50)))
        self.sl_count.setValue(int(s.get("countdown", 3)))
        self.sl_bar.setValue(int(s.get("bar_pause_ms", 0)))
        self.cmb_slide_mode.setCurrentIndex(
            0 if s.get("slide_mode", "hold") == "hold" else 1)
        self.spin_lead.setValue(int(s.get("slide_lead_ms", 30)))
        self.spin_tail.setValue(int(s.get("slide_tail_ms", 30)))
        self.scheme = s.get("scheme", "slide")
        idx = [k for k, _v in NOTE_SCHEMES].index(self.scheme) \
            if self.scheme in [k for k, _v in NOTE_SCHEMES] else 1
        self.cmb_scheme.setCurrentIndex(idx)
        mode = s.get("mode", "both")
        keys = [k for k, _v in INJECT_MODES]
        self.cmb_mode.setCurrentIndex(keys.index(mode) if mode in keys else 0)
        self.chk_lock.setChecked(bool(s.get("lock_window", True)))
        self.chk_min.setChecked(bool(s.get("minimize", True)))
        self.chk_slide_reset.setChecked(bool(s.get("slide_reset", True)))
        self.chk_anchor.setChecked(bool(s.get("anchor_cursor", True)))
        # 「窗口置顶」「全局热键」的 setChecked 会触发 toggled → 回调里会 show()，
        # 而那时窗口还没显示，会导致首次启动窗口以最小化状态出现。这里屏蔽信号后手动应用。
        for cb, key, default in ((self.chk_top, "topmost", True),
                                 (self.chk_hot, "hotkeys", True)):
            cb.blockSignals(True)
            cb.setChecked(bool(s.get(key, default)))
            cb.blockSignals(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.chk_top.isChecked())
        self._on_interval(self.sl_interval.value())
        self._on_hold()
        self._on_count()
        self._on_bar()
        self._on_mode()
        self._on_scheme()
        self._update_elevation()

    def _load_draft(self):
        text = self.settings.get("draft_text") or SAMPLES["小星星"]
        self.text.blockSignals(True)
        self.text.setPlainText(text)
        self.text.blockSignals(False)
        self.reparse()

    def _collect_settings(self):
        s = dict(self.settings)
        s.update({
            "interval": self.sl_interval.value(),
            "hold_ms": self.sl_hold.value(),
            "countdown": self.sl_count.value(),
            "bar_pause_ms": self.sl_bar.value(),
            "slide_mode": "hold" if self.cmb_slide_mode.currentIndex() == 0 else "click",
            "slide_lead_ms": self.spin_lead.value(),
            "slide_tail_ms": self.spin_tail.value(),
            "mode": self.kb.mode,
            "scheme": self.scheme,
            "port": self.port_edit.text().strip(),
            "lock_window": self.chk_lock.isChecked(),
            "minimize": self.chk_min.isChecked(),
            "topmost": self.chk_top.isChecked(),
            "hotkeys": self.chk_hot.isChecked(),
            "slide_reset": self.chk_slide_reset.isChecked(),
            "anchor_cursor": self.chk_anchor.isChecked(),
            "draft_name": self.name_edit.text(),
            "draft_text": self.text.toPlainText(),
        })
        return s

    def _write_draft(self):
        self.settings = self._collect_settings()
        save_json(SETTINGS_FILE, self.settings)

    def hold_ms(self):
        return int(self.sl_hold.value())

    def _on_interval(self, _v=None):
        ms = self.sl_interval.value()
        self.lab_interval.setText("%.2f 秒" % (ms / 1000.0))
        self.bpm_label.setText("≈ %.1f BPM（每拍 %.2f 秒）" % (60000.0 / ms, ms / 1000.0))
        self._update_hold_hint()
        self.update_stats()
        self._draft_timer.start(600)

    def _on_hold(self, _v=None):
        ms = self.sl_hold.value()
        self.lab_hold.setText("%.2f 秒" % (ms / 1000.0) if ms >= 1000
                              else "%d 毫秒" % ms)
        self._update_hold_hint()
        self._draft_timer.start(600)

    def _update_hold_hint(self):
        """把按住时长也做成可视化的：秒数 + 占一拍的比例 + 长条。"""
        ms = self.sl_hold.value()
        iv = max(1, self.sl_interval.value())
        pct = 100.0 * ms / iv
        filled = max(1, min(20, int(round(pct / 5.0))))
        self.hold_label.setText(
            "按住 %.2f 秒 / 间隔 %.2f 秒 = 占 %.0f%%   %s"
            % (ms / 1000.0, iv / 1000.0, pct,
               "█" * filled + "·" * (20 - filled)))

    def _on_count(self, _v=None):
        v = self.sl_count.value()
        self.lab_count.setText(("%d 秒" % v) if v else "不倒数")
        self._draft_timer.start(600)

    def _on_bar(self, _v=None):
        """小节线（|）后面的停顿，可自己调。"""
        ms = self.sl_bar.value()
        bars = len([t for t in self.tokens if t["type"] == "bar"])
        self.lab_bar.setText("%.2f 秒" % (ms / 1000.0) if ms else "不停顿")
        self.bar_label.setText(
            "每个 | 之后停 %.2f 秒（本曲共 %d 个小节线，整曲多 %.1f 秒）"
            % (ms / 1000.0, bars, bars * ms / 1000.0))
        self.update_stats()
        self._draft_timer.start(600)

    def bar_pause_sec(self):
        return self.sl_bar.value() / 1000.0

    def slide_opts(self):
        return {
            "slide_mode": "hold" if self.cmb_slide_mode.currentIndex() == 0 else "click",
            "slide_lead_ms": self.spin_lead.value(),
            "slide_tail_ms": self.spin_tail.value(),
        }

    def _on_scheme(self, idx=None):
        if not isinstance(idx, int) or not (0 <= idx < len(NOTE_SCHEMES)):
            idx = self.cmb_scheme.currentIndex()
        if idx < 0:
            idx = 0
        self.scheme = NOTE_SCHEMES[idx][0]
        if self.scheme == "slide":
            self.set_hint("半音阶方案：8 个键 z x c v b n m , ，半音用「低半音键 + 鼠标右键(升调档)」"
                          "吹出，奏完自动切回中键自然档。需要游戏在前台。")
        else:
            self.set_hint("Shift 方案：z x c v b n m 为 1-7，Shift+字母为升调，逗号是高音 1。")
        self._push_to_roll()
        self._draft_timer.start(600)

    def _on_mode(self, _v=None):
        self.kb.mode = INJECT_MODES[self.cmb_mode.currentIndex()][0]

    def _toggle_topmost(self, on):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if self.isVisible():          # 窗口已经显示时才需要重新 show 让改动生效
            self.show()

    def _update_elevation(self):
        if is_process_elevated(self.own_pid):
            self.elev_label.setText("本程序：管理员权限 ✔")
            self.elev_label.setStyleSheet("color:%s" % C["accent2"])
            self.elev_btn.hide()
        else:
            self.elev_label.setText("本程序：普通权限（游戏若以管理员运行则收不到按键）")
            self.elev_label.setStyleSheet("color:%s" % C["accent"])
            self.elev_btn.show()

    def set_hint(self, text):
        self.hint.setText(text)

    def set_state(self, text, color=None):
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color:%s; font-weight:bold"
                                        % (color or C["accent2"]))

    # ------------------------------------------------------------- 曲谱处理
    def _on_text_changed(self):
        self.parse_timer.start(180)

    def reparse(self):
        self.tokens, self.errors, self.ignored = parse_full(self.text.toPlainText())
        play = playable(self.tokens)
        beats = total_beats(self.tokens)
        secs = beats * self.sl_interval.value() / 1000.0
        self._progress_total = secs
        if self.errors:
            self.parse_label.setText("⚠ %d 处无法识别" % len(self.errors))
            self.parse_label.setStyleSheet("color:%s" % C["danger"])
            self.log("解析到 %d 处无法识别：%s" % (
                len(self.errors), "、".join("第%d行「%s」" % (l, w)
                                           for l, w, _m in self.errors[:5])))
        elif not play:
            self.parse_label.setText("曲谱为空")
            self.parse_label.setStyleSheet("color:%s" % C["accent"])
        else:
            text = "✔ 解析正常"
            if self.ignored:
                text += "（忽略 %d 处非音符文字）" % len(self.ignored)
            self.parse_label.setText(text)
            self.parse_label.setStyleSheet("color:%s" % C["accent2"])
        self.update_stats()
        if self.selected >= len(self.tokens):
            self.selected = -1
        self._push_to_roll()
        self._draft_timer.start(700)

    def update_stats(self):
        play = playable(self.tokens)
        beats = total_beats(self.tokens)
        secs = estimate_seconds(self.tokens, self.sl_interval.value() / 1000.0,
                                self.bar_pause_sec())
        self.stats_label.setText("%d 音 · %d 休止 · %s 拍 ≈ %.1f 秒"
                                 % (len([t for t in play if t["type"] == "note"]),
                                    len([t for t in play if t["type"] == "rest"]),
                                    _trim(beats), secs))
        self.time_label.setText("0 / %d    0.0s / %.1fs" % (len(play), secs))

    def _push_to_roll(self):
        bar_beats = (self.sl_bar.value() / 1000.0
                     / max(0.001, self.sl_interval.value() / 1000.0))
        self.roll.set_tokens(self.tokens, self.scheme, bar_beats)
        self.roll.set_selected(self.selected)

    def _commit_tokens(self, tokens):
        self.text.blockSignals(True)
        self.text.setPlainText(serialize_tokens(tokens))
        self.text.blockSignals(False)
        self.reparse()

    def on_roll_click(self, idx):
        """在卷帘上点音符 → 曲谱里对应的那段高亮。"""
        self.selected = idx
        if 0 <= idx < len(self.tokens):
            tok = self.tokens[idx]
            self.set_hint("选中：%s  →  按键 %s（%s）"
                          % (token_label(tok), token_key_text(tok, self.scheme),
                             SOLFEGE.get(tok["degree"], "")))
            self._select_in_text(tok)
        else:
            self._clear_text_selection()
        self.roll.set_selected(idx)

    def _select_in_text(self, tok):
        """把记号在曲谱里的那一段高亮出来。

        用 extraSelections 而不是普通选区：普通选区在控件失焦时几乎看不见，
        而从卷帘点过来时焦点并不在文本框上。
        """
        start, end = tok.get("start"), tok.get("end")
        if start is None or end is None:
            return
        doc_len = len(self.text.toPlainText())
        if start >= doc_len:
            return
        start = max(0, min(start, doc_len))
        end = max(0, min(end, doc_len))
        sel = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#4a3a10"))
        fmt.setForeground(QColor("#ffd479"))
        fmt.setFontWeight(QFont.Bold)
        sel.format = fmt
        cur = QTextCursor(self.text.document())
        cur.setPosition(start)
        cur.setPosition(end, QTextCursor.KeepAnchor)
        sel.cursor = cur
        self._syncing = True
        self.text.setExtraSelections([sel])
        caret = self.text.textCursor()
        caret.setPosition(start)
        self.text.setTextCursor(caret)      # 只为了滚动到该处，不带选区
        self.text.ensureCursorVisible()
        self._syncing = False

    def _clear_text_selection(self):
        self._syncing = True
        self.text.setExtraSelections([])
        self._syncing = False

    def _on_text_cursor(self):
        """反向同步：光标落到哪个记号上，卷帘里就选中它。"""
        if getattr(self, "_syncing", False):
            return
        pos = self.text.textCursor().position()
        idx = -1
        for i, tok in enumerate(self.tokens):
            s, e = tok.get("start"), tok.get("end")
            if s is not None and s <= pos <= e:
                idx = i
                break
        if idx != self.selected:
            self.selected = idx
            self.roll.set_selected(idx)

    def move_sel(self, step):
        i = self.selected + step
        i = max(-1, min(len(self.tokens) - 1, i))
        self.selected = i
        self.roll.set_selected(i)

    def delete_sel(self):
        if not (0 <= self.selected < len(self.tokens)):
            self.set_hint("先在卷帘上点选一个音符")
            return
        tokens = [dict(t) for t in self.tokens]
        tokens.pop(self.selected)
        self.selected = min(self.selected, len(tokens) - 1)
        self._commit_tokens(tokens)

    def insert_rest(self):
        self._insert({"type": "rest", "degree": 0, "accidental": 0, "sharp": False,
                      "octave": 0, "beats": 1.0})

    def insert_bar(self):
        self._insert({"type": "bar", "degree": 0, "accidental": 0, "sharp": False,
                      "octave": 0, "beats": 0.0})

    def _insert(self, tok):
        tokens = [dict(t) for t in self.tokens]
        at = self.selected if 0 <= self.selected < len(tokens) else len(tokens)
        tokens.insert(at, tok)
        self.selected = at + 1 if at + 1 < len(tokens) else -1
        self._commit_tokens(tokens)

    def scale_sel(self, factor):
        if not (0 <= self.selected < len(self.tokens)) or \
                self.tokens[self.selected]["type"] == "bar":
            self.set_hint("先在卷帘上点选一个音符或休止符")
            return
        tokens = [dict(t) for t in self.tokens]
        tokens[self.selected]["beats"] = max(
            0.125, min(16.0, tokens[self.selected]["beats"] * factor))
        self._commit_tokens(tokens)

    def undo(self):
        self.text.undo()

    # ------------------------------------------------------------- 曲谱库
    def persist_scores(self):
        save_json(SCORES_FILE, self.scores)

    def save_current(self):
        self.reparse()
        name = (self.name_edit.text() or "").strip() or "未命名曲谱"
        self.name_edit.setText(name)
        entry = {"name": name, "text": self.text.toPlainText(),
                 "count": len([t for t in self.tokens if t["type"] == "note"]),
                 "updated": time.strftime("%m-%d %H:%M")}
        for item in self.scores:
            if item.get("name") == name:
                item.update(entry)
                break
        else:
            self.scores.insert(0, entry)
        self.persist_scores()
        self.set_hint("已保存到曲谱库：%s" % name)

    def load_score(self, item):
        self.name_edit.setText(item.get("name", ""))
        self.text.blockSignals(True)
        self.text.setPlainText(item.get("text", ""))
        self.text.blockSignals(False)
        self.selected = -1
        self.reparse()
        self.set_hint("已载入：%s" % item.get("name", ""))

    def load_sample_menu(self):
        from PySide6.QtWidgets import QInputDialog
        names = list(SAMPLES.keys())
        name, ok = QInputDialog.getItem(self, "载入示例", "选择示例曲谱：", names, 0, False)
        if ok and name:
            self.name_edit.setText(name)
            self.text.blockSignals(True)
            self.text.setPlainText(SAMPLES[name])
            self.text.blockSignals(False)
            self.selected = -1
            self.reparse()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入曲谱", "", "曲谱文件 (*.txt *.json *.jianpu);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = fh.read()
        except Exception as exc:                         # noqa: BLE001
            QMessageBox.critical(self, APP_TITLE, "读取失败：%r" % (exc,))
            return
        base = os.path.splitext(os.path.basename(path))[0]
        if path.lower().endswith(".json"):
            try:
                data = json.loads(raw)
            except Exception:                            # noqa: BLE001
                data = None
            if isinstance(data, list):
                if QMessageBox.question(self, APP_TITLE,
                                        "这是曲谱库文件，共 %d 份。\n合并到本地曲谱库吗？"
                                        % len(data)) == QMessageBox.Yes:
                    add = [it for it in data if isinstance(it, dict) and it.get("text")]
                    self.scores = add + self.scores
                    self.persist_scores()
                    self.set_hint("已合并 %d 份曲谱" % len(add))
                    return
            elif isinstance(data, dict) and isinstance(data.get("text"), str):
                self.name_edit.setText(data.get("name") or base)
                self.text.blockSignals(True)
                self.text.setPlainText(data["text"])
                self.text.blockSignals(False)
                self.reparse()
                return
        self.name_edit.setText(base)
        self.text.blockSignals(True)
        self.text.setPlainText(raw)
        self.text.blockSignals(False)
        self.reparse()
        self.set_hint("已导入 %s" % os.path.basename(path))

    def export_txt(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 TXT", (self.name_edit.text() or "曲谱") + ".txt", "文本 (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text.toPlainText())
            self.set_hint("已导出 %s" % path)

    def export_json(self):
        name = self.name_edit.text() or "曲谱"
        path, _ = QFileDialog.getSaveFileName(self, "导出 JSON", name + ".json",
                                              "JSON (*.json)")
        if path:
            save_json(path, {"name": name, "text": self.text.toPlainText()})
            self.set_hint("已导出 %s" % path)

    # ------------------------------------------------------------- 演奏
    def start(self):
        if self.player and self.player.is_alive():
            return
        tokens, errors = parse_score(self.text.toPlainText())
        if not playable(tokens):
            QMessageBox.warning(self, APP_TITLE, "曲谱为空，先写点音符吧。")
            return
        if errors and QMessageBox.question(
                self, APP_TITLE, "曲谱里有 %d 处无法识别的记号，会被跳过。\n仍要开始？"
                % len(errors)) != QMessageBox.Yes:
            return
        self._launch_play(tokens, "演奏曲谱")

    def toggle_pause(self):
        if self.player and self.player.is_alive():
            self.player.toggle_pause()

    def stop(self):
        if self.player and self.player.is_alive():
            self.player.stop()
        self.kb.release_all()
        self.ime.release()

    def test_play(self):
        if self.player and self.player.is_alive():
            return
        self._launch_play(TEST_TOKENS, "试吹：8 个键依次发送")

    def accidental_test(self):
        """自然音与对应半音交替，用来判断游戏里升调到底怎么触发。"""
        if self.player and self.player.is_alive():
            return
        self.set_hint("半音测试：先吹 1 2 4 5 四个自然音，再用同一个键吹它们的升调。"
                      "如果自然音响了、升调没响或没变调，就是「音符方案」选错了。")
        self._launch_play(TEST_ACCIDENTAL, "半音测试")

    def _launch_play(self, tokens, tip):
        self.kb.mode = INJECT_MODES[self.cmb_mode.currentIndex()][0]
        self.set_hint(tip + "（倒计时期间请切到游戏窗口并打开口琴）")
        self.log("开始：%s 方案=%s 注入=%s 间隔=%.2fs 按住=%dms"
                 % (tip, self.scheme, self.kb.mode,
                    self.sl_interval.value() / 1000.0, self.hold_ms()))
        if self.chk_min.isChecked():
            self.showMinimized()
        self.player = Player(self.q.put, tokens, {
            "interval": self.sl_interval.value() / 1000.0,
            "hold_ms": self.hold_ms(),
            "countdown": int(self.sl_count.value()),
            "lock_window": self.chk_lock.isChecked(),
            "own_pid": self.own_pid,
            "keyboard": self.kb,
            "ime": self.ime,
            "target_hwnd": self.target_hwnd,
            "scheme": self.scheme,
            "slide_reset": self.chk_slide_reset.isChecked(),
            "anchor_cursor": self.chk_anchor.isChecked(),
            "slide_mode": "hold" if self.cmb_slide_mode.currentIndex() == 0 else "click",
            "slide_lead_ms": self.spin_lead.value(),
            "slide_tail_ms": self.spin_tail.value(),
            "bar_pause": self.sl_bar.value() / 1000.0,
        })
        self.player.start()
        self.set_state("倒计时…", C["accent"])
        self._set_buttons(True, False)

    def _set_buttons(self, playing, paused):
        self.btn_start.setEnabled(not (playing and not paused))
        self.btn_start.setText("▶ 继续演奏" if (playing and paused) else
                               ("▶ 演奏中…" if playing else "▶ 启动演奏"))
        self.btn_pause.setEnabled(playing and not paused)
        self.btn_stop.setEnabled(playing)

    def release_keys(self):
        self.kb.release_all()
        self.set_hint("已发送 Shift 与所有字母键的抬起事件")

    # ------------------------------------------------------------- 消息泵
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass

    def _handle(self, kind, payload):
        if kind == "log":
            self.log(str(payload))
        elif kind == "countdown":
            self.set_state("%.1f 秒后开始…" % payload, C["accent"])
        elif kind == "target":
            self.set_state("演奏中")
        elif kind == "note":
            idx, total, tok, done_s, all_s, done_beats = payload
            self.roll.set_position(done_beats, True)
            self.now_label.setText("%s   %s" % (token_label(tok),
                                                token_key_text(tok, self.scheme)))
            self.prog.setValue(int(1000 * (idx + 1) / max(1, total)))
            self.time_label.setText("%d / %d    %.1fs / %.1fs"
                                    % (idx + 1, total, done_s, all_s))
            if self._playing_score:
                self._select_in_text(tok)      # 演奏到哪，曲谱里就高亮到哪
        elif kind == "bar":
            done_s, all_s = payload
            self.now_label.setText("|  小节停顿")
            self.time_label.setText("小节停顿    %.1fs / %.1fs" % (done_s, all_s))
        elif kind == "paused":
            self.set_state("已暂停", C["accent"])
            self._set_buttons(True, True)
        elif kind == "resumed":
            self.set_state("演奏中")
            self._set_buttons(True, False)
        elif kind == "finished":
            self.set_state("演奏完成")
            self.now_label.setText("结束")
            self.prog.setValue(1000)
        elif kind == "error":
            self.set_state("已停止", C["danger"])
            self.set_hint(str(payload))
            self.log("停止：%s" % payload)
            QMessageBox.warning(self, APP_TITLE, str(payload))
        elif kind == "hotkey":
            if payload == 1:
                if self.player and self.player.is_alive():
                    self.toggle_pause()
                else:
                    self.start()
            elif payload == 2:
                self.stop()
            elif payload == 3:
                self.stop()
                self.kb.release_all()
        elif kind == "ended":
            self.ime.release()
            self.kb.release_all()
            self._set_buttons(False, False)
            if self.status_label.text() != "演奏完成":
                self.set_state("待机")
            self.showNormal()

    # ------------------------------------------------------------- 热键
    def _start_hotkeys(self):
        if self.hotkeys:
            return
        self.hotkeys = HotkeyThread(lambda hid: self.q.put(("hotkey", hid)))
        self.hotkeys.start()
        self.hotkeys.ready.wait(0.5)
        self.log("全局热键已启用：F8 启动/暂停、F9 停止、F12 紧急停止")

    def _toggle_hotkeys(self, on):
        if on:
            self._start_hotkeys()
        elif self.hotkeys:
            self.hotkeys.stop()
            self.hotkeys = None

    # ------------------------------------------------------------- 诊断与工具
    def log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        self._log_lines = getattr(self, "_log_lines", [])
        self._log_lines.append(line)
        if len(self._log_lines) > 800:
            del self._log_lines[:200]
        try:
            with open(os.path.join(APP_DIR, "run.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:                                # noqa: BLE001
            pass
        if getattr(self, "_log_dialog", None):
            self._log_dialog.edit.appendPlainText(line)

    def show_log(self):
        dlg = TextDialog(self, "运行日志",
                         "\n".join(getattr(self, "_log_lines", [])),
                         "演奏过程、目标窗口、按键与失败原因都记在这里，"
                         "同时写入程序目录的 run.log。")
        self._log_dialog = dlg
        dlg.finished.connect(lambda _r: setattr(self, "_log_dialog", None))
        dlg.exec()

    def show_diagnostics(self):
        hwnd = self.target_hwnd or foreground_window()
        findings, advice = diagnose_window(hwnd)
        self.log("诊断目标窗口：%s" % describe_window(hwnd))
        for line in advice:
            self.log("诊断建议：%s" % line)
        TextDialog(self, "目标窗口诊断",
                   "【检测结果】\n  " + "\n  ".join(findings)
                   + "\n\n【结论与建议】\n  " + "\n  ".join(advice),
                   "判断软件注入是不是被系统权限或反作弊挡住了。").exec()

    def show_help(self):
        TextDialog(self, "曲谱格式与使用步骤", HELP_TEXT.replace("【", "\n【")).exec()

    def choose_target(self):
        wins = [w for w in list_windows() if w["pid"] != self.own_pid]
        wins.sort(key=lambda w: w["title"].lower())
        if not wins:
            self.set_hint("没有找到可选的窗口")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("选择目标窗口")
        dlg.resize(620, 420)
        dlg.setStyleSheet(QSS)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("选中游戏窗口（口琴界面所属的那个）"))
        lw = QListWidget()
        for w in wins:
            path = process_path(w["pid"])
            lw.addItem("%s    [%s]  PID %d"
                       % (w["title"], os.path.basename(path) if path else "?", w["pid"]))
        lay.addWidget(lw)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("确定")
        ok.clicked.connect(dlg.accept)
        row.addWidget(ok)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        lay.addLayout(row)
        if dlg.exec() == QDialog.Accepted and lw.currentRow() >= 0:
            w = wins[lw.currentRow()]
            self.target_hwnd = w["hwnd"]
            self.target_label.setText("%s  [PID %d]" % (w["title"][:36], w["pid"]))
            self.log("已选定目标窗口：%s" % describe_window(w["hwnd"]))

    def clear_target(self):
        self.target_hwnd = None
        self.target_label.setText("自动（当前前台窗口）")

    def connect_hid(self):
        try:
            self.hid.open(self.port_edit.text().strip() or "COM3")
            self.kb.hid = self.hid
            self.set_hint("硬件键盘已连接：%s" % self.hid.port)
            self.log("硬件键盘已连接：%s" % self.hid.port)
        except Exception as exc:                         # noqa: BLE001
            self.log("硬件连接失败：%r" % (exc,))
            QMessageBox.critical(self, APP_TITLE,
                                 "连接串口失败：%r\n\n请确认设备已插好，"
                                 "并在设备管理器里确认端口号（例如 COM3）。" % (exc,))

    def restart_admin(self):
        if QMessageBox.question(self, APP_TITLE,
                                "将以管理员身份重新启动本程序。\n继续？") == QMessageBox.Yes:
            if relaunch_as_admin():
                self.close()
            else:
                QMessageBox.warning(self, APP_TITLE, "提权被取消或失败。")

    def on_vkey(self, lane):
        """点虚拟键盘：发一个真实按键（方便手动试音）。"""
        degree = 8 if lane == 7 else lane + 1
        try:
            self.kb.tap_letter(degree, False, self.hold_ms())
        except Exception as exc:                         # noqa: BLE001
            self.set_hint("发键失败：%r" % (exc,))

    def self_test(self):
        """开一个输入框，用真实注入打字进去，验证本机能不能送键。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("注入自检")
        dlg.resize(560, 260)
        dlg.setStyleSheet(QSS)
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("下面输入框会自动收到 zxcvbnmZXCVBNM；\n"
                             "两边一致 = 注入正常，为空 = 本机拦截了注入按键。"))
        edit = QLineEdit()
        edit.setFont(QFont("Consolas", 13))
        lay.addWidget(edit)
        result = QLabel("准备中…")
        result.setWordWrap(True)
        lay.addWidget(result)
        close = QPushButton("关闭")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close)

        hwnd = int(dlg.winId())
        guard = ImeGuard()
        diag = []
        tries = [0]

        def phase1():
            ok = force_foreground(hwnd)
            diag.append("取得前台=%s" % ok)
            if not ok:
                result.setText("拿不到前台焦点，自检中止。 " + " ".join(diag))
                return
            edit.setFocus()
            guard.engage(hwnd)
            diag.append("焦点窗口=%s 解除=%d 个"
                        % (getattr(guard, "focus_hwnd", None), len(guard.detached)))
            QTimer.singleShot(250, phase2)

        def phase2():
            diag.append("发送前仍在前台=%s" % (foreground_window() == hwnd))
            self.kb.mode = INJECT_MODES[self.cmb_mode.currentIndex()][0]
            for i in range(1, 8):
                self.kb.tap_letter(i, False, 30)
            for i in range(1, 8):
                self.kb.tap_letter(i, True, 30)
            QTimer.singleShot(250, phase3)

        def phase3():
            got = edit.text()
            # 偶尔会被别的窗口抢走焦点导致一个字都没收到，这里自动重试两次
            if not got and tries[0] < 2:
                tries[0] += 1
                diag.append("第 %d 次没收到，重试" % tries[0])
                edit.clear()
                QTimer.singleShot(350, phase1)
                return
            guard.release()
            diag.append("收到=%r" % got)
            self.last_self_test = (got == "zxcvbnmZXCVBNM", got)
            if got == "zxcvbnmZXCVBNM":
                result.setText("✔ 注入正常。\n" + " ".join(diag))
                result.setStyleSheet("color:%s" % C["accent2"])
            elif not got:
                result.setText("✘ 一个字都没收到。先手动把输入法切成英文再试一次；\n"
                               "仍为空就点「诊断」看是不是被反作弊拦了。\n"
                               + " ".join(diag))
                result.setStyleSheet("color:%s" % C["danger"])
            else:
                result.setText("△ 收到 %r，与预期不一致。\n%s" % (got, " ".join(diag)))
                result.setStyleSheet("color:%s" % C["accent"])
            self.log("注入自检：%s" % " ".join(diag))

        QTimer.singleShot(400, phase1)
        dlg.exec()

    # ------------------------------------------------------------- 关闭
    def closeEvent(self, event):
        try:
            if self.player and self.player.is_alive():
                self.player.stop()
                self.player.join(timeout=1.0)
        except Exception:                                # noqa: BLE001
            pass
        self.kb.release_all()
        self.ime.release()
        try:
            self.hid.close()
        except Exception:                                # noqa: BLE001
            pass
        if self.hotkeys:
            self.hotkeys.stop()
        self._write_draft()
        save_json(SCORES_FILE, self.scores)
        event.accept()


def main():
    QApplication.setApplicationName(APP_TITLE)
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
