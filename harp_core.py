# -*- coding: utf-8 -*-
"""三角洲口琴 · 核心逻辑（无界面）

简谱解析 / 键位映射 / 按键注入 / 输入法处理 / 进程诊断 / 演奏调度。
被 tkinter 版 harmonica_auto.py 和 PySide6 版 harp_qt.py 共用。

关于注入：
  * SendInput 的按键会进入系统输入队列，游戏用 GetAsyncKeyState /
    DirectInput / RawInput 轮询都能读到。
  * 中文输入法会把注入按键吃成 VK_PROCESSKEY，导致游戏完全收不到字符，
    所以演奏前会自动关掉目标窗口的输入法，结束后还原（见 ImeGuard）。
  * 内核级反作弊可能丢弃带「模拟」标记的按键，这种情况只能走串口硬件键盘
    （见 SerialHid 与 hardware/harmonica_hid.ino）。
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：配置/曲谱/日志要放在 exe 旁边，而不是临时解包目录
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCORES_FILE = os.path.join(APP_DIR, "scores.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
APP_TITLE = "三角洲口琴 · 自动按键器"

# 配色
C_BG = "#0f141b"
C_PANEL = "#182029"
C_FIELD = "#0b1118"
C_LINE = "#2a3646"
C_FG = "#e6edf3"
C_DIM = "#8d9bad"
C_ACCENT = "#f0b429"
C_ACCENT2 = "#2dd4bf"
C_DANGER = "#f87171"

# ============================================================================
# 1. 简谱解析
# ============================================================================
DEGREE_SEMITONE = {1: 0, 2: 2, 3: 4, 4: 5, 5: 7, 6: 9, 7: 11}
DEGREE_VK = {1: 0x5A, 2: 0x58, 3: 0x43, 4: 0x56, 5: 0x42, 6: 0x4E, 7: 0x4D}
KEY_LETTER = {1: "z", 2: "x", 3: "c", 4: "v", 5: "b", 6: "n", 7: "m"}
LETTER_DEGREE = {v: k for k, v in KEY_LETTER.items()}
SOLFEGE = {1: "do", 2: "re", 3: "mi", 4: "fa", 5: "sol", 6: "la", 7: "si", 8: "do'"}

# 游戏口琴的 8 个物理键（参考 ManboHakimi-Harp 实测：第 8 个键是逗号 = 高音 1）
SCALE_KEYS = ["z", "x", "c", "v", "b", "n", "m", ","]
SCALE_SEMI = [0, 2, 4, 5, 7, 9, 11, 12]
SCALE_VK = [0x5A, 0x58, 0x43, 0x56, 0x42, 0x4E, 0x4D, 0xBC]      # , = VK_OEM_COMMA


def resolve_note(degree, accidental):
    """简谱音级 + 升降号 -> (物理键下标 0-7, 半音档 -1/0/+1)。

    游戏口琴是半音阶口琴：8 个自然音键 + 推键整体升降半音。
    所以任何半音都用「低半音的那个键 + 升调档」吹出，
    例如 #4 用 fa 键 + 升调档；而 #3 等于 fa，直接用自然档按 fa 键。
    """
    if degree == 8:
        semi = 12 + accidental
    elif 1 <= degree <= 7:
        semi = DEGREE_SEMITONE[degree] + accidental
    else:
        return 0, 0
    while semi < 0:
        semi += 12
    while semi > 12:
        semi -= 12
    if semi in SCALE_SEMI:
        return SCALE_SEMI.index(semi), 0
    below = [i for i, s in enumerate(SCALE_SEMI) if s < semi]
    return (below[-1] if below else 0), 1


def to_shift_form(degree, accidental):
    """把降号转成等价的升号写法，供 Shift 方案使用（b3 就是 #2）。"""
    if accidental >= 0:
        return degree, accidental
    semi = (12 if degree == 8 else DEGREE_SEMITONE.get(degree, 0)) + accidental
    while semi < 0:
        semi += 12
    if semi in SCALE_SEMI:
        idx = SCALE_SEMI.index(semi)
        return (8 if idx == 7 else idx + 1), 0
    below = [i for i, s in enumerate(SCALE_SEMI) if s < semi]
    idx = below[-1] if below else 0
    return (8 if idx == 7 else idx + 1), 1


# 音符方案：两种，取决于游戏里升调到底怎么实现
NOTE_SCHEMES = (
    ("shift", "7 键 + Shift 升调"),
    ("slide", "8 键 + 鼠标推键（半音阶）"),
)
SCHEME_LABELS = dict(NOTE_SCHEMES)
SCHEME_BY_LABEL = {label: key for key, label in NOTE_SCHEMES}

# 推键对应的鼠标键：左=降调档 中=自然档 右=升调档
SLIDE_BUTTON = {-1: "left", 0: "middle", 1: "right"}

NOTE_RE = re.compile(r"^(#?)([1-8zxcvbnmZXCVBNM])(#?)([',]*)(?::(\d+(?:\.\d+)?))?$")
FLAT_RE = re.compile(r"^b([1-8])([',]*)(?::(\d+(?:\.\d+)?))?$")
REST_RE = re.compile(r"^0[',]*(?::(\d+(?:\.\d+)?))?$")


# 这些符号在简谱里常被当分隔符用，统一当空白处理
SEPARATOR_RE = re.compile(r"[、;；/]")

# 一行里切记号：| 单独成记号；其余按空白与分隔符切开
TOKEN_SCAN_RE = re.compile(r"\|+|[^|\s、;；/]+")


def iter_line_words(line):
    """从一行里切出 (记号文本, 起始列, 结束列)。

    只在原行上定位，不改变文本长度——这样记号位置能直接映射回原文。
    处理：// 注释截断、| 单独成记号、顿号分号斜杠当分隔符、逗号拆分。
    """
    cut = line.find("//")
    if cut >= 0:
        line = line[:cut]
    for m in TOKEN_SCAN_RE.finditer(line):
        chunk = m.group(0)
        base = m.start()
        if chunk[0] == "|" or chunk == "," or "," not in chunk:
            yield chunk, base, base + len(chunk)
            continue
        # 逗号夹在音符之间时当分隔符（1,2,3）
        cursor = 0
        for part in chunk.split(","):
            if part:
                i = chunk.find(part, cursor)
                yield part, base + i, base + i + len(part)
                cursor = i + len(part)


