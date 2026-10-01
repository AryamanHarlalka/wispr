"""Wispr v3 daemon — hold Right Option anywhere, speak, get cleaned text
pasted at the cursor. Double-tap Right Option toggles hands-free mode.

Pipeline: record (sounddevice, chunk-decoded incrementally in the
background — see IncrementalTranscriber) -> transcribe tail only
(faster-whisper, dictionary + vocab initial_prompt) -> clean (snippets ->
Haiku under a hard budget -> rules fallback) -> paste (clipboard-
preserving) -> history -> learn (from the LLM's known-term substitutions
and from the user's own edits to the pasted text). Audio never leaves the
Mac; only *text* may go to the API.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from . import (cleanup, cloud_stt, dictionary as dict_mod, history, learn as learn_mod,
               modes as modes_mod, stt, vocab as vocab_mod)
from .config import (WISPR_HOME, cleanup_wait_s, ensure_home, learn_from_edits,
                     learn_from_llm, load_corrections, load_modes,
                     load_snippets, load_vocab, paste_instant, paste_revise,
                     hotkey, revise_max_chars, revise_window_s, stt_backend,
                     stt_beam_size, stt_model)
from .indicator import Indicator

SAMPLE_RATE = 16000
DOUBLE_TAP_S = 0.45
# A press shorter than this never carried speech, so it is read as a tap
# rather than a dictation. Holding longer keeps the original zero-latency
# push-to-talk path: stop and paste the moment the key comes up.
TAP_MAX_S = 0.35
# Backend + model are a config swap (0c) -- see config.stt_backend()/
# stt_model() for the current defaults and the benchmark reasoning behind
# them. BEAM_SIZE and CONDITION_ON_PREVIOUS_TEXT are the 0d decode-tuning
# knobs: beam_size=1 trades a little accuracy for materially faster decode
# (measured in `wispr bench`); condition_on_previous_text=False stops the
# runaway-repetition failure mode dictation is prone to (a long pause mid
# -utterance otherwise biases the next segment toward repeating itself).
BACKEND_NAME = stt_backend()
MODEL_NAME = stt_model()
BEAM_SIZE = stt_beam_size()
CONDITION_ON_PREVIOUS_TEXT = False


def _resample_to_16k(audio: np.ndarray, src_rate: int) -> np.ndarray:
    """Downsample captured audio to SAMPLE_RATE.

    Needed because we cannot always capture at 16 kHz directly: many Macs
    (the built-in MacBook Pro mic runs at 96 kHz) refuse a 16 kHz
    AudioUnit and fail with AUHAL -10851 / PaErrorCode -9986. Capturing at
    the device's native rate and converting here is the reliable path.

    Integer ratios use mean-pooling, which doubles as a crude anti-alias
    filter -- important going 96k -> 16k, where plain decimation folds
    everything between 8 kHz and 48 kHz back into the speech band. Other
    ratios fall back to linear interpolation, which is aliasing-prone but
    still far better than not capturing at all.
    """
    if src_rate == SAMPLE_RATE or audio.size == 0:
        return audio
    if src_rate % SAMPLE_RATE == 0:
        factor = src_rate // SAMPLE_RATE
        usable = (audio.size // factor) * factor
        if usable == 0:
            return np.zeros(0, dtype="float32")
        return audio[:usable].reshape(-1, factor).mean(axis=1).astype("float32")
    n_out = int(round(audio.size * SAMPLE_RATE / float(src_rate)))
    if n_out <= 0:
        return np.zeros(0, dtype="float32")
    return np.interp(
        np.linspace(0.0, audio.size - 1, n_out, dtype="float64"),
        np.arange(audio.size, dtype="float64"),
        audio.astype("float64"),
    ).astype("float32")


class Recorder:
    def __init__(self) -> None:
        import sounddevice as sd
        self._sd = sd
        self._frames: list[np.ndarray] = []
        self._stream = None
        self.level = 0.0
        # Rate we actually captured at; may differ from SAMPLE_RATE when the
        # device refused 16 kHz. Everything downstream still gets 16 kHz.
        self._capture_rate = SAMPLE_RATE

    def _device_default_rate(self) -> int:
        try:
            return int(self._sd.query_devices(kind="input")["default_samplerate"])
        except Exception:
            return 48000

    def start(self) -> None:
        self._frames = []
        self._capture_rate = SAMPLE_RATE

        def cb(indata, frames, t, status):  # noqa: ANN001
            self._frames.append(indata.copy())
            self.level = float(np.abs(indata).mean())

        try:
            self._open_stream(cb)
        except Exception as e:
            # Before tearing PortAudio down, try the device's own rate: on
            # a 96 kHz built-in mic, asking for 16 kHz is itself the error,
            # and reinitialising PortAudio would not have helped.
            native = self._device_default_rate()
            if native != SAMPLE_RATE:
                try:
                    self._open_stream(cb, rate=native)
                    self._capture_rate = native
                    print(f"[wispr] device refused {SAMPLE_RATE} Hz; capturing "
                          f"at {native} Hz and resampling "
                          f"({e.__class__.__name__})", flush=True)
                    return
                except Exception:
                    pass
            # CoreAudio/PortAudio wedges in practice — PaErrorCode -9986
            # ("Internal PortAudio error") and AUHAL -10851 ("Invalid
            # Property Value") both show up after a device change, a
            # sleep/wake cycle, or another process holding the input
            # device. PortAudio's host-API state is process-global, so a
            # plain retry hits the same wedged state; tearing it down and
            # re-initialising usually clears it.
            print(f"[wispr] input stream failed "
                  f"({e.__class__.__name__}: {e}) — reinitialising "
                  f"PortAudio and retrying once", flush=True)
            self._reset_portaudio()
            try:
                self._open_stream(cb)  # a second failure propagates to _start()
            except Exception:
                # Last resort: native rate after the reinit.
                native = self._device_default_rate()
                if native == SAMPLE_RATE:
                    raise
                self._open_stream(cb, rate=native)
                self._capture_rate = native

    def _open_stream(self, cb, rate: int | None = None) -> None:  # noqa: ANN001
        self._stream = self._sd.InputStream(
            samplerate=rate or SAMPLE_RATE, channels=1, dtype="float32",
            callback=cb)
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
            print(f"[wispr] PortAudio reinit failed: "
                  f"{e.__class__.__name__}: {e}", flush=True)

    def stop(self) -> np.ndarray:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return _resample_to_16k(
            np.concatenate(self._frames).flatten(), self._capture_rate)

    def snapshot(self) -> np.ndarray:
        """Everything recorded so far, without stopping the stream (B3) —
        lets the incremental decoder peek at in-progress audio. Safe to call
        from another thread: the sounddevice callback only appends new
        arrays to `_frames`, never mutates existing ones, so `list(...)`
        here can't observe a torn read under the GIL."""
        frames = list(self._frames)
        if not frames:
            return np.zeros(0, dtype="float32")
        # Must match stop()'s output rate: the incremental decoder feeds the
        # same model, which assumes 16 kHz.
        return _resample_to_16k(
            np.concatenate(frames).flatten(), self._capture_rate)


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


