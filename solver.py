"""
HidenArm: hotkey -> screenshot -> Gemini (Google AI Studio) -> answer.

No window and no taskbar icon: it runs in the background (launch via pythonw / start.vbs).
In Task Manager it shows up as pythonw.exe.

The model sees where the mouse cursor is: a red ring is drawn on the screenshot, a zoomed
crop around the cursor is attached as a second image, and the prompt says to solve the task
that is closest to the cursor.

Hotkeys (all of them can be changed in the settings window or in config.json):
  ctrl+shift+x   - screenshot, solve, copy the answer to the clipboard
  ctrl+shift+z   - same, but the answer is also typed into the focused window like a human
                   (smooth pace, typos that get corrected). Pressing it again while typing
                   cancels the typing completely.
  alt            - PAUSE typing; press again to continue from the same place
                   (Alt is only claimed while typing is in progress and is not passed to the window)
  ctrl+shift+f8 / f7 - type faster / slower (step 50 chars/min, the value is shown in a toast,
                   saved to config.json and applied immediately, even in the middle of typing)
  ctrl+shift+f10 - settings window (speed, typos, hotkeys, extra rules for the AI, guide)
  ctrl+shift+f9  - reload config.json without restarting (if you edited the file by hand)
  ctrl+shift+q   - quit

Reliability:
  - on 429/5xx and network errors the request is retried with a delay, then fallback_models are tried
  - if there is no task on the screen the clipboard is left untouched (toast "task not found")
  - a big screenshot (4K) is downscaled to max_image_side: faster and cheaper; the crop around the
    cursor stays at full resolution
  - a second instance does not start (otherwise every hotkey would fire twice)
"""
import copy
import ctypes
import json
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

import keyboard
import pyperclip
from PIL import Image, ImageDraw, ImageGrab
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from humantype import HumanTyper, cpm_for_speed
from settings_ui import SettingsWindow

APP_NAME = "HidenArm"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "solver.log"
LOG_MAX_BYTES = 512 * 1024

NO_TASK = "NO_TASK"
NO_TASK_RULE = (
    f"If there is neither a task nor a question on the screen, reply with exactly one word: {NO_TASK}.\n\n"
)
NO_COMMENTS_RULE = (
    "WRITE CODE WITHOUT COMMENTS: not a single comment in the code, in any language. No #, //, /* */, <!-- -->, "
    "-- and no explanatory lines, no docstrings and no triple-quoted strings acting as comments. Only the code "
    "itself. (Directives such as #include, #define, a shebang and the like are not comments, keep them.)"
)
RETRY_CODES = {429, 500, 502, 503, 504}