def normalize_text(s):
    """全角转半角；去 BOM。

    必须逐字符一一对应（BOM 换成空格而不是删掉），否则记号在原文里的位置会错位。
    """
    out = []
    for ch in str(s or ""):
        code = ord(ch)
        if code == 0xFEFF:                      # BOM，粘贴时常带
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code in (0x3000, 0x00A0):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def clamp_beats(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    if v <= 0:
        return 1.0
    return max(0.0625, min(64.0, v))


def _make_note(degree, accidental, octs, beats, raw):
    octs = octs or ""
    if degree == 1 and octs.count("'") >= 1:
        degree = 8                      # 1' 就是高音 1，也就是第 8 个键「,」
        octs = ""
    # 其余八度记号对只有 8 个键的口琴没有意义，接受但忽略
    return {"type": "note", "degree": degree, "accidental": accidental,
            "sharp": accidental > 0, "octave": 0,
            "beats": clamp_beats(beats) if beats else 1.0, "raw": raw}


def parse_word(word):
    if word == ",":
        # 逗号本身就是口琴的第 8 个键（高音 1）
        return _make_note(8, 0, "", None, word)
    m = FLAT_RE.match(word)
    if m:
        return _make_note(int(m.group(1)), -1, m.group(2), m.group(3), word)
    m = NOTE_RE.match(word)
    if m:
        pre, ch, post, octs, beats = m.groups()
        if ch.isdigit():
            degree = int(ch)
            accidental = 1 if (pre == "#" or post == "#") else 0
        else:
            degree = LETTER_DEGREE.get(ch.lower())
            if not degree:
                return None
            accidental = 1 if ch.isupper() else 0
        return _make_note(degree, accidental, octs, beats, word)
    m = REST_RE.match(word)
    if m:
        return {"type": "rest", "degree": 0, "accidental": 0, "sharp": False,
                "octave": 0, "beats": clamp_beats(m.group(1)) if m.group(1) else 1.0,
                "raw": word}
    return None


def token_label(tok):
    if tok["type"] == "bar":
        return "|"
    if tok["type"] == "rest":
        return "0"
    acc = tok.get("accidental", 1 if tok.get("sharp") else 0)
    return ("#" if acc > 0 else "b" if acc < 0 else "") + str(tok["degree"])


def token_key_text(tok, scheme="shift"):
    """这个音在实际键盘上要怎么按。"""
    if tok["type"] != "note":
        return ""
    degree = tok["degree"]
    acc = tok.get("accidental", 1 if tok.get("sharp") else 0)
    if scheme == "slide":
        idx, slide = resolve_note(degree, acc)
        letter = SCALE_KEYS[idx]
        if slide == 0:
            return letter
        return "%s+%s" % (letter, "右键↑" if slide > 0 else "左键↓")
    d, a = to_shift_form(degree, acc)
    if d == 8:
        return ","
    letter = KEY_LETTER.get(d)
    if not letter:
        return ""
    return ("Shift+" + letter.upper()) if a > 0 else letter


def parse_full(text):
    """曲谱文本 -> (tokens, errors, ignored)。

    errors 为 (行号, 记号, 说明)：带数字/字母却认不出来的记号。
    纯中文或纯符号的孤立词（标题行、歌词）不算错误，收进 ignored 里忽略掉。

    每个记号还会带上 start / end —— 它在整段原文里的字符区间，
    界面用它来做「选中卷帘里的音符 → 曲谱对应位置高亮」。
    """
    tokens, errors, ignored = [], [], []
    src = normalize_text(text)
    offset = 0
    for lineno, raw in enumerate(src.split("\n"), 1):
        line_start = offset
        offset += len(raw) + 1
        for word, ws, we in iter_line_words(raw):
            start, end = line_start + ws, line_start + we
            if word[0] == "|":
                tokens.append({"type": "bar", "degree": 0, "accidental": 0,
                               "sharp": False, "octave": 0, "beats": 0.0,
                               "raw": word, "line": lineno,
                               "start": start, "end": end})
                continue
            if word == "-":
                prev = tokens[-1] if tokens else None
                if prev and prev["type"] in ("note", "rest"):
                    prev["beats"] = min(64.0, prev["beats"] + 1.0)
                    prev["end"] = end          # 延长线也算进这个音的区间
                else:
                    errors.append((lineno, word, "「-」前面没有可延长的音"))
                continue
            tok = parse_word(word)
            if tok:
                tok["line"] = lineno
                tok["start"], tok["end"] = start, end
                tokens.append(tok)
                continue
            # 形如 6#1 的粘连写法（漏了空格）：拆成 6 和 #1
            if "#" in word[1:]:
                parts = [p for p in re.split(r"(?=#)", word) if p]
                subs = [parse_word(p) for p in parts]
                if len(subs) > 1 and all(subs):
                    cursor = 0
                    for part, sub in zip(parts, subs):
                        i = word.find(part, cursor)
                        sub["line"] = lineno
                        sub["start"], sub["end"] = start + i, start + i + len(part)
                        tokens.append(sub)
                        cursor = i + len(part)
                    continue
            if re.search(r"[0-9A-Za-z]", word):
                errors.append((lineno, word, "无法识别的记号"))
            else:
                ignored.append((lineno, word))       # 标题、歌词之类
    return tokens, errors, ignored


def parse_score(text):
    """兼容旧调用：只返回 (tokens, errors)。"""
    tokens, errors, _ignored = parse_full(text)
    return tokens, errors


def serialize_tokens(tokens):
    lines, cur = [], []

    def flush(suffix=""):
        s = (" ".join(cur) + suffix).strip()
        if s:
            lines.append(s)
        cur.clear()

    for tok in tokens:
        if tok["type"] == "bar":
            if cur:
                flush(" |")
            elif lines:
                lines[-1] += " |"
            continue
        cur.append(token_label(tok) if tok["beats"] == 1 else
                   "%s:%s" % (token_label(tok), _trim(tok["beats"])))
        if len(cur) >= 16:
            flush()
    flush()
    return "\n".join(lines)


def _trim(v):
    return str(round(float(v) * 1000) / 1000).rstrip("0").rstrip(".") or "0"


def playable(tokens):
    return [t for t in tokens if t["type"] in ("note", "rest")]


def total_beats(tokens):
    return sum(t["beats"] for t in playable(tokens))


def estimate_seconds(tokens, interval, bar_pause=0.0):
    """整曲预计耗时：音符拍数 × 间隔 + 小节线个数 × 小节停顿。"""
    bars = len([t for t in tokens if t["type"] == "bar"])
    return total_beats(tokens) * interval + bars * bar_pause


# ============================================================================
# 2. 键盘注入（SendInput）
# ============================================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)

ULONG_PTR = wintypes.WPARAM
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_SHIFT = 0x10
WM_INPUTLANGCHANGEREQUEST = 0x0050
KLF_ACTIVATE = 0x0001
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ULONG_PTR)
kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.BuildCommDCBW.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p)
kernel32.SetCommState.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
kernel32.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.LoadKeyboardLayoutW.argtypes = (wintypes.LPCWSTR, wintypes.UINT)
user32.LoadKeyboardLayoutW.restype = wintypes.HKL
user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
user32.GetKeyboardLayout.restype = wintypes.HKL
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)
imm32.ImmGetContext.argtypes = (wintypes.HWND,)
imm32.ImmGetContext.restype = wintypes.HANDLE
imm32.ImmAssociateContext.argtypes = (wintypes.HWND, wintypes.HANDLE)
imm32.ImmAssociateContext.restype = wintypes.HANDLE
imm32.ImmGetOpenStatus.argtypes = (wintypes.HANDLE,)
imm32.ImmGetOpenStatus.restype = wintypes.BOOL
imm32.ImmSetOpenStatus.argtypes = (wintypes.HANDLE, wintypes.BOOL)


