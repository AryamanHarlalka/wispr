"""F6 — on-screen feedback pill (Tkinter, threaded) + state file for the
menu-bar companion. Five states: listening / transcribing / cleaning /
pasted / error. The v2 lesson: invisible feedback reads as broken — the
pill sits bottom-center, always-on-top, and every state looks different.

B6 (visual rework): the pill is now a Wispr-Flow-style stadium capsule
drawn on a single canvas. Design target observed from Wispr's *public*
homepage hero animation (wisprflow.ai): a capsule pill whose recording
state is a row of ~20 thin, round-capped bars where quiet bars collapse
into dots (min height == bar width), heights easing smoothly instead of
snapping. Values here are our own; only the look was matched by eye.

Rendering details:
- The capsule body is one `create_line` with `capstyle=round` and
  `width=pill_height` — a perfect stadium with no seams.
- On macOS aqua Tk the window is made transparent
  (`-transparent` + `systemTransparent` bg) so only the capsule shows;
  where that isn't available we fall back to a solid near-black window
  (square corners, dark-on-dark, still presentable).
- Bars are round-capped `create_line`s too, so a zero-height bar is a
  dot — the signature Wispr idle look.
- A 33 ms frame loop eases displayed bar heights toward their targets
  (fast attack, slow release) and eases the pill's width when the
  content changes, so state changes morph instead of jumping.
- transcribing/cleaning show a low-amplitude traveling shimmer across
  the bars: "thinking", visibly different from live audio.

The fragile machinery is untouched from the three rounds of debugging
that got it working: the accessory-app activation policy
(`_make_accessory_app`), Tk owning the process main thread, and the
queue-based producer/consumer between daemon threads and the Tk loop.
"""
from __future__ import annotations

import json
import math
import queue
import threading
import time

from .config import MURMUR_HOME, ensure_home

STATE_FILE = MURMUR_HOME / "state.json"

# --- palette (ours; Apple-system-adjacent accents) ---
PILL_BG = "#161616"
TEXT_FG = "#d9d7d0"
COLORS = {
    "listening": ("#f5f3ec", "listening"),
    "transcribing": ("#ffbd2e", "transcribing"),
    "cleaning": ("#5ac8fa", "cleaning"),
    "pasted": ("#32d74b", "pasted"),
    "error": ("#ff5f57", "error"),
}

# --- capsule + waveform geometry (B6) ---
BAR_COUNT = 20
BAR_W = 3            # bar thickness; also the "dot" diameter at rest
BAR_PERIOD = 6       # horizontal distance between bar centers
BAR_MAX_H = 20       # tallest bar (peaks stay inside the capsule)
PILL_H = 32
PAD_X = 15           # capsule end-cap padding before first/last content
WAVE_W = (BAR_COUNT - 1) * BAR_PERIOD
FONT = ("Helvetica Neue", 12)
BOTTOM_MARGIN = 118  # px above the bottom screen edge (clears the Dock)

# Recorder.level is a raw abs-mean of float32 samples — typically small
# (quiet speech ~0.01-0.05). This gain just maps that range onto 0..1 for
# bar height; it's a cosmetic scale, not a calibrated meter.
LEVEL_GAIN = 22.0

