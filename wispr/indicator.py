"""On-screen feedback pill + state file for the menu-bar companion.

States: listening / transcribing / cleaning / pasted / error. Invisible
feedback reads as broken, so the pill sits bottom-center, always on top,
and every state looks different.

2026-09-06: rewritten on AppKit (NSPanel + a custom NSView), replacing the
Tk implementation. Tk had two problems that could not be fixed from the
outside:

  * A Tk toplevel belongs to one Space. When the app being dictated into
    was full-screen (its own Space), showing the pill either did not
    appear at all or yanked macOS back to the desktop mid-sentence. Tk
    re-applies its own window attributes on every map, which is why
    setting the collection behaviour after `deiconify` was a coin toss.
  * Tk's window could still become key, so Wispr itself was sometimes the
    frontmost app when the next hotkey press arrived, and the paste went
    to Wispr instead of the user's app.

The panel here is a non-activating NSPanel that declares to Cocoa:
  CanJoinAllSpaces     -- exists in every Space, never forces a switch;
  FullScreenAuxiliary  -- allowed to float over a full-screen app;
  Stationary           -- does not slide with Spaces animations;
  IgnoresCycle         -- never part of Cmd-` / app switching.
It cannot become key or main, ignores the mouse, and the process runs
with the Accessory activation policy, so it never takes focus from the
app the user is dictating into.

Look: a stadium capsule with a row of thin round-capped bars whose quiet
bars collapse into dots (the idle look), heights easing smoothly instead
of snapping; transcribing/cleaning show a low travelling shimmer.

Threading contract (unchanged from the Tk version): AppKit owns the
process main thread (`run()` blocks on NSApplication.run). The daemon's
heavy work runs on a background thread started via `start_background`;
state updates flow through queues drained by an NSTimer on the main loop.
"""
from __future__ import annotations

import json
import math
import os
import queue
import signal
import threading
import time

from .config import WISPR_HOME, ensure_home

STATE_FILE = WISPR_HOME / "state.json"

# --- palette ---
PILL_BG = (0x16, 0x16, 0x16)
TEXT_FG = (0xd9, 0xd7, 0xd0)
COLORS = {
    "listening": ((0xf5, 0xf3, 0xec), "listening"),
    "transcribing": ((0xff, 0xbd, 0x2e), "transcribing"),
    "cleaning": ((0x5a, 0xc8, 0xfa), "cleaning"),
    "pasted": ((0x32, 0xd7, 0x4b), "pasted"),
    "error": ((0xff, 0x5f, 0x57), "error"),
}
IDLE_COLOR = (0x88, 0x88, 0x88)

# --- capsule + waveform geometry ---
BAR_COUNT = 20
BAR_W = 3.0          # bar thickness; also the "dot" diameter at rest
BAR_PERIOD = 6.0     # horizontal distance between bar centers
BAR_MAX_H = 20.0     # tallest bar (peaks stay inside the capsule)
PILL_H = 32.0
PAD_X = 15.0         # capsule end-cap padding before first/last content
WAVE_W = (BAR_COUNT - 1) * BAR_PERIOD
TEXT_GAP = 12.0
FONT_SIZE = 12.5
BOTTOM_MARGIN = 26.0  # px above the visible screen area (Dock excluded)
SHADOW = True

# Recorder.level is a raw abs-mean of float32 samples (quiet speech
# ~0.01-0.05); this gain maps it onto 0..1 for bar height.
LEVEL_GAIN = 22.0

# --- animation timing ---
FRAME_S = 1.0 / 30.0
ATTACK = 0.55         # per-frame easing toward a *louder* target
RELEASE = 0.22        # per-frame easing toward a *quieter* target
WIDTH_EASE = 0.35     # per-frame easing of pill width changes
ALPHA_EASE = 0.45     # per-frame easing of window alpha
COLOR_EASE = 0.35     # per-frame easing of the accent colour
PILL_ALPHA = 0.94
SHIMMER_SPEED = 5.2   # rad/s of the travelling "thinking" wave
SHIMMER_AMP = 5.0     # px amplitude of that wave
PASTED_HOLD_S = 0.9   # how long "pasted" stays up before fading


def write_state(state: str, detail: str = "") -> None:
    ensure_home()
    try:
        STATE_FILE.write_text(json.dumps(
            {"state": state, "detail": detail, "ts": time.time()}))
    except OSError:
        pass