class Keyboard(object):
    """按键注入。mode:
        both / scancode / vkod  —— SendInput 的三种参数组合
        post                    —— 直接给目标窗口发窗口消息（不要求前台）
        keybd                   —— 旧接口 keybd_event
        serial                  —— 通过串口驱动真 USB HID 键盘
    """

    def __init__(self):
        self.mode = "both"
        self.target = None       # PostMessage 模式的目标窗口（一般是焦点子窗口）
        self.hid = None          # SerialHid 实例
        self.cur_slide = 0       # 当前半音推键档位 -1/0/+1
        self.mouse_anchor = None  # 推键前把鼠标挪到这里（目标窗口中心）
        self.anchor_cursor = True     # 是否自动挪鼠标
        self.reset_after_slide = True  # click 模式：半音奏完是否立即切回自然档
        self.slide_mode = "hold"       # hold=按住推键 / click=点一下切档
        self.slide_lead_ms = 30        # hold 模式：鼠标比按键早按下的时间
        self.slide_tail_ms = 30        # hold 模式：按键松开后鼠标再按多久
        self._saved_cursor = None
        self._lock = threading.Lock()
        self._down = set()

    VK_CHAR = {0x5A: "z", 0x58: "x", 0x43: "c", 0x56: "v", 0x42: "b",
               0x4E: "n", 0x4D: "m", 0xBC: ","}
    MOUSE_FLAGS = {"left": (0x0002, 0x0004), "middle": (0x0020, 0x0040),
                   "right": (0x0008, 0x0010)}

    def _event(self, vk, up=False):
        scan = user32.MapVirtualKeyW(vk, 0)
        if self.mode == "scancode":
            wvk, wscan, flags = 0, scan, KEYEVENTF_SCANCODE
        elif self.mode == "vkod":
            wvk, wscan, flags = vk, 0, 0
        else:
            wvk, wscan, flags = vk, scan, KEYEVENTF_SCANCODE
        if up:
            flags |= KEYEVENTF_KEYUP
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(wVk=wvk, wScan=wscan, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def _send(self, events):
        if not events:
            return True
        arr = (INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
        return sent == len(events)

    def _post(self, vk, up=False):
        """给目标窗口直接投递 WM_KEYDOWN / WM_KEYUP（不需要窗口在前台）。"""
        if not self.target:
            return False
        scan = user32.MapVirtualKeyW(vk, 0)
        lparam = 1 | (scan << 16)
        if up:
            lparam |= (1 << 30) | (1 << 31)
        return bool(user32.PostMessageW(self.target,
                                        WM_KEYUP if up else WM_KEYDOWN, vk, lparam))

    def _key(self, vk, up=False):
        """按当前模式送出一次按键事件。"""
        if self.mode == "post":
            return self._post(vk, up)
        if self.mode == "keybd":
            user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0), 2 if up else 0, 0)
            return True
        return self._send([self._event(vk, up=up)])

    def key_down(self, vk):
        with self._lock:
            ok = self._key(vk)
            if ok:
                self._down.add(vk)
            return ok

    def key_up(self, vk):
        with self._lock:
            ok = self._key(vk, up=True)
            self._down.discard(vk)
            return ok

    def _mouse_event(self, flag):
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0,
                            dwExtraInfo=0)
        return inp

    def mouse_down(self, button):
        flags = self.MOUSE_FLAGS.get(button)
        if not flags:
            return False
        with self._lock:
            return self._send([self._mouse_event(flags[0])])

    def mouse_up(self, button):
        flags = self.MOUSE_FLAGS.get(button)
        if not flags:
            return False
        with self._lock:
            return self._send([self._mouse_event(flags[1])])

    def mouse_click(self, button, hold_ms=30):
        """点一下鼠标。游戏口琴的推键就是鼠标键：左=降调 中=自然 右=升调。"""
        if not self.mouse_down(button):
            return False
        time.sleep(max(0.01, hold_ms / 1000.0))
        self.mouse_up(button)
        return True

    def _point_cursor(self):
        """把光标挪到目标窗口中心，否则鼠标事件会落到别的窗口上。"""
        if self.anchor_cursor and self.mouse_anchor:
            try:
                user32.SetCursorPos(int(self.mouse_anchor[0]),
                                    int(self.mouse_anchor[1]))
                time.sleep(0.02)
            except Exception:                            # noqa: BLE001
                pass

    def tap_vk(self, vk, shift=False, hold_ms=50):
        """按下并松开一个虚拟键码；shift=True 时先按住 Shift。"""
        hold = max(0.005, hold_ms / 1000.0)
        if self.mode == "serial":
            if self.hid is None or not self.hid.handle:
                raise RuntimeError("硬件模式还没连上串口")
            ch = self.VK_CHAR.get(vk)
            if ch is None:
                return
            tag = "S" if shift else ""
            self.hid.write("+%s%s" % (tag, ch))
            time.sleep(hold)
            self.hid.write("-%s%s" % (tag, ch))
            return
        with self._lock:
            if shift:
                self._key(VK_SHIFT)
            self._key(vk)
            self._down.add(vk)
        time.sleep(hold)
        with self._lock:
            self._key(vk, up=True)
            self._down.discard(vk)
            if shift:
                self._key(VK_SHIFT, up=True)

    def tap_letter(self, degree, sharp, hold_ms):
        """虚拟键盘与注入自检用：按度数直接发一个音。"""
        if degree == 8:
            self.tap_vk(SCALE_VK[7], False, hold_ms)
        else:
            self.tap_vk(DEGREE_VK.get(degree, 0x5A), bool(sharp), hold_ms)

    def tap_note(self, degree, accidental, scheme, hold_ms):
        """演奏用：按音符方案决定实际按哪个键、要不要推键。

        推键有两种可能的手感，用 slide_mode 选：

          "hold"（默认，半音阶口琴的实际做法）
              推键是「按住」的——鼠标键按住期间音才升高。
              所以必须在按键之前按下鼠标、等音吹完再松开；
              只点一下再按键的话，按键时推键已经回位了，吹出来还是自然音。

          "click"
              推键是「点一下切档」的：点一次保持住，再点中键回自然档。
        """
        if scheme == "slide":
            idx, slide = resolve_note(degree, accidental)
            vk = SCALE_VK[idx]
            if slide != 0 and self.slide_mode == "hold":
                self._point_cursor()
                self.mouse_down(SLIDE_BUTTON[slide])
                time.sleep(max(0.005, self.slide_lead_ms / 1000.0))
                self.tap_vk(vk, False, hold_ms)
                time.sleep(max(0.005, self.slide_tail_ms / 1000.0))
                self.mouse_up(SLIDE_BUTTON[slide])
                self.cur_slide = 0
                return
            if slide != self.cur_slide:
                self._click_slide(slide)
                self.cur_slide = slide
            self.tap_vk(vk, False, hold_ms)
            if slide != 0 and self.reset_after_slide:
                # 参考项目文档的做法：半音吹完立刻切回自然档，
                # 免得游戏的推键状态和程序记录的对不上
                time.sleep(0.03)
                self._click_slide(0)
                self.cur_slide = 0
        else:
            d, a = to_shift_form(degree, accidental)
            if d == 8:
                self.tap_vk(SCALE_VK[7], False, hold_ms)
            else:
                self.tap_vk(DEGREE_VK.get(d, 0x5A), a > 0, hold_ms)

    # ---- 推键（鼠标）辅助 ----
    def _click_slide(self, slide):
        """点一下鼠标切推键档（click 模式用）。"""
        self._point_cursor()
        return self.mouse_click(SLIDE_BUTTON[slide])

    def save_cursor(self):
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            self._saved_cursor = (pt.x, pt.y)

    def restore_cursor(self):
        if self._saved_cursor:
            try:
                user32.SetCursorPos(int(self._saved_cursor[0]),
                                    int(self._saved_cursor[1]))
            except Exception:                            # noqa: BLE001
                pass
            self._saved_cursor = None

    def set_anchor_from_window(self, hwnd):
        """把推键落点设成目标窗口中心。"""
        rect = wintypes.RECT()
        if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            self.mouse_anchor = ((rect.left + rect.right) // 2,
                                 (rect.top + rect.bottom) // 2)
            return True
        return False

    def reset_slide(self):
        """把推键切回自然档，避免演奏结束后游戏停在升调档。"""
        if self.cur_slide != 0:
            try:
                self.mouse_click("middle")
            except Exception:                            # noqa: BLE001
                pass
            self.cur_slide = 0

    def hold_shift(self):
        with self._lock:
            self._key(VK_SHIFT)
            self._down.add(VK_SHIFT)

    def release_all(self):
        """把可能卡住的键全部松开。

        Shift 卡住会让游戏一直处于大写状态，所以即使没记录到也补一次抬起。
        """
        with self._lock:
            if self.mode == "serial":
                if self.hid and self.hid.handle:
                    try:
                        self.hid.write("-S")
                    except Exception:                    # noqa: BLE001
                        pass
                self._down.clear()
                return
            pending = set(self._down)
            self._down.clear()
            if self.mode == "post":
                self._post(VK_SHIFT, up=True)
                for vk in pending:
                    self._post(vk, up=True)
                return
            if self.mode == "keybd":
                user32.keybd_event(VK_SHIFT, 0, 2, 0)
                for vk in pending:
                    user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0), 2, 0)
                return
            events = [self._event(VK_SHIFT, up=True)]
            for vk in pending:
                events.append(self._event(vk, up=True))
            self._send(events)