# --- clipboard ownership (2026-08-04) ---------------------------------
#
# The "text appears, then vanishes" incident. Two independent halves of
# this file cooperated to destroy dictations:
#
#   * _revise_in_place backspaced the instant paste away and only THEN
#     tried to paste the improved version — destructive first. When the
#     second half failed, or the backspace run overran the start of the
#     field (that is the macOS error beep), the text was gone.
#   * paste_text unconditionally spawned a thread that restored the
#     user's previous clipboard 0.6 s later. So the documented recovery,
#     "the text is on the clipboard, Cmd-V gets it back", was false: a
#     restore thread from a paste that had already finished overwrote it.
#     The dictation then existed nowhere at all.
#
# Everything Wispr puts on the pasteboard now goes through
# _claim_clipboard(), which stamps a generation number, and every restore
# is arbitrated against that stamp:
#   * a restore only fires if the clipboard is still exactly the
#     generation it was scheduled for — a newer write always wins;
#   * a restore never fires while a revision is still in flight, or while
#     a generation is ARMED, meaning that text is the only copy the user
#     has left.
# When those rules conflict with putting the user's old clipboard back,
# the old clipboard loses. Losing a copied URL is an annoyance; losing a
# dictation is data loss.
_clip_lock = threading.RLock()
_clip = {
    "gen": 0,       # bumped on every write Wispr makes
    "text": None,   # what Wispr last wrote, or None if the user owns it
    "prior": None,  # what the user had before Wispr's first write in the chain
    "armed": 0,     # generation that must never be restored over (0 = none)
    "pending": 0,   # >0 while a revision may still land
}
# Let the target app consume the paste before putting the old clipboard
# back. Named so the tests can shrink it instead of sleeping for real.
RESTORE_DELAY_S = 0.6
# How long a restore waits for a revision/recovery to clear before giving
# up. Giving up leaves the dictation on the clipboard, which is the safe
# direction.
RESTORE_GIVE_UP_S = 12.0


def _claim_clipboard(text: str) -> int:
    """Write `text` to the pasteboard and return its generation stamp.

    A chained claim inherits the original `prior`: if the clipboard still
    holds what Wispr last wrote, the thing worth restoring later is
    still the user's own clipboard from before Wispr touched it, not
    Wispr's intermediate text. That is what stops a revision from
    "restoring" the rules draft it just replaced.
    """
    with _clip_lock:
        current = _pasteboard_read()
        if _clip["text"] is not None and current == _clip["text"]:
            prior = _clip["prior"]
        else:
            prior = current
        _clip["gen"] += 1
        _clip["text"] = text
        _clip["prior"] = prior
        _pasteboard_write(text)
        return _clip["gen"]


def _arm_recovery() -> int:
    """Mark whatever Wispr last wrote as the user's only remaining copy.
    No restore may run over it — see _schedule_restore."""
    with _clip_lock:
        _clip["armed"] = _clip["gen"]
        return _clip["armed"]


def _disarm_recovery(gen: int) -> None:
    with _clip_lock:
        if _clip["armed"] == gen:
            _clip["armed"] = 0


def _begin_revision_window() -> None:
    """A revision may still touch the keyboard and the clipboard, so hold
    every restore until _end_revision_window(). Without this the instant
    paste's 0.6 s restore lands in the middle of the revision and the
    clipboard stops being a recovery buffer at exactly the moment it is
    the only copy."""
    with _clip_lock:
        _clip["pending"] += 1


def _end_revision_window() -> None:
    with _clip_lock:
        _clip["pending"] = max(0, _clip["pending"] - 1)


def _stage_clipboard(text: str) -> int:
    """Put the replacement text on the clipboard and arm it, WITHOUT
    sending any keystroke.

    Called before the backspaces in _revise_in_place, so that from the
    moment the first character is deleted a complete copy of what
    replaces it is already one Cmd-V away. Nothing between here and the
    paste can leave the user holding neither version.
    """
    gen = _claim_clipboard(text)
    _arm_recovery()
    return gen


def _schedule_restore(gen: int) -> None:
    """Put the user's own clipboard back, but only if that can be done
    without overwriting something they still need."""
    with _clip_lock:
        if _clip["gen"] != gen or _clip["prior"] is None:
            return  # nothing to restore (empty or non-text clipboard)

    def restore() -> None:
        time.sleep(RESTORE_DELAY_S)
        deadline = time.time() + RESTORE_GIVE_UP_S
        while True:
            with _clip_lock:
                if _clip["gen"] != gen:
                    return  # a newer write owns the clipboard; never clobber
                if not (_clip["pending"] > 0 or _clip["armed"] == gen):
                    prior = _clip["prior"]
                    if prior is None:
                        return
                    try:
                        _pasteboard_write(prior)
                    except Exception:
                        return
                    _clip["gen"] += 1
                    _clip["text"] = None
                    _clip["prior"] = None
                    return
            if time.time() > deadline:
                print("[wispr] clipboard restore skipped — the dictation is "
                      "still the only copy of itself, leaving it on the "
                      "clipboard", flush=True)
                return
            time.sleep(0.1)

    threading.Thread(target=restore, daemon=True).start()


# How hard to try to get the target app back in front before pasting.
ACTIVATE_ATTEMPTS = 3
# 0.5 s per attempt: if the target app is full-screen it lives in its own
# Space, and bringing it forward runs the Spaces animation first. A wait
# that expires mid-animation reads as "the app never came back".
ACTIVATE_WAIT_S = 0.5


def _front_is(target_app) -> bool:
    """True when `target_app` is the frontmost app right now.

    A None target means "no expectation" -- callers that never captured an
    app (tests, `wispr last`) are not blocked by this check.
    """
    if target_app is None:
        return True
    try:
        pid = int(target_app.processIdentifier())
    except Exception:
        return True
    front = _frontmost_pid()
    return front is None or front == pid


def _frontmost_name() -> str:
    try:
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return "?"
        return str(front.bundleIdentifier() or front.localizedName() or "?")
    except Exception:
        return "?"


