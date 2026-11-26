"""
HidenArm settings window. Opened with a hotkey (Ctrl+Shift+F10 by default).
It is an ordinary window: while it is open it is visible on the screen and in the taskbar; once closed the
program is windowless again. While it is open the app's hotkeys are released (otherwise they would fire
while a new hotkey is being recorded).
"""
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import keyboard

APP_NAME = "HidenArm"
GUIDE_PATH = Path(__file__).resolve().parent / "GUIDE.txt"

HOTKEY_ROWS = [
    ("hotkey_solve", "Solve, answer to clipboard"),
    ("hotkey_type", "Type the clipboard text like a human"),
    ("hotkey_clipboard", "Task from clipboard, answer to clipboard"),
    ("hotkey_pause", "Pause / resume typing"),
    ("hotkey_speed_down", "Type slower"),
    ("hotkey_speed_up", "Type faster"),
    ("hotkey_settings", "Open this window"),
    ("hotkey_quit", "Quit the program"),
]
ALL_HOTKEYS = [k for k, _ in HOTKEY_ROWS] + ["hotkey_reload"]
HOTKEY_TITLES = dict(HOTKEY_ROWS, hotkey_reload="Reload config.json")

CROP_PRESETS = {
    "Small": [800, 500],
    "Medium (default)": [1100, 700],
    "Large (for long tasks)": [1600, 1000],
    "Off (whole screen only)": [0, 0],
}
NEWLINE_CHOICES = {
    "Enter (normal)": "enter",
    "Shift+Enter (for chats: Telegram, WhatsApp, Discord)": "shift+enter",
}
SPEED_PRESETS = (("Slow", 250), ("Like a normal person", 500), ("Fast", 900))


def pretty(hotkey: str) -> str:
    return " + ".join(p.strip().capitalize() for p in hotkey.split("+"))


