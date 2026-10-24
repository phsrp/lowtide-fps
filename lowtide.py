"""
Lowtide - FPS overlay with 1% / 0.1% lows, CPU / GPU temps and usage, and RAM usage.
Works over fullscreen games.

How it works:
  * PresentMon (Intel's frame capture tool) records every frame every process presents,
    whether the game is windowed, borderless or fullscreen.
  * LibreHardwareMonitor (via lib/SensorHelper.exe) reads the sensors people quote:
    CPU "Core (Tctl/Tdie)" (AMD) / "CPU Package" (Intel), "CPU Total" usage,
    GPU "GPU Core" temp and "GPU Core" usage.
  * The overlay is a click-through, never-focused, always-on-top window that re-asserts
    its topmost position constantly, so fullscreen games can't bury it - and it never
    steals focus, so it never kicks a game out of fullscreen.

Closing / controls:
  * Tray icon (by the clock) -> right-click -> Settings / Quit
  * Ctrl+Alt+O settings, Ctrl+Alt+R reset stats, Ctrl+Alt+V show / hide, Ctrl+Alt+Q quit
    (all rebindable in settings)

Command line:
  --set-startup on|off   add / remove the "start with Windows" task, then exit (used by the installer)
"""

import ctypes
import ctypes.wintypes as wt
import csv
import glob
import heapq
from array import array
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime

APP = "Lowtide"
VERSION = "1.0.0"
REPO = "phsrp/lowtide-fps"
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", HERE)           # bundled files (assets, lib, vendor)
ASSETS = os.path.join(RES, "assets")
DATA = os.path.join(os.environ.get("APPDATA", HERE), APP)
LOG_DIR = os.path.join(DATA, "logs")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
SENSOR_HELPER = os.path.join(RES, "lib", "SensorHelper.exe")

REFRESH_MS = 250           # overlay redraw interval
GRAPH_FRAMES = 240         # frametime graph width in frames
IDLE_TIMEOUT = 3.0         # seconds without frames before an app counts as "stopped"
EXCLUDE = ["dwm.exe", "explorer.exe", "python.exe", "pythonw.exe", "Lowtide.exe", "cleanmeter.exe",
           "msedgewebview2.exe", "chrome.exe", "msedge.exe", "firefox.exe",
           "Discord.exe", "Spotify.exe", "TextInputHost.exe", "SearchHost.exe",
           "StartMenuExperienceHost.exe", "ShellExperienceHost.exe", "SensorHelper.exe",
           # live wallpapers render all the time but are never "the game"
           "wallpaper32.exe", "wallpaper64.exe", "webwallpaper32.exe", "webwallpaper64.exe",
           "Lively.exe", "Lively.PlayerWebView2.exe", "Lively.PlayerMpv.exe"]

DEFAULTS = {
    "view": "full",             # full / compact
    "corner": "top-left",       # top-left / top-right / bottom-left / bottom-right / custom
    "pos": None,                # [x, y] when corner is "custom" (dragged while unlocked)
    "margin": 12,
    "opacity": 0.94,
    "scale": 1.0,
    "window_seconds": 30,       # rolling window the avg / lows are calculated over; 0 = since reset
    "lows_method": "average",   # average (of the slowest 1%) / percentile (99th percentile frame)
    "show_fps": True,
    "show_lows": True,
    "show_frametime": True,
    "show_graph": True,
    "show_cpu": True,
    "show_gpu": True,
    "show_ram": True,
    "auto_update": True,        # check GitHub for a new version on launch
    "theme": "Rosé Pine Dawn",
    "custom_themes": {},        # name -> {role: "#rrggbb"}
    # action -> [modifier flags, virtual key code, label]; rebind them in the settings panel
    "hotkeys": {
        "settings": [0x3, 0x4F, "Ctrl+Alt+O"],
        "reset": [0x3, 0x52, "Ctrl+Alt+R"],
        "toggle": [0x3, 0x56, "Ctrl+Alt+V"],
        "quit": [0x3, 0x51, "Ctrl+Alt+Q"],
    },
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
gdi32 = ctypes.windll.gdi32

# Tabler icon codepoints (tabler-icons webfont 3.49.0)
ICON = {
    "logo": "", "cpu": "", "gpu": "\U00010299", "ram": "",
    "eye": "", "refresh": "", "power": "", "keyboard": "",
    "layout": "", "adjustments": "", "chart-line": "", "rocket": "",
    "view": "", "alert": "", "link": "", "download": "",
    "palette": "", "plus": "", "trash": "", "settings": "",
    "copy": "", "selected": "", "unselected": "",
}
FONT = "Montserrat"
FONT_SEMI = "Montserrat SemiBold"
FONT_MED = "Montserrat Medium"
ICON_FONT = "tabler-icons"

# ----------------------------------------------------------------- themes
# Each theme sets these roles. The module-level colour names below are swapped by apply_theme().
THEME_ROLES = [
    ("BASE", "Background"), ("SURFACE", "Cards"), ("OVERLAY", "Buttons"),
    ("BORDER", "Borders"), ("TRACK", "Switch off / slider track"),
    ("TEXT", "Text"), ("SUBTLE", "Secondary text"), ("MUTED", "Faint text"),
    ("ACCENT", "Accent"), ("LOGO", "Logo"),
    ("GOOD", "Good (60+ FPS)"), ("WARN", "Warning (30-60 FPS, hot)"), ("BAD", "Bad (under 30 FPS)"),
]
BUILTIN_THEMES = {
    "Rosé Pine Dawn": {
        "BASE": "#faf4ed", "SURFACE": "#fffaf3", "OVERLAY": "#f2e9e1", "BORDER": "#dfdad9",
        "TRACK": "#cecacd", "TEXT": "#575279", "SUBTLE": "#797593", "MUTED": "#9893a5",
        "ACCENT": "#907aa9", "LOGO": "#d7827e", "GOOD": "#286983", "WARN": "#ea9d34", "BAD": "#b4637a",
    },
    "Rosé Pine": {
        "BASE": "#191724", "SURFACE": "#1f1d2e", "OVERLAY": "#26233a", "BORDER": "#403d52",
        "TRACK": "#524f67", "TEXT": "#e0def4", "SUBTLE": "#908caa", "MUTED": "#6e6a86",
        "ACCENT": "#c4a7e7", "LOGO": "#ebbcba", "GOOD": "#9ccfd8", "WARN": "#f6c177", "BAD": "#eb6f92",
    },
}
DEFAULT_THEME = "Rosé Pine Dawn"
KEY = "#010101"     # transparent key colour for the overlay window

BASE = SURFACE = OVERLAY = BORDER = TRACK = TEXT = SUBTLE = MUTED = ""
ACCENT = LOGO = GOOD = WARN = BAD = ""


def all_themes(settings):
    return {**BUILTIN_THEMES, **settings.get("custom_themes", {})}


def apply_theme(settings):
    themes = all_themes(settings)
    theme = themes.get(settings.get("theme"), BUILTIN_THEMES[DEFAULT_THEME])
    full = {**BUILTIN_THEMES[DEFAULT_THEME], **theme}
    if full["BASE"].lower() == KEY:   # the overlay would turn see-through
        full["BASE"] = "#020202"
    globals().update(full)


apply_theme({})


def fps_color(v):
    return GOOD if v >= 60 else WARN if v >= 30 else BAD


def temp_color(v):
    return TEXT if v < 80 else WARN if v < 90 else BAD


def load_color(v):
    return TEXT if v < 95 else WARN


# ----------------------------------------------------------------- settings
def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    s["hotkeys"] = {**DEFAULTS["hotkeys"], **s["hotkeys"]}
    return s


def save_settings(s):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


# ----------------------------------------------------------------- startup task
def _launch_command():
    if FROZEN:
        return sys.executable, ""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return (pyw if os.path.exists(pyw) else exe), f'"{os.path.abspath(__file__)}"'


def _powershell(script):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                          capture_output=True, text=True)


