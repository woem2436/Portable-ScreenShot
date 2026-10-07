"""Portable ScreenShot - 按键序列截图 + 轻量配置界面。

线程模型：主线程 Tkinter 设置窗口；hook 线程装 WH_KEYBOARD_LL 低级键盘钩子并按“按下顺序”
匹配绑定的键序列（抓屏交给 drain 线程，钩子回调必须立刻返回）；托盘线程由 pystray 管理。
config.json 与 exe 同目录，保持便携。
"""

import ctypes
import datetime
import json
import os
import queue
import sys
import threading
import time
import winsound
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_SNAPSHOT = 0x2C
VK_ESCAPE = 0x1B
VK_BACK = 0x08
WINDOW_TITLE = "Portable ScreenShot 设置"
GUI_SCALE = 2.0

# 绑定是“按下顺序”而非“同时按住”，所以最多记 SEQ_MAX 个键，
# 相邻两键间隔超过 SEQ_TIMEOUT 秒就重新计数，避免隔了很久的两次按键凑成一次触发。
SEQ_MAX = 4
SEQ_TIMEOUT = 1.0

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
WM_APP = 0x8000
QS_ALLINPUT = 0x04FF
LLKHF_UP = 0x80
PM_REMOVE = 1
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

VKEY_NAMES = {
    VK_SNAPSHOT: "PrtSc", VK_ESCAPE: "Esc", 0x0D: "Enter", 0x09: "Tab", 0x20: "空格",
    0x08: "Backspace", 0x2D: "Insert", 0x2E: "Delete", 0x21: "PageUp", 0x22: "PageDown",
    0x24: "Home", 0x23: "End", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt", 0x13: "Pause", 0x14: "CapsLock",
    0x5B: "LWin", 0x5C: "RWin", 0xBA: ";", 0xBF: "/", 0xC0: "`", 0xDE: "'",
    0xBD: "-", 0xBB: "=", 0xDC: "\\", 0xDB: "[", 0x5D: "Apps",
}
for _i in range(15):
    VKEY_NAMES[0x70 + _i] = f"F{_i + 1}"
for _i in range(10):
    VKEY_NAMES[0x30 + _i] = str(_i)
    VKEY_NAMES[0x60 + _i] = f"Num{_i}"
for _i in range(26):
    VKEY_NAMES[0x41 + _i] = chr(0x41 + _i)


def key_name(vk):
    return VKEY_NAMES.get(int(vk), f"0x{int(vk):02X}")


def hotkey_label(seq):
    if not seq:
        return "未绑定"
    return " → ".join(key_name(vk) for vk in seq)


def normalize_vk(code):
    code = int(code)
    # 按键事件偶尔把字母带成小写 ASCII（0x61-0x7A），而虚拟键码只有 0x41-0x5A 这一段，
    # 照原样存下来就永远匹配不上真实按键。
    return code - 0x20 if 0x61 <= code <= 0x7A else code


def normalize_seq(value):
    if value is None:
        return []
    out = []
    for code in value:
        vk = normalize_vk(code)
        if vk not in out:
            out.append(vk)
    return out[:SEQ_MAX]


def is_frozen():
    return getattr(sys, "frozen", False)


def app_dir():
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    "hotkey_seq": [VK_SNAPSHOT],
    "format": "png",
    "quality": 92,
    "save_dir": os.path.join(app_dir(), "Screenshots"),
    "prefix": "shot_",
    "naming": "timestamp",
    "monitor": "primary",
    "copy_to_clipboard": False,
    "play_sound": False,
    "png_compression": 3,
}

MONITOR_PRIMARY = "primary"
MONITOR_ALL = "all"

QUALITY_TIERS = (
    (50, 64, "压缩较强，体积最小，文字与色带边缘会有块状伪影"),
    (65, 79, "均衡，观感尚可，体积适中"),
    (80, 92, "高质量，细节保留好，体积明显增大"),
    (93, 100, "接近无损，体积最大，与 PNG 相比仍有轻微损失"),
)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        for key in cfg:
            if key in stored:
                cfg[key] = stored[key]
    except (OSError, ValueError):
        pass
    cfg["hotkey_seq"] = normalize_seq(cfg.get("hotkey_seq"))
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, CONFIG_PATH
    except OSError as exc:
        return False, str(exc)


KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # union 必须包含最大的 MOUSEINPUT，否则 INPUT 尺寸不符，SendInput 会静默失败
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("ii", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def copy_to_clipboard(img):
    """以 CF_DIB 写入剪贴板，避免依赖 pywin32 / ImageGrab。"""
    rgb = img.convert("RGB")
    bits = rgb.tobytes("raw", "BGR")
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = rgb.width
    header.biHeight = -rgb.height  # 负值 = 自顶向下
    header.biPlanes = 1
    header.biBitCount = 24
    header.biSizeImage = len(bits)
    payload = bytes(header) + bits

    # 64 位下必须声明指针类型，否则句柄被按 32 位 int 传参会截断
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError(f"GlobalAlloc 失败: {ctypes.WinError()}")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise OSError(f"GlobalLock 失败: {ctypes.WinError()}")
    ctypes.memmove(ptr, payload, len(payload))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise OSError(f"OpenClipboard 失败: {ctypes.WinError()}")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            kernel32.GlobalFree(handle)
            raise OSError(f"SetClipboardData 失败: {ctypes.WinError()}")
        handle = None  # 所有权交给剪贴板
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)
    return True


BAD_NAME_CHARS = set('\\/:*?"<>|\r\n\t')


def safe_file_prefix(value):
    """文件名前缀来自 config.json，去掉路径分隔符才不会写到保存目录外面去。"""
    cleaned = "".join(ch for ch in str(value) if ch not in BAD_NAME_CHARS).strip(". ")
    return cleaned[:32] or "shot_"


def next_sequence(save_dir, prefix, ext):
    highest = 0
    suffix = f".{ext}"
    try:
        for entry in os.listdir(save_dir):
            if not entry.startswith(prefix) or not entry.endswith(suffix):
                continue
            digits = entry[len(prefix):-len(suffix)]
            if digits.isdigit():
                highest = max(highest, int(digits))
    except OSError:
        pass
    return highest + 1