class SettingsWindow:
    def __init__(self, root, cfg: dict, apply, on_close):
        """apply(flat_dict) -> None if saved, otherwise an error text. on_close(saved: bool)."""
        self.cfg, self.apply, self.on_close = cfg, apply, on_close
        self.closed = False
        self.recording = None
        self.rec_queue: "queue.Queue" = queue.Queue()
        self.hk_vars: dict = {}
        self.hk_btns: dict = {}

        w = self.win = tk.Toplevel(root)
        w.title(f"{APP_NAME}: settings")
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", self.cancel)

        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self._tab_main(nb)
        self._tab_keys(nb)
        self._tab_help(nb)

        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.cancel).pack(side="right", padx=6)
        ttk.Label(bar, foreground="#666", text="While this window is open the program's hotkeys are off.").pack(side="left")

        w.update_idletasks()
        x = (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2
        y = max(0, (w.winfo_screenheight() - w.winfo_reqheight()) // 3)
        w.geometry(f"+{x}+{y}")
        w.attributes("-topmost", True)
        w.after(300, lambda: w.attributes("-topmost", False))
        w.lift()
        w.focus_force()
        w.after(100, self._poll)

    # --- tabs ---
    def _tab_main(self, nb):
        t = self.cfg["typing"]
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  General  ")

        box = ttk.LabelFrame(f, text="Typing speed, characters per minute", padding=8)
        box.pack(fill="x")
        self.speed = tk.IntVar(value=int(t["speed"]))
        tk.Scale(box, from_=int(self.cfg["speed_min"]), to=int(self.cfg["speed_max"]),
                 resolution=int(self.cfg["speed_step"]), orient="horizontal", variable=self.speed,
                 length=540).pack()
        row = ttk.Frame(box)
        row.pack()
        for label, value in SPEED_PRESETS:
            ttk.Button(row, text=f"{label} ({value})", command=lambda v=value: self.speed.set(v)).pack(side="left", padx=4)
        ttk.Label(box, foreground="#666", text="This is the real speed, pauses and typos included. "
                  "A normal person types 200-400.").pack(pady=(6, 0))

        box = ttk.LabelFrame(f, text="Typos: percent of letters typed wrong (and then corrected)", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.typo = tk.DoubleVar(value=round(float(t["typo_rate"]) * 100, 1))
        tk.Scale(box, from_=0, to=10, resolution=0.5, orient="horizontal", variable=self.typo, length=540).pack()
        ttk.Label(box, foreground="#666", text="0 = type without a single typo.").pack()

        grid = ttk.Frame(f)
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="New line:").grid(row=0, column=0, sticky="w", pady=3)
        cur = t.get("newline_key", "enter")
        names = list(NEWLINE_CHOICES)
        self.newline = tk.StringVar(value=next((n for n, v in NEWLINE_CHOICES.items() if v == cur), names[0]))
        ttk.Combobox(grid, textvariable=self.newline, values=names, state="readonly", width=52).grid(
            row=0, column=1, sticky="w", padx=8)

        ttk.Label(grid, text="Area near cursor:").grid(row=1, column=0, sticky="w", pady=3)
        crop = list(self.cfg["cursor_crop"])
        self.crop_names = dict(CROP_PRESETS)
        current = next((n for n, v in CROP_PRESETS.items() if v == crop), None)
        if current is None:
            current = f"Custom from config.json ({crop[0]}x{crop[1]})"
            self.crop_names[current] = crop
        self.crop = tk.StringVar(value=current)
        ttk.Combobox(grid, textvariable=self.crop, values=list(self.crop_names), state="readonly", width=52).grid(
            row=1, column=1, sticky="w", padx=8)

        ttk.Label(grid, text="Gemini model:").grid(row=2, column=0, sticky="w", pady=3)
        self.model = tk.StringVar(value=self.cfg["model"])
        ttk.Entry(grid, textvariable=self.model, width=55).grid(row=2, column=1, sticky="w", padx=8)

        self.toast = tk.BooleanVar(value=bool(self.cfg["show_toast"]))
        ttk.Checkbutton(f, text="Show small hints in the corner of the screen (✓, ⌨, ✗ etc.)",
                        variable=self.toast).pack(anchor="w", pady=(6, 0))
        self.no_comments = tk.BooleanVar(value=bool(self.cfg.get("no_comments", True)))
        ttk.Checkbutton(f, text="Code without comments (no #, //, /* */ or docstrings)",
                        variable=self.no_comments).pack(anchor="w")

        ttk.Label(f, text="Your permanent rules for the AI (added to every request), for example: "
                  "\"English variable names\"").pack(anchor="w", pady=(8, 2))
        self.rules = tk.Text(f, height=3, width=70, wrap="word", font=("Segoe UI", 10))
        self.rules.pack(fill="x")
        self.rules.insert("1.0", self.cfg.get("extra_rules", ""))

    def _tab_keys(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="  Hotkeys  ")
        ttk.Label(f, text="Click \"Change\", then press the key combination you want on the keyboard. "
                  "Esc cancels.", wraplength=560).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        for i, (key, title) in enumerate(HOTKEY_ROWS, start=1):
            ttk.Label(f, text=title).grid(row=i, column=0, sticky="w", pady=5)
            var = tk.StringVar(value=self.cfg[key])
            self.hk_vars[key] = var
            shown = tk.StringVar(value=pretty(var.get()))
            var.trace_add("write", lambda *_a, v=var, s=shown: s.set(pretty(v.get())))
            ttk.Label(f, textvariable=shown, font=("Segoe UI", 10, "bold"), width=22).grid(row=i, column=1, padx=12)
            btn = ttk.Button(f, text="Change", command=lambda k=key: self.record(k))
            btn.grid(row=i, column=2)
            self.hk_btns[key] = btn
        ttk.Label(f, foreground="#666", wraplength=560,
                  text="Tip: use combinations with Ctrl+Shift or F-keys so they do not interfere with normal work. "
                       "Arrow keys together with Shift select text in the window the answer is typed into, "
                       "which is why F7 and F8 are used for speed.").grid(
            row=len(HOTKEY_ROWS) + 1, column=0, columnspan=3, sticky="w", pady=(14, 0))

    def _tab_help(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="  How to use  ")
        try:
            text = GUIDE_PATH.read_text(encoding="utf-8")
        except OSError:
            text = "GUIDE.txt was not found next to the program."
        sb = ttk.Scrollbar(f)
        sb.pack(side="right", fill="y")
        t = tk.Text(f, height=24, width=84, wrap="word", font=("Segoe UI", 10), yscrollcommand=sb.set, padx=6, pady=4)
        t.pack(side="left", fill="both", expand=True)
        sb.config(command=t.yview)
        t.insert("1.0", text)
        t.config(state="disabled")

    # --- hotkey recording ---
    def record(self, key: str):
        if self.recording:
            return
        self.recording = key
        for b in self.hk_btns.values():
            b.config(state="disabled")
        self.hk_btns[key].config(text="Press keys…")
        threading.Thread(target=self._read_hotkey, daemon=True).start()

    def _read_hotkey(self):
        try:
            hk = keyboard.read_hotkey(suppress=False)  # waits until a combination is pressed and released
        except Exception:  # noqa: BLE001
            hk = None
        self.rec_queue.put(hk)

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                self._got(self.rec_queue.get_nowait())
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def _got(self, hk):
        key, self.recording = self.recording, None
        if key is None:
            return
        if hk and hk.lower() != "esc":
            self.hk_vars[key].set(hk.lower())
        for b in self.hk_btns.values():
            b.config(state="normal", text="Change")

    # --- buttons ---
    def save(self):
        if self.recording:
            messagebox.showinfo(APP_NAME, "First press the key combination you want (or Esc).", parent=self.win)
            return
        hotkeys = {k: v.get().strip().lower() for k, v in self.hk_vars.items()}
        hotkeys["hotkey_reload"] = self.cfg["hotkey_reload"]
        dup = sorted({pretty(v) for v in hotkeys.values() if list(hotkeys.values()).count(v) > 1})
        if dup:
            messagebox.showerror(APP_NAME, "Different actions share the same hotkey: " + ", ".join(dup) +
                                 ".\nEvery action needs its own combination.", parent=self.win)
            return
        model = self.model.get().strip()
        if not model:
            messagebox.showerror(APP_NAME, "The model name cannot be empty.", parent=self.win)
            return
        flat = {
            "typing.speed": int(self.speed.get()),
            "typing.typo_rate": round(float(self.typo.get()) / 100, 4),
            "typing.newline_key": NEWLINE_CHOICES[self.newline.get()],
            "cursor_crop": list(self.crop_names[self.crop.get()]),
            "show_toast": bool(self.toast.get()),
            "no_comments": bool(self.no_comments.get()),
            "extra_rules": self.rules.get("1.0", "end").strip(),
            "model": model,
            **hotkeys,
        }
        error = self.apply(flat)
        if error:
            messagebox.showerror(APP_NAME, error, parent=self.win)
            return
        self._close(saved=True)

    def cancel(self):
        self._close(saved=False)

    def _close(self, saved: bool):
        if self.closed:
            return
        self.closed = True
        self.win.destroy()
        self.on_close(saved)
