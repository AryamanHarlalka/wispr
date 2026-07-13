"""F6 — on-screen feedback pill (Tkinter, threaded) + state file for the
menu-bar companion. Five states: listening / transcribing / cleaning /
pasted / error. The v2 lesson: invisible feedback reads as broken — the
pill sits bottom-center, always-on-top, and every state looks different.
"""
from __future__ import annotations

import json
import queue
import threading
import time

from .config import MURMUR_HOME, ensure_home

STATE_FILE = MURMUR_HOME / "state.json"

COLORS = {
    "listening": ("#1db954", "● listening"),
    "transcribing": ("#e8a13a", "… transcribing"),
    "cleaning": ("#5aa2e8", "✦ cleaning"),
    "pasted": ("#1db954", "✓ pasted"),
    "error": ("#e85a5a", "✗ error"),
}


def write_state(state: str, detail: str = "") -> None:
    ensure_home()
    try:
        STATE_FILE.write_text(json.dumps(
            {"state": state, "detail": detail, "ts": time.time()}))
    except OSError:
        pass


class Indicator:
    """Runs Tk in its own thread; the daemon posts states via a queue."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set(self, state: str, detail: str = "") -> None:
        write_state(state, detail)
        self._q.put((state, detail))

    def hide(self) -> None:
        write_state("idle")
        self._q.put(("hide", ""))

    # --- tk thread ---
    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:
            return  # headless: state file still works for the menu bar
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        try:
            self._root.attributes("-alpha", 0.92)
        except Exception:
            pass
        self._label = tk.Label(
            self._root, text="", fg="white", bg="#181818",
            font=("Menlo", 13), padx=18, pady=8)
        self._label.pack()
        self._root.configure(bg="#181818")
        self._poll()
        self._root.mainloop()

    def _place(self) -> None:
        self._root.update_idletasks()
        w = self._label.winfo_reqwidth()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._root.geometry(f"+{(sw - w) // 2}+{sh - 120}")

    def _poll(self) -> None:
        try:
            while True:
                state, detail = self._q.get_nowait()
                if state == "hide":
                    self._root.withdraw()
                    continue
                color, text = COLORS.get(state, ("#888888", state))
                if detail:
                    text = f"{text} · {detail}"
                self._label.config(text=text, fg=color)
                self._place()
                self._root.deiconify()
                if state == "pasted":
                    self._root.after(900, self._root.withdraw)
        except queue.Empty:
            pass
        self._root.after(80, self._poll)