def _activate_target(target_app) -> bool:  # noqa: ANN001
    """Bring the app that was frontmost when the hotkey went down back to
    the front, and wait until macOS confirms the switch.

    Three attempts, not one: a single activateWithOptions_ loses the race
    whenever something else is activating at the same moment -- and with
    the Fn/globe hotkey something usually is, because macOS runs its own
    "Press globe key to" action off the very key press that starts the
    dictation. Returns True once the target is confirmed frontmost.
    """
    if target_app is None:
        return True
    try:
        from AppKit import (NSApplicationActivateIgnoringOtherApps,
                            NSWorkspace)
        ws = NSWorkspace.sharedWorkspace()
        pid = int(target_app.processIdentifier())
        for _ in range(ACTIVATE_ATTEMPTS):
            front = ws.frontmostApplication()
            if front is not None and int(front.processIdentifier()) == pid:
                return True  # already in front: don't bounce it
            target_app.activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps)
            deadline = time.time() + ACTIVATE_WAIT_S
            while time.time() < deadline:
                front = ws.frontmostApplication()
                if front is not None and int(front.processIdentifier()) == pid:
                    return True
                time.sleep(0.02)
        return False
    except Exception:
        return False


def paste_text(text: str, target_app=None) -> None:
    """Copy -> reactivate the original target app -> Cmd-V -> restore the
    prior clipboard (table stakes, spec §1).

    B5: the text is on the clipboard before the keystroke is ever
    attempted, so a failure leaves the dictation recoverable.

    2026-08-04: that promise is now actually kept. The prior clipboard is
    only restored by a thread that can prove the clipboard still holds
    exactly this paste and that no recovery is pending; if the keystroke
    fails, this generation is armed and nothing will overwrite it. Before
    this, a restore thread happily wrote over the one remaining copy 0.6 s
    later and "Cmd-V to recover" recovered whatever the user had copied
    an hour ago.
    """
    gen = _claim_clipboard(text)
    _activate_target(target_app)
    if not _front_is(target_app):
        _activate_target(target_app)  # one more go before giving up
    if not _front_is(target_app):
        # Refuse to send the keystroke. A blind Cmd-V is not a harmless
        # no-op -- it dumps the dictation into whatever window happens to
        # be in front. The text is already on the clipboard and already in
        # history, so failing here is recoverable; pasting into the wrong
        # window is not.
        _arm_recovery()
        raise RuntimeError(
            "target app never came back to the front (front is "
            + _frontmost_name() + ")")
    try:
        _send_cmd_v()
    except Exception:
        # The keystroke never landed, so this text exists nowhere else.
        _arm_recovery()
        raise
    _disarm_recovery(gen)
    _schedule_restore(gen)


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
            f"paste failed (grant Accessibility to Wispr.app in "
            f"System Settings > Privacy & Security): {r.stderr.strip()[:120]}")


