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
                     load_snippets, load_vocab)
from .indicator import Indicator

SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.4
MODEL_NAME = "small.en"


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


def paste_text(text: str) -> None:
    """Copy -> Cmd-V -> restore prior clipboard (table stakes, spec §1)."""
    prior = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode())
    time.sleep(0.08)
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down',
    ])

    def restore() -> None:
        time.sleep(0.6)
        subprocess.run(["pbcopy"], input=prior)

    threading.Thread(target=restore, daemon=True).start()


class Daemon:
    def __init__(self) -> None:
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
        self.indicator = Indicator()
        self.recorder = Recorder()
        self.recording = False
        self.hands_free = False
        self._last_release = 0.0
        self._lock = threading.Lock()

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
        # reload lightweight config every dictation so edits stick live
        self.snippets = load_snippets()
        self.corrections = load_corrections()
        self.indicator.set("listening",
                           "hands-free" if self.hands_free else "")
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
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio: np.ndarray) -> None:
        t0 = time.time()
        bundle = modes_mod.frontmost_bundle_id()
        mode = modes_mod.mode_for(bundle, self.modes)
        try:
            self.indicator.set("transcribing")
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=5,
                initial_prompt=vocab_mod.initial_prompt(
                    self.vocab, self.corrections),
                vad_filter=True,
            )
            raw = " ".join(s.text.strip() for s in segments).strip()
            if not raw:
                self.indicator.set("error", "heard nothing")
                time.sleep(1.2)
                self.indicator.hide()
                return
            self.indicator.set("cleaning")
            cleaned, path = cleanup.clean_text(
                raw, mode, self.vocab, self.corrections, self.snippets)
            paste_text(cleaned)
            ms = int((time.time() - t0) * 1000)
            self.indicator.set("pasted", f"{len(cleaned.split())}w · {ms}ms")
            history.append(bundle, mode, raw, cleaned, path, ms)
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
    Daemon().run()
