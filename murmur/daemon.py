"""Murmur v3 daemon — hold Right Option anywhere, speak, get cleaned text
pasted at the cursor. Double-tap Right Option toggles hands-free mode.

Pipeline: record (sounddevice) -> transcribe (faster-whisper small.en,
vault-vocab initial_prompt) -> clean (snippets -> rules -> Haiku w/ 1200 ms
budget -> rules fallback) -> paste (clipboard-preserving) -> history.
Audio never leaves the Mac; only cleaned *text* may go to the API (F1).
"""
from __future__ import annotations

import subprocess
import threading
import time

import numpy as np

from . import cleanup, history, modes as modes_mod, vocab as vocab_mod
from .config import (MURMUR_HOME, ensure_home, load_corrections, load_modes,
                     load_snippets, load_vocab, whisper_model)
from .indicator import Indicator

SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.4
# small.en unless overridden — see config.whisper_model() (B2). distil-small.en
# is wired as an opt-in knob; left off by default since we can't cheaply
# verify it doesn't regress accuracy on the vault vocab in this session.
MODEL_NAME = whisper_model()


class Recorder:
    def __init__(self) -> None:
        import sounddevice as sd
        self._sd = sd
        self._frames: list[np.ndarray] = []
        self._stream = None
        self.level = 0.0

    def start(self) -> None:
        self._frames = []

        def cb(indata, frames, t, status):  # noqa: ANN001
            self._frames.append(indata.copy())
            self.level = float(np.abs(indata).mean())

        self._stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=cb)
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._frames).flatten()


def _pasteboard_read() -> str | None:
    """Current pasteboard text (None if empty/non-text) via NSPasteboard —
    no pbpaste subprocess (B2). Text-only, same coverage pbpaste had."""
    from AppKit import NSPasteboard, NSPasteboardTypeString
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)


def _pasteboard_write(text: str) -> None:
    from AppKit import NSPasteboard, NSPasteboardTypeString
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def _send_cmd_v() -> None:
    """Synthetic Cmd-V via Quartz CGEventPost (B2) — replaces the ~150 ms
    osascript round-trip. Quartz is already installed as a pynput dep.
    Falls back to osascript if Quartz is unavailable, keeping the old
    error reporting (osascript stderr surfaces missing Accessibility)."""
    try:
        import Quartz
        kVK_ANSI_V = 9
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        down = Quartz.CGEventCreateKeyboardEvent(src, kVK_ANSI_V, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, kVK_ANSI_V, False)
        Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        return
    except Exception:
        pass
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to keystroke "v" using command down'],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"paste failed (grant Accessibility to Terminal/python in "
            f"System Settings > Privacy & Security): {r.stderr.strip()[:120]}")


def paste_text(text: str, target_app=None) -> None:
    """Copy -> reactivate original target app -> Cmd-V -> restore prior
    clipboard (table stakes, spec §1).

    `target_app` is the NSRunningApplication captured the instant the hotkey
    was pressed (see Daemon._start). Murmur's own pill activates itself the
    moment it's shown, so by the time we're ready to paste, the real
    frontmost app is almost always Murmur, not whatever the user was
    dictating into — every history.jsonl entry logged "org.python.python".
    Explicitly re-activating the captured app right before sending Cmd-V is
    what actually fixes where the paste lands, regardless of what stole
    focus in between. That capture-and-reactivate sequence MUST stay ahead
    of the keystroke — do not reorder.

    B2: fixed sleeps replaced by polling frontmost — with the accessory-app
    policy the target usually never lost focus, so the wait is ~0 instead of
    a hardcoded 230 ms; when focus did move we wait only as long as the
    activation actually takes (cap 300 ms).

    B5: the prior clipboard is restored only after a successful keystroke.
    If anything here raises, the cleaned text is already ON the clipboard,
    so a manual Cmd-V recovers the dictation.
    """
    prior = _pasteboard_read()
    _pasteboard_write(text)
    if target_app is not None:
        try:
            from AppKit import (NSApplicationActivateIgnoringOtherApps,
                                NSWorkspace)
            target_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            ws = NSWorkspace.sharedWorkspace()
            deadline = time.time() + 0.3
            while time.time() < deadline:
                front = ws.frontmostApplication()
                if front is not None and \
                        front.processIdentifier() == target_app.processIdentifier():
                    break
                time.sleep(0.02)
            else:
                time.sleep(0.05)  # never confirmed; small settle beat
        except Exception:
            pass
    _send_cmd_v()

    if prior is None:
        return  # nothing to restore (empty or non-text clipboard)

    def restore() -> None:
        time.sleep(0.6)  # let the target app consume the paste first
        try:
            _pasteboard_write(prior)
        except Exception:
            pass

    threading.Thread(target=restore, daemon=True).start()


