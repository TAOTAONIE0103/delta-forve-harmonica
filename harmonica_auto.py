# -*- coding: utf-8 -*-
"""三角洲口琴 · 自动按键器（tkinter 版界面）

核心逻辑（简谱解析 / 键位映射 / 注入 / 诊断 / 演奏调度）都在 harp_core.py，
这里只保留 tkinter 界面。PySide6 版界面见 harp_qt.py。
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox

from harp_core import *          # noqa: F401,F403
from harp_core import _trim      # noqa: F401

# ============================================================================
# 7. 界面
# ============================================================================
class App(object):
    def __init__(self, root):
        self.root = root
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(load_json(SETTINGS_FILE, {}))
        self.scores = load_json(SCORES_FILE, [])
        if not isinstance(self.scores, list):
            self.scores = []

        self.tokens = []
        self.errors = []
        self.ignored = []
        self.selected = -1
        self.chip_buttons = []
        self.undo_stack = []
        self.player = None
        self.hotkeys = None
        self.q = queue.Queue()
        self.kb = Keyboard()
        self.ime = ImeGuard()
        self.own_pid = kernel32.GetCurrentProcessId()
        self._closing = False
        self._typing_job = None
        self._draft_job = None
        self._progress = 0.0
        self._playing_score = True
        self.last_self_test = None
        self.scheme = "shift"
        self.target_hwnd = None
        self.hid = SerialHid()
        self.kb.hid = self.hid
        self.log_lines = []
        self.log_text = None
        self.log_win = None
        self.diag_win = None

        root.title(APP_TITLE)
        root.configure(bg=C_BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1240, sw - 60), min(880, sh - 80)
        root.geometry("%dx%d+%d+%d" % (max(960, w), max(700, h),
                                       max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        root.minsize(960, 680)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_style()
        self._build_ui()
        self._apply_settings_to_ui()

        if self.settings.get("topmost"):
            root.attributes("-topmost", True)
        if self.settings.get("hotkeys"):
            self._start_hotkeys()

        text = self.settings.get("draft_text") or SAMPLES["小星星"]
        self.name_var.set(self.settings.get("draft_name") or "小星星")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.reparse(select_reset=True)

        try:
            with open(os.path.join(APP_DIR, "run.log"), "w", encoding="utf-8") as fh:
                fh.write("=== 三角洲口琴 自动按键器 启动 %s ===\n"
                         % time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:                                # noqa: BLE001
            pass
        self.log("程序启动，本程序%s"
                 % ("以管理员权限运行" if is_process_elevated(
                     kernel32.GetCurrentProcessId()) else "以普通权限运行"))

        self.root.after(40, self._pump)
        self.root.after(200, self._first_tip)

    # ---------------- 样式 ----------------
    def _build_style(self):
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*Background", C_BG)
        self.root.option_add("*Foreground", C_FG)

    def _panel(self, parent, title=None):
        """面板外框。标题占第 0 行，内部一律用 grid，避免和 pack 混用。"""
        outer = tk.Frame(parent, bg=C_PANEL, highlightbackground=C_LINE,
                         highlightthickness=1, bd=0)
        if title:
            tk.Label(outer, text=title, bg=C_PANEL, fg=C_ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=0, column=0, columnspan=8, sticky="w", padx=12, pady=(9, 4))
        return outer

    def _btn(self, parent, text, cmd, bg="#22303f", fg=C_FG, width=None):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground="#2b3b4d", activeforeground=fg,
                      relief="flat", bd=0, padx=10, pady=5, cursor="hand2",
                      highlightthickness=0)
        if width:
            b.configure(width=width)
        return b

    # ---------------- 布局 ----------------
    def _build_ui(self):
        root = self.root

        # 顶栏
        top = tk.Frame(root, bg=C_BG)
        top.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(top, text="▲ 三角洲口琴 · 自动按键器", bg=C_BG, fg=C_ACCENT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        tk.Label(top, text="  按曲谱自动向游戏窗口发送按键（口琴 8 键 Z X C V B N M , ）",
                 bg=C_BG, fg=C_DIM).pack(side="left")
        self.status_var = tk.StringVar(value="待机")
        self.status_lbl = tk.Label(top, textvariable=self.status_var, bg=C_BG,
                                   fg=C_ACCENT2, font=("Microsoft YaHei UI", 11, "bold"))
        self.status_lbl.pack(side="right")

        body = tk.Frame(root, bg=C_BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        body.columnconfigure(0, weight=3, uniform="c")
        body.columnconfigure(1, weight=2, uniform="c")
        body.rowconfigure(0, weight=1)

        self._build_score_panel(body)
        self._build_play_panel(body)
        self._build_bottom(root)

    # ---- ① 曲谱 ----
    def _build_score_panel(self, parent):
        panel = self._panel(parent, "① 曲谱")
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        panel.rowconfigure(5, weight=1)
        panel.columnconfigure(0, weight=1)

        # 第一行：曲名 + 保存
        bar = tk.Frame(panel, bg=C_PANEL)
        bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        tk.Label(bar, text="曲名", bg=C_PANEL, fg=C_DIM).pack(side="left")
        self.name_var = tk.StringVar()
        tk.Entry(bar, textvariable=self.name_var, bg=C_FIELD, fg=C_FG,
                 insertbackground=C_FG, relief="flat", width=14).pack(
            side="left", padx=(6, 10), ipady=4)
        self._btn(bar, "保存到曲谱库", self.save_current, "#3a4f68").pack(side="left")

        # 第二行：导入导出
        bar2 = tk.Frame(panel, bg=C_PANEL)
        bar2.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))
        self.sample_var = tk.StringVar(value="载入示例…")
        om = tk.OptionMenu(bar2, self.sample_var, *SAMPLES.keys(),
                           command=self.load_sample)
        om.configure(bg="#22303f", fg=C_FG, relief="flat", highlightthickness=0,
                     activebackground="#2b3b4d", bd=0, padx=4, pady=2)
        om["menu"].configure(bg=C_PANEL, fg=C_FG)
        om.pack(side="left")
        for text, cmd in (("导入文件", self.import_file), ("导出 TXT", self.export_txt),
                          ("导出 JSON", self.export_json), ("清空", self.clear_score)):
            self._btn(bar2, text, cmd).pack(side="left", padx=(6, 0))

        wrap = tk.Frame(panel, bg=C_PANEL)
        wrap.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))
        wrap.columnconfigure(0, weight=1)
        self.text = tk.Text(wrap, bg=C_FIELD, fg=C_FG, insertbackground=C_ACCENT2,
                            relief="flat", wrap="word", height=8,
                            font=("Consolas", 12), padx=8, pady=6)
        self.text.grid(row=0, column=0, sticky="ew")
        sb = tk.Scrollbar(wrap, command=self.text.yview, bg=C_PANEL,
                          troughcolor=C_FIELD, relief="flat", bd=0)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)
        self.text.tag_configure("hl", background="#2b4a54", foreground="#ffffff")
        self.text.bind("<<Modified>>", self._on_text_modified)
        self.text.bind("<KeyRelease>", lambda e: self._schedule_parse())

        tools = tk.Frame(panel, bg=C_PANEL)
        tools.grid(row=4, column=0, sticky="ew", padx=12)
        tk.Label(tools, text="可视化编辑", bg=C_PANEL, fg=C_ACCENT2).pack(side="left")
        for text, cmd in (("←", lambda: self.move_sel(-1)), ("→", lambda: self.move_sel(1)),
                          ("删除", self.delete_sel), ("休止 0", self.insert_rest),
                          ("小节线 |", self.insert_bar), ("时值 ×2", lambda: self.scale_sel(2)),
                          ("时值 ÷2", lambda: self.scale_sel(0.5)),
                          ("撤销", self.undo)):
            self._btn(tools, text, cmd).pack(side="left", padx=(6, 0))

        chips_wrap = tk.Frame(panel, bg=C_PANEL)
        chips_wrap.grid(row=5, column=0, sticky="nsew", padx=12, pady=(6, 4))
        chips_wrap.rowconfigure(0, weight=1)
        chips_wrap.columnconfigure(0, weight=1)
        self.chips_canvas = tk.Canvas(chips_wrap, bg=C_FIELD, highlightthickness=0,
                                      height=120)
        self.chips_canvas.grid(row=0, column=0, sticky="nsew")
        csb = tk.Scrollbar(chips_wrap, orient="vertical", command=self.chips_canvas.yview,
                           bg=C_PANEL, troughcolor=C_FIELD, relief="flat", bd=0)
        csb.grid(row=0, column=1, sticky="ns")
        self.chips_canvas.configure(yscrollcommand=csb.set)
        self.chips_frame = tk.Frame(self.chips_canvas, bg=C_FIELD)
        self.chips_canvas.create_window((0, 0), window=self.chips_frame, anchor="nw")
        self.chips_frame.bind(
            "<Configure>",
            lambda e: self.chips_canvas.configure(scrollregion=self.chips_canvas.bbox("all")))
        self.chips_canvas.bind(
            "<Configure>",
            lambda e: self.chips_canvas.itemconfigure(
                self.chips_canvas.find_all()[0], width=e.width))

        info = tk.Frame(panel, bg=C_PANEL)
        info.grid(row=6, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.parse_var = tk.StringVar(value="—")
        self.parse_lbl = tk.Label(info, textvariable=self.parse_var, bg=C_PANEL,
                                  fg=C_DIM, anchor="w")
        self.parse_lbl.pack(side="left")
        self.stats_var = tk.StringVar(value="")
        tk.Label(info, textvariable=self.stats_var, bg=C_PANEL, fg=C_DIM).pack(side="right")

    # ---- ② 演奏 ----
    def _build_play_panel(self, parent):
        panel = self._panel(parent, "② 演奏")
        panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 6))
        panel.columnconfigure(0, weight=1)

        pad = tk.Frame(panel, bg=C_PANEL)
        pad.grid(row=1, column=0, sticky="ew", padx=12)

        row = tk.Frame(pad, bg=C_PANEL)
        row.pack(fill="x")
        self.start_btn = tk.Button(row, text="▶ 启动演奏", command=self.start,
                                   bg=C_ACCENT, fg="#1b1305", relief="flat", bd=0,
                                   font=("Microsoft YaHei UI", 11, "bold"),
                                   padx=14, pady=9, cursor="hand2",
                                   activebackground="#ffc63f")
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.pause_btn = self._btn(row, "⏸ 暂停", self.toggle_pause)
        self.pause_btn.pack(side="left", padx=6, fill="x", expand=True)
        self.stop_btn = self._btn(row, "⏹ 停止", self.stop, bg="#332023")
        self.stop_btn.pack(side="left", fill="x", expand=True)

        self.now_lbl = tk.Label(pad, text="—", bg=C_FIELD, fg=C_ACCENT,
                                font=("Consolas", 17, "bold"), anchor="w",
                                padx=10, pady=6)
        self.now_lbl.pack(fill="x", pady=(8, 3))
        self.prog_lbl = tk.Label(pad, text="0 / 0    0.0s / 0.0s", bg=C_PANEL,
                                 fg=C_DIM, anchor="w", font=("Consolas", 10))
        self.prog_lbl.pack(fill="x")

        self.prog_bar = tk.Canvas(pad, height=8, bg=C_FIELD, highlightthickness=0)
        self.prog_bar.pack(fill="x", pady=(4, 2))
        self._prog_fill = self.prog_bar.create_rectangle(0, 0, 0, 8, fill=C_ACCENT2,
                                                         outline="")
        self.prog_bar.bind("<Configure>", lambda e: self._draw_progress())

        # 设置区
        grid = tk.Frame(pad, bg=C_PANEL)
        grid.pack(fill="x", pady=(10, 0))
        grid.columnconfigure(1, weight=1)

        self.interval_var = tk.IntVar(value=1000)
        self.hold_var = tk.IntVar(value=50)
        self.count_var = tk.IntVar(value=3)

        self._slider(grid, 0, "音符间隔", self.interval_var, 100, 3000, 50,
                     lambda v: "%.2f 秒" % (v / 1000.0), self._on_interval)
        self._slider(grid, 1, "按住时长", self.hold_var, 10, 300, 5,
                     lambda v: "%d 毫秒" % v, self._on_hold)
        self._slider(grid, 2, "启动倒计时", self.count_var, 0, 10, 1,
                     lambda v: ("%d 秒" % v) if v else "不倒数", None)

        opt = tk.Frame(pad, bg=C_PANEL)
        opt.pack(fill="x", pady=(8, 0))
        opt.columnconfigure(0, weight=1)
        opt.columnconfigure(1, weight=1)
        self.lock_var = tk.BooleanVar(value=True)
        self.min_var = tk.BooleanVar(value=True)
        self.top_var = tk.BooleanVar(value=True)
        self.hot_var = tk.BooleanVar(value=True)
        checks = (("锁定目标窗口", self.lock_var, None),
                  ("启动时最小化", self.min_var, None),
                  ("窗口置顶", self.top_var, self._toggle_topmost),
                  ("全局热键 F8/F9/F12", self.hot_var, self._toggle_hotkeys))
        for i, (text, var, cmd) in enumerate(checks):
            tk.Checkbutton(opt, text=text, variable=var, command=cmd, bg=C_PANEL,
                           fg=C_FG, selectcolor=C_FIELD, activebackground=C_PANEL,
                           activeforeground=C_FG, anchor="w", highlightthickness=0,
                           font=("Microsoft YaHei UI", 9)).grid(
                row=i // 2, column=i % 2, sticky="w")

        mode = tk.Frame(pad, bg=C_PANEL)
        mode.pack(fill="x", pady=(8, 0))
        tk.Label(mode, text="注入方式", bg=C_PANEL, fg=C_DIM).pack(side="left")
        self.mode_var = tk.StringVar(value=INJECT_LABELS["both"])
        om = tk.OptionMenu(mode, self.mode_var, *[v for _k, v in INJECT_MODES],
                           command=self._on_mode)
        om.configure(bg="#22303f", fg=C_FG, relief="flat", highlightthickness=0,
                     activebackground="#2b3b4d", bd=0, padx=6, pady=2,
                     font=("Microsoft YaHei UI", 9))
        om["menu"].configure(bg=C_PANEL, fg=C_FG)
        om.pack(side="left", padx=(6, 0))

        ser = tk.Frame(pad, bg=C_PANEL)
        ser.pack(fill="x", pady=(4, 0))
        tk.Label(ser, text="音符方案", bg=C_PANEL, fg=C_DIM).pack(side="left")
        self.scheme_var = tk.StringVar(value=SCHEME_LABELS["shift"])
        som = tk.OptionMenu(ser, self.scheme_var, *[v for _k, v in NOTE_SCHEMES],
                            command=self._on_scheme)
        som.configure(bg="#22303f", fg=C_FG, relief="flat", highlightthickness=0,
                      activebackground="#2b3b4d", bd=0, padx=6, pady=2,
                      font=("Microsoft YaHei UI", 9))
        som["menu"].configure(bg=C_PANEL, fg=C_FG)
        som.pack(side="left", padx=(4, 0))

        ser = tk.Frame(pad, bg=C_PANEL)
        ser.pack(fill="x", pady=(4, 0))
        tk.Label(ser, text="串口（仅硬件模式）", bg=C_PANEL, fg=C_DIM).pack(side="left")
        self.port_var = tk.StringVar(value="COM3")
        tk.Entry(ser, textvariable=self.port_var, bg=C_FIELD, fg=C_FG, width=7,
                 relief="flat", insertbackground=C_FG).pack(side="left", padx=5)
        self._btn(ser, "连接硬件", self.connect_hid).pack(side="left")
        self.hid_lbl = tk.Label(ser, text="未连接", bg=C_PANEL, fg=C_DIM,
                                font=("Microsoft YaHei UI", 9))
        self.hid_lbl.pack(side="left", padx=6)

        tgt = tk.Frame(pad, bg=C_PANEL)
        tgt.pack(fill="x", pady=(4, 0))
        tk.Label(tgt, text="目标窗口", bg=C_PANEL, fg=C_DIM).pack(side="left")
        self.target_var = tk.StringVar(value="自动（倒计时结束时的前台窗口）")
        tk.Label(tgt, textvariable=self.target_var, bg=C_PANEL, fg=C_FG, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=6)
        self._btn(tgt, "选择…", self.choose_target).pack(side="right")
        self._btn(tgt, "清除", self.clear_target).pack(side="right", padx=(0, 4))

        elev = tk.Frame(pad, bg=C_PANEL)
        elev.pack(fill="x", pady=(4, 0))
        self.elev_lbl = tk.Label(elev, text="", bg=C_PANEL, fg=C_DIM, anchor="w",
                                 font=("Microsoft YaHei UI", 9))
        self.elev_lbl.pack(side="left")
        self.elev_btn = self._btn(elev, "以管理员身份重启", self.restart_admin, "#3a4f68")

        act = tk.Frame(pad, bg=C_PANEL)
        act.pack(fill="x", pady=(10, 10))
        self._btn(act, "🔍 注入自检", self.self_test, "#3a4f68").pack(side="left")
        self._btn(act, "🎵 试吹一遍", self.test_play).pack(side="left", padx=5)
        self._btn(act, "🩺 诊断", self.show_diagnostics, "#3a4f68").pack(side="left")
        self._btn(act, "📜 日志", self.show_log).pack(side="left", padx=5)
        self._btn(act, "⌨ 松开所有键", self.release_keys, "#332023",
                  C_DANGER).pack(side="left")

        self._build_keyboard(pad).pack(fill="x", pady=(2, 0))

        self.hint = tk.Label(pad, text="", bg=C_PANEL, fg=C_DIM, justify="left",
                             anchor="w", wraplength=380,
                             font=("Microsoft YaHei UI", 9))
        self.hint.pack(fill="x", pady=(8, 10))

    def _slider(self, parent, row, label, var, lo, hi, step, fmt, cb):
        tk.Label(parent, text=label, bg=C_PANEL, fg=C_DIM).grid(
            row=row, column=0, sticky="w", pady=2)
        val = tk.Label(parent, text=fmt(var.get()), bg=C_PANEL, fg=C_FG, width=9,
                       anchor="e")
        val.grid(row=row, column=2, sticky="e")
        sc = tk.Scale(parent, from_=lo, to=hi, resolution=step, orient="horizontal",
                      variable=var, showvalue=False, bg=C_PANEL, fg=C_FG,
                      troughcolor=C_FIELD, highlightthickness=0, bd=0,
                      activebackground=C_ACCENT, sliderrelief="flat",
                      command=lambda v: (val.configure(text=fmt(float(v))),
                                         cb and cb(float(v))))
        sc.grid(row=row, column=1, sticky="ew", padx=8)
        return sc

    # ---- ③④ 底部 ----
    def _build_bottom(self, parent):
        bottom = tk.Frame(parent, bg=C_BG)
        bottom.pack(fill="both", expand=False, padx=12, pady=(0, 12))
        bottom.columnconfigure(0, weight=1, uniform="b")
        bottom.columnconfigure(1, weight=1, uniform="b")

        lib = self._panel(bottom, "③ 曲谱库（保存在 scores.json）")
        lib.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        lib.rowconfigure(1, weight=1)
        lib.columnconfigure(0, weight=1)
        inner = tk.Frame(lib, bg=C_PANEL)
        inner.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.lib_list = tk.Listbox(inner, bg=C_FIELD, fg=C_FG, height=5, relief="flat",
                                   selectbackground="#2b4a54", selectforeground=C_FG,
                                   highlightthickness=0, activestyle="none")
        self.lib_list.pack(side="left", fill="both", expand=True)
        btns = tk.Frame(inner, bg=C_PANEL)
        btns.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd, col in (("载入", self.load_selected, "#3a4f68"),
                               ("覆盖保存", self.overwrite_selected, "#22303f"),
                               ("重命名", self.rename_selected, "#22303f"),
                               ("删除", self.delete_selected, "#332023"),
                               ("导出全部", self.export_all, "#22303f")):
            self._btn(btns, text, cmd, col, C_DANGER if col == "#332023" else C_FG).pack(
                fill="x", pady=(0, 5))

        help_panel = self._panel(bottom, "④ 曲谱格式 & 使用步骤")
        help_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        help_panel.rowconfigure(1, weight=1)
        help_panel.columnconfigure(0, weight=1)
        txt = tk.Text(help_panel, bg=C_PANEL, fg=C_DIM, relief="flat", height=5,
                      wrap="word", font=("Microsoft YaHei UI", 9), padx=12, pady=8)
        txt.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 8))
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")

    # ---------------- 曲谱解析与显示 ----------------
    def _on_text_modified(self, _evt=None):
        self.text.edit_modified(False)
        self._schedule_parse()

    def _schedule_parse(self):
        if self._typing_job:
            self.root.after_cancel(self._typing_job)
        self._typing_job = self.root.after(180, self.reparse)

    def current_text(self):
        return self.text.get("1.0", "end-1c")

    def reparse(self, select_reset=False):
        self._typing_job = None
        self.tokens, self.errors, self.ignored = parse_full(self.current_text())
        if select_reset:
            self.selected = -1
        elif self.selected >= len(self.tokens):
            self.selected = len(self.tokens) - 1
        self.render_chips()
        self.update_status()
        self._save_draft()

    def update_status(self):
        items = playable(self.tokens)
        beats = total_beats(self.tokens)
        secs = estimate_seconds(self.tokens, self.interval_var.get() / 1000.0,
                                self.settings.get("bar_pause_ms", 0) / 1000.0)
        self.stats_var.set("%d 个音 · %d 休止 · 共 %s 拍 ≈ %.1f 秒"
                           % (len([t for t in items if t["type"] == "note"]),
                              len([t for t in items if t["type"] == "rest"]),
                              _trim(beats), secs))
        if self.errors:
            shown = "、".join("第%d行「%s」" % (ln, w) for ln, w, _ in self.errors[:3])
            self.parse_var.set("⚠ %d 处无法识别：%s%s"
                               % (len(self.errors), shown,
                                  " …" if len(self.errors) > 3 else ""))
            self.parse_lbl.configure(fg=C_DANGER)
        elif not items:
            self.parse_var.set("曲谱为空")
            self.parse_lbl.configure(fg=C_ACCENT)
        else:
            text = "✔ 解析正常"
            if self.ignored:
                text += "（忽略 %d 处非音符文字）" % len(self.ignored)
            self.parse_var.set(text)
            self.parse_lbl.configure(fg=C_ACCENT2)
        if not (self.player and self.player.is_alive()):
            self.prog_lbl.configure(text="0 / %d    %.1fs / %.1fs" % (len(items), 0, secs))

    def render_chips(self):
        for w in self.chips_frame.winfo_children():
            w.destroy()
        self.chip_buttons = []
        cols = 14
        for i, tok in enumerate(self.tokens):
            label = token_label(tok)
            sub = token_key_text(tok, getattr(self, "scheme", "shift"))
            text = label if not sub else "%s\n%s" % (label, sub)
            selected = (i == self.selected)
            bg = "#2c2a1d" if selected else ("#161f2a" if tok["type"] == "bar"
                                             else "#1d2836")
            fg = C_ACCENT if selected else (C_DIM if tok["type"] != "note" else C_FG)
            b = tk.Button(self.chips_frame, text=text, bg=bg, fg=fg, relief="flat",
                          bd=0, padx=6, pady=2, cursor="hand2",
                          font=("Consolas", 10),
                          highlightthickness=1,
                          highlightbackground=C_ACCENT if selected else "#34465b",
                          activebackground="#26364a",
                          command=lambda idx=i: self.select_chip(idx))
            b.grid(row=i // cols, column=i % cols, padx=3, pady=3, sticky="ew")
            if tok["beats"] != 1:
                b.configure(text="%s\n×%s" % (label, _trim(tok["beats"])))
            self.chip_buttons.append(b)
        for c in range(cols):
            self.chips_frame.columnconfigure(c, weight=1, minsize=48)

    def select_chip(self, idx):
        self.selected = idx
        self.render_chips()
        self._highlight_in_text(idx)

    def _highlight_in_text(self, idx):
        """在曲谱文本里把选中的记号高亮出来（位置由解析器给出的字符区间）。"""
        if not hasattr(self, "text"):
            return
        self.text.tag_remove("hl", "1.0", "end")
        if not (0 <= idx < len(self.tokens)):
            return
        tok = self.tokens[idx]
        start, end = tok.get("start"), tok.get("end")
        if start is None or end is None:
            return
        try:
            self.text.tag_add("hl", "1.0 + %dc" % start, "1.0 + %dc" % end)
            self.text.see("1.0 + %dc" % start)
        except Exception:                                # noqa: BLE001
            pass

    def _highlight_chip(self, idx):
        for i, b in enumerate(self.chip_buttons):
            if i == idx:
                b.configure(bg="#123029", highlightbackground=C_ACCENT2)
            elif i == self.selected:
                b.configure(bg="#2c2a1d", highlightbackground=C_ACCENT)
            else:
                tok = self.tokens[i] if i < len(self.tokens) else None
                base = "#161f2a" if tok and tok["type"] == "bar" else "#1d2836"
                b.configure(bg=base, highlightbackground="#34465b")

    # ---------------- 编辑操作 ----------------
    def commit(self, tokens, undo_text=None):
        if undo_text is None:
            undo_text = self.current_text()
        self.undo_stack.append(undo_text)
        if len(self.undo_stack) > 60:
            self.undo_stack.pop(0)
        text = serialize_tokens(tokens)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.reparse()

    def undo(self):
        if not self.undo_stack:
            self.set_hint("没有可撤销的操作")
            return
        text = self.undo_stack.pop()
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.reparse()

    def move_sel(self, step):
        i = self.selected + step
        if i < -1:
            i = -1
        if i >= len(self.tokens):
            i = len(self.tokens) - 1
        self.selected = i
        self.render_chips()

    def delete_sel(self):
        if not (0 <= self.selected < len(self.tokens)):
            self.set_hint("先点选一个记号再删除")
            return
        tokens = [dict(t) for t in self.tokens]
        tokens.pop(self.selected)
        idx = min(self.selected, len(tokens) - 1)
        self.commit(tokens)
        self.selected = idx
        self.render_chips()

    def insert_rest(self):
        self._insert_token({"type": "rest", "degree": 0, "accidental": 0,
                            "sharp": False, "octave": 0, "beats": 1.0})

    def insert_bar(self):
        self._insert_token({"type": "bar", "degree": 0, "accidental": 0,
                            "sharp": False, "octave": 0, "beats": 0.0})

    def _insert_token(self, tok):
        tokens = [dict(t) for t in self.tokens]
        at = self.selected if 0 <= self.selected < len(tokens) else len(tokens)
        tokens.insert(at, tok)
        self.commit(tokens)
        # 插入点前移，连续插入时按先后顺序排列
        self.selected = at + 1 if at + 1 < len(tokens) else -1
        self.render_chips()

    def scale_sel(self, factor):
        if not (0 <= self.selected < len(self.tokens)) or \
                self.tokens[self.selected]["type"] == "bar":
            self.set_hint("先点选一个音符或休止符")
            return
        tokens = [dict(t) for t in self.tokens]
        tokens[self.selected]["beats"] = max(
            0.125, min(16.0, tokens[self.selected]["beats"] * factor))
        self.commit(tokens)
        self.render_chips()

    def input_note(self, degree, sharp):
        """虚拟键盘录入：替换选中记号，或在末尾追加。"""
        acc = 1 if sharp else 0
        tokens = [dict(t) for t in self.tokens]
        idx = self.selected
        appended = False
        if idx < 0 or idx >= len(tokens):
            tokens.append({"type": "note", "degree": degree, "accidental": acc,
                           "sharp": bool(sharp), "octave": 0, "beats": 1.0})
            idx = len(tokens) - 1
            appended = True
        else:
            tok = tokens[idx]
            if tok["type"] == "note":
                tok["degree"], tok["accidental"] = degree, acc
                tok["sharp"] = bool(sharp)
            elif tok["type"] == "rest":
                tok.update({"type": "note", "degree": degree, "accidental": acc,
                            "sharp": bool(sharp), "beats": tok["beats"] or 1.0})
            else:
                tokens.insert(idx, {"type": "note", "degree": degree,
                                    "accidental": acc, "sharp": bool(sharp),
                                    "octave": 0, "beats": 1.0})
        self.commit(tokens)
        self.selected = -1 if appended or idx + 1 >= len(tokens) else idx + 1
        self.render_chips()

    # ---------------- 虚拟键盘 ----------------
    def _build_keyboard(self, parent):
        wrap = tk.Frame(parent, bg=C_PANEL)
        rows = [("升调键（大写 / Shift）", True), ("基本音键（小写，含逗号 = 高音 1）", False)]
        self.key_buttons = {}
        for title, sharp_row in rows:
            tk.Label(wrap, text=title, bg=C_PANEL, fg=C_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(6, 2))
            line = tk.Frame(wrap, bg=C_PANEL)
            line.pack(fill="x")
            degrees = range(1, 8) if sharp_row else [1, 2, 3, 4, 5, 6, 7, 8]
            for degree in degrees:
                letter = KEY_LETTER[degree] if degree <= 7 else ","
                show = letter.upper() if sharp_row else letter
                jp = ("#%d" % degree) if sharp_row else ("8" if degree == 8 else str(degree))
                b = tk.Button(line, text="%s\n%s" % (show, jp),
                              bg="#2c374a" if sharp_row else "#d7e0ea",
                              fg="#cfe3ff" if sharp_row else "#0e1620",
                              relief="flat", bd=0, padx=4, pady=6,
                              font=("Consolas", 12, "bold"), cursor="hand2",
                              activebackground=C_ACCENT)
                b.pack(side="left", fill="x", expand=True, padx=2)
                b.bind("<ButtonPress-1>",
                       lambda e, d=degree, s=sharp_row: self.on_key_press(d, s))
                self.key_buttons[(degree, sharp_row)] = b
        return wrap

    def on_key_press(self, degree, sharp):
        """点虚拟键盘 = 真发一个按键（可用于在游戏里手动试音），同时录入曲谱。"""
        self.kb.tap_letter(degree, sharp, self.hold_ms())
        if not (self.player and self.player.is_alive()):
            self.input_note(degree, sharp)

    def on_key_release(self, _degree, _sharp):
        pass

    def hold_ms(self):
        return int(self.hold_var.get())

    # ---------------- 演奏控制 ----------------
    def start(self):
        if self.player and self.player.is_alive():
            return
        tokens, errors = parse_score(self.current_text())
        if not playable(tokens):
            messagebox.showwarning(APP_TITLE, "曲谱为空，先写点音符吧。")
            return
        if errors:
            if not messagebox.askyesno(
                    APP_TITLE, "曲谱里有 %d 处无法识别的记号，它们会被跳过。\n仍要开始演奏吗？"
                    % len(errors)):
                return
        self._playing_score = True
        self._launch(tokens, "开始演奏")

    def test_play(self):
        """试吹：把 14 个键依次发给游戏，用来确认游戏端确实收到按键。"""
        if self.player and self.player.is_alive():
            return
        self._playing_score = False
        self._launch(TEST_TOKENS, "试吹：14 个键依次发送")

    def _launch(self, tokens, tip):
        self.kb.mode = INJECT_BY_LABEL.get(self.mode_var.get(), "both")
        self.set_hint(tip + ("，倒计时期间请切到游戏窗口并打开口琴界面"
                             if self.kb.mode not in ("post", "serial")
                             else "（当前注入方式不需要窗口在前台）"))
        self.log("开始演奏：%s，注入方式=%s，间隔=%.2fs，目标=%s"
                 % (tip, self.kb.mode, self.interval_var.get() / 1000.0,
                    self.target_var.get()))
        self.log("音符方案：%s" % self.scheme_var.get())
        if self.min_var.get():
            self.root.iconify()
        self.player = Player(self.q.put, tokens, {
            "interval": self.interval_var.get() / 1000.0,
            "hold_ms": self.hold_ms(),
            "countdown": int(self.count_var.get()),
            "lock_window": bool(self.lock_var.get()),
            "own_pid": self.own_pid,
            "keyboard": self.kb,
            "ime": self.ime,
            "target_hwnd": self.target_hwnd,
            "scheme": self.scheme,
            "bar_pause": self.settings.get("bar_pause_ms", 0) / 1000.0,
            "slide_mode": self.settings.get("slide_mode", "hold"),
            "slide_lead_ms": self.settings.get("slide_lead_ms", 30),
            "slide_tail_ms": self.settings.get("slide_tail_ms", 30),
        })
        self.player.start()
        self.set_state("倒计时…", C_ACCENT)
        self._set_buttons(playing=True, paused=False)

    def toggle_pause(self):
        if self.player and self.player.is_alive():
            self.player.toggle_pause()

    def stop(self):
        if self.player and self.player.is_alive():
            self.player.stop()
        self.kb.release_all()
        self.ime.release()

    def release_keys(self):
        self.kb.release_all()
        self.set_hint("已发送 Shift 与所有字母键的抬起事件")

    def _set_buttons(self, playing, paused):
        self.start_btn.configure(
            text="▶ 继续演奏" if (playing and paused) else
                 ("▶ 演奏中…" if playing else "▶ 启动演奏"),
            state="disabled" if (playing and not paused) else "normal")
        self.pause_btn.configure(state="normal" if (playing and not paused) else "disabled")
        self.stop_btn.configure(state="normal" if playing else "disabled")

    # ---------------- 状态回报 ----------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(40, self._pump)

    def _handle(self, kind, payload):
        if kind == "log":
            self.log(str(payload))
        elif kind == "countdown":
            self.set_state("%.1f 秒后开始…" % payload, C_ACCENT)
        elif kind == "target":
            self.set_state("演奏中", C_ACCENT2)
            self.set_hint("目标窗口：%s" % (payload or "(无标题)"))
        elif kind == "note":
            idx, total, tok, done_s, all_s, _done_beats = payload
            self.now_lbl.configure(
                text="%s   %s" % (token_label(tok),
                                  token_key_text(tok, self.scheme) or "休止"))
            self.prog_lbl.configure(text="%d / %d    %.1fs / %.1fs"
                                         % (idx + 1, total, done_s, all_s))
            self._progress = (idx + 1) / float(total or 1)
            self._draw_progress()
            if self._playing_score:
                self._highlight_chip(self._token_index(idx))
        elif kind == "bar":
            done_s, all_s = payload
            self.now_lbl.configure(text="|  小节停顿")
            self.prog_lbl.configure(text="小节停顿    %.1fs / %.1fs" % (done_s, all_s))
        elif kind == "hotkey":
            if payload == 1:                       # F8 启动 / 暂停 / 继续
                if self.player and self.player.is_alive():
                    self.toggle_pause()
                else:
                    self.start()
            elif payload == 2:                     # F9 停止
                self.stop()
            elif payload == 3:                     # F12 紧急停止
                self.stop()
                self.kb.release_all()
                self.set_hint("已紧急停止并松开所有按键")
        elif kind == "paused":
            self.set_state("已暂停", C_ACCENT)
            self._set_buttons(playing=True, paused=True)
        elif kind == "resumed":
            self.set_state("演奏中", C_ACCENT2)
            self._set_buttons(playing=True, paused=False)
        elif kind == "finished":
            self.set_state("演奏完成", C_ACCENT2)
            self.now_lbl.configure(text="结束")
            self._progress = 1.0
            self._draw_progress()
        elif kind == "error":
            self.set_state("已停止", C_DANGER)
            self.set_hint(str(payload))
            self.log("停止：%s" % payload)
            messagebox.showwarning(APP_TITLE, str(payload))
        elif kind == "ended":
            self.ime.release()
            self.kb.release_all()
            self._set_buttons(playing=False, paused=False)
            if self.status_var.get() not in ("演奏完成",):
                self.set_state("待机", C_DIM)
            else:
                self.set_state("演奏完成", C_ACCENT2)
            self.root.deiconify()

    def _token_index(self, play_idx):
        n = -1
        for i, tok in enumerate(self.tokens):
            if tok["type"] in ("note", "rest"):
                n += 1
                if n == play_idx:
                    return i
        return -1

    def _draw_progress(self):
        w = self.prog_bar.winfo_width()
        frac = getattr(self, "_progress", 0.0)
        self.prog_bar.coords(self._prog_fill, 0, 0, int(w * frac), 8)

    def set_state(self, text, color):
        self.status_var.set(text)
        self.status_lbl.configure(fg=color)

    def set_hint(self, text):
        self.hint.configure(text=text)

    # ---------------- 设置回调 ----------------
    def _on_interval(self, _v):
        self.update_status()

    def _on_hold(self, _v):
        pass

    def _on_mode(self):
        self.kb.mode = self.mode_var.get()

    def _toggle_topmost(self):
        self.root.attributes("-topmost", bool(self.top_var.get()))

    def _toggle_hotkeys(self):
        if self.hot_var.get():
            self._start_hotkeys()
        elif self.hotkeys:
            self.hotkeys.stop()
            self.hotkeys = None
            self.set_hint("已关闭全局热键")

    def _start_hotkeys(self):
        if self.hotkeys:
            return
        self.hotkeys = HotkeyThread(self._on_hotkey)
        self.hotkeys.start()
        self.hotkeys.ready.wait(0.5)
        self.set_hint("全局热键已启用：F8 启动/暂停、F9 停止、F12 紧急停止")

    def _on_hotkey(self, hid):
        self.q.put(("hotkey", hid))

    def _apply_settings_to_ui(self):
        s = self.settings
        self.interval_var.set(int(s.get("interval", 1000)))
        self.hold_var.set(int(s.get("hold_ms", 50)))
        self.count_var.set(int(s.get("countdown", 3)))
        self.kb.mode = s.get("mode", "both")
        self.mode_var.set(INJECT_LABELS.get(self.kb.mode, INJECT_LABELS["both"]))
        self.port_var.set(s.get("port", "COM3"))
        self.scheme = s.get("scheme", "shift")
        self.scheme_var.set(SCHEME_LABELS.get(self.scheme, SCHEME_LABELS["shift"]))
        self.lock_var.set(bool(s.get("lock_window", True)))
        self.min_var.set(bool(s.get("minimize", True)))
        self.top_var.set(bool(s.get("topmost", True)))
        self.hot_var.set(bool(s.get("hotkeys", True)))
        self.prog_lbl.configure(text="0 / 0    0.0s / 0.0s")
        self.render_library()
        self._update_elevation()
        self._on_mode(self.mode_var.get())

    def _collect_settings(self):
        s = dict(self.settings)
        s.update({
            "interval": int(self.interval_var.get()),
            "hold_ms": int(self.hold_var.get()),
            "countdown": int(self.count_var.get()),
            "mode": self.kb.mode,
            "scheme": self.scheme,
            "port": self.port_var.get().strip(),
            "lock_window": bool(self.lock_var.get()),
            "minimize": bool(self.min_var.get()),
            "topmost": bool(self.top_var.get()),
            "hotkeys": bool(self.hot_var.get()),
            "draft_name": self.name_var.get(),
            "draft_text": self.current_text(),
        })
        return s

    def _save_draft(self):
        if getattr(self, "_draft_job", None):
            self.root.after_cancel(self._draft_job)
        self._draft_job = self.root.after(800, self._write_draft)

    def _write_draft(self):
        self._draft_job = None
        self.settings = self._collect_settings()
        save_json(SETTINGS_FILE, self.settings)

    # ---------------- 曲谱库 ----------------
    def render_library(self):
        self.lib_list.delete(0, "end")
        self._lib_order = list(self.scores)
        for i, item in enumerate(self._lib_order):
            t = item.get("updated", "")
            self.lib_list.insert("end", "%s    %s    %s"
                                 % (item.get("name", "未命名"),
                                    "%d 音" % item.get("count", 0), t))

    def save_current(self):
        self.reparse()          # 确保 tokens 反映当前文本（输入后 180ms 内点保存也不会存错）
        name = (self.name_var.get() or "").strip() or "未命名曲谱"
        self.name_var.set(name)
        count = len([t for t in self.tokens if t["type"] == "note"])
        entry = {"name": name, "text": self.current_text(), "count": count,
                 "updated": time.strftime("%m-%d %H:%M")}
        for item in self.scores:
            if item.get("name") == name:
                item.update(entry)
                break
        else:
            self.scores.insert(0, entry)
        self._persist_scores()
        self.set_hint("已保存到曲谱库：%s" % name)

    def _persist_scores(self):
        save_json(SCORES_FILE, self.scores)
        self.render_library()

    def _lib_index(self):
        sel = self.lib_list.curselection()
        return sel[0] if sel else None

    def load_selected(self):
        i = self._lib_index()
        if i is None:
            self.set_hint("先在曲谱库里选一份")
            return
        item = self._lib_order[i]
        self.name_var.set(item.get("name", ""))
        self.text.delete("1.0", "end")
        self.text.insert("1.0", item.get("text", ""))
        self.selected = -1
        self.reparse(select_reset=True)
        self.set_hint("已载入：%s" % item.get("name", ""))

    def overwrite_selected(self):
        i = self._lib_index()
        if i is None:
            self.set_hint("先在曲谱库里选一份")
            return
        old = self._lib_order[i].get("name")
        self.name_var.set(old)
        self.save_current()

    def rename_selected(self):
        i = self._lib_index()
        if i is None:
            return
        item = self._lib_order[i]
        new = simpledialog_ask(self.root, "重命名曲谱", "新名称：", item.get("name", ""))
        if new:
            item["name"] = new
            self._persist_scores()

    def delete_selected(self):
        i = self._lib_index()
        if i is None:
            return
        item = self._lib_order[i]
        if messagebox.askyesno(APP_TITLE, "确定删除「%s」？" % item.get("name", "")):
            self.scores = [x for x in self.scores if x is not item]
            self._persist_scores()

    def export_all(self):
        if not self.scores:
            self.set_hint("曲谱库是空的")
            return
        path = filedialog.asksaveasfilename(
            title="导出全部曲谱", defaultextension=".json",
            initialfile="harmonica-scores.json",
            filetypes=[("JSON", "*.json")])
        if path:
            save_json(path, self.scores)
            self.set_hint("已导出 %d 份曲谱到 %s" % (len(self.scores), path))

    # ---------------- 导入导出 ----------------
    def import_file(self):
        path = filedialog.askopenfilename(
            title="导入曲谱",
            filetypes=[("曲谱文件", "*.txt *.json *.jianpu"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = fh.read()
        except Exception as exc:                          # noqa: BLE001
            messagebox.showerror(APP_TITLE, "读取失败：%r" % (exc,))
            return
        base = os.path.splitext(os.path.basename(path))[0]
        if path.lower().endswith(".json"):
            try:
                data = json.loads(raw)
            except Exception:                             # noqa: BLE001
                data = None
            if isinstance(data, list):
                if messagebox.askyesno(APP_TITLE,
                                       "这是曲谱库文件，共 %d 份。\n合并到本地曲谱库吗？"
                                       % len(data)):
                    self.scores = data + self.scores
                    self._persist_scores()
                    self.set_hint("已合并 %d 份曲谱" % len(data))
                    return
            elif isinstance(data, dict) and isinstance(data.get("text"), str):
                self.name_var.set(data.get("name") or base)
                self.text.delete("1.0", "end")
                self.text.insert("1.0", data["text"])
                self.reparse(select_reset=True)
                self.set_hint("已导入：%s" % (data.get("name") or base))
                return
        self.name_var.set(base)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", raw)
        self.reparse(select_reset=True)
        self.set_hint("已导入 %s（%d 个音）"
                      % (os.path.basename(path),
                         len([t for t in self.tokens if t["type"] == "note"])))

    def export_txt(self):
        name = (self.name_var.get() or "曲谱").strip()
        path = filedialog.asksaveasfilename(title="导出 TXT", defaultextension=".txt",
                                            initialfile=name + ".txt",
                                            filetypes=[("文本", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.current_text())
            self.set_hint("已导出 %s" % path)

    def export_json(self):
        name = (self.name_var.get() or "曲谱").strip()
        path = filedialog.asksaveasfilename(title="导出 JSON", defaultextension=".json",
                                            initialfile=name + ".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            save_json(path, {"name": name, "text": self.current_text()})
            self.set_hint("已导出 %s" % path)

    def clear_score(self):
        if messagebox.askyesno(APP_TITLE, "确定清空当前曲谱？"):
            self.text.delete("1.0", "end")
            self.name_var.set("")
            self.reparse(select_reset=True)

    def load_sample(self, key):
        if key in SAMPLES:
            self.name_var.set(key)
            self.text.delete("1.0", "end")
            self.text.insert("1.0", SAMPLES[key])
            self.reparse(select_reset=True)
        self.sample_var.set("载入示例…")

    # ---------------- 注入自检 ----------------
    def self_test(self):
        """开一个输入框，用真实注入打字进去，验证本机能不能送键。"""
        win = tk.Toplevel(self.root)
        win.title("注入自检")
        win.configure(bg=C_BG)
        win.geometry("560x260")
        win.attributes("-topmost", True)
        tk.Label(win, text="① 下面的输入框会自动收到 zxcvbnmZXCVBNM",
                 bg=C_BG, fg=C_FG).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(win, text="② 若两边一致 = 注入正常；若为空 = 本机拦截了注入按键",
                 bg=C_BG, fg=C_DIM, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=14)
        entry = tk.Entry(win, bg=C_FIELD, fg=C_FG, insertbackground=C_ACCENT2,
                         relief="flat", font=("Consolas", 14))
        entry.pack(fill="x", padx=14, pady=10, ipady=6)
        result = tk.Label(win, text="准备中…", bg=C_BG, fg=C_ACCENT, justify="left",
                          anchor="w", wraplength=520)
        result.pack(fill="x", padx=14)
        tk.Button(win, text="关闭", command=win.destroy, bg="#22303f", fg=C_FG,
                  relief="flat", padx=12, pady=4, cursor="hand2").pack(pady=10)

        win.update_idletasks()
        win.update()
        hwnd = int(win.frame(), 16)
        guard = ImeGuard()
        diag = []
        self._selftest_tries = 0

        def phase1():
            fg_ok = force_foreground(hwnd)
            diag.append("取得前台=%s" % fg_ok)
            if not fg_ok:
                self.last_self_test = (False, "", " ".join(diag))
                result.configure(text="拿不到前台焦点，自检中止（可能是被其它窗口抢了焦点）",
                                 fg=C_DANGER)
                return
            entry.focus_force()
            win.update()                # 等 Tk 把焦点真正落到输入框上
            guard.engage(hwnd)          # 关键：先关掉这个窗口的输入法
            diag.append("顶层=%s 焦点窗口=%s 解除=%d 个"
                        % (hwnd, getattr(guard, "focus_hwnd", None), len(guard.detached)))
            win.after(250, phase2)

        def phase2():
            diag.append("发送前仍在前台=%s" % (foreground_window() == hwnd))
            self.kb.mode = self.mode_var.get()
            for ch in "zxcvbnm":
                self.kb.tap_letter("zxcvbnm".index(ch) + 1, False, 30)
            for ch in "ZXCVBNM":
                self.kb.tap_letter("zxcvbnm".index(ch.lower()) + 1, True, 30)
            win.after(250, phase3)

        def phase3():
            got = entry.get()
            # 偶尔会被别的窗口抢走焦点导致一个字都没收到，这里自动重试两次
            if got == "" and self._selftest_tries < 2:
                self._selftest_tries += 1
                diag.append("第 %d 次没收到，重试" % self._selftest_tries)
                entry.delete(0, "end")
                win.after(350, phase1)
                return
            guard.release()
            diag.append("收到=%r" % got)
            self.last_self_test = (got == "zxcvbnmZXCVBNM", got, " ".join(diag))
            if got == "zxcvbnmZXCVBNM":
                result.configure(
                    text="✔ 注入正常，本机可以把按键送进窗口。\n"
                         "  收到的内容：%r\n  %s" % (got, " ".join(diag)), fg=C_ACCENT2)
            elif got == "":
                result.configure(
                    text="✘ 一个字都没收到。请先手动切换输入法到英文（Shift 或 Ctrl+空格），"
                         "再点一次自检。\n  也可能是游戏/安全软件拦截了注入按键。\n  %s"
                         % " ".join(diag), fg=C_DANGER)
            else:
                result.configure(text="△ 收到 %r，与预期不完全一致（可能漏键或顺序错乱）\n  %s"
                                      % (got, " ".join(diag)), fg=C_ACCENT)

        win.after(500, phase1)

    def _first_tip(self):
        self.set_hint("第一次用：先点「注入自检」确认本机能送键，再点「试吹一遍」到游戏里验证。"
                      "游戏没反应时点「诊断」并把结果发出来。")

    def _on_scheme(self, label=None):
        self.scheme = SCHEME_BY_LABEL.get(label or self.scheme_var.get(), "shift")
        if self.scheme == "slide":
            self.set_hint("半音阶方案：8 个键 z x c v b n m , ，半音用「低半音键 + 鼠标右键(升调档)」"
                          "吹出，奏完自动切回中键自然档。需要鼠标在游戏里可用。")
        else:
            self.set_hint("Shift 方案：z x c v b n m 为 1-7，Shift+字母为升调，逗号是高音 1。")
        self.render_chips()
        self.update_status()

    def _on_mode(self, label=None):
        self.kb.mode = INJECT_BY_LABEL.get(label or self.mode_var.get(), "both")
        if self.kb.mode == "post":
            self.set_hint("PostMessage：直接把按键消息投递给目标窗口，游戏不需要在前台。"
                          "少数游戏不处理窗口消息，无效就换回 SendInput。")
        elif self.kb.mode == "serial":
            self.set_hint("硬件模式：填好串口号后点『连接硬件』。需要一块 Arduino Pro Micro / "
                          "Leonardo，烧录 hardware/harmonica_hid.ino，按键对反作弊完全等同于真键盘。")
        elif self.kb.mode == "keybd":
            self.set_hint("keybd_event：旧接口，某些游戏/保护对它和 SendInput 的判定不同。")
        else:
            self.set_hint("SendInput 模式需要游戏窗口保持在前台（切走会自动停止）。")

    # ---------------- 诊断 / 日志 / 硬件 ----------------
    def log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        self.log_lines.append(line)
        if len(self.log_lines) > 800:
            del self.log_lines[:200]
        try:
            with open(os.path.join(APP_DIR, "run.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:                                # noqa: BLE001
            pass
        if self.log_text is not None:
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except Exception:                            # noqa: BLE001
                pass

    def _text_window(self, title, subtitle, width=720, height=420, attr="log_win"):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=C_BG)
        win.geometry("%dx%d" % (width, height))
        win.attributes("-topmost", True)
        tk.Label(win, text=subtitle, bg=C_BG, fg=C_DIM, justify="left",
                 anchor="w", font=("Microsoft YaHei UI", 9)).pack(
            fill="x", padx=12, pady=(10, 4))
        box = tk.Text(win, bg=C_FIELD, fg=C_FG, relief="flat", wrap="word",
                      font=("Consolas", 10), padx=8, pady=6)
        box.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        bar = tk.Frame(win, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=(0, 10))
        return win, box, bar

    def show_log(self):
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.lift()
            return
        win, box, bar = self._text_window(
            "运行日志", "演奏过程、目标窗口、按键与失败原因都会记在这里，"
                        "同时写入程序目录的 run.log。")
        self.log_win, self.log_text = win, box
        box.insert("1.0", "\n".join(self.log_lines) + "\n")
        box.see("end")
        box.configure(state="disabled")

        def copy_log():
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(self.log_lines))
            self.set_hint("日志已复制到剪贴板")

        self._btn(bar, "复制全部", copy_log, "#3a4f68").pack(side="left")
        self._btn(bar, "关闭", win.destroy).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "log_text", None),
                                                  setattr(self, "log_win", None),
                                                  win.destroy()))

    def show_diagnostics(self):
        if self.diag_win is not None and self.diag_win.winfo_exists():
            self.diag_win.lift()
            return
        hwnd = self.target_hwnd or foreground_window()
        findings, advice = diagnose_window(hwnd)
        win, box, bar = self._text_window(
            "目标窗口诊断", "对目标窗口做一次体检，判断软件注入是不是被系统或反作弊挡住了。")
        self.diag_win = win
        box.insert("end", "【检测结果】\n")
        for line in findings:
            box.insert("end", "  " + line + "\n")
        box.insert("end", "\n【结论与建议】\n")
        for line in advice:
            box.insert("end", "  " + line + "\n")
        self.log("诊断目标窗口：%s" % describe_window(hwnd))
        for line in advice:
            self.log("诊断建议：%s" % line)

        def copy_diag():
            text = "\n".join(findings) + "\n\n" + "\n".join(advice)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_hint("诊断结果已复制到剪贴板，可以直接发给别人看")

        self._btn(bar, "复制结果", copy_diag, "#3a4f68").pack(side="left")
        self._btn(bar, "关闭", win.destroy).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: (setattr(self, "diag_win", None),
                                                  win.destroy()))

    def choose_target(self):
        wins = [w for w in list_windows() if w["pid"] != self.own_pid]
        wins.sort(key=lambda w: w["title"].lower())
        if not wins:
            self.set_hint("没有找到可选的窗口")
            return
        win = tk.Toplevel(self.root)
        win.title("选择目标窗口")
        win.configure(bg=C_BG)
        win.geometry("640x420")
        win.attributes("-topmost", True)
        tk.Label(win, text="选中游戏窗口（口琴界面所属的那个），双击或按确定",
                 bg=C_BG, fg=C_DIM).pack(anchor="w", padx=12, pady=(10, 6))
        lst = tk.Listbox(win, bg=C_FIELD, fg=C_FG, relief="flat", activestyle="none",
                         selectbackground="#2b4a54", highlightthickness=0)
        lst.pack(fill="both", expand=True, padx=12)
        proc_cache = {}
        for w in wins:
            pid = w["pid"]
            if pid not in proc_cache:
                path = process_path(pid)
                proc_cache[pid] = os.path.basename(path) if path else "?"
            lst.insert("end", "%s    [%s]  PID %d" % (w["title"], proc_cache[pid], pid))
        bar = tk.Frame(win, bg=C_BG)
        bar.pack(fill="x", padx=12, pady=10)

        def take():
            sel = lst.curselection()
            if not sel:
                return
            w = wins[sel[0]]
            self.target_hwnd = w["hwnd"]
            self.target_var.set("%s  [PID %d]" % (w["title"][:40], w["pid"]))
            self.log("已选定目标窗口：%s" % describe_window(w["hwnd"]))
            win.destroy()

        lst.bind("<Double-Button-1>", lambda e: take())
        self._btn(bar, "确定", take, "#3a4f68").pack(side="left")
        self._btn(bar, "取消", win.destroy).pack(side="left", padx=6)

    def clear_target(self):
        self.target_hwnd = None
        self.target_var.set("自动（倒计时结束时的前台窗口）")
        self.log("已清除指定目标窗口，恢复为自动模式")

    def connect_hid(self):
        try:
            self.hid.open(self.port_var.get().strip() or "COM3")
            self.kb.hid = self.hid
            self.hid_lbl.configure(text="已连接 %s" % self.hid.port, fg=C_ACCENT2)
            self.log("硬件键盘已连接：%s" % self.hid.port)
        except Exception as exc:                         # noqa: BLE001
            self.hid_lbl.configure(text="连接失败", fg=C_DANGER)
            self.log("硬件连接失败：%r" % (exc,))
            messagebox.showerror(APP_TITLE,
                                 "连接串口失败：%r\n\n请确认设备已插好，"
                                 "并在设备管理器里确认端口号（例如 COM3）。" % (exc,))

    def restart_admin(self):
        if messagebox.askyesno(APP_TITLE, "将以管理员身份重新启动本程序，当前窗口会关闭。\n继续？"):
            if relaunch_as_admin():
                self.on_close()
            else:
                messagebox.showwarning(APP_TITLE, "提权被取消或失败，程序仍以普通权限运行。")

    def _update_elevation(self):
        me = is_process_elevated(kernel32.GetCurrentProcessId())
        if me:
            self.elev_lbl.configure(text="本程序：管理员权限 ✔", fg=C_ACCENT2)
            self.elev_btn.pack_forget()
        else:
            self.elev_lbl.configure(
                text="本程序：普通权限（游戏若以管理员运行则收不到按键）", fg=C_ACCENT)
            self.elev_btn.pack(side="right")

    # ---------------- 关闭 ----------------
    def on_close(self):
        self._closing = True
        try:
            if self.player and self.player.is_alive():
                self.player.stop()
                self.player.join(timeout=1.0)
        except Exception:                                 # noqa: BLE001
            pass
        self.kb.release_all()
        self.ime.release()
        try:
            self.hid.close()
        except Exception:                                # noqa: BLE001
            pass
        if self.hotkeys:
            self.hotkeys.stop()
        self.settings = self._collect_settings()
        save_json(SETTINGS_FILE, self.settings)
        save_json(SCORES_FILE, self.scores)
        self.root.destroy()


def simpledialog_ask(parent, title, prompt, initial):
    from tkinter import simpledialog
    return simpledialog.askstring(title, prompt, initialvalue=initial, parent=parent)



def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