def startup_enabled():
    r = subprocess.run(["schtasks", "/Query", "/TN", APP], stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                       capture_output=True)
    return r.returncode == 0


def set_startup(on):
    """A scheduled task (not the Run key) so it can start with admin rights without a UAC prompt."""
    if not on:
        _powershell(f"Unregister-ScheduledTask -TaskName '{APP}' -Confirm:$false -ErrorAction SilentlyContinue")
        return not startup_enabled()
    exe, args = _launch_command()
    arg_part = f"-Argument '{args}'" if args else ""
    script = (
        f"$a = New-ScheduledTaskAction -Execute '{exe}' {arg_part} -WorkingDirectory '{HERE}';"
        "$t = New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\";"
        "$p = New-ScheduledTaskPrincipal -UserId \"$env:USERDOMAIN\\$env:USERNAME\" -RunLevel Highest -LogonType Interactive;"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew;"
        f"Register-ScheduledTask -TaskName '{APP}' -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null"
    )
    _powershell(script)
    return startup_enabled()


# ----------------------------------------------------------------- setup helpers
def ensure_admin():
    """The exe asks for admin through its manifest; this covers running the .py directly."""
    if shell32.IsUserAnAdmin():
        return
    exe, args = _launch_command()
    params = " ".join([args] + [f'"{a}"' for a in sys.argv[1:]]).strip()
    rc = shell32.ShellExecuteW(None, "runas", exe, params, HERE, 1)
    if rc <= 32:
        user32.MessageBoxW(None, "Lowtide needs admin rights to capture frames.", APP, 0x10)
    sys.exit(0)


def single_instance():
    kernel32.CreateMutexW(None, False, "Local\\LowtideOverlay")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32.MessageBoxW(None, "Lowtide is already running.\n\nIt lives in the tray by the clock: "
                                 "right-click its icon for Settings or Quit.", APP, 0x40)
        sys.exit(0)


def load_fonts():
    """Loads Montserrat + Tabler icons privately for this process (no install needed)."""
    for path in glob.glob(os.path.join(ASSETS, "*.ttf")):
        gdi32.AddFontResourceExW(path, 0x10, None)  # FR_PRIVATE