class Capturer:
    """常驻 mss 实例做抓屏；编码交给后台线程，热键按下后立刻返回。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._sct = None
        self._lock = threading.Lock()
        self._jobs = queue.Queue()
        self._inflight = set()
        self._seq = None
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="encoder")
        self._writer.start()
        self.last_result = None
        self.last_error = None

    def _region(self, sct):
        monitor = self.cfg.get("monitor", MONITOR_PRIMARY)
        if monitor == MONITOR_ALL:
            return sct.monitors[0]
        if isinstance(monitor, int) and 1 <= monitor < len(sct.monitors):
            return sct.monitors[monitor]
        return sct.monitors[1]

    def capture_now(self):
        import mss

        t0 = time.perf_counter()
        with self._lock:
            if self._sct is None:
                self._sct = mss.MSS()
            mon = self._region(self._sct)
            shot = self._sct.grab(mon)
            path, ext = self._alloc_path()
            job = _Job(path, ext, bytes(shot.rgb), shot.width, shot.height, dict(self.cfg))
            self._inflight.add(path)
        grab_ms = (time.perf_counter() - t0) * 1000
        self._jobs.put(job)
        self.last_result = (path, (shot.width, shot.height), grab_ms)
        self.last_error = None
        return path, (shot.width, shot.height), grab_ms

    def save(self):
        path, size, ms = self.capture_now()
        self._jobs.join()
        if self.last_error:
            raise RuntimeError(self.last_error)
        return path, size, ms

    def _alloc_path(self):
        cfg = self.cfg
        fmt = str(cfg.get("format", "png")).lower()
        ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "bmp": "bmp"}.get(fmt, "png")
        save_dir = cfg.get("save_dir") or os.path.join(app_dir(), "Screenshots")
        os.makedirs(save_dir, exist_ok=True)
        prefix = safe_file_prefix(cfg.get("prefix", "shot_"))

        if cfg.get("naming") == "sequence":
            if self._seq is None:
                self._seq = next_sequence(save_dir, prefix, ext)
            number = self._seq
            self._seq += 1
            name = f"{prefix}{number:04d}.{ext}"
        else:
            stamp = datetime.datetime.now()
            name = f"{prefix}{stamp:%Y%m%d_%H%M%S}_{stamp.microsecond // 1000:03d}.{ext}"

        path = os.path.join(save_dir, name)
        attempt = 1
        while path in self._inflight or os.path.exists(path):
            path = os.path.join(save_dir, f"{os.path.splitext(name)[0]}_{attempt}.{ext}")
            attempt += 1
        return path, ext

    def _write_loop(self):
        from PIL import Image

        while True:
            job = self._jobs.get()
            tmp = f"{job.path}.part"  # 先写临时名再原子改名，中途被杀也不会留下半张图
            try:
                img = Image.frombytes("RGB", (job.width, job.height), job.pixels)
                if job.ext == "png":
                    img.save(tmp, "PNG", compress_level=int(job.cfg.get("png_compression", 3)))
                elif job.ext == "jpg":
                    img.save(tmp, "JPEG", quality=int(job.cfg.get("quality", 92)), subsampling=0)
                else:
                    img.save(tmp, "BMP")
                os.replace(tmp, job.path)
                if job.cfg.get("copy_to_clipboard"):
                    copy_to_clipboard(img)
                if job.cfg.get("play_sound"):
                    winsound.Beep(1200, 40)
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{job.path}: {exc}"
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            finally:
                with self._lock:
                    self._inflight.discard(job.path)
                self._jobs.task_done()

    def wait_idle(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.02)
        return False

    def reset_naming_cache(self):
        with self._lock:
            self._seq = None

    def dispose(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


class _Job:
    __slots__ = ("path", "ext", "pixels", "width", "height", "cfg")

    def __init__(self, path, ext, pixels, width, height, cfg):
        self.path = path
        self.ext = ext
        self.pixels = pixels
        self.width = width
        self.height = height
        self.cfg = cfg


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KeySequenceHook:
    """WH_KEYBOARD_LL 上按“按下顺序”匹配绑定序列，所以 Z→Ctrl 与 Ctrl→Z 是两回事。

    钩子回调必须极快返回，否则整台机器的输入都会卡住，因此这里只往队列丢一个信号，
    真正的抓屏由 _drain 线程做。
    """

    def __init__(self, on_capture, on_status):
        self.on_capture = on_capture
        self.on_status = on_status
        self.thread = threading.Thread(target=self._loop, daemon=True, name="keyboard-hook")
        self._worker = threading.Thread(target=self._drain, daemon=True, name="trigger")
        self._signals = queue.Queue(maxsize=1)
        self._pending = None
        self._stop = False
        self._tid = None
        self._seq = []
        self._index = 0
        self._last_step = 0.0
        self._paused = False
        self._proc = HOOKPROC(self._callback)  # 必须长期持有引用，被 GC 后钩子会崩

    def set_paused(self, value):
        """绑定弹窗打开期间暂停触发，免得边按键边截图。"""
        self._paused = bool(value)

    def start(self):
        self.thread.start()
        self._worker.start()

    def configure(self, seq):
        self._pending = normalize_seq(seq)
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    @property
    def binding(self):
        return list(self._seq)

    def is_alive(self):
        return self.thread.is_alive()

    def _drain(self):
        while True:
            self._signals.get()
            try:
                self.on_capture()
            except Exception as exc:  # 回调抛错不能让线程退出，否则之后按键再也不会截图
                self.on_status(f"截图线程异常：{exc}", False)

    def _feed(self, vk):
        if not self._seq or self._paused:
            return
        now = time.monotonic()
        if now - self._last_step > SEQ_TIMEOUT:
            self._index = 0
        self._last_step = now
        if vk != self._seq[self._index]:
            self._index = 1 if vk == self._seq[0] else 0
            return
        self._index += 1
        if self._index >= len(self._seq):
            self._index = 0
            try:
                self._signals.put_nowait(None)
            except queue.Full:
                pass

    def _callback(self, ncode, wparam, lparam):
        # 回调里抛异常会让 ctypes 返回 0，等于把这次按键从整台机器面前吞掉，
        # 还会跳过 CallNextHookEx，所以这里必须兜住。
        try:
            if ncode == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not info.flags & LLKHF_UP:
                    self._feed(int(info.vkCode))
        except Exception:
            pass
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _apply_pending(self):
        if self._pending is None:
            return
        self._seq, self._pending = self._pending, None
        self._index = 0
        self.on_status(f"截图键：{hotkey_label(self._seq)}", bool(self._seq))

    def _loop(self):
        # ctypes 默认按 32 位 int 处理返回值与入参，64 位下 HMODULE/HHOOK 会被截断，
        # SetWindowsHookExW 会报 ERROR_MOD_NOT_FOUND，所以原型必须先声明。
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_long
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self._tid = kernel32.GetCurrentThreadId()
        self._apply_pending()
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            err = kernel32.GetLastError()
            self.on_status(f"键盘钩子安装失败（{ctypes.WinError(err)}），截图键不会生效", False)
            return
        msg = wintypes.MSG()
        while not self._stop:
            self._apply_pending()
            user32.MsgWaitForMultipleObjects(0, None, False, 250, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._stop = True
                    break
        user32.UnhookWindowsHookEx(hook)

    def stop(self):
        self._stop = True
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


def enumerate_monitors():
    names = ["主显示器", "全部显示器（拼成一张）"]
    keys = [MONITOR_PRIMARY, MONITOR_ALL]
    try:
        import mss

        with mss.MSS() as sct:
            for i, mon in enumerate(sct.monitors[1:], start=1):
                names.append(f"显示器 {i}  {mon['width']}x{mon['height']} @({mon['left']},{mon['top']})")
                keys.append(i)
    except Exception:
        pass
    return names, keys


# ---------------- GUI ----------------


def build_settings_window(cfg, manager):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    # Tk 的字体按“点”取尺寸，放大 scaling 后所有字体与控件度量一起变大；
    # 代码里手写的像素值另用 px() 乘同一系数，两者合起来正好是 GUI_SCALE 倍。
    root.tk.call("tk", "scaling", float(root.tk.call("tk", "scaling")) * GUI_SCALE)

    def px(value):
        return int(value * GUI_SCALE)

    status_var = tk.StringVar(master=root, value="正在安装键盘钩子…")
    root.title(WINDOW_TITLE)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))

    pending = {"seq": list(cfg["hotkey_seq"])}
    body = ttk.Frame(root, padding=px(12))
    body.grid(row=0, column=0, sticky="nsew")

    def section(row, title):
        ttk.Label(body, text=title, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(px(10), px(2)))
        return row + 1

    r = section(0, "快捷键")
    hotkey_var = tk.StringVar(master=root, value=hotkey_label(pending["seq"]))
    ttk.Label(body, text="截图键").grid(row=r, column=0, sticky="w")
    row_key = ttk.Frame(body)
    row_key.grid(row=r, column=1, sticky="w", pady=px(2))
    hotkey_entry = ttk.Entry(row_key, textvariable=hotkey_var, width=14, state="readonly")
    hotkey_entry.pack(side="left")
    # 这个框只显示绑定结果；它一旦拿到焦点，用户随手按的字母会被 Tk 当成无效输入而敲系统铃，
    # 听起来就像“按了键但程序没反应”。
    hotkey_entry.configure(takefocus=False)
    hotkey_entry.bind("<KeyPress>", lambda _e: "break")
    ttk.Button(row_key, text="绑定…", width=8, command=lambda: open_binding_dialog()).pack(
        side="left", padx=(px(6), 0))
    r += 1

    def apply_binding(seq):
        """绑定立刻生效并落盘，不必再点“保存设置”，否则很容易按 Esc 之后就去按键、
        结果什么也没绑上。其余表单项仍只在“保存设置”时写入。"""
        seq = normalize_seq(seq)
        pending["seq"] = seq
        cfg["hotkey_seq"] = list(seq)
        hotkey_var.set(hotkey_label(seq))
        if manager:
            manager.configure(seq)
        ok, info = save_config(cfg)
        status_var.set(f"截图键已{'设为 ' + hotkey_label(seq) if seq else '取消'}"
                       if ok else f"绑定已生效，但配置写入失败：{info}")
        return ok

    def open_binding_dialog():
        dlg = tk.Toplevel(root)
        dlg.title("绑定截图键")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        pressed = []
        live_var = tk.StringVar(master=dlg, value="…")
        pad = px(18)
        ttk.Label(dlg, text="请依次按下要绑定的按键", font=("Segoe UI", 11)).pack(
            pady=(pad, px(6)), padx=pad)
        ttk.Label(dlg, textvariable=live_var, font=("Segoe UI", 16, "bold"),
                  foreground="#1f8a4c").pack(pady=(0, px(10)), padx=pad)
        ttk.Label(dlg, text="按 Esc 确认；没按任何键时 Esc = 取消绑定；Backspace 回退一个键",
                  foreground="#666").pack(pady=(0, pad + px(8)), padx=pad)

        def finish():
            # Esc：有键就绑它，一个键没按就是“取消绑定，不留截图键”
            if manager:
                manager.set_paused(False)
            apply_binding(pressed)
            dlg.destroy()

        def cancel():
            if manager:
                manager.set_paused(False)
            dlg.destroy()

        def on_key(event):
            vk = normalize_vk(event.keycode)
            if not vk:
                return "break"  # 合成事件可能只带字符不带键码，绑了也匹配不上真实按键
            if vk == VK_ESCAPE:
                finish()
                return "break"
            if vk == VK_BACK:  # 误录时回退一个键，不必重开弹窗
                if pressed:
                    pressed.pop()
                live_var.set(hotkey_label(pressed) if pressed else "…")
                return "break"
            if len(pressed) < SEQ_MAX and vk not in pressed:
                pressed.append(vk)
                live_var.set(hotkey_label(pressed))
            return "break"

        if manager:
            manager.set_paused(True)
        dlg.bind("<KeyPress>", on_key)
        dlg.protocol("WM_DELETE_WINDOW", cancel)  # 点 ✕ = 保持原绑定
        dlg.transient(root)  # 跟着主窗走，别留在桌面角落
        dlg.update_idletasks()
        cx = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_reqwidth()) // 2
        cy = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{max(0, cx)}+{max(0, cy)}")
        # 窗口真正映射之后再抓键盘，早于这一步的 grab_set 在 Windows 上可能静默失效，
        # 于是按键落回主窗口、一个键也录不上。
        dlg.wait_visibility()
        dlg.grab_set()
        dlg.focus_force()
        dlg.bind("<Button-1>", lambda _e: dlg.focus_force())

    r = section(r, "输出")
    ttk.Label(body, text="图片格式").grid(row=r, column=0, sticky="w")
    fmt_box = ttk.Combobox(body, values=["png", "jpg", "bmp"], state="readonly", width=8)
    fmt_box.set(cfg["format"])
    fmt_box.grid(row=r, column=1, sticky="w", pady=px(2))
    r += 1

    ttk.Label(body, text="JPG 质量").grid(row=r, column=0, sticky="w")
    quality = tk.IntVar(value=int(cfg["quality"]))
    quality_desc = tk.StringVar(master=root)
    slider = ttk.Scale(body, from_=50, to=100, variable=quality, command=lambda _v: refresh_quality(),
                       length=px(160))
    slider.grid(row=r, column=1, sticky="w")
    r += 1
    ttk.Label(body, textvariable=quality_desc, foreground="#555").grid(row=r, column=1, sticky="w")
    r += 1

    def refresh_quality():
        q = int(quality.get())
        tier = next(text for lo, hi, text in QUALITY_TIERS if lo <= q <= hi)
        if fmt_box.get() == "jpg":
            slider.state(["!disabled"])
            quality_desc.set(f"{q} · {tier}")
        else:
            slider.state(["disabled"])
            quality_desc.set(f"{q} · 当前格式为 {fmt_box.get()}，此项暂不生效")

    fmt_box.bind("<<ComboboxSelected>>", lambda _e: refresh_quality())
    refresh_quality()

    ttk.Label(body, text="命名方式").grid(row=r, column=0, sticky="w")
    naming = ttk.Combobox(body, values=["timestamp", "sequence"], state="readonly", width=11)
    naming.set(cfg["naming"])
    naming.grid(row=r, column=1, sticky="w", pady=px(2))
    r += 1

    ttk.Label(body, text="文件名前缀").grid(row=r, column=0, sticky="w")
    prefix_var = tk.StringVar(value=cfg["prefix"])
    ttk.Entry(body, textvariable=prefix_var, width=12).grid(row=r, column=1, sticky="w")
    r += 1

    r = section(r, "保存位置")
    ttk.Label(body, text="保存目录").grid(row=r, column=0, sticky="w")
    dir_var = tk.StringVar(value=cfg["save_dir"])
    row_dir = ttk.Frame(body)
    row_dir.grid(row=r, column=1, sticky="w")
    ttk.Entry(row_dir, textvariable=dir_var, width=30).pack(side="left")
    ttk.Button(row_dir, text="浏览…", width=7, command=lambda: browse_dir()).pack(side="left", padx=4)
    r += 1

    def browse_dir():
        chosen = filedialog.askdirectory(initialdir=dir_var.get() or app_dir())
        if chosen:
            dir_var.set(chosen)

    ttk.Label(body, text="打开目录").grid(row=r, column=0, sticky="w")
    ttk.Button(body, text="在资源管理器中打开", command=lambda: open_dir()).grid(
        row=r, column=1, sticky="w", pady=px(2))
    r += 1

    def open_dir():
        path = dir_var.get().strip() or os.path.join(app_dir(), "Screenshots")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        if not os.path.isdir(path):  # 手改过的 config.json 可能把它指向一个文件，别直接 startfile
            path = app_dir()
        os.startfile(path)

    r = section(r, "抓取范围")
    ttk.Label(body, text="显示器").grid(row=r, column=0, sticky="w")
    mon_names, mon_keys = enumerate_monitors()
    mon_box = ttk.Combobox(body, values=mon_names, state="readonly", width=26)
    try:
        mon_box.current(mon_keys.index(cfg["monitor"]))
    except ValueError:
        mon_box.current(0)
    mon_box.grid(row=r, column=1, sticky="w", pady=px(2))
    r += 1

    r = section(r, "附加选项")
    clip_var = tk.BooleanVar(value=bool(cfg["copy_to_clipboard"]))
    sound_var = tk.BooleanVar(value=bool(cfg["play_sound"]))
    for label, var in (
        ("同时复制到剪贴板", clip_var),
        ("截图成功后提示音", sound_var),
    ):
        ttk.Checkbutton(body, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1

    r += 1

    def apply_and_save():
        new = dict(cfg)
        new["hotkey_seq"] = list(pending["seq"])
        new["format"] = fmt_box.get()
        new["quality"] = int(quality.get())
        new["naming"] = naming.get()
        new["prefix"] = prefix_var.get() or "shot_"
        new["save_dir"] = os.path.abspath(dir_var.get().strip() or os.path.join(app_dir(), "Screenshots"))
        new["monitor"] = mon_keys[mon_box.current()]
        new["copy_to_clipboard"] = bool(clip_var.get())
        new["play_sound"] = bool(sound_var.get())
        cfg.clear()
        cfg.update(new)
        ok, info = save_config(cfg)
        manager.configure(new["hotkey_seq"])
        status_var.set(f"配置已保存（{info}）")
        if not ok:
            messagebox.showerror("写入失败", info)
        return ok

    def hide_to_background():
        apply_and_save()
        root.withdraw()
        status_var.set("已在后台运行，按截图热键即可抓屏")

    actions = ttk.Frame(body)
    actions.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(px(16), 0))
    ttk.Button(actions, text="保存设置", command=apply_and_save).pack(side="left")
    ttk.Button(actions, text="隐藏到后台",
               command=hide_to_background).pack(side="left", padx=px(6))

    bar = ttk.Frame(root)
    bar.grid(row=1, column=0, sticky="ew")
    ttk.Label(bar, textvariable=status_var, padding=(px(12), px(6)), foreground="#333").pack(side="left")
    ttk.Label(bar, text="v1.0", foreground="#999", padding=(px(12), px(6))).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", hide_to_background)
    return root, status_var


def build_icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 6, 30, 28], radius=5, fill=(38, 168, 91, 255))
    d.rounded_rectangle([10, 1, 22, 9], radius=2, fill=(30, 140, 76, 255))
    d.ellipse([9, 11, 23, 25], fill=(245, 250, 247, 240))
    d.ellipse([13, 15, 19, 21], fill=(38, 168, 91, 255))
    return img


def acquire_single_instance():
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\PortableScreenShot_SingleInstance")
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def focus_existing_window():
    # 只按类名找会唤起别人家的 Tk 窗口，必须连标题一起匹配
    hwnd = user32.FindWindowW("TkTopLevel", WINDOW_TITLE)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


# ---------------- app wiring ----------------


def run(cfg, headless=False, tray=True):
    capturer = Capturer(cfg)
    notes = queue.Queue()
    ui = {"show": False, "quit": False}

    def report(text):
        notes.put(text)

    def do_capture():
        try:
            path, size, ms = capturer.capture_now()
            report(f"{os.path.basename(path)} 已抓取 {size[0]}x{size[1]}  抓屏 {ms:.0f}ms（后台写盘）")
        except Exception as exc:  # 单次失败不能让热键线程退出
            capturer.last_error = str(exc)
            report(f"截图失败：{exc}")

    manager = KeySequenceHook(do_capture, lambda text, ok: report(text))
    manager.configure(cfg["hotkey_seq"])
    manager.start()

    if headless:
        return capturer, manager, do_capture

    root, status_var = build_settings_window(cfg, manager)

    icon = None
    if tray:
        try:
            import pystray

            def request_show():
                ui["show"] = True

            def request_quit():
                manager.stop()
                ui["quit"] = True

            menu = pystray.Menu(
                pystray.MenuItem("打开设置", lambda _i, _m: request_show(), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda _i, _m: request_quit()),
            )
            icon = pystray.Icon(
                "portable_screenshot", build_icon_image(),
                f"Portable ScreenShot · 截图键 {hotkey_label(cfg['hotkey_seq'])}", menu)
            threading.Thread(target=icon.run, daemon=True, name="tray").start()
        except Exception:
            icon = None

    def pump():
        # Tcl 不是线程安全的：钩子线程和托盘线程都只往队列/字典里丢消息，
        # 一切 Tk 调用都回到这里由主线程执行。
        while True:
            try:
                status_var.set(notes.get_nowait())
            except queue.Empty:
                break
        if ui["show"]:
            ui["show"] = False
            root.deiconify()
            root.lift()
        if ui["quit"]:
            root.destroy()
            return
        root.after(120, pump)

    root.after(60, pump)

    def watch_thread():
        if not manager.is_alive():
            report("热键线程已退出，请重新启动程序")
        root.after(2000, watch_thread)

    root.after(1200, watch_thread)

    try:
        root.mainloop()
    finally:
        if icon is not None:
            icon.stop()
        manager.stop()
        capturer.dispose()


def wait_until(predicate, timeout=4.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_selftest():
    cfg = load_config()
    cfg["save_dir"] = os.path.join(app_dir(), "selftest_out")
    cfg["prefix"] = "selftest_"
    cfg["naming"] = "timestamp"
    cfg["format"] = "png"
    cfg["copy_to_clipboard"] = False
    cfg["play_sound"] = False
    os.makedirs(cfg["save_dir"], exist_ok=True)
    for stale in os.listdir(cfg["save_dir"]):
        try:
            os.remove(os.path.join(cfg["save_dir"], stale))
        except OSError:
            pass
    results = []
    fires = []
    capturer = Capturer(cfg)

    def on_capture():
        fires.append(capturer.capture_now())

    status_log = []
    manager = KeySequenceHook(on_capture, lambda text, ok: status_log.append(text))
    manager.start()

    def key_event(vk, up=False):
        # 注入按键统一用 SendInput：低级键盘钩子能看到注入键，所以序列匹配可以全自动验证
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ii.ki.wVk = int(vk)
        inp.ii.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def press(vk):
        key_event(vk)
        time.sleep(0.03)
        key_event(vk, up=True)

    def bind(seq):
        manager.configure(seq)
        return wait_until(lambda: manager.binding == list(seq))

    def count_fires(action):
        before = len(fires)
        action()
        time.sleep(0.6)
        return len(fires) - before

    F13, F14, F15 = 0x7C, 0x7D, 0x7E

    bound = bind([VK_SNAPSHOT])
    fired = count_fires(lambda: press(VK_SNAPSHOT))
    capturer.wait_idle()
    landed = fired == 1 and os.path.isfile(fires[-1][0])
    results.append(("单键绑定并触发（PrtSc）", bound and landed,
                    status_log[-1] if status_log else ""))

    # 顺序敏感：绑定 F13→F14 时，先按 F14 再按 F13 不该触发
    bound = bind([F13, F14])
    wrong = count_fires(lambda: (press(F14), time.sleep(0.05), press(F13)))
    right = count_fires(lambda: (press(F13), time.sleep(0.05), press(F14)))
    capturer.wait_idle()
    results.append(("组合键顺序敏感（反序不触发、正序触发）", bound and wrong == 0 and right == 1, (wrong, right)))

    # 两键间隔超过 SEQ_TIMEOUT 就不算同一次组合
    stale = count_fires(lambda: (press(F13), time.sleep(SEQ_TIMEOUT + 0.4), press(F14)))
    results.append((f"间隔超过 {SEQ_TIMEOUT}s 的两键不触发", stale == 0, stale))

    # 绑定弹窗里没按键就按 Esc = 取消绑定，此时任何键都不该截图
    bound = bind([])
    idle = count_fires(lambda: (press(VK_SNAPSHOT), press(F13), press(F14)))
    results.append(("取消绑定后不存在截图键", bound and idle == 0 and hotkey_label([]) == "未绑定", idle))

    bound = bind([F15])
    old = count_fires(lambda: (press(VK_SNAPSHOT), press(F13), time.sleep(0.05), press(F14)))
    new = count_fires(lambda: press(F15))
    capturer.wait_idle()
    results.append(("换绑后旧键失效、新键生效", bound and old == 0 and new == 1, (old, new)))

    seq_checks = [
        (normalize_seq(None) == [], "None 视为未绑定"),
        (normalize_seq([0x42, 0x42, 0x43]) == [0x42, 0x43], "重复键去重"),
        (normalize_seq([0x71]) == [0x51], "小写 ASCII 键码归一到 VK"),
        (len(normalize_seq([0x41, 0x42, 0x43, 0x44, 0x45])) == SEQ_MAX, "超过 SEQ_MAX 截断"),
        (hotkey_label([0x41, 0x11]) == "A → Ctrl", "标签按顺序展示"),
    ]
    bad = [name for ok, name in seq_checks if not ok]
    results.append(("按键序列规范化规则", not bad, bad or "ok"))

    for fmt in ("png", "jpg", "bmp"):
        cfg["format"] = fmt
        path, size, ms = capturer.save()
        ok = os.path.isfile(path) and os.path.getsize(path) > 1000
        results.append((f"格式 {fmt} 落盘 ({size[0]}x{size[1]}, 抓屏 {ms:.0f}ms)", ok, os.path.basename(path)))
    cfg["format"] = "png"

    cfg["prefix"] = r"..\..\evil_"
    escaped = capturer.save()[0]
    leftovers = [e for e in os.listdir(cfg["save_dir"]) if e.endswith(".part")]
    cfg["prefix"] = "selftest_"
    inside = os.path.dirname(os.path.abspath(escaped)) == os.path.abspath(cfg["save_dir"])
    results.append(("前缀不能把文件写出保存目录", inside and not leftovers, os.path.basename(escaped)))

    name_checks = [
        (safe_file_prefix("") == "shot_", "空前缀回退 shot_"),
        (safe_file_prefix("a<b>:c") == "abc", "非法字符剔除"),
        (safe_file_prefix("../../x") == "x", "路径穿越剔除"),
    ]
    bad_name = [name for ok, name in name_checks if not ok]
    results.append(("文件名前缀清洗规则", not bad_name, bad_name or "ok"))

    cfg["copy_to_clipboard"] = True
    try:
        path, size, ms = capturer.save()
        clip_err = capturer.last_error
    finally:
        cfg["copy_to_clipboard"] = False
    user32.OpenClipboard(None)
    has_dib = bool(user32.IsClipboardFormatAvailable(CF_DIB))
    user32.CloseClipboard()
    results.append(("剪贴板 CF_DIB", has_dib and not clip_err, clip_err or os.path.basename(path)))

    burst = [capturer.capture_now()[2] for _ in range(5)]
    drained = capturer.wait_idle(15)
    count = len([e for e in os.listdir(cfg["save_dir"]) if e.startswith("selftest_")])
    results.append((f"连打 5 张延迟 {max(burst):.0f}ms", drained and count >= 5, [f"{t:.0f}" for t in burst]))

    cfg["naming"] = "sequence"
    cfg["prefix"] = "seq_"
    capturer.reset_naming_cache()
    names = [os.path.basename(capturer.save()[0]) for _ in range(2)]
    cfg["naming"] = "timestamp"
    cfg["prefix"] = "selftest_"
    results.append(("序号命名连续", names[0].endswith("0001.png") and names[1].endswith("0002.png"), names))

    mon_names, mon_keys = enumerate_monitors()
    results.append(("显示器枚举", len(mon_names) >= 2 and mon_keys[0] == MONITOR_PRIMARY, mon_names))

    manager.stop()
    capturer.dispose()
    print("\n=== SELFTEST ===")
    failed = 0
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ->  {detail}")
        failed += 0 if ok else 1
    print(f"=== {len(results) - failed}/{len(results)} passed ===")
    return 1 if failed else 0


def main():
    if "--selftest" in sys.argv:
        return run_selftest()
    args = sys.argv[1:]
    instance_handle = acquire_single_instance()  # 句柄需存活到进程结束
    if instance_handle is None and focus_existing_window():
        print("已有实例在运行，已唤起其设置窗口")
        return 0
    cfg = load_config()
    run(cfg, headless="--no-gui" in args, tray="--no-tray" not in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
