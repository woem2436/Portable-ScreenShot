"""Portable ScreenShot - 全局热键秒截屏 + 轻量配置界面。

线程模型：主线程 Tkinter 设置窗口；hotkey 线程跑 Win32 消息循环 + RegisterHotKey；
托盘线程由 pystray 管理。config.json 与 exe 同目录，保持便携。
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

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP = 0x8000
PM_REMOVE = 1
QS_ALLINPUT = 0x04FF
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

HOTKEY_ID = 0xE101
VK_SNAPSHOT = 0x2C
GUI_SCALE = 2.0

# 允许无修饰键直接截图的按键，避免误绑普通字母键
SINGLE_KEY_VKS = {VK_SNAPSHOT} | {0x70 + i for i in range(12)}
MODIFIER_ONLY_VKS = {0x10, 0x11, 0x12, 0x13, 0x5B, 0x5C, 0x0A, 0x14}

VKEY_NAMES = {
    VK_SNAPSHOT: "PrtSc", 0x1B: "Esc", 0x0D: "Enter", 0x09: "Tab", 0x20: "空格",
    0x2D: "Insert", 0x2E: "Delete", 0x21: "PageUp", 0x22: "PageDown",
    0x24: "Home", 0x23: "End", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
}
for _i in range(12):
    VKEY_NAMES[0x70 + _i] = f"F{_i + 1}"
for _i in range(10):
    VKEY_NAMES[0x30 + _i] = str(_i)


def is_frozen():
    return getattr(sys, "frozen", False)


def app_dir():
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    "hotkey_vk": VK_SNAPSHOT,
    "hotkey_mods": 0,
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
    if int(cfg.get("hotkey_mods", 0)) & MOD_NOREPEAT:
        cfg["hotkey_mods"] = int(cfg["hotkey_mods"]) & ~MOD_NOREPEAT
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, CONFIG_PATH
    except OSError as exc:
        return False, str(exc)


def is_key_down(vk):
    return bool(user32.GetKeyState(vk) & 0x8000)


def current_modifiers():
    # 不用 Tk 的 event.state：Windows 上它是 MK_* 位，Alt/Win 读不准
    mods = 0
    if is_key_down(0x10):
        mods |= MOD_SHIFT
    if is_key_down(0x11):
        mods |= MOD_CONTROL
    if is_key_down(0x12):
        mods |= MOD_ALT
    if is_key_down(0x5B) or is_key_down(0x5C):
        mods |= MOD_WIN
    return mods


def validate_binding(vk, mods):
    if mods == 0 and vk not in SINGLE_KEY_VKS:
        return False, "单键截图只推荐 PrtSc 或 F1-F12，其余键请搭配 Ctrl / Alt / Shift。"
    return True, ""


def hotkey_label(vk, mods):
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    if mods & MOD_WIN:
        parts.append("Win")
    name = VKEY_NAMES.get(vk)
    if name is None:
        name = chr(vk) if 32 <= vk < 0x100 else f"0x{vk:02X}"
    parts.append(name)
    return " + ".join(parts)


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
        prefix = cfg.get("prefix", "shot_")

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
            try:
                img = Image.frombytes("RGB", (job.width, job.height), job.pixels)
                if job.ext == "png":
                    img.save(job.path, "PNG", compress_level=int(job.cfg.get("png_compression", 3)))
                elif job.ext == "jpg":
                    img.save(job.path, "JPEG", quality=int(job.cfg.get("quality", 92)), subsampling=0)
                else:
                    img.save(job.path, "BMP")
                if job.cfg.get("copy_to_clipboard"):
                    copy_to_clipboard(img)
                if job.cfg.get("play_sound"):
                    winsound.Beep(1200, 40)
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{job.path}: {exc}"
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


class HotkeyManager:
    """热键的注册/换绑/触发都在这一个线程的消息循环里完成。"""

    def __init__(self, on_capture, on_status):
        self.on_capture = on_capture
        self.on_status = on_status
        self.thread = threading.Thread(target=self._loop, daemon=True, name="hotkey-loop")
        self._pending = None
        self._stop = False
        self._tid = None
        self._registered = None

    def start(self):
        self.thread.start()

    def configure(self, vk, mods):
        self._pending = (int(vk), int(mods) | MOD_NOREPEAT)
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def is_alive(self):
        return self.thread.is_alive()

    def _apply_pending(self):
        if self._pending is None:
            return
        vk, mods = self._pending
        self._pending = None
        if self._registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = None
        if user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
            self._registered = (vk, mods)
            self.on_status(f"全局热键 {hotkey_label(vk, mods & ~MOD_NOREPEAT)} 已注册", True)
        else:
            self.on_status(f"热键 {hotkey_label(vk, mods & ~MOD_NOREPEAT)} 注册失败（已被占用）", False)

    def _loop(self):
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # 先摸一次消息队列，确保 RegisterHotKey 有队列可用
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            pass
        self._apply_pending()
        while not self._stop:
            self._apply_pending()
            user32.MsgWaitForMultipleObjects(0, None, False, 200, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    self._stop = True
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.on_capture()
        if self._registered:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = None

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

    status_var = tk.StringVar(master=root, value="正在注册全局热键…")
    root.title("Portable ScreenShot 设置")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))

    pending = {"vk": int(cfg["hotkey_vk"]), "mods": int(cfg["hotkey_mods"])}
    body = ttk.Frame(root, padding=px(12))
    body.grid(row=0, column=0, sticky="nsew")

    def section(row, title):
        ttk.Label(body, text=title, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(px(10), px(2)))
        return row + 1

    r = section(0, "快捷键")
    hotkey_var = tk.StringVar(value=hotkey_label(pending["vk"], pending["mods"]))
    ttk.Label(body, text="截图热键").grid(row=r, column=0, sticky="w")
    entry = ttk.Entry(body, textvariable=hotkey_var, width=24)
    entry.grid(row=r, column=1, sticky="w", pady=px(2))
    r += 1

    def capture_key(event):
        vk = int(event.keycode)
        if vk in MODIFIER_ONLY_VKS:
            return "break"
        mods = current_modifiers()
        ok, reason = validate_binding(vk, mods)
        if not ok:
            messagebox.showwarning("容易被误按", reason)
            return "break"
        pending["vk"], pending["mods"] = vk, mods
        hotkey_var.set(hotkey_label(vk, mods))
        return "break"

    entry.bind("<Button-1>", lambda _e: entry.focus_set())
    entry.bind("<KeyPress>", capture_key)
    entry.bind("<Escape>", lambda _e: "break")

    r = section(r, "输出")
    ttk.Label(body, text="图片格式").grid(row=r, column=0, sticky="w")
    fmt_box = ttk.Combobox(body, values=["png", "jpg", "bmp"], state="readonly", width=8)
    fmt_box.set(cfg["format"])
    fmt_box.grid(row=r, column=1, sticky="w", pady=px(2))
    r += 1

    ttk.Label(body, text="JPG 质量").grid(row=r, column=0, sticky="w")
    quality = tk.IntVar(value=int(cfg["quality"]))
    ttk.Scale(body, from_=50, to=100, variable=quality, length=px(160)).grid(row=r, column=1, sticky="w")
    r += 1

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
        os.makedirs(path, exist_ok=True)
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
        new["hotkey_vk"] = pending["vk"]
        new["hotkey_mods"] = pending["mods"]
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
        manager.configure(new["hotkey_vk"], new["hotkey_mods"])
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
    ttk.Button(actions, text="隐藏到后台（热键继续有效）",
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
    hwnd = user32.FindWindowW("TkTopLevel", None)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


# ---------------- app wiring ----------------


def run(cfg, headless=False, tray=True):
    capturer = Capturer(cfg)
    state = {"root": None, "status_var": None}

    def report(text):
        state["last"] = text
        root = state["root"]
        if root is not None and state["status_var"] is not None:
            try:
                root.after(0, lambda: state["status_var"].set(text))
            except Exception:
                pass

    def do_capture():
        try:
            path, size, ms = capturer.capture_now()
            report(f"{os.path.basename(path)} 已抓取 {size[0]}x{size[1]}  抓屏 {ms:.0f}ms（后台写盘）")
        except Exception as exc:  # 单次失败不能让热键线程退出
            capturer.last_error = str(exc)
            report(f"截图失败：{exc}")

    manager = HotkeyManager(do_capture, lambda text, ok: report(text))
    manager.configure(cfg["hotkey_vk"], cfg["hotkey_mods"])
    manager.start()

    if headless:
        return capturer, manager, do_capture

    root, status_var = build_settings_window(cfg, manager)
    state["status_var"] = status_var
    state["root"] = root
    if state.get("last"):
        status_var.set(state["last"])

    icon = None
    if tray:
        try:
            import pystray

            def open_settings():
                root.deiconify()
                root.lift()

            menu = pystray.Menu(
                pystray.MenuItem("打开设置", lambda _i, _m: root.after(0, open_settings), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda _i, _m: (manager.stop(), root.after(0, root.destroy))),
            )
            icon = pystray.Icon(
                "portable_screenshot", build_icon_image(),
                f"Portable ScreenShot · {hotkey_label(cfg['hotkey_vk'], cfg['hotkey_mods'])}", menu)
            threading.Thread(target=icon.run, daemon=True, name="tray").start()
        except Exception:
            icon = None

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
    manager = HotkeyManager(on_capture, lambda text, ok: status_log.append(text))
    manager.start()

    def key_event(vk, up=False):
        # 注入按键统一用 SendInput：keybd_event 在部分机器上不产生热键事件
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ii.ki.wVk = int(vk)
        inp.ii.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def press(vk):
        key_event(vk)
        time.sleep(0.03)
        key_event(vk, up=True)

    MOD_VKS = [(MOD_CONTROL, 0x11), (MOD_SHIFT, 0x10), (MOD_ALT, 0x12)]

    def press_combo(mods, vk):
        for flag, mod_vk in MOD_VKS:
            if mods & flag:
                key_event(mod_vk)
        press(vk)
        for flag, mod_vk in reversed(MOD_VKS):
            if mods & flag:
                key_event(mod_vk, up=True)
        time.sleep(0.25)

    test_vk, test_mods = 0x78, MOD_CONTROL | MOD_SHIFT  # Ctrl+Shift+F9
    manager.configure(test_vk, test_mods)
    bound = wait_until(lambda: manager._registered == (test_vk, test_mods | MOD_NOREPEAT))
    time.sleep(0.3)
    press_combo(test_mods, test_vk)
    press_combo(test_mods, test_vk)
    fired = wait_until(lambda: len(fires) >= 2)
    capturer.wait_idle()
    landed = all(os.path.isfile(path) for path, _s, _m in fires)
    results.append(("热键触发截图（Ctrl+Shift+F9）", bound and fired and landed, len(fires)))

    # 换绑：旧键应失效，新键应生效
    second_vk, second_mods = 0x79, MOD_CONTROL | MOD_SHIFT  # Ctrl+Shift+F10
    manager.configure(second_vk, second_mods)
    rebound = wait_until(lambda: manager._registered == (second_vk, second_mods | MOD_NOREPEAT))
    press_combo(test_mods, test_vk)
    time.sleep(0.5)
    after_old = len(fires)
    press_combo(second_mods, second_vk)
    after_new = wait_until(lambda: len(fires) > after_old)
    results.append(("换绑后旧热键失效、新热键生效", rebound and after_old == len(fires) - 1 and after_new, (after_old, len(fires))))

    # PrintScreen 只能验证注册成功：Windows 会过滤注入的 PrtSc，模拟按键不会触发热键
    manager.configure(VK_SNAPSHOT, 0)
    prtscc_bound = wait_until(lambda: manager._registered == (VK_SNAPSHOT, MOD_NOREPEAT))
    results.append(("PrtSc 单键注册（需物理按键确认触发）", prtscc_bound, status_log[-1] if status_log else ""))
    manager.configure(test_vk, test_mods)
    wait_until(lambda: manager._registered == (test_vk, test_mods | MOD_NOREPEAT))

    for fmt in ("png", "jpg", "bmp"):
        cfg["format"] = fmt
        path, size, ms = capturer.save()
        ok = os.path.isfile(path) and os.path.getsize(path) > 1000
        results.append((f"格式 {fmt} 落盘 ({size[0]}x{size[1]}, 抓屏 {ms:.0f}ms)", ok, os.path.basename(path)))
    cfg["format"] = "png"

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

    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x10, 0, 0, 0)  # Shift down
    time.sleep(0.15)
    held = current_modifiers()
    user32.keybd_event(0x10, 0, 2, 0)
    user32.keybd_event(0x11, 0, 2, 0)
    results.append(("按住 Ctrl+Shift 时修饰键识别", held == (MOD_CONTROL | MOD_SHIFT), hex(held)))

    checks = [
        (validate_binding(VK_SNAPSHOT, 0)[0], True, "PrtSc 单键"),
        (validate_binding(0x76, 0)[0], True, "F7 单键"),
        (validate_binding(0x42, 0)[0], False, "B 单键被拒"),
        (validate_binding(0x42, MOD_CONTROL)[0], True, "Ctrl+B"),
    ]
    bad = [name for allowed, expected, name in checks if bool(allowed) != expected]
    results.append(("热键绑定校验规则", not bad, bad or "ok"))

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
