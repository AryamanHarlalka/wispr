"""Murmur v3 daemon — hold Right Option anywhere, speak, get cleaned text
pasted at the cursor. Double-tap Right Option toggles hands-free mode.

Pipeline: record (sounddevice, chunk-decoded incrementally in the
background — see IncrementalTranscriber, B3) -> transcribe tail only
(faster-whisper, vault-vocab initial_prompt) -> clean (snippets -> rules ->
Haiku w/ 2500 ms budget -> rules fallback) -> paste (clipboard-preserving)
-> history. Audio never leaves the Mac; only cleaned *text* may go to the
API (F1).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import cleanup, history, modes as modes_mod, stt, vocab as vocab_mod
from .config import (MURMUR_HOME, ensure_home, load_corrections, load_modes,
                     load_snippets, load_vocab, stt_backend, stt_beam_size,
                     stt_model)
from .indicator import Indicator

SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.4
# Backend + model are a config swap (0c) -- see config.stt_backend()/
# stt_model() for the current defaults and the benchmark reasoning behind
# them. BEAM_SIZE and CONDITION_ON_PREVIOUS_TEXT are the 0d decode-tuning
# knobs: beam_size=1 trades a little accuracy for materially faster decode
# (measured in `murmur bench`); condition_on_previous_text=False stops the
# runaway-repetition failure mode dictation is prone to (a long pause mid
# -utterance otherwise biases the next segment toward repeating itself).
BACKEND_NAME = stt_backend()
MODEL_NAME = stt_model()
BEAM_SIZE = stt_beam_size()
CONDITION_ON_PREVIOUS_TEXT = False


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

        try:
            self._open_stream(cb)
        except Exception as e:
            # CoreAudio/PortAudio wedges in practice — PaErrorCode -9986
            # ("Internal PortAudio error") and AUHAL -10851 ("Invalid
            # Property Value") both show up after a device change, a
            # sleep/wake cycle, or another process holding the input
            # device. PortAudio's host-API state is process-global, so a
            # plain retry hits the same wedged state; tearing it down and
            # re-initialising usually clears it.
            print(f"[murmur] input stream failed "
                  f"({e.__class__.__name__}: {e}) — reinitialising "
                  f"PortAudio and retrying once", flush=True)
            self._reset_portaudio()
            self._open_stream(cb)  # a second failure propagates to _start()

    def _open_stream(self, cb) -> None:  # noqa: ANN001
        self._stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=cb)
        self._stream.start()

    def _reset_portaudio(self) -> None:
        """Tear down and re-initialise the process-global PortAudio host
        API. Best-effort: never raises, so the retry in start() is what
        decides whether recording actually recovered."""
        try:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self._sd._terminate()
            time.sleep(0.15)
            self._sd._initialize()
        except Exception as e:
            print(f"[murmur] PortAudio reinit failed: "
                  f"{e.__class__.__name__}: {e}", flush=True)

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._frames).flatten()

    def snapshot(self) -> np.ndarray:
        """Everything recorded so far, without stopping the stream (B3) —
        lets the incremental decoder peek at in-progress audio. Safe to call
        from another thread: the sounddevice callback only appends new
        arrays to `_frames`, never mutates existing ones, so `list(...)`
        here can't observe a torn read under the GIL."""
        frames = list(self._frames)
        if not frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(frames).flatten()


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


CHUNK_INTERVAL_S = 3.0   # how often the background worker checks for new audio
MIN_CHUNK_AUDIO_S = 4.0  # don't bother decoding a chunk shorter than this
TRAILING_GUARD_S = 0.6   # don't commit a VAD segment that ends this close to
                         # "now" — it may still be mid-utterance


