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
    """Owns the Tk root + mainloop. Tk/Cocoa on macOS requires this to run on
    the process's real main thread — v3's first cut spun Tk on a background
    thread while pynput drove the main thread's runloop, and the two fought
    over NSWindow ownership until Python crashed with an uncaught NSException.
    Fix: Indicator now *is* the main-thread owner. The daemon's heavy init
    (model load) and the hotkey listener run on a background thread instead,
    started via `start_background`; state updates flow back through the same
    queue as before, drained by Tk's own `after()` polling on the main loop.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._root = None
        try:
            import tkinter as tk
            self._tk = tk
        except Exception:
            self._tk = None  # headless: state file still works for the menu bar

    def set(self, state: str, detail: str = "") -> None:
        write_state(state, detail)
        self._q.put((state, detail))

    def hide(self) -> None:
        write_state("idle")
        self._q.put(("hide", ""))

    def start_background(self, target, *args, **kwargs) -> None:
        """Run the daemon's own work (model load + hotkey listener) on a
        background thread, keeping this thread free for Tk's mainloop."""
        threading.Thread(target=target, args=args, kwargs=kwargs,
                          daemon=True).start()

    def run(self) -> None:
        """Build the Tk window and block on mainloop. Must be called from the
        real main thread (i.e. from `if __name__ == '__main__'` / process
        entry), after `start_background` has kicked off the daemon logic."""
        if self._tk is None:
            # headless fallback: just idle so the process stays alive for
            # the background thread; state file still updates.
            while True:
                time.sleep(3600)
        tk = self._tk
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
        self._make_accessory_app()
        self._poll()
        self._root.mainloop()

    def _make_accessory_app(self) -> None:
        """Stop this process from ever becoming macOS's 'frontmost
        application'. Root cause of the paste bug: showing a Tk window
        activates the whole host process, so history.jsonl showed
        org.python.python as frontmost during every dictation — the Cmd-V
        keystroke was landing on Python, not whatever app the user was
        dictating into. Wispr Flow's overlay avoids this by running as an
        accessory app (like a menu-bar app / LSUIElement) that can never
        take frontmost focus.

        This MUST run after tk.Tk() has already built its window — Tk does
        its own NSApplication/Cocoa setup on first Tk() call, and calling
        NSApplication.sharedApplication() before that fights over who
        initializes NSApp, which is what crashed with an uncaught
        NSException on the previous attempt. Grabbing the shared instance
        now just returns the one Tk already created and configured, so this
        only flips a policy flag on it rather than racing its setup.
        """
        try:
            from AppKit import (NSApplication,
                                NSApplicationActivationPolicyAccessory)
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)
        except Exception:
            pass  # non-macOS or pyobjc missing: don't block startup over it

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