def find_presentmon():
    candidates = [
        os.path.join(RES, "vendor", "presentmon.exe"),
        os.path.join(HERE, "vendor", "presentmon.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Cleanmeter", "presentmon.exe"),
        shutil.which("presentmon.exe") or "",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    user32.MessageBoxW(None, "presentmon.exe not found. Reinstall Lowtide.", APP, 0x10)
    sys.exit(1)


def pawnio_installed():
    r = subprocess.run(["sc", "query", "PawnIO"], stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                       capture_output=True)
    return r.returncode == 0


# ----------------------------------------------------------------- frame stats
class ProcStats:
    """Frame times of one swap chain. The window is measured in frame time (like RTSS),
    not in when the data happened to arrive from PresentMon."""

    def __init__(self, name):
        self.name = name
        self.frames = deque()            # frame times (ms) inside the window
        self.window_ms = 0.0             # sum of self.frames
        self.last_seen = 0.0
        self.session = array("f")        # every frame since start / reset (log + "Since reset")

    def add(self, now, ft, window_s):
        self.last_seen = now
        self.session.append(ft)
        if window_s <= 0:                # "Since reset": the session is the window
            return
        self.frames.append(ft)
        self.window_ms += ft
        limit = window_s * 1000.0
        while len(self.frames) > 1 and self.window_ms - self.frames[0] >= limit:
            self.window_ms -= self.frames.popleft()

    def rewindow(self, window_s):
        self.frames.clear()
        self.window_ms = 0.0
        if window_s <= 0:
            return
        for ft in reversed(self.session):
            if self.frames and self.window_ms + ft > window_s * 1000.0:
                break
            self.frames.appendleft(ft)
            self.window_ms += ft

    def frametimes(self, window_s):
        return list(self.session) if window_s <= 0 else list(self.frames)

    def reset(self):
        self.frames.clear()
        self.window_ms = 0.0
        self.session = array("f")


def current_fps(frametimes):
    """FPS over the last second of frames, the way RTSS / Afterburner show it
    (sliding 1000 ms average). Returns (fps, average frame time in ms)."""
    total, n = 0.0, 0
    for ft in reversed(frametimes):
        total += ft
        n += 1
        if total >= 1000.0:
            break
    return (1000.0 * n / total, total / n) if total > 0 else (0.0, 0.0)


def compute(frametimes, method="average"):
    """avg fps, 1% low, 0.1% low over the given frames.
    average:    fps of the mean of the slowest 1% / 0.1% of frames ("1% low average",
                the textbook definition, CapFrameX "1% low average")
    percentile: fps at the 99th / 99.9th percentile frame time (CapFrameX "P1 / P0.1")"""
    n = len(frametimes)
    if n == 0:
        return None
    total = sum(frametimes)
    avg = 1000.0 * n / total if total > 0 else 0.0

    def low(pct):
        k = max(1, int(round(n * pct)))
        worst = heapq.nlargest(k, frametimes)
        ft = worst[-1] if method == "percentile" else sum(worst) / k
        return 1000.0 / ft if ft > 0 else 0.0

    return avg, low(0.01), low(0.001)


class Capture:
    """Runs PresentMon and feeds frame times into per-process stats."""

    FT_COLUMNS = ["MsBetweenPresents", "FrameTime", "msBetweenPresents", "MsBetweenAppStart"]

    def __init__(self, presentmon, settings):
        self.presentmon = presentmon
        self.settings = settings
        self.lock = threading.Lock()
        self.procs = {}   # (pid, swap chain) -> ProcStats
        self.proc = None
        self.error = None

    def start(self):
        args = [self.presentmon, "--output_stdout", "--no_console_stats",
                "--stop_existing_session", "--session_name", "Lowtide",
                "--no_track_input", "--no_track_gpu"]
        for e in EXCLUDE:
            args += ["--exclude", e]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                     bufsize=1, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_err(self):
        for line in self.proc.stderr:
            line = line.strip()
            if line and "error" in line.lower():
                self.error = line

    def _read(self):
        reader = csv.reader(self.proc.stdout)
        header = None
        for row in reader:
            if header is None:
                header = row
                idx = {name.lstrip("﻿").strip(): i for i, name in enumerate(header)}
                i_app = idx.get("Application")
                i_pid = idx.get("ProcessID")
                i_sc = idx.get("SwapChainAddress")
                i_ft = next((idx[c] for c in self.FT_COLUMNS if c in idx), None)
                i_cpu = idx.get("CPUStartTime", idx.get("TimeInSeconds"))
                last_start = {}
                continue
            if len(row) < len(header):
                continue
            try:
                # one game can present from several swap chains (launcher UI, video...);
                # mixing them would double count, so each one is tracked on its own
                key = (int(row[i_pid]), row[i_sc] if i_sc is not None else "")
                if i_ft is not None:
                    ft = float(row[i_ft])
                else:  # fall back to deltas of the start timestamp (seconds)
                    t = float(row[i_cpu])
                    prev = last_start.get(key)
                    last_start[key] = t
                    if prev is None:
                        continue
                    ft = (t - prev) * 1000.0
            except (ValueError, TypeError):
                continue
            if not (0.0 < ft < 2000.0):
                continue
            now = time.time()
            with self.lock:
                ps = self.procs.get(key)
                if ps is None:
                    ps = self.procs[key] = ProcStats(row[i_app])
                ps.add(now, ft, self.settings["window_seconds"])
        if self.error is None:
            self.error = "PresentMon stopped"

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        # make sure the ETW session doesn't linger
        subprocess.run([self.presentmon, "--terminate_existing_session", "--session_name", "Lowtide"],
                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class Sensors:
    """CPU/GPU temp + usage from lib/SensorHelper.exe (LibreHardwareMonitor), RAM from Windows."""

    def __init__(self):
        self.data = {}
        self.proc = None

    def start(self):
        if not os.path.isfile(SENSOR_HELPER):
            return
        self.proc = subprocess.Popen([SENSOR_HELPER, "1000"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=os.path.dirname(SENSOR_HELPER),
                                     stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                                     bufsize=1, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.data = json.loads(line)
            except ValueError:
                pass

    def get(self, key):
        v = self.data.get(key)
        # LHM reports 0.0 for temps it can't read (e.g. no driver access)
        if v is None or (key.endswith("_temp") and v <= 0):
            return None
        return v

    @staticmethod
    def ram():
        """(used GB, total GB, usage %)"""
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        gb = 1024 ** 3
        return (m.ullTotalPhys - m.ullAvailPhys) / gb, m.ullTotalPhys / gb, float(m.dwMemoryLoad)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def write_session_log(ps, method="average"):
    """Saves a summary when a game stops - handy for true exclusive-fullscreen games."""
    if len(ps.session) < 200:
        return
    r = compute(ps.session, method)
    os.makedirs(LOG_DIR, exist_ok=True)
    dur = sum(ps.session) / 1000.0
    line = (f"{datetime.now():%Y-%m-%d %H:%M:%S}  {ps.name:<28} "
            f"{dur/60:6.1f} min  avg {r[0]:6.1f}  1% {r[1]:6.1f}  0.1% {r[2]:6.1f}  ({method})\n")
    with open(os.path.join(LOG_DIR, "sessions.log"), "a", encoding="utf-8") as f:
        f.write(line)


# ----------------------------------------------------------------- updater
def parse_version(v):
    try:
        return tuple(int(x) for x in v.strip().lstrip("vV").split(".")[:3])
    except ValueError:
        return (0, 0, 0)


class Updater:
    """Checks GitHub releases on launch; installs the new setup silently when you say yes."""

    def __init__(self, app):
        self.app = app
        self.latest = None      # (version, installer url, release page)
        self.status = ""
        self.busy = False

    def check(self, manual=False):
        if self.busy:
            return
        self.busy = True
        self.status = "Checking..."
        threading.Thread(target=self._check, args=(manual,), daemon=True).start()

    def _check(self, manual):
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                         headers={"User-Agent": APP, "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.load(r)
            ver = rel.get("tag_name", "").lstrip("vV")
            setup = next((a for a in rel.get("assets", [])
                          if a["name"].lower().endswith(".exe") and "setup" in a["name"].lower()), None)
            if setup and parse_version(ver) > parse_version(VERSION):
                self.latest = (ver, setup["browser_download_url"], rel.get("html_url"))
                self.status = f"Version {ver} is available"
            else:
                self.latest = None
                self.status = "You're on the latest version"
        except Exception:
            self.status = "Couldn't reach GitHub"
        self.busy = False
        self.app.root.after(0, lambda: self.app.update_checked(manual))

    def install(self):
        if not self.latest or self.busy:
            return
        ver, url, page = self.latest
        if not FROZEN:  # running from source: just show the release
            webbrowser.open(page)
            return
        self.busy = True
        threading.Thread(target=self._install, args=(ver, url), daemon=True).start()

    def _install(self, ver, url):
        path = os.path.join(os.environ.get("TEMP", DATA), f"Lowtide-Setup-{ver}.exe")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while chunk := r.read(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.status = f"Downloading... {done * 100 // total}%"
                        self.app.root.after(0, self.app.settings_panel.refresh_update)
        except Exception:
            self.busy = False
            self.status = "Download failed, try again"
            self.app.root.after(0, self.app.settings_panel.refresh_update)
            return
        self.status = "Installing..."
        # the installer closes this copy, updates, and starts the new one
        subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.app.root.after(0, self.app.quit)


# ----------------------------------------------------------------- win32 bits
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40

user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetParent.restype = wt.HWND


def foreground_pid():
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


def colorref(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return ctypes.c_int(r | (g << 8) | (b << 16))


def is_dark(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def style_title_bar(hwnd):
    """Title bar in the theme's colours (Windows 11; Windows 10 just gets dark/light)."""
    dwm = ctypes.windll.dwmapi
    dark = ctypes.c_int(1 if is_dark(BASE) else 0)
    dwm.DwmSetWindowAttribute(wt.HWND(hwnd), 20, ctypes.byref(dark), ctypes.sizeof(dark))
    for attr, col in ((35, BASE), (36, TEXT), (34, BORDER)):  # caption, text, border
        c = colorref(col)
        dwm.DwmSetWindowAttribute(wt.HWND(hwnd), attr, ctypes.byref(c), ctypes.sizeof(c))


# ----------------------------------------------------------------- overlay
class Overlay:
    W = 264

    def __init__(self, root, capture, sensors, settings):
        self.cap = capture
        self.sensors = sensors
        self.s = settings
        self.visible = True
        self.root = root
        self.dpi = root.winfo_fpixels("1i") / 96.0
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.c = tk.Canvas(root, bg=KEY, highlightthickness=0)
        self.c.pack(fill="both", expand=True)
        root.update_idletasks()
        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        self.last_key = None
        self._stats_cache = None
        self.size = None
        self.unlocked = False
        self._drag = None
        self.c.bind("<ButtonPress-1>", self._drag_start)
        self.c.bind("<B1-Motion>", self._drag_move)
        self.c.bind("<ButtonRelease-1>", self._drag_end)
        self.apply_settings()

    # -- unlock / drag. Unlocked only drops click-through; the window still never takes
    # focus, so it keeps drawing over fullscreen games.
    def set_unlocked(self, on):
        self.unlocked = on
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        style = (style & ~WS_EX_TRANSPARENT) if on else (style | WS_EX_TRANSPARENT)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
        if on and not self.visible:
            self.toggle()

    def _drag_start(self, e):
        if self.unlocked:
            self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            x, y = e.x_root - self._drag[0], e.y_root - self._drag[1]
            self.s["corner"], self.s["pos"] = "custom", [x, y]
            self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, _e):
        if self._drag:
            self._drag = None
            save_settings(self.s)

    def px(self, v):
        return int(round(v * self.dpi * self.s["scale"]))

    def font(self, family, size):
        return (family, -self.px(size))

    def apply_settings(self):
        self.root.attributes("-alpha", self.s["opacity"])
        self.size = None  # force re-place on next draw

    def _place(self, w, h):
        if self.size == (w, h):
            return
        self.size = (w, h)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        m = self.px(self.s["margin"])
        if self.s["corner"] == "custom" and self.s["pos"]:
            # keep it on the (multi-monitor) desktop
            vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
            x = min(max(self.s["pos"][0], vx), vx + vw - w)
            y = min(max(self.s["pos"][1], vy), vy + vh - h)
        else:
            x = m if "left" in self.s["corner"] else sw - w - m
            y = m if "top" in self.s["corner"] else sh - h - m
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.c.config(width=w, height=h)

    def keep_on_top(self):
        if self.visible:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.root.after(100, self.keep_on_top)

    def pick_target(self):
        """The app in front if it's rendering, else the last one that was in front
        (e.g. while you alt-tab to check something). Never a random background app."""
        now = time.time()
        fg = foreground_pid()
        procs = self.cap.procs
        dead = [k for k, ps in procs.items() if now - ps.last_seen > IDLE_TIMEOUT]
        for k in dead:
            write_session_log(procs.pop(k), self.s["lows_method"])
        busiest = lambda keys: max(keys, key=lambda k: len(procs[k].session))
        fg_keys = [k for k in procs if k[0] == fg]
        if fg_keys:
            return busiest(fg_keys)       # the game's main swap chain presents the most
        if self.last_key in procs:
            return self.last_key
        return None

    def toggle(self):
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
        else:
            self.root.withdraw()

    def reset(self):
        with self.cap.lock:
            for ps in self.cap.procs.values():
                ps.reset()

    # -- drawing
    def draw(self):
        try:
            self._draw()
        finally:
            self.root.after(REFRESH_MS, self.draw)

    def snapshot(self):
        with self.cap.lock:
            key = self.pick_target()
            self.last_key = key
            ps = self.cap.procs.get(key) if key else None
            fts = ps.frametimes(self.s["window_seconds"]) if ps else []
            recent = list(ps.session[-2000:]) if ps else []
            name = ps.name if ps else ""
        return name, fts, recent

    def stats(self, fts):
        """compute() with a cache, so long "Since reset" sessions don't re-sort every redraw."""
        now = time.time()
        big = len(fts) > 20000
        if big and self._stats_cache and now - self._stats_cache[0] < 1.0:
            return self._stats_cache[1]
        r = compute(fts, self.s["lows_method"])
        self._stats_cache = (now, r) if big else None
        return r

    def _text(self, x, y, text, color, family, size, anchor="nw"):
        return self.c.create_text(x, y, text=text, fill=color, font=self.font(family, size), anchor=anchor)

    def _draw(self):
        self.c.delete("all")
        name, fts, recent = self.snapshot()
        if self.s["view"] == "compact":
            w, h = self._draw_compact(name, fts, recent)
        else:
            w, h = self._draw_full(name, fts, recent)
        self._place(w, h)
        bg = self._round_rect(0, 0, w - 1, h - 1, self.px(8 if self.s["view"] == "compact" else 12),
                              fill=BASE, outline=ACCENT if self.unlocked else BORDER,
                              width=2 if self.unlocked else 1)
        self.c.tag_lower(bg)
        self.c.config(cursor="fleur" if self.unlocked else "")

    def _draw_full(self, name, fts, recent):
        s, px, t = self.s, self.px, self._text
        W, pad = px(self.W), px(16)
        y = px(14)
        show_frames = s["show_fps"] or s["show_lows"] or s["show_frametime"] or s["show_graph"]
        r = self.stats(fts)
        if show_frames:
            if r is None:
                t(pad, y, self.cap.error or "Waiting for a game", MUTED, FONT_MED, 11)
                y += px(26)
            else:
                avg, l1, l01 = r
                cur, cur_ft = current_fps(recent)
                t(pad, y, name.removesuffix(".exe")[:26], MUTED, FONT_MED, 10)
                if s["show_frametime"]:
                    t(W - pad, y, f"{cur_ft:.1f} ms", MUTED, FONT_MED, 10, "ne")
                y += px(18)
                if s["show_fps"]:
                    big_font = self.font(FONT_SEMI, 38)
                    big = self.c.create_text(pad - px(1), y, text=f"{cur:.0f}", fill=fps_color(cur),
                                             font=big_font, anchor="nw")
                    # sit "FPS" on the big number's baseline
                    baseline = y + tkfont.Font(font=big_font).metrics("ascent")
                    small = self.font(FONT_SEMI, 11)
                    self.c.create_text(self.c.bbox(big)[2] + px(5),
                                       baseline + tkfont.Font(font=small).metrics("descent"),
                                       text="FPS", fill=MUTED, font=small, anchor="sw")
                    y += px(50)
                if s["show_lows"]:
                    col = (W - 2 * pad) / 3
                    for i, (label, v) in enumerate((("AVG", avg), ("1% LOW", l1), ("0.1% LOW", l01))):
                        x = pad + i * col
                        t(x, y, label, MUTED, FONT_SEMI, 8)
                        t(x, y + px(13), f"{v:.0f}", fps_color(v) if i else TEXT, FONT_SEMI, 15)
                    y += px(38)
                if s["show_graph"]:
                    self._graph(recent[-GRAPH_FRAMES:], pad, y, W - pad, y + px(30))
                    y += px(38)
        hw_rows = [k for k in ("cpu", "gpu", "ram") if s[f"show_{k}"]]
        if hw_rows:
            if show_frames:
                self.c.create_line(pad, y, W - pad, y, fill=BORDER)
                y += px(10)
            for key in hw_rows:
                self._hw_row(key, pad, y, W)
                y += px(22)
        return W, y + px(8)

    def _hw_row(self, key, pad, y, W):
        px, t = self.px, self._text
        mid = y + px(9)
        t(pad, mid, ICON[key], MUTED, ICON_FONT, 15, "w")
        t(pad + px(22), mid, key.upper(), TEXT, FONT_SEMI, 11, "w")
        if key == "ram":
            used, total, load = self.sensors.ram()
            first = ("Used", f"{used:.1f} GB", TEXT)
        else:
            temp = self.sensors.get(f"{key}_temp")
            load = self.sensors.get(f"{key}_load")
            first = ("Temp", "--" if temp is None else f"{temp:.0f}°C",
                     MUTED if temp is None else temp_color(temp))
        second = ("Usage", "--" if load is None else f"{load:.0f}%",
                  MUTED if load is None else load_color(load))
        # "Temp: 62°C" in the middle, "Usage: 15%" right-aligned
        for (label, value, color), x, anchor in ((first, px(78), "w"), (second, W - pad, "e")):
            if anchor == "e":
                v = t(x, mid, value, color, FONT_SEMI, 11, "e")
                t(self.c.bbox(v)[0] - px(4), mid, f"{label}:", MUTED, FONT_MED, 10, "e")
            else:
                lb = t(x, mid, f"{label}:", MUTED, FONT_MED, 10, "w")
                t(self.c.bbox(lb)[2] + px(4), mid, value, color, FONT_SEMI, 11, "w")

    def _draw_compact(self, name, fts, recent):
        """Everything on one line: 143 FPS  AVG 137  1% 45  0.1% 45 · CPU 62°C 15% · ..."""
        s, px = self.s, self.px
        H, pad = px(28), px(12)
        mid = H // 2
        x = pad
        groups = []
        r = self.stats(fts)
        if r:
            avg, l1, l01 = r
            cur, cur_ft = current_fps(recent)
            g = []
            if s["show_fps"]:
                g += [(f"{cur:.0f}", fps_color(cur), FONT_SEMI), ("FPS", MUTED, FONT_MED)]
            if s["show_lows"]:
                g += [("AVG", MUTED, FONT_MED), (f"{avg:.0f}", TEXT, FONT_SEMI),
                      ("1%", MUTED, FONT_MED), (f"{l1:.0f}", fps_color(l1), FONT_SEMI),
                      ("0.1%", MUTED, FONT_MED), (f"{l01:.0f}", fps_color(l01), FONT_SEMI)]
            if s["show_frametime"]:
                g += [(f"{cur_ft:.1f}", TEXT, FONT_SEMI), ("ms", MUTED, FONT_MED)]
            if g:
                groups.append(g)
        elif s["show_fps"] or s["show_lows"]:
            groups.append([("Waiting for a game", MUTED, FONT_MED)])
        for key in ("cpu", "gpu"):
            if s[f"show_{key}"]:
                temp, load = self.sensors.get(f"{key}_temp"), self.sensors.get(f"{key}_load")
                groups.append([(key.upper(), MUTED, FONT_MED),
                               ("--" if temp is None else f"{temp:.0f}°C",
                                MUTED if temp is None else temp_color(temp), FONT_SEMI),
                               ("--" if load is None else f"{load:.0f}%",
                                MUTED if load is None else load_color(load), FONT_SEMI)])
        if s["show_ram"]:
            used, total, load = self.sensors.ram()
            groups.append([("RAM", MUTED, FONT_MED), (f"{load:.0f}%", load_color(load), FONT_SEMI)])
        for gi, g in enumerate(groups):
            if gi:
                x += px(8)
                self.c.create_line(x, mid - px(6), x, mid + px(6), fill=TRACK)
                x += px(9)
            for i, (text, color, fam) in enumerate(g):
                item = self._text(x, mid, text, color, fam, 11, "w")
                x = self.c.bbox(item)[2] + px(4 if i % 2 == 0 else 7)
        return int(x + pad - px(4)), H

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.c.create_polygon(pts, smooth=True, **kw)

    def _graph(self, fts, x0, y0, x1, y1):
        c = self.c
        c.create_line(x0, y1, x1, y1, fill=BORDER)
        if len(fts) < 2:
            return
        # flat at the typical frame time; only real stutters (1.5x typical and 2 ms+ slower) spike
        typical = sorted(fts)[len(fts) // 2]
        shown = [ft if ft > typical * 1.5 and ft - typical > 2.0 else typical for ft in fts]
        top = max(max(shown), typical * 3, 1000.0 / 30)  # leave headroom so the line sits low
        step = (x1 - x0) / (GRAPH_FRAMES - 1)
        pts = []
        for i, ft in enumerate(shown):
            pts += [x0 + i * step, y1 - (y1 - y0) * min(ft / top, 1.0)]
        c.create_line(*pts, fill=ACCENT, width=max(1, self.px(1.5)))

    def run(self):
        self.keep_on_top()
        self.draw()


# ----------------------------------------------------------------- settings panel
class SettingsPanel:
    def __init__(self, app):
        self.app = app
        self.win = None
        self.capturing = None
        self.tab = "general"

    def reopen(self, tab=None):
        """Rebuilds the window in place (tab switch, theme change)."""
        pos = None
        if self.win and self.win.winfo_exists():
            pos = (self.win.winfo_x(), self.win.winfo_y())
            self.win.destroy()
            self.win = None
        if tab:
            self.tab = tab
        self.open()
        if pos:
            self.win.geometry(f"+{pos[0]}+{pos[1]}")

    def toggle(self):
        if self.win and self.win.winfo_exists():
            self.close()
        else:
            self.open()

    def close(self):
        if self.capturing:
            self.capturing = None
            self.app.reload_hotkeys()
        if self.app.overlay.unlocked:
            self.app.overlay.set_unlocked(False)
        if self.win:
            self.win.destroy()
            self.win = None

    def open(self):
        if self.win and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return
        s = self.app.settings
        win = self.win = tk.Toplevel(self.app.root)
        win.title(f"{APP} settings")
        win.configure(bg=BASE)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)
        ico = os.path.join(ASSETS, "lowtide.ico")
        if os.path.isfile(ico):
            win.iconbitmap(ico)
        win.update_idletasks()
        style_title_bar(user32.GetParent(win.winfo_id()) or win.winfo_id())

        outer = tk.Frame(win, bg=BASE, padx=24, pady=20)
        outer.pack(fill="both", expand=True)

        # title
        top = tk.Frame(outer, bg=BASE)
        top.pack(fill="x", pady=(0, 14))
        tk.Label(top, text=ICON["logo"], font=(ICON_FONT, 24), fg=LOGO, bg=BASE).pack(side="left")
        tk.Label(top, text=APP, font=(FONT_SEMI, 17), fg=TEXT, bg=BASE).pack(side="left", padx=(8, 0))
        tk.Label(top, text=f"v{VERSION}", font=(FONT_MED, 9), fg=MUTED, bg=BASE).pack(side="left", padx=8, pady=(6, 0))
        tabs = tk.Frame(top, bg=OVERLAY, padx=3, pady=3)
        tabs.pack(side="right")
        for name, label, icon in (("general", "General", "settings"), ("theme", "Theme", "palette")):
            on = self.tab == name
            t = tk.Label(tabs, text=f"{ICON[icon]}", font=(ICON_FONT, 12), padx=6, pady=3, cursor="hand2",
                         bg=SURFACE if on else OVERLAY, fg=ACCENT if on else SUBTLE)
            t.pack(side="left")
            l = tk.Label(tabs, text=label, font=(FONT_SEMI if on else FONT_MED, 10), padx=0, pady=4,
                         cursor="hand2", bg=SURFACE if on else OVERLAY, fg=TEXT if on else SUBTLE)
            l.pack(side="left", ipadx=6)
            for w in (t, l):
                w.bind("<Button-1>", lambda e, n=name: self.reopen(n) if self.tab != n else None)

        cols = tk.Frame(outer, bg=BASE)
        cols.pack(fill="both", expand=True)
        left = tk.Frame(cols, bg=BASE)
        left.pack(side="left", fill="y", anchor="n")
        right = tk.Frame(cols, bg=BASE)
        right.pack(side="left", fill="y", anchor="n", padx=(16, 0))

        def card(parent, icon, title):
            f = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                         padx=16, pady=12)
            f.pack(fill="x", pady=(0, 12))
            row = tk.Frame(f, bg=SURFACE)
            row.pack(fill="x", pady=(0, 8))
            tk.Label(row, text=ICON[icon], font=(ICON_FONT, 13), fg=ACCENT, bg=SURFACE).pack(side="left")
            tk.Label(row, text=title, font=(FONT_SEMI, 10), fg=TEXT, bg=SURFACE).pack(side="left", padx=(6, 0))
            return f

        switch_imgs = self.switch_images()

        def switch(parent, text, get, set_):
            row = tk.Frame(parent, bg=SURFACE, cursor="hand2")
            row.pack(fill="x", pady=3)
            lbl = tk.Label(row, text=text, font=(FONT, 10), fg=TEXT, bg=SURFACE)
            lbl.pack(side="left")
            sw = tk.Label(row, bg=SURFACE, bd=0, cursor="hand2")
            sw.pack(side="right")

            def paint():
                sw.config(image=switch_imgs[bool(get())])

            def flip(_e):
                set_(not get())
                paint()
            for w in (row, lbl, sw):
                w.bind("<Button-1>", flip)
            paint()
            return paint

        def setting_switch(parent, key, text):
            def set_(v):
                s[key] = v
                self.changed()
            switch(parent, text, lambda: s[key], set_)

        def segmented(parent, options, key):
            row = tk.Frame(parent, bg=OVERLAY, padx=3, pady=3)
            row.pack(anchor="w")
            labels = []

            def refresh():
                for lbl, val in labels:
                    on = s[key] == val
                    lbl.config(bg=SURFACE if on else OVERLAY, fg=TEXT if on else SUBTLE,
                               font=(FONT_SEMI if on else FONT_MED, 9))

            for text, val in options:
                lbl = tk.Label(row, text=text, padx=10, pady=4, cursor="hand2")
                lbl.pack(side="left")
                lbl.bind("<Button-1>", lambda e, v=val: (s.__setitem__(key, v), refresh(), self.changed()))
                labels.append((lbl, val))
            refresh()

        def slider(parent, key, label, lo, hi, step, fmt):
            head = tk.Frame(parent, bg=SURFACE)
            head.pack(fill="x", pady=(4, 0))
            tk.Label(head, text=label, font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(side="left")
            val = tk.Label(head, text=fmt(s[key]), font=(FONT_SEMI, 10), fg=SUBTLE, bg=SURFACE)
            val.pack(side="right")
            cw, ch, r = 268, 22, 8
            cv = tk.Canvas(parent, width=cw, height=ch, bg=SURFACE, highlightthickness=0, cursor="hand2")
            cv.pack(anchor="w")

            def paint():
                cv.delete("all")
                x = r + (cw - 2 * r) * (s[key] - lo) / (hi - lo)
                cv.create_line(r, ch / 2, cw - r, ch / 2, fill=BORDER, width=4, capstyle="round")
                cv.create_line(r, ch / 2, x, ch / 2, fill=ACCENT, width=4, capstyle="round")
                cv.create_oval(x - r, ch / 2 - r, x + r, ch / 2 + r, fill=SURFACE, outline=ACCENT, width=2)

            def moved(e):
                frac = min(max((e.x - r) / (cw - 2 * r), 0.0), 1.0)
                v = round(round((lo + frac * (hi - lo)) / step) * step, 2)
                if v != s[key]:
                    s[key] = v
                    val.config(text=fmt(v))
                    paint()
                    self.changed()
            cv.bind("<Button-1>", moved)
            cv.bind("<B1-Motion>", moved)
            paint()

        def button(parent, icon, text, cmd, color, side="left"):
            b = tk.Frame(parent, bg=OVERLAY, cursor="hand2", padx=12, pady=7)
            b.pack(side=side, padx=(0, 8))
            i = tk.Label(b, text=ICON[icon], font=(ICON_FONT, 13), fg=color, bg=OVERLAY)
            i.pack(side="left")
            t = tk.Label(b, text=text, font=(FONT_SEMI, 10), fg=color, bg=OVERLAY)
            t.pack(side="left", padx=(5, 0))
            for w in (b, i, t):
                w.bind("<Button-1>", lambda e: cmd())
                w.bind("<Enter>", lambda e: [x.config(bg=BORDER) for x in (b, i, t)])
                w.bind("<Leave>", lambda e: [x.config(bg=OVERLAY) for x in (b, i, t)])

        if self.tab == "theme":
            self.build_theme(left, right, card, button)
            return

        # left column
        c = card(left, "view", "View")
        segmented(c, [("Full", "full"), ("Compact", "compact")], "view")

        c = card(left, "eye", "Show")
        setting_switch(c, "show_fps", "Current FPS")
        setting_switch(c, "show_lows", "Average, 1% and 0.1% lows")
        setting_switch(c, "show_frametime", "Frame time")
        setting_switch(c, "show_graph", "Frame time graph (full view)")
        setting_switch(c, "show_cpu", "CPU temp and usage")
        setting_switch(c, "show_gpu", "GPU temp and usage")
        setting_switch(c, "show_ram", "RAM usage")
        if not pawnio_installed():
            row = tk.Frame(c, bg=SURFACE, cursor="hand2")
            row.pack(fill="x", pady=(6, 0))
            hint = tk.Label(row, text=ICON["alert"], font=(ICON_FONT, 12), fg=WARN, bg=SURFACE)
            hint.pack(side="left")
            msg = tk.Label(row, text="CPU temp needs the free PawnIO driver. Get it", font=(FONT_MED, 9),
                           fg=WARN, bg=SURFACE, cursor="hand2")
            msg.pack(side="left", padx=(4, 0))
            for w in (row, hint, msg):
                w.bind("<Button-1>", lambda e: webbrowser.open("https://pawnio.eu/"))

        c = card(left, "adjustments", "Look")
        tk.Label(c, text="Corner", font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(anchor="w", pady=(0, 4))
        segmented(c, [("Top left", "top-left"), ("Top right", "top-right"),
                      ("Bottom left", "bottom-left"), ("Bottom right", "bottom-right")], "corner")
        ov = self.app.overlay
        switch(c, "Unlock to drag it anywhere", lambda: ov.unlocked, ov.set_unlocked)
        tk.Label(c, text="Locks again when you close settings.", font=(FONT, 9),
                 fg=MUTED, bg=SURFACE).pack(anchor="w")
        slider(c, "opacity", "Opacity", 0.3, 1.0, 0.05, lambda v: f"{v * 100:.0f}%")
        slider(c, "scale", "Size", 0.7, 1.8, 0.1, lambda v: f"{v:.1f}x")

        # right column
        c = card(right, "chart-line", "Lows calculated over")
        segmented(c, [("10s", 10), ("30s", 30), ("1 min", 60), ("5 min", 300), ("Since reset", 0)],
                  "window_seconds")
        tk.Label(c, text="1% / 0.1% low method", font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(anchor="w", pady=(10, 4))
        segmented(c, [("Slowest 1% average", "average"), ("Percentile", "percentile")], "lows_method")
        tk.Label(c, text="Use \"Since reset\" + reset stats to measure a run like an Afterburner benchmark.",
                 font=(FONT, 9), fg=MUTED, bg=SURFACE, wraplength=340, justify="left").pack(anchor="w", pady=(6, 0))

        c = card(right, "keyboard", "Hotkeys")
        tk.Label(c, text="Click one, then press the new combo. Esc cancels.",
                 font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w", pady=(0, 6))
        for action, what in (("settings", "Open settings"), ("reset", "Reset stats"),
                             ("toggle", "Show / hide overlay"), ("quit", "Quit")):
            row = tk.Frame(c, bg=SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=what, font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(side="left")
            key_lbl = tk.Label(row, font=(FONT_SEMI, 9), padx=8, pady=3, cursor="hand2")
            key_lbl.pack(side="right")
            key_lbl.bind("<Button-1>", lambda e, a=action, l=key_lbl: self.start_capture(a, l))
            self.show_hotkey(action, key_lbl)
        win.bind("<KeyPress>", self.on_key)

        c = card(right, "rocket", "Startup")
        state = {"on": startup_enabled()}

        def set_startup_(v):
            state["on"] = set_startup(v)
        switch(c, "Start with Windows", lambda: state["on"], set_startup_)
        tk.Label(c, text="Starts hidden in the tray, no admin prompt.", font=(FONT, 9),
                 fg=MUTED, bg=SURFACE).pack(anchor="w", pady=(2, 0))

        c = card(right, "download", "Updates")
        setting_switch(c, "auto_update", "Check for updates on launch")
        row = tk.Frame(c, bg=SURFACE)
        row.pack(fill="x", pady=(6, 0))
        self.update_label = tk.Label(row, font=(FONT, 9), fg=MUTED, bg=SURFACE)
        self.update_label.pack(side="left")
        self.update_btn = tk.Label(row, font=(FONT_SEMI, 9), padx=10, pady=3, cursor="hand2")
        self.update_btn.pack(side="right")
        self.update_btn.bind("<Button-1>", lambda e: self.update_clicked())
        self.refresh_update()

        btns = tk.Frame(right, bg=BASE)
        btns.pack(fill="x", pady=(4, 0))
        button(btns, "refresh", "Reset stats", self.app.overlay.reset, TEXT)
        button(btns, "eye", "Hide / show", self.app.overlay.toggle, TEXT)
        button(btns, "power", "Quit", self.app.quit, BAD)

    def changed(self):
        save_settings(self.app.settings)
        self.app.overlay.apply_settings()
        with self.app.cap.lock:  # a new window length applies to the frames already captured
            for ps in self.app.cap.procs.values():
                ps.rewindow(self.app.settings["window_seconds"])

    # -- theme tab
    def build_theme(self, left, right, card, button):
        s = self.app.settings
        themes = all_themes(s)
        current = s["theme"] if s["theme"] in themes else DEFAULT_THEME
        custom = s.setdefault("custom_themes", {})

        def select(name):
            s["theme"] = name
            self.theme_changed()

        c = card(left, "palette", "Themes")
        for name, theme in themes.items():
            on = name == current
            row = tk.Frame(c, bg=OVERLAY if on else SURFACE, cursor="hand2", padx=8, pady=6)
            row.pack(fill="x", pady=2)
            mark = tk.Label(row, text=ICON["selected" if on else "unselected"], font=(ICON_FONT, 14),
                            fg=ACCENT if on else TRACK, bg=row["bg"])
            mark.pack(side="left")
            lbl = tk.Label(row, text=name, font=(FONT_SEMI if on else FONT, 10), fg=TEXT, bg=row["bg"])
            lbl.pack(side="left", padx=(8, 0))
            sw = tk.Label(row, image=self.swatch_image(theme, row["bg"]), bg=row["bg"], bd=0, cursor="hand2")
            sw.pack(side="right")
            if name in BUILTIN_THEMES:
                tk.Label(row, text="built-in", font=(FONT, 8), fg=MUTED, bg=row["bg"]).pack(side="right", padx=8)
            for w in (row, mark, lbl, sw):
                w.bind("<Button-1>", lambda e, n=name: select(n))

        def new_theme():
            base_name = "My theme"
            name, n = base_name, 2
            while name in themes:
                name, n = f"{base_name} {n}", n + 1
            custom[name] = dict(themes[current])
            select(name)

        def delete_theme():
            custom.pop(current, None)
            select(DEFAULT_THEME)

        btns = tk.Frame(left, bg=BASE)
        btns.pack(fill="x")
        button(btns, "copy", "New theme from this one", new_theme, TEXT)
        if current in custom:
            button(btns, "trash", "Delete", delete_theme, BAD)

        c = card(right, "adjustments", "Colours")
        if current not in custom:
            tk.Label(c, text=f"{current} is built in, so it can't be edited.\n"
                             "Make a copy with \"New theme from this one\" and change that.",
                     font=(FONT, 10), fg=SUBTLE, bg=SURFACE, justify="left").pack(anchor="w")
            return

        # name
        row = tk.Frame(c, bg=SURFACE)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text="Name", font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(side="left")
        name_var = tk.StringVar(value=current)
        entry = tk.Entry(row, textvariable=name_var, font=(FONT_MED, 10), width=20, relief="flat",
                         bg=OVERLAY, fg=TEXT, insertbackground=TEXT, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        entry.pack(side="right", ipady=3)

        def rename(_e=None):
            new = name_var.get().strip()
            if new and new != current and new not in themes:
                custom[new] = custom.pop(current)
                select(new)
        entry.bind("<Return>", rename)
        entry.bind("<FocusOut>", rename)

        tk.Label(c, text="Click a colour to pick one, or type a hex code and press Enter.",
                 font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w", pady=(0, 6))
        theme = custom[current]
        for role, label in THEME_ROLES:
            row = tk.Frame(c, bg=SURFACE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=(FONT, 10), fg=TEXT, bg=SURFACE).pack(side="left")
            hex_var = tk.StringVar(value=theme.get(role, BUILTIN_THEMES[DEFAULT_THEME][role]))
            e = tk.Entry(row, textvariable=hex_var, font=(FONT_MED, 9), width=9, relief="flat",
                         bg=OVERLAY, fg=TEXT, insertbackground=TEXT, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT, justify="center")
            e.pack(side="right", ipady=2)
            sw = tk.Label(row, width=3, bg=hex_var.get(), cursor="hand2", relief="flat",
                          highlightthickness=1, highlightbackground=BORDER)
            sw.pack(side="right", padx=(0, 8))

            def set_color(value, role=role):
                value = value.strip()
                if not value.startswith("#"):
                    value = "#" + value
                try:
                    self.win.winfo_rgb(value)   # validates the colour
                except tk.TclError:
                    return
                if len(value) == 4:  # #abc -> #aabbcc
                    value = "#" + "".join(ch * 2 for ch in value[1:])
                theme[role] = value.lower()
                self.theme_changed()

            def pick(_e, role=role, var=hex_var):
                from tkinter import colorchooser
                rgb, hx = colorchooser.askcolor(var.get(), parent=self.win, title=f"{label}")
                if hx:
                    set_color(hx, role)
            sw.bind("<Button-1>", pick)
            e.bind("<Return>", lambda ev, var=hex_var, role=role: set_color(var.get(), role))

    def swatch_image(self, theme, bg):
        """Every colour of a theme as smooth, slightly overlapping dots."""
        from PIL import Image, ImageDraw, ImageTk
        dpi = self.win.winfo_fpixels("1i") / 96.0
        ss = 4
        d_px, step = round(18 * dpi), round(12 * dpi)
        roles = [r for r, _ in THEME_ROLES]
        w, h = step * (len(roles) - 1) + d_px + 2, d_px + 2
        im = Image.new("RGB", (w * ss, h * ss), bg)
        draw = ImageDraw.Draw(im)
        ring = max(1, round(1.5 * dpi)) * ss
        for i, role in enumerate(roles):
            x = (1 + i * step) * ss
            box = (x, ss, x + d_px * ss, ss + d_px * ss)
            draw.ellipse(box, fill=bg)  # gap ring so overlapping dots stay readable
            draw.ellipse((box[0] + ring, box[1] + ring, box[2] - ring, box[3] - ring),
                         fill=theme.get(role, BUILTIN_THEMES[DEFAULT_THEME][role]))
        img = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS), master=self.win)
        self._swatches = getattr(self, "_swatches", []) + [img]  # keep references alive
        return img

    def theme_changed(self):
        apply_theme(self.app.settings)
        save_settings(self.app.settings)
        self.app.overlay.apply_settings()
        self.reopen()

    def switch_images(self):
        """Anti-aliased on/off switch images (Tk's own shapes are jagged)."""
        from PIL import Image, ImageDraw, ImageTk
        dpi = self.win.winfo_fpixels("1i") / 96.0
        w, h, ss = round(36 * dpi), round(20 * dpi), 4
        imgs = {}
        for on in (False, True):
            im = Image.new("RGB", (w * ss, h * ss), SURFACE)
            d = ImageDraw.Draw(im)
            d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=h * ss // 2, fill=ACCENT if on else TRACK)
            pad = 3 * ss * dpi
            knob = h * ss - 2 * pad
            x0 = (w * ss - pad - knob) if on else pad
            d.ellipse((x0, pad, x0 + knob, pad + knob), fill=SURFACE)
            imgs[on] = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS), master=self.win)
        self._switch_imgs = imgs  # keep a reference so Tk doesn't drop them
        return imgs

    # -- updates
    def update_clicked(self):
        up = self.app.updater
        if up.latest:
            up.install()
        else:
            up.check(manual=True)
        self.refresh_update()

    def refresh_update(self):
        if not (self.win and self.win.winfo_exists()):
            return
        up = self.app.updater
        self.update_label.config(text=up.status or f"You have v{VERSION}",
                                 fg=GOOD if up.latest else MUTED)
        if up.latest:
            self.update_btn.config(text=f"Update to {up.latest[0]}", fg=SURFACE, bg=GOOD)
        else:
            self.update_btn.config(text="Check now", fg=TEXT, bg=OVERLAY)

    # -- hotkey rebinding
    def show_hotkey(self, action, lbl):
        label = self.app.settings["hotkeys"][action][2]
        if action in self.app.hotkeys_failed:
            lbl.config(text=f"{label}  (in use)", fg=BAD, bg=OVERLAY)
        else:
            lbl.config(text=label, fg=TEXT, bg=OVERLAY)

    def start_capture(self, action, lbl):
        if self.capturing:
            self.show_hotkey(*self.capturing)
        self.capturing = (action, lbl)
        lbl.config(text="Press keys...", fg=SURFACE, bg=ACCENT)
        self.app.pause_hotkeys()  # so the old combos don't fire while you type the new one

    def on_key(self, e):
        if not self.capturing:
            return
        action, lbl = self.capturing
        if e.keysym == "Escape":
            self.capturing = None
            self.app.reload_hotkeys(lambda: self.show_hotkey(action, lbl))
            return
        if e.keysym.split("_")[0] in ("Control", "Shift", "Alt", "Win", "Super", "Meta"):
            return  # wait for the actual key
        down = lambda vk: user32.GetAsyncKeyState(vk) & 0x8000
        mods, names = 0, []
        # Tk's event state: Shift 0x1, Control 0x4, Alt 0x20000 (Windows); Win isn't in it
        for flag, state, vks, name in ((0x2, 0x4, (0x11,), "Ctrl"), (0x1, 0x20000, (0x12,), "Alt"),
                                       (0x4, 0x1, (0x10,), "Shift"), (0x8, 0, (0x5B, 0x5C), "Win")):
            if (e.state & state) or any(down(vk) for vk in vks):
                mods |= flag
                names.append(name)
        if not mods:
            lbl.config(text="Add Ctrl, Alt, Shift or Win")
            return
        key = e.keysym.upper() if len(e.keysym) == 1 else e.keysym.replace("_", " ").title()
        self.app.settings["hotkeys"][action] = [mods, e.keycode, "+".join(names + [key])]
        self.capturing = None
        save_settings(self.app.settings)
        self.app.reload_hotkeys(lambda: self.show_hotkey(action, lbl))


# ----------------------------------------------------------------- tray icon
def make_tray(app):
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(os.path.join(ASSETS, "lowtide.png"))
    except OSError:
        img = Image.new("RGBA", (64, 64), LOGO)

    def later(fn):
        return lambda icon, item: app.root.after(0, fn)

    menu = pystray.Menu(
        pystray.MenuItem("Settings", later(app.settings_panel.open), default=True),
        pystray.MenuItem("Show / hide overlay", later(app.overlay.toggle)),
        pystray.MenuItem("Unlocked (drag to move)",
                         later(lambda: app.overlay.set_unlocked(not app.overlay.unlocked)),
                         checked=lambda item: app.overlay.unlocked),
        pystray.MenuItem("Reset stats", later(app.overlay.reset)),
        pystray.MenuItem("Check for updates", later(lambda: (app.settings_panel.open(),
                                                             app.updater.check(manual=True)))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", later(app.quit)),
    )
    icon = pystray.Icon(APP, img, APP, menu)
    icon.run_detached()
    return icon


# ----------------------------------------------------------------- hotkeys
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8001   # re-register from settings
WM_APP_PAUSE = 0x8002    # unregister while capturing a new combo


def hotkey_thread(app):
    """RegisterHotKey has to happen on the thread that pumps messages, so the
    settings panel asks this thread to (re)register via posted messages."""
    actions = {1: ("settings", app.settings_panel.toggle), 2: ("reset", app.overlay.reset),
               3: ("toggle", app.overlay.toggle), 4: ("quit", app.quit)}

    def unregister():
        for hid in actions:
            user32.UnregisterHotKey(None, hid)

    def register():
        unregister()
        failed = set()
        for hid, (name, _) in actions.items():
            mods, vk, _label = app.settings["hotkeys"][name]
            if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                failed.add(name)
        app.hotkeys_failed = failed

    app.hotkey_thread_id = kernel32.GetCurrentThreadId()
    register()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam in actions:
            app.root.after(0, actions[msg.wParam][1])
        elif msg.message == WM_APP_RELOAD:
            register()
            if app.on_hotkeys_reloaded:
                app.root.after(0, app.on_hotkeys_reloaded)
        elif msg.message == WM_APP_PAUSE:
            unregister()


# ----------------------------------------------------------------- app
class App:
    def __init__(self):
        self.settings = load_settings()
        apply_theme(self.settings)
        load_fonts()
        self.cap = Capture(find_presentmon(), self.settings)
        self.sensors = Sensors()
        self.root = tk.Tk()
        self.root.title(APP)
        self.overlay = Overlay(self.root, self.cap, self.sensors, self.settings)
        self.settings_panel = SettingsPanel(self)
        self.tray = None
        self.closing = False
        self.hotkey_thread_id = None
        self.hotkeys_failed = set()
        self.on_hotkeys_reloaded = None
        self.updater = Updater(self)
        self.update_prompt = None

    # -- updates
    def update_checked(self, manual):
        self.settings_panel.refresh_update()
        if not self.updater.latest or manual:
            return
        ver = self.updater.latest[0]
        if self.tray:
            try:
                self.tray.notify(f"Lowtide {ver} is available. Open settings from the tray to update.", APP)
            except Exception:
                pass
        if self.overlay.last_key is None:  # no game rendering, safe to show a window
            self.show_update_prompt()

    def show_update_prompt(self):
        if self.update_prompt and self.update_prompt.winfo_exists():
            return
        ver = self.updater.latest[0]
        w = self.update_prompt = tk.Toplevel(self.root)
        w.title(f"{APP} update")
        w.configure(bg=BASE)
        w.resizable(False, False)
        w.attributes("-topmost", True)
        ico = os.path.join(ASSETS, "lowtide.ico")
        if os.path.isfile(ico):
            w.iconbitmap(ico)
        w.update_idletasks()
        style_title_bar(user32.GetParent(w.winfo_id()) or w.winfo_id())
        body = tk.Frame(w, bg=BASE, padx=24, pady=20)
        body.pack()
        head = tk.Frame(body, bg=BASE)
        head.pack(anchor="w")
        tk.Label(head, text=ICON["download"], font=(ICON_FONT, 18), fg=GOOD, bg=BASE).pack(side="left")
        tk.Label(head, text=f"Lowtide {ver} is out", font=(FONT_SEMI, 13), fg=TEXT, bg=BASE).pack(side="left", padx=8)
        tk.Label(body, text=f"You have {VERSION}. Update now? Lowtide restarts by itself.",
                 font=(FONT, 10), fg=SUBTLE, bg=BASE).pack(anchor="w", pady=(8, 16))
        btns = tk.Frame(body, bg=BASE)
        btns.pack(anchor="e")

        def btn(text, fg, bg, cmd):
            b = tk.Label(btns, text=text, font=(FONT_SEMI, 10), fg=fg, bg=bg, padx=14, pady=6, cursor="hand2")
            b.pack(side="left", padx=(8, 0))
            b.bind("<Button-1>", lambda e: cmd())

        def later():
            w.destroy()

        def now():
            w.destroy()
            self.updater.install()
        btn("Later", TEXT, OVERLAY, later)
        btn("Update now", SURFACE, GOOD, now)
        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_width()) // 2
        y = (w.winfo_screenheight() - w.winfo_height()) // 3
        w.geometry(f"+{x}+{y}")

    def pause_hotkeys(self):
        if self.hotkey_thread_id:
            user32.PostThreadMessageW(self.hotkey_thread_id, WM_APP_PAUSE, 0, 0)

    def reload_hotkeys(self, done=None):
        self.on_hotkeys_reloaded = done
        if self.hotkey_thread_id:
            user32.PostThreadMessageW(self.hotkey_thread_id, WM_APP_RELOAD, 0, 0)

    def quit(self):
        if self.closing:
            return
        self.closing = True
        with self.cap.lock:
            for ps in self.cap.procs.values():
                write_session_log(ps, self.settings["lows_method"])
        self.cap.stop()
        self.sensors.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        self.cap.start()
        self.sensors.start()
        self.tray = make_tray(self)
        threading.Thread(target=hotkey_thread, args=(self,), daemon=True).start()
        self.overlay.run()
        if self.settings["auto_update"]:
            self.root.after(3000, self.updater.check)
        try:
            self.root.mainloop()
        finally:
            self.cap.stop()
            self.sensors.stop()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--set-startup":
        set_startup(sys.argv[2] == "on")
        return
    ensure_admin()
    single_instance()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    try:
        App().run()
    except Exception:
        import traceback
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now()} v{VERSION}\n{traceback.format_exc()}\n")
        user32.MessageBoxW(None, "Lowtide crashed. Details were saved to\n"
                                 + os.path.join(DATA, "crash.log"), APP, 0x10)


if __name__ == "__main__":
    main()