DEFAULT_CONFIG = {
    "api_key": "",  # better not to store it here: set the GEMINI_API_KEY environment variable instead
    "model": "gemini-3.5-flash-lite",
    "fallback_models": [],   # backup models, tried in order if the main one is unavailable (404) or overloaded
    "retries": 3,            # attempts per model on 429/5xx/network errors
    "timeout_seconds": 60,
    "max_image_side": 1920,  # longest side of the screenshot, px (0 = do not downscale)
    "hotkey_solve": "ctrl+shift+x",
    "hotkey_quit": "ctrl+shift+q",
    "hotkey_type": "ctrl+shift+z",
    "hotkey_pause": "alt",  # pause / resume typing; only active while typing and not passed to the window
    "hotkey_speed_up": "ctrl+shift+f8",      # type faster (by speed_step), works during typing too
    "hotkey_speed_down": "ctrl+shift+f7",    # type slower
    "hotkey_reload": "ctrl+shift+f9",        # reload config.json without restarting (new hotkeys included)
    "hotkey_settings": "ctrl+shift+f10",     # settings window
    "speed_step": 50,
    "speed_min": 100,
    "speed_max": 1500,
    "show_toast": True,
    "toast_seconds": 2,
    "prompt": (
        "The screenshot shows a task or a question (for example a programming assignment "
        "or a message in a messenger). Find it and solve it.\n"
        "STRICTLY follow every condition of the task, without changing or inventing anything:\n"
        "- Programming language: only the one named in the task. If none is named, use the language "
        "of the code samples, the file name or the editor window; if that is unclear too, use Python.\n"
        "- Method: if recursion, a specific algorithm, data structure, class or pattern is required, "
        "do it exactly that way and not another.\n"
        "- Prohibitions: if a loop, built-in function, library, operator, array, slice, etc. must not be "
        "used, do not use it in any form, including hidden ones. For example, when recursion is required "
        "there must be no for, no while, no range, no map/reduce, no generators or comprehensions, and "
        "no equivalents in another syntax.\n"
        "- Take function, class and variable names, signatures and the input/output format exactly "
        "as written in the task.\n"
        "- Follow the constraints (complexity, memory, line count, language version, style).\n"
        "- Write the simplest and shortest code, understandable for a beginner: the simplest constructs "
        "and clear names, without unnecessary abstractions, classes, decorators, type annotations, error "
        "handling or optimizations unless the task requires them. But simplicity is never a reason to "
        "break the conditions above: requirements first, simplicity second.\n"
        "- Do not add anything that was not asked for: if a function is requested give only the function; "
        "if a whole program is requested give the complete program; no tests or usage examples unless "
        "requested.\n"
        "Before answering, mentally check the solution against each condition in turn and fix any "
        "violation.\n"
        "Reply only with the finished solution/answer, with no preamble and no description of the "
        "screenshot. Write text answers in the language of the task. "
        "Use plain text: no LaTeX, no $ signs and no markdown."
    ),
    "no_comments": True,  # True = the AI writes code without a single comment (#, //, /* */, docstring)
    "extra_rules": "",  # your permanent rules, added to every request (for example: "English variable names")
    # --- cursor ---
    "cursor_aware": True,       # show the AI where the cursor is
    "cursor_crop": [1100, 700],  # size of the crop around the cursor (width, height); [0, 0] = no crop
    "cursor_hint": (
        "The mouse cursor is at x={x}, y={y} (screen {w}x{h}: {px}% across, {py}% down) "
        "and is marked with a red ring on the screenshot. {crop_note}"
        "The task or question you need is near the cursor: solve the one closest to the red ring "
        "(under it, above it, next to it or in the same block/message). All other text on the screen is "
        "only context and must not be solved. Search the whole screen only if there is neither a task "
        "nor a question near the cursor."
    ),
    # --- human-like typing (hotkey_type) ---
    "typing": {
        "speed": 500,          # REAL typing speed, characters per minute (pauses and typos included)
        "typo_rate": 0.035,    # probability of a typo per letter (0 = no typos)
        "think_chance": 0.02,  # probability of a "thinking" pause between words
        "newline_key": "enter",  # how to start a new line: "enter" or "shift+enter" (for chats)
    },
}

# Windows: the cursor coordinates and the screenshot must use the same physical pixels
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


# --- log ---
def log(msg: str) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))  # a single archive, the old one is overwritten
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def fatal(msg: str) -> None:
    """There is no console under pythonw, so critical problems are shown in a message box."""
    log("FATAL: " + msg)
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, APP_NAME, 0x10)
    except Exception:  # noqa: BLE001
        pass