def foreground_window():
    return user32.GetForegroundWindow()


def window_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def focused_child(hwnd):
    """取该窗口所属线程里真正拥有键盘焦点的那个子窗口。

    输入法上下文是按窗口关联的，而按键送给的是焦点窗口而不是顶层窗口，
    所以关输入法必须作用在焦点窗口上（例如 Tk 的 Entry、游戏的渲染子窗口）。
    """
    if not hwnd:
        return hwnd
    try:
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
            return int(gti.hwndFocus)
    except Exception:                                    # noqa: BLE001
        pass
    return hwnd


def force_foreground(hwnd):
    if user32.GetForegroundWindow() == hwnd:
        return True
    try:
        tid_fg = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        tid_me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(tid_me, tid_fg, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(tid_me, tid_fg, False)
    except Exception:                                    # noqa: BLE001
        return False
    time.sleep(0.05)
    return user32.GetForegroundWindow() == hwnd


# ============================================================================
# 3. 输入法处理
# ============================================================================
class ImeGuard(object):
    """演奏期间关掉目标窗口的输入法，结束后还原。

    中文输入法打开时，注入的按键会变成 WM_KEYDOWN(VK_PROCESSKEY) 且不产生
    WM_CHAR，游戏里表现为「完全没反应」。这是本程序最关键的兼容处理。
    """

    def __init__(self):
        self.hwnd = None
        self.detached = []          # [(hwnd, 原上下文), ...]
        self.ctx_seen = 0
        self.old_hkl = None
        self.changed_layout = False

    def engage(self, hwnd):
        self.release()
        self.hwnd = hwnd
        if not hwnd:
            return
        # 顶层窗口 + 真正有焦点的子窗口都要处理：输入法上下文是按窗口关联的。
        targets = []
        focus = focused_child(hwnd)
        for h in (hwnd, focus):
            if h and h not in targets:
                targets.append(h)
        self.focus_hwnd = focus
        for h in targets:
            # 先关「打开状态」，再无条件解除上下文。
            # 解除上下文是让注入按键能变成 WM_CHAR 的关键，不能放进 if 里。
            try:
                ctx = imm32.ImmGetContext(h)
                if ctx:
                    self.ctx_seen = int(ctx)
                    imm32.ImmSetOpenStatus(ctx, False)
            except Exception:                            # noqa: BLE001
                pass
            try:
                old = imm32.ImmAssociateContext(h, None)
                self.detached.append((h, old))
            except Exception:                            # noqa: BLE001
                pass
        try:
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            self.old_hkl = user32.GetKeyboardLayout(tid)
            hkl_en = user32.LoadKeyboardLayoutW("00000409", KLF_ACTIVATE)
            if hkl_en:
                user32.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, hkl_en)
                self.changed_layout = True
        except Exception:                                # noqa: BLE001
            pass

    def release(self):
        for h, old in self.detached:
            try:
                imm32.ImmAssociateContext(h, old)
            except Exception:                            # noqa: BLE001
                pass
        if self.hwnd and self.changed_layout and self.old_hkl:
            try:
                user32.PostMessageW(self.hwnd, WM_INPUTLANGCHANGEREQUEST,
                                    0, self.old_hkl)
            except Exception:                            # noqa: BLE001
                pass
        self.hwnd = None
        self.detached = []
        self.old_hkl = None
        self.changed_layout = False