class IncrementalTranscriber:
    """B3 — decode committed audio in the background while the user is
    still talking, so on key-release only the trailing tail needs
    decoding. This is the riskiest piece of the v3 latency work, so it is
    built to fail safe: every decode call is wrapped, and any error just
    means `finalize()` returns None — the caller (Daemon._process) then
    falls back to the exact whole-utterance decode path that shipped
    before this feature. A dictation can never be lost to a bug here.

    Runs on its own background thread, started at Daemon._start() and
    joined inside finalize(). It never touches the Indicator/Tk mainloop
    and doesn't change how Daemon's own hotkey-listener thread or the
    per-dictation _process thread are structured — it's an independent
    worker that Daemon._process consults once, at the very start.

    `model_lock` is shared with Daemon._process's own transcribe() calls
    so at most one decode ever runs against the shared WhisperModel
    instance at a time — faster-whisper/ctranslate2 concurrent-call safety
    isn't documented, so this sidesteps the question entirely rather than
    relying on it.
    """

    def __init__(self, model, model_lock: threading.Lock,
                 vocab: list[str], corrections: dict[str, str]) -> None:
        self._model = model
        self._model_lock = model_lock
        self._vocab = vocab
        self._corrections = corrections
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._committed_text = ""
        self._committed_samples = 0
        self._failed = False

    def start(self, recorder: "Recorder") -> None:
        self._thread = threading.Thread(
            target=self._run, args=(recorder,), daemon=True)
        self._thread.start()

    def _run(self, recorder: "Recorder") -> None:
        while not self._stop_evt.wait(CHUNK_INTERVAL_S):
            try:
                self._maybe_commit_chunk(recorder)
            except Exception:
                # Defensive: stop trying: finalize() will see _failed and
                # tell the caller to fall back to a plain whole-decode.
                self._failed = True
                return

    def _decode(self, audio: np.ndarray, extra_prompt: str) -> list:
        prompt = vocab_mod.initial_prompt(self._vocab, self._corrections)
        if extra_prompt:
            prompt = f"{extra_prompt[-400:]} {prompt}"
        with self._model_lock:
            segments = self._model.transcribe(
                audio, language="en", beam_size=BEAM_SIZE,
                initial_prompt=prompt, vad_filter=True,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT)
        return segments

    def _maybe_commit_chunk(self, recorder: "Recorder") -> None:
        audio = recorder.snapshot()
        new_len_s = (len(audio) - self._committed_samples) / SAMPLE_RATE
        if new_len_s < MIN_CHUNK_AUDIO_S:
            return
        chunk = audio[self._committed_samples:]
        segments = self._decode(chunk, self._committed_text)
        if not segments:
            return
        # Only commit segments that aren't right at the edge of "now" —
        # they may still be mid-utterance and get revised/continued.
        safe_end_s = new_len_s - TRAILING_GUARD_S
        commit = [s for s in segments if s.end <= safe_end_s]
        if not commit:
            return
        text = " ".join(s.text.strip() for s in commit).strip()
        if not text:
            return
        self._committed_text = f"{self._committed_text} {text}".strip()
        self._committed_samples += int(commit[-1].end * SAMPLE_RATE)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def finalize(self, final_audio: np.ndarray) -> str | None:
        """Stop the worker, decode just the trailing tail, and return the
        full transcript — or None if incremental decoding failed at any
        point, telling the caller to fall back to a plain whole-decode."""
        self.stop()
        if self._failed:
            return None
        try:
            tail = final_audio[self._committed_samples:]
            tail_segments = self._decode(tail, self._committed_text)
            tail_text = " ".join(s.text.strip() for s in tail_segments).strip()
        except Exception:
            return None
        return f"{self._committed_text} {tail_text}".strip()


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
        self._model_lock = threading.Lock()  # guards all model.transcribe() calls
        self._incremental: IncrementalTranscriber | None = None

        # Warm the Anthropic client (keychain read + TLS) off the critical
        # path so the first real dictation doesn't pay it (B1).
        cleanup.warm_client()

        print(f"[murmur] loading {BACKEND_NAME}/{MODEL_NAME} …", flush=True)
        self.model = stt.build_backend(BACKEND_NAME, MODEL_NAME)
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
        try:
            self._begin_capture()
        except Exception as e:
            # A capture failure must never escape into the pynput callback.
            # pynput re-raises callback exceptions out of the listener
            # thread, which ends it: the process stays alive (launchd sees a
            # healthy PID, KeepAlive never fires) but the hotkey is dead
            # until a manual restart. That's the "it just stopped working"
            # failure. Reset state so the *next* press can try again —
            # self.recording was already True above, and leaving it stuck
            # would deadlock every future press at the guard.
            with self._lock:
                self.recording = False
            self._incremental = None
            try:
                self.recorder.stop()
            except Exception:
                pass
            print(f"[murmur] could not start recording: "
                  f"{e.__class__.__name__}: {e}", flush=True)
            self.indicator.set("error", f"mic unavailable — {str(e)[:40]}")

    def _begin_capture(self) -> None:
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
        # B3: start decoding in the background while the user keeps
        # talking, so long (hands-free) dictations only pay for the tail
        # on release instead of the whole recording.
        self._incremental = IncrementalTranscriber(
            self.model, self._model_lock, self.vocab, self.corrections)
        self._incremental.start(self.recorder)
        # B4: feed Recorder.level to the pill's waveform while listening —
        # a small standalone thread (like the incremental worker above),
        # not a restructuring of the existing hotkey/_process threading.
        threading.Thread(target=self._level_feed, daemon=True).start()

    def _level_feed(self) -> None:
        while self.recording:
            self.indicator.push_level(self.recorder.level)
            time.sleep(0.05)

    def _stop_and_process(self) -> None:
        with self._lock:
            if not self.recording:
                return
            self.recording = False
        try:
            audio = self.recorder.stop()
        except Exception as e:
            # Same contract as _start(): closing a wedged stream can raise,
            # and this runs on the pynput callback thread too. Drop the
            # take rather than the listener.
            self._incremental = None
            print(f"[murmur] could not stop recording: "
                  f"{e.__class__.__name__}: {e}", flush=True)
            self.indicator.set("error", f"recording lost — {str(e)[:40]}")
            return
        incremental = self._incremental
        self._incremental = None
        _dump_dir = os.environ.get("MURMUR_DEBUG_DUMP_DIR", "").strip()
        if _dump_dir and audio.size:
            try:
                Path(_dump_dir).mkdir(parents=True, exist_ok=True)
                stt._write_wav(
                    audio, str(Path(_dump_dir) / f"dump_{int(time.time())}.wav"))
            except Exception:
                pass
        if audio.size < SAMPLE_RATE * 0.3:  # <0.3 s: accidental tap
            if incremental is not None:
                incremental.stop()
            self.indicator.hide()
            return
        target_app = self._target_app
        threading.Thread(target=self._process,
                          args=(audio, target_app, incremental),
                          daemon=True).start()

    def _process(self, audio: np.ndarray, target_app=None,
                 incremental: "IncrementalTranscriber | None" = None) -> None:
        t0 = time.time()
        bundle = modes_mod.bundle_id_of(target_app)
        mode = modes_mod.mode_for(bundle, self.modes)
        try:
            self.indicator.set("transcribing")
            raw = None
            if incremental is not None:
                try:
                    raw = incremental.finalize(audio)
                except Exception:
                    raw = None  # defensive: fall through to whole-decode
            if raw is None:
                with self._model_lock:
                    segments = self.model.transcribe(
                        audio, language="en", beam_size=BEAM_SIZE,
                        initial_prompt=vocab_mod.initial_prompt(
                            self.vocab, self.corrections),
                        vad_filter=True,
                        condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                    )
                raw = " ".join(s.text.strip() for s in segments).strip()
            t_whisper = time.time()
            if not raw:
                self.indicator.set("error", "heard nothing")
                time.sleep(1.2)
                self.indicator.hide()
                return
            self.indicator.set("cleaning")
            # B5: cleanup.clean_text() already has its own rules-only
            # fallback ladder and shouldn't raise, but if it somehow does,
            # fall back to the raw transcript rather than losing the
            # dictation entirely — better a slightly messy paste than none.
            try:
                cleaned, path = cleanup.clean_text(
                    raw, mode, self.vocab, self.corrections, self.snippets)
            except Exception as e:
                print(f"[murmur] cleanup raised, pasting raw transcript: "
                      f"{e.__class__.__name__}: {e}", flush=True)
                cleaned, path = raw, "cleanup_error"
            t_clean = time.time()
            # B5: paste_text() writes `cleaned` to the clipboard before it
            # ever attempts the keystroke, so on failure the clipboard
            # already holds the text — no prior-clipboard restore happens
            # in that case (see paste_text's own docstring). Here we just
            # need to surface a persistent error instead of the "pasted"
            # state, and skip history's normal latency path.
            try:
                paste_text(cleaned, target_app)
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                self.indicator.set(
                    "error", f"paste failed — text on clipboard: {str(e)[:40]}")
                history.append(bundle, mode, raw, cleaned, "paste_error", ms)
                return  # no sleep/hide: pill stays until the next dictation
            t_paste = time.time()
            ms = int((t_paste - t0) * 1000)
            self.indicator.set("pasted", f"{len(cleaned.split())}w · {ms}ms")
            history.append(bundle, mode, raw, cleaned, path, ms, stages={
                "whisper_ms": (t_whisper - t0) * 1000,
                "cleanup_ms": (t_clean - t_whisper) * 1000,
                "paste_ms": (t_paste - t_clean) * 1000,
            })
        except Exception as e:
            # B5: everything else (Whisper crash, unexpected errors) — show
            # the cause and leave the pill up until the next dictation
            # starts (Daemon._start's own indicator.set("listening", ...)
            # naturally replaces it); no auto-hide, so a real failure can't
            # silently disappear before the user notices.
            self.indicator.set("error", str(e)[:60])
            try:
                history.append(bundle, mode, locals().get("raw", ""), "",
                               "error", int((time.time() - t0) * 1000))
            except Exception:
                pass

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

        def _guard(fn, name):
            """Last line of defence around every pynput callback.

            pynput propagates an exception raised inside a callback out of
            the listener thread and stops the listener. Because the daemon
            process itself survives that, launchd's KeepAlive never fires
            and the tool looks alive while the hotkey is deaf. Nothing
            reached from a callback is allowed to raise past this point."""
            def wrapped(key):  # noqa: ANN001
                try:
                    fn(key)
                except Exception as e:
                    print(f"[murmur] {name} handler error: "
                          f"{e.__class__.__name__}: {e}", flush=True)
                    try:
                        self.indicator.set("error", str(e)[:60])
                    except Exception:
                        pass
            return wrapped

        while True:
            with keyboard.Listener(
                    on_press=_guard(on_press, "on_press"),
                    on_release=_guard(on_release, "on_release")) as ln:
                print("[murmur] daemon running. Ctrl-C to quit.", flush=True)
                ln.join()
            # The guards mean our own code can no longer end the listener,
            # but macOS still can: it disables an event tap that blocks for
            # too long, and revoking/regranting Accessibility tears the tap
            # down. Rebuild rather than fall out of run() into a live-but-
            # deaf process. Any recording in flight is abandoned first so
            # the new listener starts from a known state.
            try:
                with self._lock:
                    self.recording = False
                self.hands_free = False
                self.recorder.stop()
            except Exception:
                pass
            print("[murmur] hotkey listener stopped — restarting in 2 s",
                  flush=True)
            time.sleep(2.0)


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
