"""F6 — on-screen feedback pill (Tkinter, threaded) + state file for the
menu-bar companion. Five states: listening / transcribing / cleaning /
pasted / error. The v2 lesson: invisible feedback reads as broken — the
pill sits bottom-center, always-on-top, and every state looks different.

B4: the pill now draws a live waveform off Recorder.level during
"listening" (fed via push_level()), and state changes cross-fade their
accent color + the window's show/hide alpha instead of snapping — reads
closer to Wispr's polish without touching the fragile accessory-app /
capture-and-reactivate machinery below, which is untouched from the
three rounds of debugging that got it working.
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

# --- waveform geometry (B4) ---
BAR_COUNT = 24
BAR_WIDTH = 4
BAR_GAP = 2
BAR_MAX_H = 20
CANVAS_W = BAR_COUNT * (BAR_WIDTH + BAR_GAP)
CANVAS_H = BAR_MAX_H + 4
# Recorder.level is a raw abs-mean of float32 samples — typically small
# (quiet speech ~0.01-0.05). This gain just maps that range onto 0..1 for
# bar height; it's a cosmetic scale, not a calibrated meter.
LEVEL_GAIN = 18.0

# --- animation timing (B4) ---
FADE_STEPS = 5
FADE_MS = 16          # ~80ms full fade
COLOR_STEPS = 6
COLOR_MS = 18         # ~108ms color cross-fade between states
PILL_ALPHA = 0.92


def write_state(state: str, detail: str = "") -> None:
    ensure_home()
    try:
        STATE_FILE.write_text(json.dumps(
            {"state": state, "detail": detail, "ts": time.time()}))
    except OSError:
        pass


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lerp_hex(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    bl = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


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
        self._level_q: queue.Queue[float] = queue.Queue()
        self._levels: list[float] = [0.0] * BAR_COUNT
        self._root = None
        self._state = "idle"
        self._current_color = "#888888"
        self._visible = False
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

    def push_level(self, level: float) -> None:
        """B4 — feed a Recorder.level sample in; drawn on the next poll
        tick while state == 'listening'. Cheap producer side (one float
        queued); all drawing happens on the Tk thread in _poll()."""
        self._level_q.put(level)

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
            self._root.attributes("-alpha", 0.0)  # fade_in() brings it up
        except Exception:
            pass
        self._label = tk.Label(
            self._root, text="", fg="white", bg="#181818",
            font=("Menlo", 13), padx=18, pady=6)
        self._label.pack()
        self._canvas = tk.Canvas(
            self._root, width=CANVAS_W, height=CANVAS_H,
            bg="#181818", highlightthickness=0)
        self._canvas.pack(pady=(0, 6))
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

    # --- B4: waveform ---
    def _draw_waveform(self, active: bool) -> None:
        c = self._canvas
        c.delete("bar")
        x = 2
        for lvl in self._levels:
            h = max(2, int(min(lvl, 1.0) * BAR_MAX_H)) if active else 2
            y0 = (CANVAS_H - h) // 2
            y1 = y0 + h
            c.create_rectangle(
                x, y0, x + BAR_WIDTH, y1,
                fill=self._current_color if active else "#333333",
                outline="", tags="bar")
            x += BAR_WIDTH + BAR_GAP

    def _drain_levels(self) -> bool:
        moved = False
        try:
            while True:
                lvl = self._level_q.get_nowait()
                self._levels.pop(0)
                self._levels.append(lvl * LEVEL_GAIN)
                moved = True
        except queue.Empty:
            pass
        return moved

    # --- B4: smooth transitions ---
    def _animate_color(self, target: str) -> None:
        start = self._current_color

        def step(i: int = 0) -> None:
            t = i / COLOR_STEPS
            color = _lerp_hex(start, target, t)
            self._current_color = color
            try:
                self._label.config(fg=color)
            except Exception:
                return
            if self._state == "listening":
                self._draw_waveform(active=True)
            if i < COLOR_STEPS:
                self._root.after(COLOR_MS, step, i + 1)

        step(0)

    def _fade_alpha(self, target: float, on_done=None) -> None:
        try:
            current = self._root.attributes("-alpha")
        except Exception:
            current = target
        steps = FADE_STEPS

        def step(i: int = 0) -> None:
            t = i / steps
            a = current + (target - current) * t
            try:
                self._root.attributes("-alpha", a)
            except Exception:
                pass
            if i < steps:
                self._root.after(FADE_MS, step, i + 1)
            elif on_done:
                on_done()

        step(0)

    def _show(self) -> None:
        if not self._visible:
            self._visible = True
            self._root.deiconify()
            self._fade_alpha(PILL_ALPHA)

    def _hide_now(self) -> None:
        self._visible = False
        self._fade_alpha(0.0, on_done=self._root.withdraw)

    def _poll(self) -> None:
        leveled = self._drain_levels()
        try:
            while True:
                state, detail = self._q.get_nowait()
                if state == "hide":
                    self._state = "idle"
                    self._hide_now()
                    continue
                color, text = COLORS.get(state, ("#888888", state))
                if detail:
                    text = f"{text} · {detail}"
                self._label.config(text=text)
                changed_state = state != self._state
                self._state = state
                self._place()
                self._show()
                if changed_state:
                    self._animate_color(color)
                else:
                    self._current_color = color
                    self._label.config(fg=color)
                if state != "listening":
                    self._draw_waveform(active=False)
                if state == "pasted":
                    self._root.after(900, self._hide_now)
        except queue.Empty:
            pass
        if leveled and self._state == "listening":
            self._draw_waveform(active=True)
        self._root.after(80, self._poll)