# ============================================================================
# 3.5 进程/窗口诊断与串口硬件支持
# ============================================================================
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
LIST_MODULES_ALL = 0x03
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
kernel32.IsWow64Process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE))
psapi.EnumProcessModulesEx.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
psapi.GetModuleBaseNameW.argtypes = (wintypes.HANDLE, wintypes.HMODULE,
                                     wintypes.LPWSTR, wintypes.DWORD)
user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
shell32.ShellExecuteW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int)

# 常见反作弊/保护模块关键字：命中说明游戏很可能在过滤模拟输入
SUSPECT_MODULES = ("ace", "anticheat", "anti-cheat", "easyanticheat", "eac",
                   "battleye", "beservice", "tenprotect", "tp3", "sguard",
                   "xhunter", "npgame", "gameguard", "mhyprot", "vanguard",
                   "safemon", "qmproxy")


def is_process_elevated(pid):
    """进程是否以管理员（高完整性）运行；查不到返回 None。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            value = ctypes.c_ulong(0)
            size = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(value),
                ctypes.sizeof(value), ctypes.byref(size))
            return bool(value.value) if ok else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def process_path(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(handle)
    return None


def process_is_wow64(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        flag = wintypes.BOOL()
        if kernel32.IsWow64Process(handle, ctypes.byref(flag)):
            return bool(flag.value)
    finally:
        kernel32.CloseHandle(handle)
    return None


def suspect_modules(pid):
    """列出目标进程中疑似反作弊/保护模块。返回 None 表示读不到（本身也是信号）。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        count = 2048
        arr = (wintypes.HMODULE * count)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcessModulesEx(handle, ctypes.byref(arr), ctypes.sizeof(arr),
                                          ctypes.byref(needed), LIST_MODULES_ALL):
            return None
        total = min(needed.value // ctypes.sizeof(wintypes.HMODULE), count)
        found = []
        for i in range(total):
            name = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleBaseNameW(handle, arr[i], name, 260):
                low = name.value.lower()
                if any(key in low for key in SUSPECT_MODULES):
                    found.append(name.value)
        return found
    finally:
        kernel32.CloseHandle(handle)


def list_windows():
    """列出所有可见且有标题的顶层窗口。"""
    result = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if title.value.strip():
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                result.append({"hwnd": int(hwnd), "title": title.value,
                               "cls": cls.value, "pid": window_pid(hwnd)})
        return True

    user32.EnumWindows(callback, 0)
    return result


def describe_window(hwnd):
    """一行描述一个窗口，用于日志。"""
    if not hwnd:
        return "(无)"
    pid = window_pid(hwnd)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    return "%s（PID %d，类名 %s）" % (window_title(hwnd) or "(无标题)", pid, cls.value)


def diagnose_window(hwnd):
    """对目标窗口做一次完整体检，返回 (结论列表, 建议列表)。"""
    findings, advice = [], []
    if not hwnd:
        return ["没有目标窗口"], ["先在游戏里打开口琴界面，再点『选择目标窗口』选中游戏"]

    title = window_title(hwnd)
    pid = window_pid(hwnd)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    path = process_path(pid)
    elevated = is_process_elevated(pid)
    wow64 = process_is_wow64(pid)
    mine = kernel32.GetCurrentProcessId()
    my_elevated = is_process_elevated(mine)

    findings.append("目标窗口：%s" % (title or "(无标题)"))
    findings.append("窗口类名：%s" % cls.value)
    findings.append("进程：PID %d  %s" % (pid, os.path.basename(path) if path else "?"))
    findings.append("程序位数：%s" % ("32 位(WOW64)" if wow64 else
                                      ("64 位" if wow64 is False else "无法读取")))
    findings.append("是否管理员运行：%s" % ({True: "是", False: "否", None: "无法读取"}
                                            [elevated]))
    findings.append("本程序是否管理员运行：%s" % ("是" if my_elevated else "否"))

    if elevated and not my_elevated:
        advice.append("★ 游戏是管理员权限、本程序不是。Windows 会拦掉低权限进程发给"
                      "高权限窗口的一切模拟输入。请点『以管理员身份重启』。")

    focus = focused_child(hwnd)
    findings.append("焦点子窗口：%s" % (focus if focus != hwnd else "就是顶层窗口本身"))

    mods = suspect_modules(pid)
    if mods is None:
        findings.append("读取目标进程模块：被拒绝")
        advice.append("读不到目标进程的模块列表，通常说明游戏有内核级保护。"
                      "这种保护一般也会过滤模拟按键。")
    else:
        findings.append("疑似反作弊模块：%s" % ("、".join(mods) if mods else "未发现"))
        if mods:
            advice.append("★ 检测到反作弊模块（%s）。内核级反作弊会丢弃带「模拟」标记的"
                          "按键，这种情况下软件注入基本无解，只能用硬件键盘方案。"
                          % "、".join(mods))

    if window_pid(hwnd) == mine:
        advice.append("★ 目标是本程序自己，请选择真正的游戏窗口。")

    if not advice:
        advice.append("目标进程没有明显的权限或保护问题。请依次尝试不同的『注入方式』："
                      "键码+扫描码 → 仅扫描码 → 仅虚拟键码 → 窗口消息(PostMessage)。")
    return findings, advice


class SerialHid(object):
    """通过串口驱动真正的 USB HID 键盘（Arduino Pro Micro / Leonardo 等）。

    这类设备上报的按键和真键盘完全一样，内核级反作弊无法区分，
    是所有软件注入都失效时的可靠方案。协议见 hardware/harmonica_hid.ino。
    """

    def __init__(self):
        self.handle = None
        self.port = "COM3"
        self.baud = 115200

    def open(self, port=None, baud=None):
        self.close()
        if port:
            self.port = port
        if baud:
            self.baud = baud
        path = "\\\\.\\%s" % self.port
        handle = kernel32.CreateFileW(path, 0x40000000, 0, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value or handle is None:
            raise OSError("打不开串口 %s（错误码 %d）" % (self.port, ctypes.get_last_error()))
        self.handle = handle
        dcb = ctypes.create_string_buffer(64)
        ctypes.memset(dcb, 0, 64)
        ctypes.cast(dcb, ctypes.POINTER(wintypes.DWORD))[0] = ctypes.sizeof(dcb)
        cmd = ("baud=%d parity=n data=8 stop=1" % self.baud).encode("ascii")
        if kernel32.BuildCommDCBW(cmd, dcb):
            kernel32.SetCommState(handle, dcb)
        time.sleep(2.0)          # 等 Arduino 复位完成
        return True

    def write(self, text):
        if not self.handle:
            return False
        data = (text + "\n").encode("ascii", "ignore")
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self.handle, data, len(data),
                                ctypes.byref(written), None)
        return bool(ok) and written.value == len(data)

    def close(self):
        if self.handle:
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:                            # noqa: BLE001
                pass
            self.handle = None


def relaunch_as_admin():
    """以管理员身份重新启动自己。"""
    params = '"%s"' % os.path.abspath(sys.argv[0])
    rc = shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return rc > 32


# ============================================================================
# 4. 演奏线程
# ============================================================================
class Player(threading.Thread):
    """按曲谱定时发送按键。通过 queue 把状态回报给界面。"""

    def __init__(self, report, tokens, opts):
        threading.Thread.__init__(self, daemon=True)
        self.report = report
        self.tokens = list(tokens)
        self.interval = opts["interval"]
        self.hold_ms = opts["hold_ms"]
        self.countdown = opts["countdown"]
        self.lock_window = opts["lock_window"]
        self.own_pid = opts["own_pid"]
        self.kb = opts["keyboard"]
        self.ime = opts["ime"]
        self.explicit_target = opts.get("target_hwnd") or None
        self.scheme = opts.get("scheme", "shift")
        self.slide_reset = opts.get("slide_reset", True)
        self.anchor_cursor = opts.get("anchor_cursor", True)
        self.slide_mode = opts.get("slide_mode", "hold")
        self.slide_lead_ms = opts.get("slide_lead_ms", 30)
        self.slide_tail_ms = opts.get("slide_tail_ms", 30)
        self.bar_pause = max(0.0, opts.get("bar_pause", 0.0))
        # 窗口消息模式不需要前台；但推键方案要发鼠标，鼠标只能点前台窗口
        self.need_focus = (self.kb.mode not in ("post", "serial")
                           or self.scheme == "slide")
        self.stop_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.target = None

    # ---- 外部控制 ----
    def stop(self):
        self.stop_evt.set()
        self.pause_evt.clear()

    def toggle_pause(self):
        if self.pause_evt.is_set():
            self.pause_evt.clear()
        else:
            self.pause_evt.set()

    # ---- 主循环 ----
    def run(self):
        try:
            self._run()
        except Exception as exc:                         # noqa: BLE001
            self.report(("error", "演奏异常：%r" % (exc,)))
        finally:
            try:
                self.kb.reset_slide()
            except Exception:                            # noqa: BLE001
                pass
            try:
                self.kb.restore_cursor()
            except Exception:                            # noqa: BLE001
                pass
            self.kb.release_all()
            self.ime.release()
            self.report(("ended", None))

    def _run(self):
        # 小节线也当成一个「停一下」的步骤参与计时
        steps = [t for t in self.tokens if t["type"] in ("note", "rest", "bar")]
        items = [t for t in steps if t["type"] != "bar"]
        if not items:
            self.report(("error", "曲谱为空，没有可演奏的音"))
            return

        # 倒计时（留时间切换到游戏窗口）
        if self.countdown > 0:
            end = time.perf_counter() + self.countdown
            while True:
                left = end - time.perf_counter()
                if left <= 0 or self.stop_evt.is_set():
                    break
                self.report(("countdown", left))
                time.sleep(0.05)
            if self.stop_evt.is_set():
                return

        # 锁定目标窗口
        if self.explicit_target:
            self.target = self.explicit_target
            self.ime.engage(self.target)
            self.kb.target = focused_child(self.target)
            self.report(("target", window_title(self.target)))
            self.report(("log", "使用指定目标窗口：%s" % describe_window(self.target)))
            self.report(("log", "按键将投递到子窗口 %s"
                         % describe_window(self.kb.target)))
        elif self.lock_window:
            hwnd = foreground_window()
            if not hwnd:
                self.report(("error", "拿不到前台窗口，已停止"))
                return
            if window_pid(hwnd) == self.own_pid:
                self.report(("error", "前台还是本程序自己，没有切到游戏窗口，已停止"))
                return
            self.target = hwnd
            self.ime.engage(hwnd)
            self.kb.target = focused_child(hwnd)
            self.report(("target", window_title(hwnd)))
            self.report(("log", "锁定前台窗口：%s" % describe_window(hwnd)))
        else:
            self.ime.engage(foreground_window())
            self.kb.target = focused_child(foreground_window())
        self.report(("log", "注入方式：%s%s" % (self.kb.mode,
                    "（不需要窗口在前台）" if not self.need_focus else "（需要窗口保持在前台）")))

        if self.scheme == "slide":
            self.kb.anchor_cursor = self.anchor_cursor
            self.kb.reset_after_slide = self.slide_reset
            self.kb.slide_mode = self.slide_mode
            self.kb.slide_lead_ms = self.slide_lead_ms
            self.kb.slide_tail_ms = self.slide_tail_ms
            self.kb.save_cursor()
            anchor = self.target or foreground_window()
            if self.anchor_cursor and self.kb.set_anchor_from_window(anchor):
                self.report(("log", "推键落点设在窗口中心 %s"
                             % (self.kb.mouse_anchor,)))
            self.report(("log", "推键方式：%s"
                         % ("按住（鼠标比按键早 %dms 按下、晚 %dms 松开）"
                            % (self.slide_lead_ms, self.slide_tail_ms)
                            if self.slide_mode == "hold" else
                            ("点一下切档，吹完%s切回自然档"
                             % ("立即" if self.slide_reset else "不")))))
        if self.bar_pause > 0:
            self.report(("log", "小节线后停顿 %.2f 秒" % self.bar_pause))

        deadline = time.perf_counter()
        total = len(items)
        total_beats = sum(t["beats"] for t in items) or 1.0
        total_secs = (total_beats * self.interval
                      + self.bar_pause * len([t for t in steps if t["type"] == "bar"]))
        done_beats = 0.0
        done_secs = 0.0
        played = 0
        for tok in steps:
            if self.stop_evt.is_set():
                return
            # 用 SendInput 时按键只会进前台窗口，切走就停，避免打到别处
            if self.need_focus and self.target and foreground_window() != self.target:
                self.report(("error", "目标窗口失去焦点，已自动停止"))
                return

            if tok["type"] == "bar":
                if self.bar_pause <= 0:
                    continue
                deadline += self.bar_pause
                done_secs += self.bar_pause
                self.report(("bar", (done_secs, total_secs)))
                ok, deadline = self._wait(deadline)
                if not ok:
                    return
                continue

            if tok["type"] == "note":
                self.kb.tap_note(tok["degree"], tok.get("accidental", 0),
                                 self.scheme, self.hold_ms)
                if played < 12 or played % 10 == 0:
                    self.report(("log", "第 %d/%d 个：%s → %s"
                                 % (played + 1, total, token_label(tok),
                                    token_key_text(tok, self.scheme))))
            played += 1
            done_beats += tok["beats"]
            done_secs += tok["beats"] * self.interval
            self.report(("note", (played - 1, total, tok, done_secs,
                                  total_secs, done_beats)))

            deadline += max(0.02, tok["beats"] * self.interval)
            ok, deadline = self._wait(deadline)
            if not ok:
                return
        self.report(("finished", total))

    def _wait(self, deadline):
        """等到 deadline；期间响应暂停与停止。返回 (是否继续, 修正后的 deadline)。"""
        while True:
            if self.stop_evt.is_set():
                return False, deadline
            if self.pause_evt.is_set():
                paused_at = time.perf_counter()
                self.report(("paused", None))
                while self.pause_evt.is_set() and not self.stop_evt.is_set():
                    time.sleep(0.03)
                deadline += time.perf_counter() - paused_at
                self.report(("resumed", None))
                continue
            left = deadline - time.perf_counter()
            if left <= 0:
                return True, deadline
            time.sleep(min(left, 0.02))


class HotkeyThread(threading.Thread):
    """全局热键（F8 启动/暂停、F9 停止、F12 紧急停止）。"""

    def __init__(self, report):
        threading.Thread.__init__(self, daemon=True)
        self.report = report
        self.tid = None
        self.ready = threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        ids = []
        for hid, vk in ((1, 0x77), (2, 0x78), (3, 0x7B)):     # F8 F9 F12
            if user32.RegisterHotKey(None, hid, MOD_NOREPEAT, vk):
                ids.append(hid)
        self.ready.set()
        if not ids:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.report(msg.wParam)
        for hid in ids:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


# ============================================================================
# 5. 存储
# ============================================================================
DEFAULT_SETTINGS = {
    "interval": 1000, "hold_ms": 50, "countdown": 3, "sharp_mode": "shift",
    "mode": "both", "lock_window": True, "hotkeys": True, "topmost": True,
    "minimize": True, "draft_name": "小星星", "draft_text": "",
    "auto_ime": True, "scheme": "slide", "port": "COM3",
    "slide_reset": True, "anchor_cursor": True, "slide_mode": "hold",
    "slide_lead_ms": 30, "slide_tail_ms": 30, "bar_pause_ms": 0,
}


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, type(fallback)) else fallback
    except Exception:                                    # noqa: BLE001
        return fallback


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:                             # noqa: BLE001
        print("保存失败 %s: %r" % (path, exc))
        return False