def ensure_single_instance():
    """Returns the mutex handle (keep it until exit) or None if another instance is already running."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\ScreenSolverSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


# --- screen and cursor ---
def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitor_rect_at(x: int, y: int) -> tuple[int, int, int, int]:
    """Rectangle of the monitor the cursor is on (in virtual-screen coordinates)."""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        raise OSError("GetMonitorInfoW failed")
    r = mi.rcMonitor
    return r.left, r.top, r.right, r.bottom


def draw_cursor_ring(img, x: int, y: int) -> None:
    """A red ring with a white backing: visible on any background, text inside stays readable."""
    d = ImageDraw.Draw(img)
    r = max(28, round(min(img.size) * 0.035))
    d.ellipse((x - r, y - r, x + r, y + r), outline="white", width=7)
    d.ellipse((x - r, y - r, x + r, y + r), outline="#e00000", width=3)


def downscale(img, max_side: int):
    """Shrinks the image if its longest side is above max_side. Returns (image, scale factor)."""
    m = max(img.size)
    if max_side <= 0 or m <= max_side:
        return img, 1.0
    k = max_side / m
    return img.resize((round(img.width * k), round(img.height * k)), Image.Resampling.LANCZOS), k


def annotate_cursor(img, cx: int, cy: int, crop_size, max_side: int = 0) -> tuple:
    """Returns (whole screenshot with the ring, crop around the cursor or None, data for the hint).
    The whole screenshot may be downscaled, the crop is always at full resolution."""
    full_w, full_h = img.size
    cx, cy = min(max(cx, 0), full_w - 1), min(max(cy, 0), full_h - 1)

    small, k = downscale(img, max_side)
    sx, sy = round(cx * k), round(cy * k)

    crop = None
    crop_note = ""
    cw, ch = (int(crop_size[0]), int(crop_size[1])) if crop_size else (0, 0)
    cw, ch = min(cw, full_w), min(ch, full_h)
    if cw > 0 and ch > 0 and (cw < full_w or ch < full_h):
        left = min(max(cx - cw // 2, 0), full_w - cw)
        top = min(max(cy - ch // 2, 0), full_h - ch)
        crop = img.crop((left, top, left + cw, top + ch))  # a copy: the ring drawn on small does not touch it
        draw_cursor_ring(crop, cx - left, cy - top)
        crop_note = (
            f"The second image is a crop of the screen around the cursor at full resolution "
            f"(the cursor in it is at x={cx - left}, y={cy - top}, also marked with a red ring). "
        )
    draw_cursor_ring(small, sx, sy)
    w, h = small.size
    info = {"x": sx, "y": sy, "w": w, "h": h,
            "px": round(sx / w * 100), "py": round(sy / h * 100), "crop_note": crop_note}
    return small, crop, info


def grab_screen(cfg: dict) -> tuple[list, str]:
    """Screenshot of the monitor under the cursor. Returns (images for the model, cursor hint or '')."""
    max_side = int(cfg["max_image_side"])
    if not cfg["cursor_aware"]:
        return [downscale(ImageGrab.grab(), max_side)[0]], ""
    cx, cy = get_cursor_pos()
    try:
        left, top, right, bottom = monitor_rect_at(cx, cy)
        img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    except Exception as e:  # noqa: BLE001
        log(f"WARN: could not find the monitor under the cursor ({e!r}), using the primary screen")
        left, top = 0, 0
        img = ImageGrab.grab()
    small, crop, info = annotate_cursor(img, cx - left, cy - top, cfg["cursor_crop"], max_side)
    try:
        hint = cfg["cursor_hint"].format(**info)
    except (KeyError, IndexError, ValueError):
        hint = cfg["cursor_hint"]
    return ([small, crop] if crop is not None else [small]), hint


# --- answer processing ---
def plain_for_typing(text: str) -> str:
    """Drops ``` lines (the model sometimes adds markdown code blocks) and unwraps answers put in $...$."""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
    out = "\n".join(lines).strip("\n")
    m = re.fullmatch(r"\s*\${1,2}([^$]+)\${1,2}\s*", out)  # the whole answer wrapped in $...$ is LaTeX, not code
    return m.group(1).strip() if m else out


def is_no_task(text: str) -> bool:
    return re.fullmatch(rf"\W*{NO_TASK}\W*", text.strip(), re.IGNORECASE) is not None


def _class_names(e: BaseException) -> set:
    return {c.__name__ for c in type(e).__mro__}


def is_timeout(e: BaseException) -> bool:
    # google-genai uses httpx or httpx2 depending on its version, so match by class names without importing
    return isinstance(e, TimeoutError) or "TimeoutException" in _class_names(e)


def is_network_error(e: BaseException) -> bool:
    """Connection lost, DNS failure, timeout: the request is worth repeating."""
    return isinstance(e, OSError) or is_timeout(e) or "TransportError" in _class_names(e)


def explain_error(e: Exception) -> str:
    """Short reason for the toast."""
    if is_timeout(e):
        return "timeout"
    if is_network_error(e):
        return "no network"
    code = getattr(e, "code", None)
    if code in (401, 403):
        return "check API key"
    if code == 404:
        return "model not found"
    if code == 429:
        return "rate limit"
    if isinstance(code, int) and code >= 500:
        return "server overloaded"
    return "error"


def wait_modifiers_released(timeout: float = 5.0) -> None:
    """The hotkey may still be held down and typing with ctrl+shift pressed is impossible: wait for release."""
    end = time.time() + timeout
    while time.time() < end:
        if not any(keyboard.is_pressed(k) for k in ("ctrl", "shift", "alt", "windows")):
            return
        time.sleep(0.05)


def copy_to_clipboard(text: str, attempts: int = 20) -> None:
    """The clipboard is sometimes locked by another program: retry a few times."""
    for i in range(attempts):
        try:
            pyperclip.copy(text)
            return
        except pyperclip.PyperclipException:
            if i == attempts - 1:
                raise
            time.sleep(0.1)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # merge "typing" key by key: fields missing in config.json keep their defaults
        cfg["typing"].update(user.pop("typing", {}))
        cfg.update(user)
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg["api_key"] = os.environ.get("GEMINI_API_KEY") or cfg["api_key"]  # the environment variable wins over the file
    return cfg


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = genai.Client(
            api_key=cfg["api_key"],
            http_options=types.HttpOptions(timeout=int(cfg["timeout_seconds"] * 1000)),
        )
        self.busy = threading.Lock()
        self.typing_now = False
        self.abort_typing = threading.Event()
        self.paused = threading.Event()
        self._last_toggle = 0.0
        self.typer: "HumanTyper | None" = None  # the typing in progress (so the speed can change on the fly)
        self.typing_text = ""
        self.settings_win: "SettingsWindow | None" = None
        self.ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()  # there is no main window
        self.toast = None

        self._hotkeys = self.bind_hotkeys(cfg)
        self.root.after(100, self.pump)

    # --- hotkeys ---
    def bind_hotkeys(self, cfg: dict) -> list:
        """Registers the hotkeys; on a bad key name removes the ones already registered and re-raises."""
        handles = []
        try:
            keyboard.parse_hotkey(cfg["hotkey_pause"])  # the pause key is only hooked while typing: validate it up front
            handles.append(keyboard.add_hotkey(cfg["hotkey_solve"], self.on_solve))
            handles.append(keyboard.add_hotkey(cfg["hotkey_type"], self.on_type))
            handles.append(keyboard.add_hotkey(cfg["hotkey_quit"], self.on_quit))
            # No suppress: with it the keyboard library broke neighbouring hotkeys. That is why speed and reload
            # use F-keys (Ctrl+Shift+F7/F8/F9): they do nothing in the window we type into.
            # Ctrl+Shift+Up/Down would not work: they would select text in the middle of typing.
            handles.append(keyboard.add_hotkey(cfg["hotkey_speed_up"], lambda: self.change_speed(+1)))
            handles.append(keyboard.add_hotkey(cfg["hotkey_speed_down"], lambda: self.change_speed(-1)))
            handles.append(keyboard.add_hotkey(cfg["hotkey_reload"], self.reload_config))
            handles.append(keyboard.add_hotkey(cfg["hotkey_settings"], lambda: self.ui_queue.put(("settings", ""))))
        except Exception:
            self.unbind_hotkeys(handles)
            raise
        return handles

    @staticmethod
    def unbind_hotkeys(handles: list) -> None:
        for h in handles:
            try:
                keyboard.remove_hotkey(h)
            except (KeyError, ValueError):
                pass

    # --- settings on the fly ---
    def change_speed(self, direction: int):
        t = self.cfg["typing"]
        speed = min(max(int(t["speed"]) + direction * int(self.cfg["speed_step"]),
                        int(self.cfg["speed_min"])), int(self.cfg["speed_max"]))
        t["speed"] = speed
        self.ui_queue.put(("ok", f"⚡ {speed} chars/min"))
        threading.Thread(target=self._apply_speed, args=(speed,), daemon=True).start()

    def _apply_speed(self, speed: int):
        # save the CURRENT value: with fast repeated key presses an older thread must not overwrite a newer value
        speed = int(self.cfg["typing"]["speed"])
        self._save_setting(("typing", "speed"), speed)
        typer, text = self.typer, self.typing_text
        if self.typing_now and typer is not None and text:
            typer.set_cpm(cpm_for_speed(text, speed, self.cfg["typing"]))  # picked up on the next character

    @classmethod
    def _save_setting(cls, path: tuple, value) -> None:
        cls._save_settings({path: value})

    _save_lock = threading.Lock()

    @classmethod
    def _save_settings(cls, items: dict) -> bool:
        """Changes values in config.json ({('typing', 'speed'): 300, ...}) and leaves the rest untouched."""
        try:
            with cls._save_lock:  # read-modify-write must not interleave between threads
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
                for path, value in items.items():
                    node = data
                    for k in path[:-1]:
                        node = node.setdefault(k, {})
                    node[path[-1]] = value
                CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except (OSError, ValueError) as e:
            log(f"WARN: could not save settings {list(items)}: {e!r}")
            return False

    # --- settings window ---
    def open_settings(self):
        """Opens the settings window (main thread). The app's hotkeys are released while it is open."""
        if self.settings_win is not None:
            self.settings_win.win.lift()
            self.settings_win.win.focus_force()
            return
        if self.typing_now:
            self.show_toast("⌨ wait until typing is done", "warn")
            return
        self.unbind_hotkeys(self._hotkeys)
        self._hotkeys = []
        try:
            self.settings_win = SettingsWindow(self.root, self.cfg, self.apply_settings, self._settings_closed)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: settings window failed to open: {e!r}")
            self.settings_win = None
            self._settings_closed(saved=False)

    def _settings_closed(self, saved: bool):
        self.settings_win = None
        if not saved:  # when saved, apply_settings has already registered the new hotkeys
            try:
                self._hotkeys = self.bind_hotkeys(self.cfg)
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: could not restore the hotkeys: {e!r}")
                self.ui_queue.put(("err", "✗ hotkeys not restored (Ctrl+Shift+F9)"))

    def apply_settings(self, flat: dict) -> "str | None":
        """flat: {'typing.speed': 300, 'hotkey_solve': 'ctrl+alt+s', ...}. None = applied and saved, else an error text."""
        new = copy.deepcopy(self.cfg)
        items = {}
        for dotted, value in flat.items():
            path = tuple(dotted.split("."))
            node = new
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            items[path] = value
        try:
            handles = self.bind_hotkeys(new)  # the hotkeys are released now (window open): check the new ones work
        except Exception as e:  # noqa: BLE001
            return f"These hotkeys cannot be used.\n{e}"
        self._hotkeys = handles
        self.cfg = new
        if not self._save_settings(items):
            self.ui_queue.put(("warn", "⚠ applied, but not saved to the file"))
        else:
            self.ui_queue.put(("ok", "✓ settings saved"))
        return None

    def reload_config(self):
        """Re-reads config.json. If it contains an error the old working configuration stays."""
        try:
            new = load_config()
            if not new["api_key"]:
                raise ValueError("no api_key")
            old = self.cfg
            self.unbind_hotkeys(self._hotkeys)
            try:
                self._hotkeys = self.bind_hotkeys(new)
            except Exception:
                self._hotkeys = self.bind_hotkeys(old)  # the new hotkeys are broken: put the old ones back
                raise
            self.cfg = new
            if (new["api_key"], new["timeout_seconds"]) != (old["api_key"], old["timeout_seconds"]):
                self.client = genai.Client(
                    api_key=new["api_key"],
                    http_options=types.HttpOptions(timeout=int(new["timeout_seconds"] * 1000)),
                )
            log("OK: config.json reloaded")
            self.ui_queue.put(("ok", "⟳ settings applied"))
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: could not reload config.json: {e!r}")
            self.ui_queue.put(("err", "✗ config.json has an error, kept the old settings"))

    # --- UI (main thread only) ---
    def pump(self):
        try:
            while True:
                kind, text = self.ui_queue.get_nowait()
                if kind == "quit":
                    self.root.destroy()
                    return
                if kind == "settings":
                    self.open_settings()
                    continue
                self.show_toast(text, kind)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def show_toast(self, text: str, kind: str):
        if not self.cfg["show_toast"]:
            return
        if self.toast is not None:
            self.toast.destroy()
        colors = {"busy": "#888888", "ok": "#3a9a5b", "err": "#b94a48", "warn": "#b08a2e"}
        t = tk.Toplevel(self.root)
        t.withdraw()                       # shown after the style is set up
        t.overrideredirect(True)           # no frame
        t.attributes("-topmost", True)
        t.attributes("-toolwindow", True)  # not shown in the taskbar and Alt+Tab
        t.attributes("-alpha", 0.6)        # semi-transparent
        lbl = tk.Label(t, text=text, bg=colors.get(kind, "#555"), fg="white",
                       font=("Segoe UI", 8), padx=6, pady=2)
        lbl.pack()
        t.update_idletasks()
        self._no_activate(t)
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        t.geometry(f"+{sw - t.winfo_reqwidth() - 8}+{sh - t.winfo_reqheight() - 56}")
        t.deiconify()
        self.toast = t
        if kind != "busy":
            t.after(int(self.cfg["toast_seconds"] * 1000), lambda: self._close_toast(t))

    @staticmethod
    def _no_activate(win) -> None:
        """The toast must not steal focus from the window we type into (WS_EX_NOACTIVATE)."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, style | 0x08000000)  # WS_EX_NOACTIVATE
        except Exception:  # noqa: BLE001
            pass

    def _close_toast(self, t):
        if self.toast is t:
            t.destroy()
            self.toast = None

    # --- work ---
    def on_quit(self):
        self.abort_typing.set()
        self.ui_queue.put(("quit", ""))

    def on_solve(self):
        self._start(type_it=False)

    def on_type(self):
        if self.typing_now:  # pressing the hotkey again during typing = cancel completely
            self.abort_typing.set()
            return
        self._start(type_it=True)

    def on_pause(self):
        """Pause / resume: typing stops at the current character and later continues from it."""
        if not self.typing_now:
            return
        now = time.monotonic()
        if now - self._last_toggle < 0.35:  # key auto-repeat and bounce must not toggle twice
            return
        self._last_toggle = now
        if self.paused.is_set():
            self.paused.clear()
            self.ui_queue.put(("busy", "⌨"))
        else:
            self.paused.set()
            self.ui_queue.put(("busy", "⏸ paused"))

    def _claim_pause_key(self):
        """Takes the pause key while typing (suppress: it does not reach the window, the Alt menu does not open).
        Returns a function that releases it."""
        key = self.cfg["hotkey_pause"]
        if "+" in key:
            h = keyboard.add_hotkey(key, self.on_pause, suppress=True)
            return lambda: keyboard.remove_hotkey(h)
        h = keyboard.on_press_key(key, lambda e: self.on_pause(), suppress=True)
        return lambda: keyboard.unhook(h)

    def _typing_wait(self, seconds: float) -> bool:
        """Pause between key presses. True = typing was cancelled. While paused we stand still and the time is not counted."""
        end = time.monotonic() + seconds
        was_paused = False
        while True:
            if self.abort_typing.is_set():
                return True
            if self.paused.is_set():
                was_paused = True
                self.abort_typing.wait(0.05)
                continue
            if was_paused:  # just resumed: wait until the hotkey is released and "come back to ourselves"
                was_paused = False
                wait_modifiers_released()
                end = max(end, time.monotonic() + 0.3)
                continue
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.abort_typing.wait(min(remaining, 0.05))

    def _start(self, type_it: bool):
        if not self.busy.acquire(blocking=False):
            return  # the previous screenshot is still being solved
        threading.Thread(target=self.solve, args=(type_it,), daemon=True).start()

    def ask_model(self, contents: list):
        """Request to Gemini with retries: 429/5xx and network errors -> wait and try again; 404 -> next model."""
        models = [self.cfg["model"], *self.cfg["fallback_models"]]
        retries = max(1, int(self.cfg["retries"]))
        last_error: Exception | None = None
        for model in models:
            for attempt in range(retries):
                try:
                    return self.client.models.generate_content(model=model, contents=contents)
                except genai_errors.APIError as e:
                    last_error = e
                    if e.code == 404:
                        log(f"WARN: model {model} is unavailable (404), trying the next one")
                        break
                    if e.code not in RETRY_CODES:
                        raise
                except Exception as e:  # noqa: BLE001
                    if not is_network_error(e):  # network, connection loss, timeout: retry; anything else: raise at once
                        raise
                    last_error = e
                if attempt + 1 < retries:
                    delay = 1.5 * 2 ** attempt + random.uniform(0, 0.5)
                    log(f"WARN: {model}: {last_error!r}; retry {attempt + 2}/{retries} in {delay:.1f} s")
                    self.ui_queue.put(("busy", f"↻ {attempt + 2}/{retries}"))
                    time.sleep(delay)
        assert last_error is not None
        raise last_error

    def solve(self, type_it: bool = False):
        try:
            self.ui_queue.put(("busy", "…"))
            images, hint = grab_screen(self.cfg)
            rules = [self.cfg["extra_rules"].strip()]
            if self.cfg["no_comments"]:
                rules.append(NO_COMMENTS_RULE)
            extra = "\n".join(r for r in rules if r)
            prompt = self.cfg["prompt"] + ("\n" + extra if extra else "") + "\n\n" + NO_TASK_RULE + hint
            resp = self.ask_model([*images, prompt])
            answer = plain_for_typing((resp.text or "").strip())
            if not answer:
                raise RuntimeError("empty response from the model")
            if is_no_task(answer):
                log("NO_TASK: no task on the screen")
                self.ui_queue.put(("warn", "? task not found"))
                return
            copy_to_clipboard(answer)  # also when typing: a fallback in case the typing is interrupted
            log("OK: " + answer[:200].replace("\n", " "))
            if type_it:
                self.type_answer(answer)
            else:
                self.ui_queue.put(("ok", "✓ in clipboard"))
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: {e!r}")
            self.ui_queue.put(("err", f"✗ {explain_error(e)} (solver.log)"))
        finally:
            self.typing_now = False
            self.busy.release()

    def type_answer(self, text: str):
        """Types text into the focused window, imitating a human."""
        tcfg = self.cfg["typing"]
        newline_key = tcfg.get("newline_key", "enter")
        self.abort_typing.clear()
        self.paused.clear()
        self.typing_now = True
        self.ui_queue.put(("busy", "⌨"))
        wait_modifiers_released()

        def send_char(c: str) -> None:
            if c == "\n":
                keyboard.send(newline_key)
            else:
                # Directly, not keyboard.write: that one "releases" held keys before every character
                # (including the pause key) and breaks their tracking.
                keyboard._os_keyboard.type_unicode(c)

        # speed is real characters per minute; the nominal pace is picked by simulation for this very text
        tcfg = {**tcfg, "cpm": cpm_for_speed(text, float(tcfg["speed"]), tcfg)}
        release_pause_key = self._claim_pause_key()
        try:
            self.typing_text = text
            self.typer = HumanTyper(
                send_char=send_char,
                send_backspace=lambda: keyboard.send("backspace"),
                wait=self._typing_wait,  # True if typing was cancelled; honours pause/resume
                cfg=tcfg,
            )
            done = self.typer.type_text(text)
        finally:
            self.typer = None
            release_pause_key()
        self.typing_now = False
        if done:
            self.ui_queue.put(("ok", "✓"))
        else:
            self.ui_queue.put(("err", "■ stopped (answer is in the clipboard)"))

    def run(self):
        self.root.mainloop()
        keyboard.unhook_all()


def main():
    mutex = ensure_single_instance()  # noqa: F841  (the handle is kept until exit)
    if mutex is None:
        sys.exit(0)  # already running
    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        fatal(f"Could not read config.json: {e}")
        sys.exit(1)
    if not cfg["api_key"]:
        fatal("No API key: set the GEMINI_API_KEY environment variable (or the api_key field in config.json).")
        sys.exit(1)
    App(cfg).run()


if __name__ == "__main__":
    main()