class Daemon:
    def __init__(self, indicator: Indicator | None = None) -> None:
        ensure_home()
        self.modes = load_modes()
        self.snippets = load_snippets()
        self.corrections = load_corrections()
        self.vocab = load_vocab()
        if not self.vocab:
            try:
                vocab_mod.write_vocab()
                self.vocab = load_vocab()
            except Exception:
                self.vocab = list(vocab_mod.SEED_TERMS)
        # Indicator must own the process main thread (Tk/Cocoa requirement).
        # main() creates it and calls .run() on the main thread; the daemon
        # itself (model load, hotkey listener) is handed in already-built or
        # runs headless if constructed standalone (e.g. tests).
        self.indicator = indicator if indicator is not None else Indicator()
        self.recorder = Recorder()
        self.recording = False
        self.hands_free = False
        self._last_release = 0.0
        self._lock = threading.Lock()
        self._target_app = None  # captured at _start(), before the pill shows

        # Warm the Anthropic client (keychain read + TLS) off the critical
        # path so the first real dictation doesn't pay it (B1).
        cleanup.warm_client()

        print(f"[murmur] loading {MODEL_NAME} …", flush=True)
        from faster_whisper import WhisperModel
        self.model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        print(f"[murmur] ready — hold Right Option to dictate "
              f"(vocab: {len(self.vocab)} terms, "
              f"llm: {'on' if cleanup._get_client() else 'off — rules only'})",
              flush=True)

    # --- capture ---
    def _start(self) -> None:
        with self._lock:
            if self.recording:
                return
            self.recording = True
        # Capture the real target app BEFORE the pill shows and steals
        # frontmost status — this is the fix. Querying frontmost after
        # recording/processing always returns Murmur itself.
        self._target_app = modes_mod.frontmost_app()
        # reload lightweight config every dictation so edits stick live
        self.snippets = load_snippets()
        self.corrections = load_corrections()
        self.indicator.set("listening",
                           "hands-free" if self.hands_free else "")
        # Open the TLS connection while the user is still speaking so the
        # Haiku call after key-release starts on a warm connection (B1).
        cleanup.prewarm_connection()
        self.recorder.start()

    def _stop_and_process(self) -> None:
        with self._lock:
            if not self.recording:
                return
            self.recording = False
        audio = self.recorder.stop()
        if audio.size < SAMPLE_RATE * 0.3:  # <0.3 s: accidental tap
            self.indicator.hide()
            return
        target_app = self._target_app
        threading.Thread(target=self._process, args=(audio, target_app),
                          daemon=True).start()

    def _process(self, audio: np.ndarray, target_app=None) -> None:
        t0 = time.time()
        bundle = modes_mod.bundle_id_of(target_app)
        mode = modes_mod.mode_for(bundle, self.modes)
        try:
            self.indicator.set("transcribing")
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1,
                initial_prompt=vocab_mod.initial_prompt(
                    self.vocab, self.corrections),
                vad_filter=True,
            )
            raw = " ".join(s.text.strip() for s in segments).strip()
            t_whisper = time.time()
            if not raw:
                self.indicator.set("error", "heard nothing")
                time.sleep(1.2)
                self.indicator.hide()
                return
            self.indicator.set("cleaning")
            cleaned, path = cleanup.clean_text(
                raw, mode, self.vocab, self.corrections, self.snippets)
            t_clean = time.time()
            paste_text(cleaned, target_app)
            t_paste = time.time()
            ms = int((t_paste - t0) * 1000)
            self.indicator.set("pasted", f"{len(cleaned.split())}w · {ms}ms")
            history.append(bundle, mode, raw, cleaned, path, ms, stages={
                "whisper_ms": (t_whisper - t0) * 1000,
                "cleanup_ms": (t_clean - t_whisper) * 1000,
                "paste_ms": (t_paste - t_clean) * 1000,
            })
        except Exception as e:  # error state preserves transcript in history
            self.indicator.set("error", str(e)[:60])
            try:
                history.append(bundle, mode, locals().get("raw", ""), "",
                               "error", int((time.time() - t0) * 1000))
            except Exception:
                pass
            time.sleep(1.5)
            self.indicator.hide()

    # --- hotkey ---
    def run(self) -> None:
        from pynput import keyboard

        def on_press(key):  # noqa: ANN001
            if key == keyboard.Key.alt_r:
                if self.hands_free and self.recording:
                    return  # release handler decides
                self._start()

        def on_release(key):  # noqa: ANN001
            if key != keyboard.Key.alt_r:
                return
            now = time.time()
            double_tap = (now - self._last_release) < DOUBLE_TAP_S
            self._last_release = now
            if double_tap and not self.hands_free:
                # second tap of a double-tap: enter hands-free (keep recording)
                self.hands_free = True
                if not self.recording:
                    self._start()
                else:
                    self.indicator.set("listening", "hands-free")
                return
            if self.hands_free:
                if double_tap:
                    return
                # single press while hands-free: stop
                self.hands_free = False
                self._stop_and_process()
                return
            self._stop_and_process()

        with keyboard.Listener(on_press=on_press, on_release=on_release) as ln:
            print("[murmur] daemon running. Ctrl-C to quit.", flush=True)
            ln.join()


def main() -> None:
    # Indicator owns this (the real process main) thread's runloop — Tk on
    # macOS crashes if its window/mainloop are created off-thread. Everything
    # else — model load (a few seconds) + the daemon itself + the hotkey
    # listener — runs on a background thread so it can't block Tk's loop
    # from coming up; see indicator.py for the full explanation.
    indicator = Indicator()

    def _run_daemon() -> None:
        Daemon(indicator=indicator).run()

    indicator.start_background(_run_daemon)
    indicator.run()