def _send_backspaces(n: int) -> None:
    """n synthetic backspaces via Quartz, used to retract an instant paste
    before replacing it (see Daemon._revise_in_place).

    Posted in small batches with a breath between them: CGEventPost is
    asynchronous and a few hundred events dispatched in a tight loop can
    outrun a slow app's event handling, which shows up as some of the
    deletions silently not landing — much worse than deleting nothing.
    """
    import Quartz
    kVK_Delete = 51
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for i in range(n):
        down = Quartz.CGEventCreateKeyboardEvent(src, kVK_Delete, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, kVK_Delete, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        if i % 40 == 39:
            time.sleep(0.012)


def _frontmost_pid() -> int | None:
    try:
        from AppKit import NSWorkspace
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return None if front is None else int(front.processIdentifier())
    except Exception:
        return None


CHUNK_INTERVAL_S = 2.0    # how often the background worker checks for new audio
MIN_CHUNK_AUDIO_S = 5.0   # don't bother decoding until this much new audio exists
MAX_CHUNK_AUDIO_S = 24.0  # force a cut before Whisper's 30 s window regardless
TRAILING_GUARD_S = 0.8    # never cut this close to "now": may be mid-word
SILENCE_MIN_S = 0.40      # a gap this long is a safe place to cut
_FRAME_S = 0.02           # energy analysis frame


def find_cut(audio: np.ndarray, start: int, limit: int,
             sample_rate: int = SAMPLE_RATE) -> int | None:
    """Sample index of the LAST silence gap (>= SILENCE_MIN_S) that lies
    within [start, limit), or None. Cutting in silence means neither the
    committed chunk nor the tail starts mid-word.

    Energy-based, on the raw audio, so it costs microseconds and never
    touches the model. The threshold is relative to the region's own
    loudness so a quiet speaker and a noisy room both work."""
    if limit - start < int(SILENCE_MIN_S * sample_rate) * 2:
        return None
    region = audio[start:limit]
    frame = max(1, int(_FRAME_S * sample_rate))
    n = region.size // frame
    if n < 4:
        return None
    frames = region[:n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    loud = float(np.percentile(rms, 90)) if rms.size else 0.0
    thresh = max(1.5e-3, 0.12 * loud)
    quiet = rms < thresh
    need = max(1, int(SILENCE_MIN_S / _FRAME_S))
    best = None
    run = 0
    for i, q in enumerate(quiet):
        if q:
            run += 1
            if run >= need:
                # midpoint of the gap so far: (i - run + 1 .. i)
                best = start + int((i - run / 2.0 + 0.5) * frame)
        else:
            run = 0
    return best


class IncrementalTranscriber:
    """Decode committed audio in the background while the user is still
    talking, so on key-release only the trailing tail needs decoding.

    2026-09-06 rewrite. The previous version decoded *all* uncommitted
    audio every interval and only committed VAD segments that ended
    before the trailing guard — so a long, fluent dictation with few
    pauses re-decoded an ever-growing region every 3 s (quadratic), and
    on release the tail decode had to wait for that region's decode to
    finish AND then decode it again. history.jsonl shows 15-125 s
    whisper times on exactly those dictations. Now every chunk is bounded:
    the worker cuts at the last silence gap (or at MAX_CHUNK_AUDIO_S at
    worst), decodes just that chunk, and commits it. Each decode is a few
    seconds of audio, the tail is at most one chunk, and the worst case
    wait on release is one bounded decode.

    Built to fail safe: every decode call is wrapped, and any error just
    means `finalize()` returns None — the caller (Daemon._process) then
    falls back to the exact whole-utterance decode path. A dictation can
    never be lost to a bug here.

    `model_lock` is shared with Daemon._process's own transcribe() calls
    so at most one decode ever runs against the shared model at a time.
    """

    def __init__(self, model, model_lock: threading.Lock,
                 vocab: list[str], corrections: dict[str, str],
                 dictionary: list[str] | None = None) -> None:
        self._model = model
        self._model_lock = model_lock
        self._vocab = vocab
        self._corrections = corrections
        self._dictionary = dictionary
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._commit_lock = threading.Lock()
        self._committed_text = ""
        self._committed_samples = 0
        self._failed = False
        self.chunks = 0  # for logging/tests

    def start(self, recorder: "Recorder") -> None:
        self._thread = threading.Thread(
            target=self._run, args=(recorder,), daemon=True,
            name="wispr-incremental")
        self._thread.start()

    def _run(self, recorder: "Recorder") -> None:
        while not self._stop_evt.wait(CHUNK_INTERVAL_S):
            try:
                self._maybe_commit_chunk(recorder)
            except Exception as e:
                # Stop trying: finalize() will see _failed and tell the
                # caller to fall back to a plain whole-decode.
                print(f"[wispr] incremental decode failed, will decode whole: "
                      f"{e.__class__.__name__}: {e}", flush=True)
                self._failed = True
                return

    def _decode(self, audio: np.ndarray, extra_prompt: str) -> list:
        prompt = vocab_mod.initial_prompt(self._vocab, self._corrections,
                                          self._dictionary)
        if extra_prompt:
            # Context first, terms last: Whisper keeps the LAST ~223 prompt
            # tokens, so this order guarantees the term list survives and
            # only the oldest context is dropped.
            prompt = f"{extra_prompt[-300:]} {prompt}"
        with self._model_lock:
            segments = self._model.transcribe(
                audio, language="en", beam_size=BEAM_SIZE,
                initial_prompt=prompt, vad_filter=True,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT)
        return segments

    def _maybe_commit_chunk(self, recorder: "Recorder") -> None:
        if self._stop_evt.is_set():
            return
        audio = recorder.snapshot()
        with self._commit_lock:
            committed = self._committed_samples
            new_len_s = (len(audio) - committed) / SAMPLE_RATE
            if new_len_s < MIN_CHUNK_AUDIO_S:
                return
            limit = len(audio) - int(TRAILING_GUARD_S * SAMPLE_RATE)
            # Never look for a cut beyond MAX_CHUNK from the last commit:
            # if the worker fell behind (CPU stall), it catches up in
            # bounded chunks rather than one giant decode.
            window_end = min(limit, committed + int(MAX_CHUNK_AUDIO_S * SAMPLE_RATE))
            cut = find_cut(audio, committed + SAMPLE_RATE, window_end)
            if cut is None:
                if new_len_s < MAX_CHUNK_AUDIO_S:
                    return  # wait for a pause; nothing is lost by waiting
                cut = min(committed + int(MAX_CHUNK_AUDIO_S * SAMPLE_RATE), limit)
            chunk = audio[committed:cut]
            if self._stop_evt.is_set():
                return
            segments = self._decode(chunk, self._committed_text)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                self._committed_text = f"{self._committed_text} {text}".strip()
            self._committed_samples = cut
            self.chunks += 1

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            # A decode in flight holds the model lock; finalize() waits for
            # it there anyway. This join just keeps the thread bookkeeping
            # tidy and is bounded so a stuck decode cannot hang release.
            self._thread.join(timeout=0.5)

    def finalize(self, final_audio: np.ndarray) -> str | None:
        """Stop the worker, decode just the trailing tail, and return the
        full transcript — or None if incremental decoding failed at any
        point, telling the caller to fall back to a plain whole-decode."""
        self.stop()
        if self._failed:
            return None
        try:
            with self._commit_lock:  # waits for an in-flight commit to land
                tail = final_audio[self._committed_samples:]
                if tail.size < int(0.2 * SAMPLE_RATE):
                    return self._committed_text
                tail_segments = self._decode(tail, self._committed_text)
                tail_text = " ".join(
                    s.text.strip() for s in tail_segments).strip()
                return f"{self._committed_text} {tail_text}".strip()
        except Exception:
            return None


import concurrent.futures as _cf

_STT_POOL = _cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="wispr-stt")
AUDIO_KEEP = 150


def _cleanup_budget(raw: str) -> float:
    """Long dictations need longer to clean. A flat 2.0 s budget made 22%
    of the last 200 dictations (every one over ~90 words) skip cleanup
    entirely and paste raw. Haiku writes ~150 tokens/s, so scale with
    length: 60 words -> 2.0 s, 200 -> 3.6 s, 350 -> 5.4 s, capped at 7 s."""
    words = len((raw or "").split())
    return min(7.0, max(cleanup_wait_s(), 1.2 + 0.012 * words))  # newest N dictations kept as WAV, local only, for A/B + learning


def _save_audio_async(rec_id, audio: np.ndarray) -> None:
    """Keep the last AUDIO_KEEP takes in ~/.wispr/audio/<id>.wav so model
    changes can be measured on the owner's real speech instead
    of three bench clips. Local only; never uploaded anywhere."""
    if not rec_id or audio is None or audio.size == 0:
        return

    def _w() -> None:
        try:
            import wave
            d = WISPR_HOME / "audio"
            d.mkdir(parents=True, exist_ok=True)
            with wave.open(str(d / f"{rec_id}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes((np.clip(audio, -1, 1) * 32767)
                              .astype(np.int16).tobytes())
            files = sorted(d.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            for old in files[:-AUDIO_KEEP]:
                old.unlink(missing_ok=True)
        except Exception as e:
            print(f"[wispr] audio save failed: {e}", flush=True)
    threading.Thread(target=_w, daemon=True).start()


class Daemon:
    def __init__(self, indicator: Indicator | None = None) -> None:
        ensure_home()
        self.modes = load_modes()
        self.snippets = load_snippets()
        self.corrections = load_corrections()
        self.dictionary = dict_mod.load_dictionary()
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
        self._last_release = 0.0   # retained for the existing test fixtures
        self._press_t = 0.0        # when alt_r last went down
        self._last_tap = 0.0       # when the last *short* tap completed
        self._pending_stop = None  # Timer holding a deferred _stop_and_process
        self._lock = threading.Lock()
        self._target_app = None  # captured at _start(), before the pill shows
        # Frontmost app sampled inside the hotkey callback itself, with the
        # time it was sampled. _begin_capture() runs on a worker thread one
        # queue hop later -- by then the globe key's own system action can
        # already have changed which app is in front.
        self._front_at_press = None
        self._front_at_press_t = 0.0
        self._edit_watcher: "learn_mod.EditWatcher | None" = None
        modes_mod.TRACKER.install()
        self._model_lock = threading.Lock()  # guards all model.transcribe() calls
        self._incremental: IncrementalTranscriber | None = None
        # Monotonic dictation counter. A pending in-place revision compares
        # this before touching the keyboard: if it moved, a newer dictation
        # owns the cursor and the revision must not fire.
        self._dictation_seq = 0

        # Warm the Anthropic client (keychain read + TLS) off the critical
        # path so the first real dictation doesn't pay it (B1).
        cleanup.warm_client()

        print(f"[wispr] loading {BACKEND_NAME}/{MODEL_NAME} …", flush=True)
        self.model = stt.build_backend(BACKEND_NAME, MODEL_NAME)
        _key_name = "Fn" if hotkey() == "fn" else "Right Option"
        print(f"[wispr] ready — hold {_key_name} to dictate "
              f"(vocab: {len(self.vocab)} terms, dictionary: "
              f"{len(self.dictionary)}, corrections: {len(self.corrections)}, "
              f"llm: {'on' if cleanup._get_client() else 'off — rules only'})",
              flush=True)

    # --- capture ---
    def _start(self) -> None:
        with self._lock:
            if self.recording:
                return
            self.recording = True
            self._dictation_seq += 1
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
            if self._incremental is not None:
                self._incremental.stop()
            self._incremental = None
            try:
                self.recorder.stop()
            except Exception:
                pass
            print(f"[wispr] could not start recording: "
                  f"{e.__class__.__name__}: {e}", flush=True)
            self.indicator.set("error", f"mic unavailable — {str(e)[:40]}")

    def _begin_capture(self) -> None:
        # Capture the real target app BEFORE the pill shows. Prefer the
        # sample taken in the hotkey callback (see __init__); it predates
        # both the pill and any system panel the globe key opened. Either
        # way it goes through modes.frontmost_app(), which never returns
        # Wispr itself.
        if (self._front_at_press is not None
                and time.time() - self._front_at_press_t < 1.5):
            self._target_app = self._front_at_press
        else:
            self._target_app = modes_mod.frontmost_app()
        self._front_at_press = None
        print("[wispr] capture target: "
              + (modes_mod.bundle_id_of(self._target_app) or "?"), flush=True)
        if self._edit_watcher is not None:
            self._edit_watcher.cancel()  # the cursor belongs to this take now
            self._edit_watcher = None
        # reload lightweight config every dictation so edits stick live
        self.snippets = load_snippets()
        self.corrections = load_corrections()
        self.dictionary = dict_mod.load_dictionary()
        self.indicator.set("listening",
                           "hands-free" if self.hands_free else "")
        # Open the TLS connection while the user is still speaking so the
        # Haiku call after key-release starts on a warm connection (B1).
        cleanup.prewarm_connection()
        cloud_stt.prewarm()
        self.recorder.start()
        # B3: start decoding in the background while the user keeps
        # talking, so long (hands-free) dictations only pay for the tail
        # on release instead of the whole recording.
        self._incremental = IncrementalTranscriber(
            self.model, self._model_lock, self.vocab, self.corrections,
            self.dictionary)
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
            if self._incremental is not None:
                self._incremental.stop()
            self._incremental = None
            print(f"[wispr] could not stop recording: "
                  f"{e.__class__.__name__}: {e}", flush=True)
            self.indicator.set("error", f"recording lost — {str(e)[:40]}")
            return
        incremental = self._incremental
        self._incremental = None
        _dump_dir = os.environ.get("WISPR_DEBUG_DUMP_DIR", "").strip()
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
            print("[wispr] take dropped: only %.2fs of audio (a stray tap, "
                  "not speech)" % (audio.size / SAMPLE_RATE), flush=True)
            self.indicator.hide()
            return
        target_app = self._target_app
        threading.Thread(target=self._process,
                          args=(audio, target_app, incremental),
                          daemon=True).start()

    def _local_transcribe(self, audio: np.ndarray,
                          incremental: "IncrementalTranscriber | None") -> str:
        """The on-device path (base.en). Fallback for cloud STT and the
        whole story when offline. Never raises."""
        try:
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
                            self.vocab, self.corrections, self.dictionary),
                        vad_filter=True,
                        condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                    )
                raw = " ".join(s.text.strip() for s in segments).strip()
            return raw
        except Exception as e:
            print(f"[wispr] local stt failed: {e.__class__.__name__}: {e}",
                  flush=True)
            return ""

    def _recent_context(self, bundle: str) -> str:
        """The previous dictation into the same app within 3 minutes —
        continuity for the speech model (names, casing, topic)."""
        last = getattr(self, "_last_text", None)
        if not last:
            return ""
        b, ts, text = last
        if b != bundle or time.time() - ts > 180:
            return ""
        return text or ""

    def _process(self, audio: np.ndarray, target_app=None,
                 incremental: "IncrementalTranscriber | None" = None) -> None:
        t0 = time.time()
        bundle = modes_mod.bundle_id_of(target_app)
        mode = modes_mod.mode_for(bundle, self.modes)
        try:
            self.indicator.set("transcribing")
            # 2026-10-01: cloud STT first (~5x lower WER on the owner's voice,
            # faster than base.en on an Intel CPU), with the local model
            # decoding in parallel as the fallback. See cloud_stt.py.
            dur_s = audio.size / SAMPLE_RATE
            stt_engine, stt_note = "local", ""
            cloud_fut = None
            if cloud_stt.enabled():
                try:
                    prompt = cloud_stt.build_prompt(
                        dict_mod.prompt_terms(self.vocab, self.corrections,
                                              self.dictionary, limit=150),
                        self._recent_context(bundle))
                    cloud_fut = _STT_POOL.submit(cloud_stt.transcribe,
                                                 audio, prompt)
                except Exception as e:
                    stt_note = f"cloud submit failed: {e}"
            local_fut = _STT_POOL.submit(self._local_transcribe, audio,
                                         incremental)
            raw = None
            if cloud_fut is not None:
                try:
                    text, stt_note = cloud_fut.result(
                        timeout=cloud_stt.wait_s(dur_s))
                except Exception:
                    text, stt_note = None, "timeout"
                if text:
                    raw, stt_engine = text, f"cloud:{cloud_stt.model()}"
                else:
                    print(f"[wispr] cloud stt -> local ({stt_note})",
                          flush=True)
            if raw is None:
                raw = local_fut.result()
            t_whisper = time.time()
            if stt_engine == "local" and not raw:
                raw = ""
            if not raw:
                # "heard nothing" has two very different causes and used to
                # report both identically, which is why a dead microphone
                # looked like a transcription problem for hours. Measure the
                # signal and say which one it was.
                rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                peak = float(np.max(np.abs(audio))) if audio.size else 0.0
                if rms < 1e-4:
                    msg = "mic captured silence — check Microphone permission"
                    print(f"[wispr] SILENT capture: {audio.size / SAMPLE_RATE:.1f}s "
                          f"rms={rms:.6f} peak={peak:.6f} "
                          f"(capture rate {self.recorder._capture_rate} Hz). "
                          f"The stream opened but no audio arrived — this is "
                          f"almost always the Microphone permission, not the "
                          f"speech model.", flush=True)
                else:
                    msg = "heard nothing (audio was there)"
                    print(f"[wispr] audio present but no transcript: "
                          f"{audio.size / SAMPLE_RATE:.1f}s rms={rms:.4f} "
                          f"peak={peak:.4f} — VAD may have rejected it, or "
                          f"speech was too quiet/far.", flush=True)
                self.indicator.set("error", msg)
                time.sleep(1.6)
                self.indicator.hide()
                return
            self.indicator.set("cleaning")
            # B5: cleanup shouldn't raise, but if it somehow does, fall back
            # to the raw transcript rather than losing the dictation
            # entirely — better a slightly messy paste than none.
            revision_fut = None
            try:
                if paste_instant():
                    # Instant mode: `cleaned` is the local rules result,
                    # available with no network wait. `revision_fut` is the
                    # Haiku call still in flight; it revises after the paste
                    # instead of delaying it.
                    cleaned, path, revision_fut = cleanup.instant_result(
                        raw, mode, self.vocab, self.corrections, self.snippets)
                else:
                    # Default since 2026-09-06: the LLM pass runs before
                    # the paste, under a hard budget, so what lands on
                    # screen is the finished text.
                    cleaned, path = cleanup.clean_text(
                        raw, mode, self.vocab, self.corrections, self.snippets,
                        budget_s=_cleanup_budget(raw))
            except Exception as e:
                print(f"[wispr] cleanup raised, pasting raw transcript: "
                      f"{e.__class__.__name__}: {e}", flush=True)
                cleaned, path, revision_fut = raw, "cleanup_error", None
            t_clean = time.time()
            # 2026-08-04: history is written BEFORE the paste, not after.
            # Everything below this line types into a live document and
            # can fail; when it does, the transcript must already be
            # somewhere durable. `wispr last` reads this file, so a
            # dictation that never made it onto the screen is still one
            # command away. The record is revised in place afterwards
            # (history.update, folded by id on read) rather than appended
            # a second time — one dictation, one entry.
            rec_id = None
            try:
                rec_id = history.append(
                    bundle, mode, raw, cleaned, path,
                    int((t_clean - t0) * 1000), stages={
                        "whisper_ms": (t_whisper - t0) * 1000,
                        "cleanup_ms": (t_clean - t_whisper) * 1000,
                    }, extra={"stt": stt_engine, "stt_note": stt_note[:120]})
                _save_audio_async(rec_id, audio)
            except Exception as e:
                print(f"[wispr] could not write history before the paste: "
                      f"{e.__class__.__name__}: {e}", flush=True)
            # Hold every clipboard restore until we know whether a
            # revision is going to run. A restore firing mid-revision is
            # half of what turned a failed revision into lost text.
            _begin_revision_window()
            try:
                try:
                    paste_text(cleaned, target_app)
                except Exception as e:
                    ms = int((time.time() - t0) * 1000)
                    self.indicator.set(
                        "error",
                        f"paste failed — Cmd-V or `wispr last`: {str(e)[:40]}")
                    self._record_update(rec_id, path="paste_error",
                                        latency_ms=ms)
                    return  # no sleep/hide: pill stays until the next dictation
                t_paste = time.time()
                ms = int((t_paste - t0) * 1000)
                self.indicator.set("pasted", f"{len(cleaned.split())}w · {ms}ms")
                self._record_update(rec_id, latency_ms=ms, stages={
                    "whisper_ms": (t_whisper - t0) * 1000,
                    "cleanup_ms": (t_clean - t_whisper) * 1000,
                    "paste_ms": (t_paste - t_clean) * 1000,
                })
                # The user already has text. Anything below is a bonus pass
                # and must never be able to damage that outcome.
                final, final_path = cleaned, path
                if revision_fut is not None:
                    improved = self._revise_in_place(
                        revision_fut, cleaned, target_app, bundle, mode, raw,
                        t0, rec_id=rec_id)
                    if improved:
                        final, final_path = improved, "haiku"
                # Learning looks at what is actually on screen now.
                self._last_text = (bundle, time.time(), final)
                self._after_paste(final, raw, final_path, target_app)
            finally:
                _end_revision_window()
        except Exception as e:
            # B5: everything else (Whisper crash, unexpected errors) — show
            # the cause and leave the pill up until the next dictation
            # starts (Daemon._start's own indicator.set("listening", ...)
            # naturally replaces it); no auto-hide, so a real failure can't
            # silently disappear before the user notices.
            self.indicator.set("error", str(e)[:60])
            try:
                # Revise the pre-paste record if we got far enough to
                # write one, so a failure never doubles a dictation up
                # into two history entries.
                _rid = locals().get("rec_id")
                if _rid:
                    history.update(_rid, path="error")
                else:
                    history.append(bundle, mode, locals().get("raw", ""), "",
                                   "error", int((time.time() - t0) * 1000))
            except Exception:
                pass

    def _after_paste(self, cleaned: str, raw: str, path: str,
                     target_app) -> None:
        """Learning hooks. Never raise: a learner problem must not take
        down a dictation that already landed on screen."""
        try:
            if path == "haiku" and learn_from_llm():
                known = dict_mod.prompt_terms(
                    self.vocab, self.corrections, self.dictionary, limit=10000)
                learned = learn_mod.learn_from_llm(raw, cleaned, known)
                for wrong, right in learned:
                    print(f"[wispr] learned from cleanup: '{wrong}' -> '{right}'",
                          flush=True)
        except Exception as e:
            print(f"[wispr] llm-learn failed: {e.__class__.__name__}: {e}",
                  flush=True)
        try:
            if learn_from_edits() and cleaned.strip():
                seq = self._dictation_seq
                pid = None
                try:
                    pid = int(target_app.processIdentifier()) if target_app else None
                except Exception:
                    pid = None

                def is_current() -> bool:
                    return self._dictation_seq == seq and not self.recording

                def on_learn(pairs) -> None:
                    for wrong, right in pairs:
                        print(f"[wispr] learned from your edit: '{wrong}' -> "
                              f"'{right}'", flush=True)

                watcher = learn_mod.EditWatcher(cleaned, pid, is_current,
                                                on_learn=on_learn)
                self._edit_watcher = watcher
                watcher.start()
        except Exception as e:
            print(f"[wispr] edit-learn failed: {e.__class__.__name__}: {e}",
                  flush=True)

    def _record_update(self, rec_id, **fields) -> None:
        """Revise the history entry written before the paste. Never
        raises: a history problem must not take down a dictation that
        already landed on screen."""
        if rec_id is None:
            return
        try:
            history.update(rec_id, **fields)
        except Exception as e:
            print(f"[wispr] could not update history record {rec_id}: "
                  f"{e.__class__.__name__}: {e}", flush=True)

    def _revise_in_place(self, fut, pasted: str, target_app, bundle: str,
                         mode: str, raw: str, t0: float,
                         rec_id: str | None = None) -> str | None:
        """Second-chance cleanup: replace the instant paste with Haiku's
        version — but only when it is provably safe.

        Every guard below exists because these are real keystrokes going
        into a live document. Skipping a revision costs the user slightly
        rougher text they already have; getting it wrong deletes characters
        that were never ours. The asymmetry decides every close call here:
        when in doubt, do nothing.

        2026-08-04: the last two steps used to be, in this order,
        `_send_backspaces(len(pasted))` then `paste_text(improved)` —
        destructive first. Whenever the second half failed, or the
        backspace run walked past the start of the field (the macOS error
        beep the user reported), the text was gone from the document and
        the clipboard had already been restored out from under it. The
        replacement is now staged on the clipboard BEFORE a single
        character is deleted, and armed so no restore thread can take it
        away again.
        """
        if not paste_revise():
            return
        seq = self._dictation_seq
        remaining = revise_window_s() - (time.time() - t0)
        if remaining <= 0:
            return
        improved, path = cleanup.await_revision(fut, remaining, raw)
        if improved is None or improved == pasted:
            return
        # Guard 1 — length. Retraction is one backspace per character, so a
        # long paste means hundreds of keystrokes: slow, and a wider window
        # for something else to land mid-sequence.
        if len(pasted) > revise_max_chars():
            print(f"[wispr] revision skipped ({len(pasted)} chars > "
                  f"{revise_max_chars()} limit)", flush=True)
            return
        # Guard 2 — code editors and terminals. Auto-indent, bracket
        # pairing and autocomplete mean the characters on screen are no
        # longer the characters we pasted, so a per-character retraction
        # cannot be trusted to remove exactly our own text.
        if mode == "technical":
            return
        # Guard 3 — a newer dictation started, so the cursor belongs to it.
        if seq != self._dictation_seq or self.recording:
            return
        # Guard 4 — text macOS rewrites as you type, at a different
        # length. "..." becomes "…" and "--" becomes "—", so an app with
        # substitutions on shows FEWER characters than we sent; one
        # backspace per pasted character then eats into text that was
        # never ours. Smart quotes and autocapitalisation are 1:1 and
        # survive this check. Unsolicited autocorrect of a misspelled word
        # does not, and cannot be detected from here — which is the main
        # reason paste.revise now defaults to off.
        if any(t in pasted for t in ("...", "--")):
            print("[wispr] revision skipped (text macOS may have "
                  "substituted — the backspace count would be a guess)",
                  flush=True)
            return
        # Guard 5 — focus moved. Backspacing into a different app deletes
        # whatever happens to be under the cursor there.
        if target_app is not None:
            try:
                if _frontmost_pid() != int(target_app.processIdentifier()):
                    return
            except Exception:
                return
        # Re-assert focus and check it again. CGEventPost is asynchronous
        # and the check above is already a moment stale by the time the
        # first backspace is posted; this is the last chance to notice
        # that the cursor is somewhere else now.
        _activate_target(target_app)
        if target_app is not None:
            try:
                if _frontmost_pid() != int(target_app.processIdentifier()):
                    return
            except Exception:
                return
        # Stage the replacement BEFORE destroying anything. From here on
        # every failure path leaves `improved` on the clipboard, armed, so
        # no restore thread can overwrite it.
        try:
            _stage_clipboard(improved)
        except Exception as e:
            # We could not even get the replacement onto the clipboard, so
            # there is no safe way to delete what is on screen. Do nothing:
            # the user keeps the rules draft they already have.
            print(f"[wispr] revision skipped (clipboard unavailable): "
                  f"{e.__class__.__name__}: {e}", flush=True)
            return
        try:
            _send_backspaces(len(pasted))
            paste_text(improved, target_app)
        except Exception as e:
            # The rules text may be gone from the document, but `improved`
            # is on the clipboard and armed, so nothing will overwrite it
            # and Cmd-V genuinely recovers it. `wispr last` is the second
            # copy, written to history before any of this started.
            _arm_recovery()
            print(f"[wispr] revision failed mid-flight: "
                  f"{e.__class__.__name__}: {e}", flush=True)
            self.indicator.set("error",
                               "revision failed — Cmd-V or `wispr last`")
            return
        ms = int((time.time() - t0) * 1000)
        self.indicator.set("pasted", f"{len(improved.split())}w · {ms}ms ✎")
        # One dictation, one history entry: revise the record written
        # before the paste instead of appending a second one. Direct
        # callers (and the guard tests) pass no rec_id and still get a
        # plain append, which is the pre-2026-08-04 behaviour.
        try:
            if rec_id is None:
                history.append(bundle, mode, raw, improved, path, ms)
            else:
                history.update(rec_id, cleaned=improved, path=path,
                               latency_ms=ms)
        except Exception:
            pass
        return improved


    def _cancel_pending_stop(self) -> bool:
        """Call off a deferred stop. True if one was actually waiting.

        Used by the second press of a double-tap: the first tap's recording
        is still running and must continue uninterrupted, so the audio the
        user already spoke is not thrown away.
        """
        timer = self._pending_stop
        self._pending_stop = None
        if timer is None:
            return False
        timer.cancel()
        return True

    def _defer_stop(self, delay: float) -> None:
        """Stop and process after `delay` unless a second press cancels it.

        Runs on a timer thread rather than the pynput callback thread, so
        transcription never blocks delivery of the next key event -- which
        is precisely what made the old double-tap impossible to trigger.
        """
        self._cancel_pending_stop()

        def fire() -> None:
            self._pending_stop = None
            try:
                self._stop_and_process()
            except Exception as e:
                print(f"[wispr] deferred stop failed: "
                      f"{e.__class__.__name__}: {e}", flush=True)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._pending_stop = timer
        timer.start()

    # --- hotkey ---

    def _hotkey_down(self) -> None:
        """Hotkey went down. Key-agnostic: both backends call this."""
        self._press_t = time.time()

        # While hands-free, the key is a stop button. The release handler
        # does the work, so holding it down cannot fire twice.
        if self.hands_free:
            return

        # Second press of a double-tap: a stop is pending from the first
        # tap. Cancel it and let the still-running recording continue, so
        # anything already said survives into the hands-free session.
        resumed = self._cancel_pending_stop()

        if not self.recording:
            self._start()
        elif resumed:
            self.indicator.set("listening", "")

    def _hotkey_up(self) -> None:
        """Hotkey came up. Hold duration decides tap vs push-to-talk."""
        now = time.time()
        held = now - self._press_t

        # Hands-free: any tap ends the session and commits the text.
        if self.hands_free:
            self.hands_free = False
            self._last_tap = 0.0
            self._cancel_pending_stop()
            self._stop_and_process()
            return

        # Held long enough to have said something: ordinary push-to-talk.
        # Unchanged path -- stop and paste immediately, no added latency.
        if held >= TAP_MAX_S:
            self._last_tap = 0.0
            self._stop_and_process()
            return

        # Too short to be speech, so it is a tap. If it closes a
        # double-tap, latch hands-free and keep recording.
        if now - self._last_tap < DOUBLE_TAP_S:
            self._cancel_pending_stop()
            self._last_tap = 0.0
            self.hands_free = True
            if not self.recording:
                self._start()
            self.indicator.set("listening", "hands-free")
            return

        # First tap. Hold the stop briefly in case a second one lands;
        # if it does not, this behaves exactly like a short press.
        self._last_tap = now
        self._defer_stop(DOUBLE_TAP_S)

    def _abandon_in_flight(self) -> None:
        """Drop any recording in progress so a rebuilt listener starts clean."""
        try:
            self._cancel_pending_stop()
            with self._lock:
                self.recording = False
            self.hands_free = False
            if self._incremental is not None:
                self._incremental.stop()
                self._incremental = None
            self.recorder.stop()
        except Exception:
            pass

    def _run_fn(self) -> None:
        """Fn/globe backend: a listen-only Quartz flagsChanged tap.

        macOS never delivers the globe key as a key event, so pynput is blind
        to it; the only signal is kCGEventFlagMaskSecondaryFn on flagsChanged.
        The tap is listen-only, which means the system still performs whatever
        "Press globe key to" is configured -- set it to "Do Nothing" or the
        emoji picker opens on every dictation.

        Supervised the same way as the pynput listener: macOS disables a tap
        that blocks too long, and a disabled tap is silent rather than fatal,
        which is exactly the alive-but-deaf failure this daemon has hit before.
        """
        import Quartz

        import queue

        FN = Quartz.kCGEventFlagMaskSecondaryFn
        DISABLED = (Quartz.kCGEventTapDisabledByTimeout,
                    Quartz.kCGEventTapDisabledByUserInput)

        # Key transitions are handed to this queue by the tap callback and
        # executed here instead, off the event-delivery thread. Unbounded on
        # purpose: dropping a key-up would strand the daemon mid-recording,
        # which is worse than a momentarily long queue.
        events: "queue.Queue[bool]" = queue.Queue()

        def worker() -> None:
            while True:
                is_down = events.get()
                try:
                    if is_down:
                        self._hotkey_down()
                    else:
                        self._hotkey_up()
                except Exception as e:
                    print(f"[wispr] Fn worker error: "
                          f"{e.__class__.__name__}: {e}", flush=True)

        threading.Thread(target=worker, daemon=True,
                         name="wispr-fn-worker").start()

        while True:
            down = [False]
            tap = None

            def cb(proxy, etype, event, refcon):  # noqa: ANN001
                # MUST return almost immediately. macOS revokes a tap whose
                # callback overruns its latency budget, and _hotkey_up()
                # transcribes, cleans and pastes -- roughly a second. Doing
                # that here meant every dictation killed the tap that was
                # driving it. Read the flag, hand it to the worker, leave.
                try:
                    if etype in DISABLED:
                        print("[wispr] Fn tap disabled by macOS — re-enabling",
                              flush=True)
                        Quartz.CGEventTapEnable(tap, True)
                        return event
                    is_down = bool(Quartz.CGEventGetFlags(event) & FN)
                    if is_down != down[0]:
                        down[0] = is_down
                        if is_down:
                            # Sample the front app HERE, on the event
                            # thread, before the worker hop and before
                            # macOS's own globe-key action can move focus.
                            try:
                                self._front_at_press = \
                                    modes_mod.frontmost_app()
                                self._front_at_press_t = time.time()
                            except Exception:
                                self._front_at_press = None
                        events.put(is_down)
                except Exception as e:
                    # Same contract as the pynput guards: never let an
                    # exception escape into the tap callback.
                    print(f"[wispr] Fn handler error: "
                          f"{e.__class__.__name__}: {e}", flush=True)
                return event

            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged),
                cb, None)

            if not tap:
                print("[wispr] could not create the Fn event tap — grant "
                      "Accessibility to Wispr.app, retrying in 5 s",
                      flush=True)
                self.indicator.set("error", "Fn hotkey needs Accessibility")
                time.sleep(5.0)
                continue

            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                      Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            print("[wispr] daemon running (Fn hotkey). Ctrl-C to quit.",
                  flush=True)

            # Short slices so a tap macOS silently disabled gets noticed even
            # when no events are arriving to trigger the callback.
            while Quartz.CGEventTapIsEnabled(tap):
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode,
                                          1.0, False)

            # Only reached when the tap could not be re-armed from inside
            # the callback, i.e. it is genuinely gone. Recording state is
            # unknowable at that point, so reset it -- but note this is now
            # rare, where before it fired on every single dictation.
            self._abandon_in_flight()
            print("[wispr] Fn tap stopped — rebuilding in 2 s", flush=True)
            time.sleep(2.0)

    def run(self) -> None:
        if hotkey() == "fn":
            self._run_fn()
            return
        from pynput import keyboard

        def on_press(key):  # noqa: ANN001
            if key == keyboard.Key.alt_r:
                try:
                    self._front_at_press = modes_mod.frontmost_app()
                    self._front_at_press_t = time.time()
                except Exception:
                    self._front_at_press = None
                self._hotkey_down()

        def on_release(key):  # noqa: ANN001
            if key == keyboard.Key.alt_r:
                self._hotkey_up()

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
                    print(f"[wispr] {name} handler error: "
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
                print("[wispr] daemon running. Ctrl-C to quit.", flush=True)
                ln.join()
            # The guards mean our own code can no longer end the listener,
            # but macOS still can: it disables an event tap that blocks for
            # too long, and revoking/regranting Accessibility tears the tap
            # down. Rebuild rather than fall out of run() into a live-but-
            # deaf process. Any recording in flight is abandoned first so
            # the new listener starts from a known state.
            self._abandon_in_flight()
            print("[wispr] hotkey listener stopped — restarting in 2 s",
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