# ============================================================================
# 6. 示例曲谱
# ============================================================================
INJECT_MODES = (
    ("both", "键码+扫描码（推荐）"),
    ("scancode", "仅扫描码"),
    ("vkod", "仅虚拟键码"),
    ("post", "窗口消息 PostMessage"),
    ("keybd", "旧接口 keybd_event"),
    ("serial", "硬件键盘（串口）"),
)
INJECT_LABELS = dict(INJECT_MODES)
INJECT_BY_LABEL = {label: key for key, label in INJECT_MODES}

SAMPLES = {
    "小星星": "1 1 5 5 6 6 5:2 | 4 4 3 3 2 2 1:2\n"
              "5 5 4 4 3 3 2:2 | 5 5 4 4 3 3 2:2\n"
              "1 1 5 5 6 6 5:2 | 4 4 3 3 2 2 1:2",
    "两只老虎": "1 2 3 1 | 1 2 3 1 | 3 4 5:2 | 3 4 5:2\n"
                "5 6 5 4 3 1 | 5 6 5 4 3 1 | 1 5, 1:2 | 1 5, 1:2",
    "生日快乐": "5:0.5 5:0.5 6 5 1' 7 | 5:0.5 5:0.5 6 5 2' 1' |\n"
                "5:0.5 5:0.5 5' 3' 1' 7 6 | 4':0.5 4':0.5 3' 1' 2' 1'",
    "14 键全音阶": "1 #1 2 #2 3 4 #4 5 #5 6 #6 7",
    "大调音阶": "1 2 3 4 5 6 7 8",
}