def _lerp_rgb(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


_CLASSES = None


def _objc_classes():
    """The Objective-C subclasses, registered with the runtime exactly
    once per process (pyobjc raises on a second registration by name)."""
    global _CLASSES
    if _CLASSES is not None:
        return _CLASSES
    from AppKit import NSPanel, NSView
    from Foundation import NSObject

    class WisprPillPanel(NSPanel):
        def canBecomeKeyWindow(self):  # noqa: N802
            return False

        def canBecomeMainWindow(self):  # noqa: N802
            return False

    class WisprPillView(NSView):
        owner = None

        def isFlipped(self):  # noqa: N802
            return True

        def drawRect_(self, rect):  # noqa: N802
            ind = self.owner
            if ind is None:
                return
            try:
                ind._draw(self)
            except Exception as e:
                print(f"[wispr] indicator draw error: "
                      f"{e.__class__.__name__}: {e}", flush=True)

    class WisprPillDriver(NSObject):
        owner = None

        def tick_(self, _timer):  # noqa: N802
            ind = self.owner
            if ind is None:
                return
            try:
                ind._frame()
            except Exception as e:
                print(f"[wispr] indicator frame error: "
                      f"{e.__class__.__name__}: {e}", flush=True)

    _CLASSES = (WisprPillPanel, WisprPillView, WisprPillDriver)
    return _CLASSES


class Indicator:
    """Owns the AppKit run loop. `run()` must be called on the process main
    thread; everything else may be called from any thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._level_q: "queue.Queue[float]" = queue.Queue()
        self._targets: list[float] = [0.0] * BAR_COUNT
        self._disp: list[float] = [0.0] * BAR_COUNT
        self._state = "idle"
        self._text = ""
        self._color = IDLE_COLOR
        self._color_target = IDLE_COLOR
        self._visible = False
        self._alpha = 0.0
        self._alpha_target = 0.0
        self._disp_w = 0.0
        self._shadow_w = -1.0
        self._t0 = time.time()
        self._hide_at = 0.0
        self._panel = None
        self._view = None
        self._font = None
        self._text_w_cache: dict[str, float] = {}
        self._headless = False

    # ---- producer side (any thread) ----
    def set(self, state: str, detail: str = "") -> None:
        write_state(state, detail)
        if not self._headless:
            self._q.put((state, detail))

    def hide(self) -> None:
        write_state("idle")
        self._q.put(("hide", ""))

    def push_level(self, level: float) -> None:
        if self._headless:
            return  # nobody drains the queue without a run loop
        self._level_q.put(level)

    def start_background(self, target, *args, **kwargs) -> None:
        threading.Thread(target=target, args=args, kwargs=kwargs,
                         daemon=True).start()

    # ---- main thread ----
    def run(self) -> None:
        """Build the panel and block on the AppKit run loop. Falls back to
        a headless idle loop (state file only) where AppKit is missing, so
        the daemon still works on a machine without pyobjc."""
        try:
            self._build()
        except Exception as e:  # pragma: no cover - only off-macOS
            print(f"[wispr] indicator: AppKit unavailable ({e.__class__.__name__}: "
                  f"{e}); running without the on-screen pill", flush=True)
            self._headless = True
            while True:
                time.sleep(3600)
        # NSApplication.run swallows nothing, but Python only sees SIGINT
        # when bytecode runs; the 30 fps timer guarantees that, so Ctrl-C
        # in a terminal still quits promptly.
        try:
            signal.signal(signal.SIGINT, lambda *_: os._exit(0))
            signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        except Exception:
            pass
        self._app.run()

    def _build(self) -> None:
        from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory,
                            NSBackingStoreBuffered, NSColor, NSFont, NSMakeRect,
                            NSTimer)
        from Foundation import NSRunLoop, NSRunLoopCommonModes

        _PillPanel, _PillView, _Driver = _objc_classes()
        self._NSColor = NSColor
        self._NSMakeRect = NSMakeRect
        self._app = NSApplication.sharedApplication()
        # Accessory: no Dock icon, never the frontmost app. This is what
        # stops the pill from stealing focus from the app being dictated
        # into (the "org.python.python was frontmost" bug of old).
        self._app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        BORDERLESS = 0
        NONACTIVATING_PANEL = 1 << 7
        w = self._pill_w()
        frame = NSMakeRect(0, 0, w, PILL_H + 2)
        panel = _PillPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, BORDERLESS | NONACTIVATING_PANEL, NSBackingStoreBuffered,
            False)
        # Literal masks rather than the named constants: the names moved
        # between pyobjc releases, the bit values have not.
        CAN_JOIN_ALL_SPACES = 1 << 0
        STATIONARY = 1 << 4
        IGNORES_CYCLE = 1 << 6
        FULLSCREEN_AUXILIARY = 1 << 8
        panel.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | STATIONARY
                                     | IGNORES_CYCLE | FULLSCREEN_AUXILIARY)
        # Pop-up-menu level: above every ordinary window, including a
        # full-screen app's, and above the menu bar overlay.
        POPUP_MENU_LEVEL = 101
        panel.setLevel_(POPUP_MENU_LEVEL)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(SHADOW)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setMovableByWindowBackground_(False)
        panel.setAlphaValue_(0.0)
        try:
            panel.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorNone
        except Exception:
            pass
        view = _PillView.alloc().initWithFrame_(frame)
        view.owner = self
        panel.setContentView_(view)
        self._panel = panel
        self._view = view
        self._font = NSFont.systemFontOfSize_weight_(FONT_SIZE, 0.23)  # medium
        self._disp_w = float(w)
        self._place(w)

        self._driver = _Driver.alloc().init()
        self._driver.owner = self
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            FRAME_S, self._driver, "tick:", None, True)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timer = timer

    # ---- geometry ----
    def _text_w(self) -> float:
        if not self._text:
            return 0.0
        cached = self._text_w_cache.get(self._text)
        if cached is not None:
            return cached
        try:
            from AppKit import NSAttributedString, NSFontAttributeName
            s = NSAttributedString.alloc().initWithString_attributes_(
                self._text, {NSFontAttributeName: self._font})
            w = float(s.size().width)
        except Exception:
            w = 7.0 * len(self._text)
        if len(self._text_w_cache) > 64:
            self._text_w_cache.clear()
        self._text_w_cache[self._text] = w
        return w

    def _show_wave(self) -> bool:
        return self._state in ("listening", "transcribing", "cleaning")

    def _pill_w(self) -> float:
        if self._state == "idle" and self._disp_w > 0:
            return self._disp_w  # fading out: keep the shape it had
        w = PAD_X * 2
        show_wave = self._show_wave()
        if show_wave or self._state == "idle":
            w += WAVE_W
        tw = self._text_w()
        if tw:
            w += tw + (TEXT_GAP if show_wave else 0)
        return max(w, PILL_H + 2)

    def _screen(self):
        """The screen the pill belongs on: the one under the mouse, else
        the main screen. On a laptop this is the built-in display."""
        from AppKit import NSEvent, NSScreen
        try:
            p = NSEvent.mouseLocation()
            for s in NSScreen.screens():
                f = s.frame()
                if (f.origin.x <= p.x <= f.origin.x + f.size.width
                        and f.origin.y <= p.y <= f.origin.y + f.size.height):
                    return s
        except Exception:
            pass
        return NSScreen.mainScreen() or NSScreen.screens()[0]

    def _place(self, w: float) -> None:
        if self._panel is None:
            return
        screen = self._screen()
        vis = screen.visibleFrame()  # excludes the Dock and menu bar
        h = PILL_H + 2
        x = vis.origin.x + (vis.size.width - w) / 2.0
        y = vis.origin.y + BOTTOM_MARGIN
        cur = self._panel.frame()
        if (abs(cur.origin.x - x) > 0.5 or abs(cur.origin.y - y) > 0.5
                or abs(cur.size.width - w) > 0.5):
            self._panel.setFrame_display_(self._NSMakeRect(x, y, w, h), False)

    # ---- level intake ----
    def _drain_levels(self) -> None:
        try:
            while True:
                lvl = self._level_q.get_nowait()
                self._targets.pop(0)
                self._targets.append(min(lvl * LEVEL_GAIN, 1.0))
        except queue.Empty:
            pass

    # ---- drawing (main thread, inside drawRect) ----
    def _rgb(self, c, alpha: float = 1.0):
        return self._NSColor.colorWithCalibratedRed_green_blue_alpha_(
            c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, alpha)

    def _draw(self, view) -> None:
        from AppKit import (NSAttributedString, NSBezierPath,
                            NSFontAttributeName, NSForegroundColorAttributeName,
                            NSMakePoint)
        bounds = view.bounds()
        w = float(bounds.size.width)
        cy = (PILL_H + 2) / 2.0
        r = PILL_H / 2.0
        body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self._NSMakeRect(0.5, 1.0, w - 1.0, PILL_H), r, r)
        self._rgb(PILL_BG).setFill()
        body.fill()

        show_wave = self._show_wave()
        x = PAD_X + 1.0
        if show_wave:
            if self._state == "listening":
                color = self._color
                heights = self._disp
            else:
                color = _lerp_rgb(PILL_BG, self._color, 0.75)
                t = (time.time() - self._t0) * SHIMMER_SPEED
                heights = [SHIMMER_AMP * (0.5 + 0.5 * math.sin(t - i * 0.55))
                           for i in range(BAR_COUNT)]
            self._rgb(color).setFill()
            for hgt in heights:
                h = max(BAR_W, min(hgt, BAR_MAX_H))
                bar = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    self._NSMakeRect(x - BAR_W / 2.0, cy - h / 2.0, BAR_W, h),
                    BAR_W / 2.0, BAR_W / 2.0)
                bar.fill()
                x += BAR_PERIOD
            x += TEXT_GAP - BAR_PERIOD
        if self._text:
            fg = self._color if self._state in ("pasted", "error") else TEXT_FG
            attrs = {NSFontAttributeName: self._font,
                     NSForegroundColorAttributeName: self._rgb(fg)}
            s = NSAttributedString.alloc().initWithString_attributes_(
                self._text, attrs)
            size = s.size()
            tx = x if show_wave else PAD_X + 1.0
            s.drawAtPoint_(NSMakePoint(tx, cy - size.height / 2.0))

    # ---- frame loop (main thread, NSTimer) ----
    def _frame(self) -> None:
        now = time.time()
        try:
            while True:
                state, detail = self._q.get_nowait()
                if state == "hide":
                    self._state = "idle"
                    self._visible = False
                    self._alpha_target = 0.0
                    self._hide_at = 0.0
                    continue
                color, text = COLORS.get(state, (IDLE_COLOR, state))
                if state == "listening" and not detail:
                    text = ""  # recording is waveform-only
                elif detail:
                    text = f"{text} · {detail}"
                self._text = text
                changed = state != self._state
                self._state = state
                if state == "listening" and changed:
                    self._targets = [0.0] * BAR_COUNT
                    self._disp = [0.0] * BAR_COUNT
                self._color_target = color
                if changed and not self._visible:
                    self._color = color  # first show: no cross-fade from grey
                self._visible = True
                self._alpha_target = PILL_ALPHA
                self._hide_at = now + PASTED_HOLD_S if state == "pasted" else 0.0
        except queue.Empty:
            pass

        if self._hide_at and now >= self._hide_at:
            self._hide_at = 0.0
            self._visible = False
            self._alpha_target = 0.0
            self._state = "idle"

        # Nothing on screen and nothing fading: skip the work entirely.
        if not self._visible and self._alpha < 0.005:
            if self._panel is not None and self._panel.isVisible():
                self._panel.setAlphaValue_(0.0)
                self._panel.orderOut_(None)
            return

        self._drain_levels()
        if self._state == "listening":
            t = self._targets
            for i in range(BAR_COUNT):
                left = t[i - 1] if i > 0 else t[i]
                right = t[i + 1] if i < BAR_COUNT - 1 else t[i]
                tgt = (0.25 * left + 0.5 * t[i] + 0.25 * right) * BAR_MAX_H
                k = ATTACK if tgt > self._disp[i] else RELEASE
                self._disp[i] += (tgt - self._disp[i]) * k

        self._color = _lerp_rgb(self._color, self._color_target, COLOR_EASE)
        tw = self._pill_w()
        if abs(tw - self._disp_w) < 0.75:
            self._disp_w = tw
        else:
            self._disp_w += (tw - self._disp_w) * WIDTH_EASE
        if abs(self._alpha_target - self._alpha) < 0.01:
            self._alpha = self._alpha_target
        else:
            self._alpha += (self._alpha_target - self._alpha) * ALPHA_EASE

        if self._panel is not None:
            self._place(self._disp_w)
            if not self._panel.isVisible():
                # orderFrontRegardless: shows the panel without activating
                # the app -- the whole point of the accessory policy.
                self._panel.orderFrontRegardless()
            self._panel.setAlphaValue_(self._alpha)
            self._view.setNeedsDisplay_(True)
            if abs(self._disp_w - self._shadow_w) > 0.5:
                # The capsule outline changed; the window shadow is cached
                # from the last draw and would show as a stale halo.
                self._shadow_w = self._disp_w
                try:
                    self._panel.invalidateShadow()
                except Exception:
                    pass