# --- animation timing ---
FRAME_MS = 33         # ~30 fps render/ease loop
ATTACK = 0.55         # per-frame easing toward a *louder* target
RELEASE = 0.22        # per-frame easing toward a *quieter* target
WIDTH_EASE = 0.35     # per-frame easing of pill width changes
FADE_STEPS = 5
FADE_MS = 16          # ~80 ms full fade
COLOR_STEPS = 6
COLOR_MS = 18         # ~108 ms color cross-fade between states
PILL_ALPHA = 0.94
SHIMMER_SPEED = 5.2   # rad/s of the traveling "thinking" wave
SHIMMER_AMP = 5.0     # px amplitude of that wave


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
        self._targets: list[float] = [0.0] * BAR_COUNT   # raw scrolled levels
        self._disp: list[float] = [0.0] * BAR_COUNT      # eased on-screen px
        self._root = None
        self._state = "idle"
        self._text = ""
        self._current_color = "#888888"
        self._visible = False
        self._transparent = False
        self._disp_w = 0.0        # eased pill width
        self._placed_geo = ""     # last geometry string actually applied
        self._t0 = time.time()
        self._last_change = 0.0   # keeps drawing briefly after state changes
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
        """Feed a Recorder.level sample in; scrolled into the waveform on
        the next frame while state == 'listening'. Cheap producer side (one
        float queued); all drawing happens on the Tk thread in _frame()."""
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
        # macOS aqua Tk: fully transparent window background so only the
        # capsule we draw is visible (true rounded corners). Falls back to
        # a solid near-black window elsewhere.
        canvas_bg = PILL_BG
        try:
            self._root.attributes("-transparent", True)
            self._root.config(bg="systemTransparent")
            canvas_bg = "systemTransparent"
            self._transparent = True
        except Exception:
            self._transparent = False
        self._canvas = tk.Canvas(
            self._root, width=self._pill_w(), height=PILL_H + 2,
            bg=canvas_bg, highlightthickness=0)
        self._canvas.pack()
        try:
            import tkinter.font as tkfont
            self._font = tkfont.Font(family=FONT[0], size=FONT[1])
        except Exception:
            self._font = None
        self._make_accessory_app()
        self._disp_w = float(self._pill_w())
        self._frame()
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

    # --- geometry ---
    def _text_w(self) -> int:
        if not self._text:
            return 0
        if self._font is not None:
            return self._font.measure(self._text)
        return 7 * len(self._text)

    def _pill_w(self) -> int:
        """Target capsule width for the current content: waveform while
        listening/working, plus text when there is any."""
        w = PAD_X * 2
        show_wave = self._state in ("listening", "transcribing", "cleaning")
        if show_wave or self._state == "idle":
            w += WAVE_W
        tw = self._text_w()
        if tw:
            w += tw + (12 if show_wave else 0)
        return max(w, PILL_H + 2)

    def _place(self, w: int) -> None:
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        geo = f"{w}x{PILL_H + 2}+{(sw - w) // 2}+{sh - BOTTOM_MARGIN}"
        if geo != self._placed_geo:
            self._root.geometry(geo)
            self._canvas.config(width=w, height=PILL_H + 2)
            self._placed_geo = geo

    # --- level intake ---
    def _drain_levels(self) -> None:
        try:
            while True:
                lvl = self._level_q.get_nowait()
                self._targets.pop(0)
                self._targets.append(min(lvl * LEVEL_GAIN, 1.0))
        except queue.Empty:
            pass

    # --- drawing ---
    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")
        w = int(round(self._disp_w))
        cy = (PILL_H + 2) // 2
        r = PILL_H // 2
        # capsule body: one round-capped line = a seamless stadium
        c.create_line(r + 1, cy, w - r - 1, cy,
                      width=PILL_H, capstyle="round", fill=PILL_BG)

        show_wave = self._state in ("listening", "transcribing", "cleaning")
        x = PAD_X + 1
        if show_wave:
            if self._state == "listening":
                color = self._current_color
                heights = self._disp
            else:
                # "thinking" shimmer: low traveling wave in a dimmed accent
                color = _lerp_hex(PILL_BG, self._current_color, 0.75)
                t = (time.time() - self._t0) * SHIMMER_SPEED
                heights = [SHIMMER_AMP * (0.5 + 0.5 * math.sin(t - i * 0.55))
                           for i in range(BAR_COUNT)]
            for h in heights:
                half = max(0.0, min(h, BAR_MAX_H)) / 2.0
                # round caps make a zero-length line a BAR_W dot
                c.create_line(x, cy - half, x, cy + half,
                              width=BAR_W, capstyle="round", fill=color)
                x += BAR_PERIOD
            x += 12 - BAR_PERIOD  # gap between last bar center and text
        if self._text:
            c.create_text(x if show_wave else PAD_X + 1, cy,
                          text=self._text, anchor="w", font=FONT,
                          fill=self._current_color
                          if self._state in ("pasted", "error") else TEXT_FG)

    # --- smooth transitions ---
    def _animate_color(self, target: str) -> None:
        start = self._current_color

        def step(i: int = 0) -> None:
            t = i / COLOR_STEPS
            self._current_color = _lerp_hex(start, target, t)
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

    # --- frame loop ---
    def _frame(self) -> None:
        try:
            while True:
                state, detail = self._q.get_nowait()
                if state == "hide":
                    self._state = "idle"
                    self._hide_now()
                    continue
                color, text = COLORS.get(state, ("#888888", state))
                if state == "listening" and not detail:
                    text = ""  # Wispr look: recording is waveform-only
                elif detail:
                    text = f"{text} · {detail}"
                self._text = text
                changed_state = state != self._state
                self._state = state
                self._last_change = time.time()
                if state == "listening" and changed_state:
                    self._targets = [0.0] * BAR_COUNT
                    self._disp = [0.0] * BAR_COUNT
                self._show()
                if changed_state:
                    self._animate_color(color)
                else:
                    self._current_color = color
                if state == "pasted":
                    self._root.after(900, self._hide_now)
        except queue.Empty:
            pass

        if self._visible:
            self._drain_levels()
            if self._state == "listening":
                # neighbor-smoothed targets + attack/release easing
                t = self._targets
                for i in range(BAR_COUNT):
                    left = t[i - 1] if i > 0 else t[i]
                    right = t[i + 1] if i < BAR_COUNT - 1 else t[i]
                    tgt = (0.25 * left + 0.5 * t[i] + 0.25 * right) * BAR_MAX_H
                    k = ATTACK if tgt > self._disp[i] else RELEASE
                    self._disp[i] += (tgt - self._disp[i]) * k
            tw = float(self._pill_w())
            settled = abs(tw - self._disp_w) < 1.0
            self._disp_w = tw if settled else \
                self._disp_w + (tw - self._disp_w) * WIDTH_EASE
            # static states (pasted/error) don't need a 30 fps redraw once
            # the width morph and the ~short transition window are done
            animating = (self._state in
                         ("listening", "transcribing", "cleaning")
                         or not settled
                         or time.time() - self._last_change < 0.5)
            if animating:
                self._place(int(round(self._disp_w)))
                self._draw()
        self._root.after(FRAME_MS, self._frame)