# 「试吹」用：走完游戏口琴的全部 14 个键
TEST_TOKENS = [t for t in parse_score("1 #1 2 #2 3 #3 4 #4 5 #5 6 #6 7 #7")[0]
               if t["type"] == "note"]

# 「半音测试」用：自然音与同一个键的升调交替，用来判断升调到底怎么触发
TEST_ACCIDENTAL = [t for t in parse_score("1 #1 2 #2 4 #4 5 #5")[0]
                   if t["type"] == "note"]


# 界面用的帮助文本（tkinter 版与 Qt 版共用）
HELP_TEXT = """【导入的 txt 长什么样】
  就是一份纯文本简谱，没有任何格式要求。文件编码建议 UTF-8（带不带 BOM 都行），
  文件名会自动成为曲名。下面这些写法都能直接导入：
      // 小星星               ← 以 // 开头的行是注释，不会被演奏
      小星星                   ← 光秃秃的标题行会被忽略，不算错误
      1 1 5 5 | 6 6 5:2       ← 音符用空格分开，| 是小节线
      4、4、3 3，2 2 1:2       ← 顿号/逗号/分号/斜杠分隔也可以，全角自动转半角
      1 一闪 1 一闪 5 亮晶晶    ← 音符后面跟中文歌词会被自动忽略
  「曲谱」面板右下角会显示解析结果；只有带数字或字母的乱符号才算错误（例如 (2) xx）。

【游戏口琴的两种按键方案】按「音符方案」选一种
  A. 7 键 + Shift 升调：z x c v b n m = 1-7，Shift+字母 = #1-#7，逗号 = 高音 1
  B. 8 键 + 鼠标推键（半音阶口琴）：物理键固定是 z x c v b n m ,  共 8 个，
     半音靠口琴的推键切档：鼠标左键=降调档 / 中键=自然档 / 右键=升调档。
     程序会自动算「该用哪个键 + 哪个档」，并在需要时点一下鼠标切档。
  （参考 ManboHakimi-Harp 的实测：游戏口琴是 8 键半音阶口琴，升调走鼠标推键而不是 Shift。
    如果你确认自己那边就是 Shift 升调，就选 A。）

【曲谱写法】音符用空格分开，| 是小节线（不发声）
  1 - 7        基本音 do-si
  8 或 1'      高音 1（就是逗号键）
  #4 或 4#     升半音；b3 降半音（b3 等价 #2，程序自动换算）
  :n           时值 n 倍，默认 1 拍（1 拍 = 「音符间隔」）
  -            把上一个音延长 1 拍
  0            休止符
  ' 与 ,       高/低八度记号：口琴只有 8 个键，记号会被接受但忽略
  //           行内注释

【正式演奏步骤】
  1. 写好或导入曲谱，调好「音符间隔」（默认 1.00 秒）
  2. 点「启动演奏」，倒计时期间切到游戏窗口并打开口琴界面
  3. 倒计时结束后自动按键；切走会立刻停止（默认锁定目标窗口）
  4. 随时按 F9 停止、F12 紧急停止、F8 暂停/继续（需启用全局热键）

【游戏里完全没反应？按顺序做】
  1. 点「注入自检」——确认本机能不能把按键送进窗口
  2. 点「诊断」——检查游戏是否管理员权限、是否有反作弊模块
  3. 依次换「注入方式」：键码+扫描码 → 仅扫描码 → 仅虚拟键码 → PostMessage
  4. 游戏若以管理员运行，本程序也要「以管理员身份重启」
  5. 都不行：多半是内核级反作弊（ACE 等）丢弃了模拟按键。
     这种只能走硬件方案：按 hardware/harmonica_hid.ino 烧一块 Pro Micro，
     注入方式选「硬件键盘（串口）」——它上报的是真 USB 键盘，系统无法区分。
"""

