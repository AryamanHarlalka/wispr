"""Hands-free wake word for an assistant overlay ("hey <name>").

A tiny on-device keyword spotter (sherpa-onnx open-vocabulary KWS, a 3.3M
zipformer, no training, no account) listens for the configured phrase. Only
after it fires does anything heavier run: a Silero VAD finds the end of the
request, Wispr's own speech pipeline (cloud STT with the local model as
fallback) transcribes it, and the text is posted to the overlay exactly the
way the assistant key does (`<prefix>.hear`, userInfo text + source=wake).

After the assistant finishes speaking, a follow-up window stays open: speech
that starts inside it goes to the assistant without the wake phrase.

Isolation contract (dictation must never break):
  * everything runs on this module's own threads with its own input stream;
    the dictation Recorder is never touched;
  * every failure is caught and logged; the listener restarts itself;
  * kill switch: `[wake] enabled = false` in ~/.wispr/config.toml (or
    WISPR_WAKE=0). Default is off.

Mutes itself while dictation or the assistant key is held, while another app
is capturing the microphone (calls: Zoom, FaceTime, Meet, Teams, WhatsApp all
open the mic), and after the pause phrase until the resume phrase or the
assistant key.

Config (all optional):
    [wake]
    enabled = true
    phrases = ["hey coop"]          # what wakes it; add spelling variants
    sensitivity = 0.6               # 0..1, higher = easier to wake
    followup_s = 18                 # follow-up window after a reply
    endpoint_s = 0.9                # silence that ends a request
    max_s = 30                      # hard cap per request
    ignore_mic_apps = []            # bundle ids whose mic use is not a call
    aliases = []                    # extra spellings STT uses for the name
    device = ""                     # input name substring; "" = Wispr's (system default)

The listener never holds a Bluetooth microphone open: doing so forces the
headset into its 8 kHz call profile for as long as it runs. If the default
input is Bluetooth it uses the first wired/USB input instead, or stays idle.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import WISPR_HOME, load_config

SR = 16000
CHUNK = 1600                      # 100 ms
PREROLL_WAKE_S = 2.2              # audio before the fire kept for STT (holds the phrase)
PREROLL_FOLLOW_S = 0.6
RING_CHUNKS = 80                  # 8 s of raw audio kept for pre-roll
GREETINGS = {"hey", "hi", "hay", "okay", "ok", "yo", "hello"}
NO_SPEECH_S = 5.0                 # after a bare wake phrase, wait this long for the request
AFTER_FIRE_S = 0.15               # speech this long after the fire counts as the request
ECHO_TAIL_S = 0.5                 # room tail after the assistant stops talking
ANSWER_WAIT_S = 90.0              # give up waiting for the reply after this
FALLBACK_SPOKE_S = 12.0
SPEAKING_MAX_S = 45.0              # longest Coop reply before we assume the note was lost           # overlay without speaking/spoke notes: assume done after this
CALL_POLL_S = 2.0
AGC_TARGET = 0.06                 # speech RMS the spotter hears best
MODEL_DIR = WISPR_HOME / "wake"
KWS_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
PAUSE_FILE = WISPR_HOME / "wake-paused"
DEBUG_FILE = WISPR_HOME / "wake-debug"   # touch to log levels every 5 s
FILLERS = {"hey", "hi", "hay", "okay", "ok", "oh", "um", "uh", "so", "and", "yo", "hello"}


def log(msg: str) -> None:
    print(f"[wispr] wake: {msg}", flush=True)


# --------------------------------------------------------------- config

@dataclass
class WakeConfig:
    enabled: bool = False
    phrases: list[str] = field(default_factory=lambda: ["hey coop"])
    sensitivity: float = 0.6
    followup_s: float = 18.0
    endpoint_s: float = 0.9
    max_s: float = 30.0
    ignore_mic_apps: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    device: str = ""

    @property
    def name(self) -> str:
        """The assistant's name: the last word of the first phrase."""
        return (self.phrases[0].split() or ["assistant"])[-1].lower()

    @property
    def resume_phrase(self) -> str:
        return f"{self.name} start listening"

    def thresholds(self, strict: bool = False) -> tuple[float, float]:
        """(boost, threshold) for the spotter. Measured on synthetic speech:
        sensitivity 0.6 -> boost 2.7 / threshold 0.12 caught 76 % of
        wake phrases at 4x quieter levels with ~2 % look-alike fires, all
        of which the transcript check then drops."""
        s = min(1.0, max(0.0, self.sensitivity - (0.35 if strict else 0.0)))
        return round(1.5 + 2.0 * s, 2), round(max(0.03, 0.3 - 0.3 * s), 3)


def load_wake_config() -> WakeConfig:
    raw = load_config().get("wake", {}) or {}
    c = WakeConfig()
    env = os.environ.get("WISPR_WAKE", "").strip().lower()
    c.enabled = bool(raw.get("enabled", False)) if env == "" else env in ("1", "true", "on", "yes")
    ph = [str(p).strip().lower() for p in raw.get("phrases", []) if str(p).strip()]
    if ph:
        c.phrases = ph
    for k in ("sensitivity", "followup_s", "endpoint_s", "max_s"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            setattr(c, k, float(v))
    c.ignore_mic_apps = [str(x) for x in raw.get("ignore_mic_apps", [])]
    c.aliases = [str(x).lower() for x in raw.get("aliases", [])]
    c.device = str(raw.get("device", "")).strip()
    return c


# ------------------------------------------------- transcript handling

def sound_key(word: str) -> str:
    """A crude sound-alike key: c/k/q merge, vowel runs collapse, y and w
    count as vowels. coop/koop/coup/co-op -> kVp; cooper -> kVpVr;
    cool -> kVl. Exact key match only, so the name's consonants matter."""
    w = re.sub(r"[^a-z]", "", word.lower())
    w = w.replace("ph", "f").replace("ck", "k")
    w = re.sub(r"[cq]", "k", w)
    w = re.sub(r"[aeiouyw]+", "V", w)
    return re.sub(r"(.)\1+", r"\1", w)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", text)


def strip_wake(text: str, cfg: WakeConfig) -> tuple[bool, str]:
    """Find the wake phrase at the start of a transcript.

    Returns (found, rest-of-text). The name must appear within the first
    four words, preceded only by greeting/filler words; what follows it is
    the request, kept verbatim ("Hey Coop, what's next" -> "what's next")."""
    names = {sound_key(p.split()[-1]) for p in cfg.phrases if p.split()}
    names |= {sound_key(a) for a in cfg.aliases}
    lead = {w for p in cfg.phrases for w in p.split()[:-1]} | FILLERS
    for m in list(re.finditer(r"[A-Za-z][A-Za-z'\-]*", text))[:4]:
        w = m.group(0)
        if sound_key(w) in names:
            rest = text[m.end():].lstrip(" ,.!?;:-—").strip()
            return True, rest[:1].upper() + rest[1:] if rest else ""
        if w.lower() not in lead:
            return False, text.strip()
    return False, text.strip()


def is_pause(text: str, cfg: WakeConfig) -> bool:
    """'<name>, stop listening' (name optional)."""
    w = [x.lower() for x in _words(text)]
    if w and sound_key(w[0]) in {sound_key(cfg.name)} | {sound_key(a) for a in cfg.aliases}:
        w = w[1:]
    return w[:2] == ["stop", "listening"] and len(w) <= 4


def is_resume(text: str, cfg: WakeConfig) -> bool:
    w = [x.lower() for x in _words(text)]
    if w and sound_key(w[0]) == sound_key(cfg.name):
        w = w[1:]
    return w[:2] in (["start", "listening"], ["resume", "listening"]) and len(w) <= 4


# ------------------------------------------------------- state machine

class Machine:
    """Pure state machine; the listener feeds it events and runs the
    actions it returns. No audio, no I/O — unit tested in tests/test_wake.py.

    States: idle | capture | busy (transcribing) | answering (waiting for
    the reply to finish) | followup (window open, no wake phrase needed).
    Mutes (held, call, paused) sit on top of every state.

    Actions: ("listen",) ("cancel",) ("start", source, preroll_s)
             ("end", source) ("abort",) ("say", text)
    """

    def __init__(self, cfg: WakeConfig) -> None:
        self.cfg = cfg
        self.state = "idle"
        self.source = ""
        self.held = False
        self.call = False
        self.paused = False
        self.speaking = False
        self.saw_speaking_note = False
        self.speaking_since = 0.0
        self.spoke_at = 0.0
        self.fire_t = 0.0
        self.cap_t = 0.0
        self.last_speech = 0.0
        self.speech_seen = False
        self.window_until = 0.0
        self.answer_t = 0.0

    @property
    def muted(self) -> bool:
        return self.held or self.call

    def wants_vad(self) -> bool:
        return (not self.muted and not self.paused and not self.speaking
                and self.state in ("capture", "followup"))

    def strict(self) -> bool:
        """Coop is talking: only a clear wake phrase may interrupt."""
        return self.speaking

    # -- mute inputs
    def set_held(self, on: bool, t: float) -> list:
        if on == self.held:
            return []
        self.held = on
        if on and self.state in ("capture", "followup", "busy", "answering"):
            # the key owns the mic and the overlay now; drop ours quietly
            was = self.state
            self.state = "idle"
            return [("abort",)] if was == "capture" else []
        return []

    def set_call(self, on: bool, t: float) -> list:
        if on == self.call:
            return []
        self.call = on
        if on and self.state != "idle":
            was = self.state
            self.state = "idle"
            if was == "capture":
                return [("abort",), ("cancel",)]
        return []

    # -- spotter
    def keyword(self, kind: str, t: float) -> list:
        """kind: 'wake' or 'resume'."""
        if self.muted:
            return []
        if self.paused:
            if kind == "resume":
                self.paused = False
                return [("say", "I'm listening.")]
            return []
        if kind != "wake" or self.state in ("capture", "busy"):
            return []
        self.state, self.source = "capture", "wake"
        self.fire_t = self.cap_t = t
        self.speech_seen, self.last_speech = False, t
        self.speaking = False           # `listen` stops Coop mid-word
        return [("listen",), ("start", "wake", PREROLL_WAKE_S)]

    # -- VAD (only fed when wants_vad())
    def vad(self, speech: bool, t: float) -> list:
        if not self.wants_vad():
            return []
        if self.state == "followup":
            if speech and t >= self.spoke_at + ECHO_TAIL_S:
                self.state, self.source = "capture", "followup"
                self.fire_t = self.cap_t = t
                self.speech_seen, self.last_speech = True, t
                return [("listen",), ("start", "followup", PREROLL_FOLLOW_S)]
            return []
        if speech:
            self.last_speech = t
            if t >= self.fire_t + AFTER_FIRE_S:
                self.speech_seen = True
        return []

    # -- clock
    def tick(self, t: float) -> list:
        # Never trust the overlay to say it finished: a missed `spoke` note
        # left the spotter in its strict over-Coop mode for hours (10-14).
        if self.speaking and t - self.speaking_since >= SPEAKING_MAX_S:
            return self.spoke(t)
        if self.state == "capture":
            if t - self.cap_t >= self.cfg.max_s:
                return self._end()
            if self.speech_seen and t - self.last_speech >= self.cfg.endpoint_s:
                return self._end()
            if not self.speech_seen and t - self.cap_t >= NO_SPEECH_S:
                # a bare wake phrase and nothing after it: still transcribe
                # the pre-roll, it may hold "hey coop what's next" said fast
                return self._end()
        elif self.state == "followup" and t >= self.window_until:
            self.state = "idle"
        elif self.state == "answering":
            if not self.saw_speaking_note and t - self.answer_t >= FALLBACK_SPOKE_S:
                return self.spoke(t)
            if t - self.answer_t >= ANSWER_WAIT_S:
                self.state = "idle"
                self.speaking = False
        return []

    def _end(self) -> list:
        self.state = "busy"
        return [("end", self.source)]

    # -- results from the pipeline
    def heard(self, ok: bool, t: float) -> list:
        """ok: text was posted to the overlay. False: nothing usable."""
        if self.state != "busy":
            return []
        if ok:
            self.state, self.answer_t = "answering", t
            return []
        self.state = "idle"
        return [("cancel",)]

    def pause(self, t: float) -> list:
        self.paused = True
        self.state = "idle"
        return [("say", f"Okay, I've stopped listening. Say {self.cfg.name}, start listening, to bring me back.")]

    # -- overlay notes
    def speaking_note(self, t: float) -> list:
        self.saw_speaking_note = True
        self.speaking = True
        self.speaking_since = t
        return []

    def spoke(self, t: float) -> list:
        was_speaking = self.speaking or self.state == "answering"
        self.speaking = False
        self.spoke_at = t
        if was_speaking and not self.paused and not self.muted and self.state in ("idle", "answering", "followup"):
            self.state = "followup"
            self.window_until = t + self.cfg.followup_s
        return []

    def coop_key(self, t: float) -> list:
        """The assistant key was pressed (a `listen` we did not post)."""
        acts = []
        if self.paused:
            self.paused = False
            log("resumed by the assistant key")
        if self.state == "capture":
            acts.append(("abort",))
        self.state = "idle"
        self.speaking = False
        return acts


# --------------------------------------------------------- mic users

def other_mic_users(ignore: set[str]) -> list[str]:
    """Bundle ids / pids of other processes capturing audio input right now
    (CoreAudio process objects, macOS 14+). Any call app shows up here."""
    import ctypes
    import ctypes.util
    import struct
    global _CA, _CF
    if _CA is None:
        _CA = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
        _CF = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        _CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

    class Addr(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

    def cc(s: str) -> int:
        return struct.unpack(">I", s.encode())[0]

    def get(obj, sel, ctype, n=1):  # noqa: ANN001
        a = Addr(cc(sel), cc("glob"), 0)
        size = ctypes.c_uint32(ctypes.sizeof(ctype) * n)
        buf = (ctype * n)()
        st = _CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(a), 0, None,
                                            ctypes.byref(size), buf)
        return st, buf, size.value // ctypes.sizeof(ctype)

    a = Addr(cc("prs#"), cc("glob"), 0)
    sz = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(sz)) != 0:
        return []
    _, objs, n = get(1, "prs#", ctypes.c_uint32, max(1, sz.value // 4))
    me, out = os.getpid(), []
    for o in list(objs)[:n]:
        _, run, _ = get(o, "piri", ctypes.c_uint32)
        if not run[0]:
            continue
        _, pid, _ = get(o, "ppid", ctypes.c_int32)
        if pid[0] == me:
            continue
        _, ref, _ = get(o, "pbid", ctypes.c_void_p)
        b = ctypes.create_string_buffer(256)
        if ref[0]:
            _CF.CFStringGetCString(ref[0], b, 256, 0x08000100)
        bid = b.value.decode() or f"pid {pid[0]}"
        if bid not in ignore:
            out.append(bid)
    return out


_CA = None
_CF = None


def bluetooth_device_names() -> set[str]:
    """Names of CoreAudio devices on a Bluetooth transport."""
    import ctypes
    import struct
    other_mic_users(set())          # loads the frameworks
    cc = lambda s: struct.unpack(">I", s.encode())[0]  # noqa: E731

    class Addr(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

    a = Addr(cc("dev#"), cc("glob"), 0)
    sz = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(sz)) != 0:
        return set()
    n = sz.value // 4
    ids = (ctypes.c_uint32 * max(1, n))()
    _CA.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(sz), ids)
    out = set()
    for dev in list(ids)[:n]:
        t = ctypes.c_uint32(0)
        s = ctypes.c_uint32(4)
        ta = Addr(cc("tran"), cc("glob"), 0)
        if _CA.AudioObjectGetPropertyData(dev, ctypes.byref(ta), 0, None, ctypes.byref(s), ctypes.byref(t)) != 0:
            continue
        if t.value not in (cc("blue"), cc("blea")):
            continue
        ref = ctypes.c_void_p()
        s = ctypes.c_uint32(ctypes.sizeof(ref))
        na = Addr(cc("lnam"), cc("glob"), 0)
        if _CA.AudioObjectGetPropertyData(dev, ctypes.byref(na), 0, None, ctypes.byref(s), ctypes.byref(ref)) == 0 and ref:
            b = ctypes.create_string_buffer(256)
            _CF.CFStringGetCString(ref, b, 256, 0x08000100)
            out.add(b.value.decode())
    return out


def pick_input(want: str, devices: list[dict], default_idx: int, bluetooth: set[str]) -> int | None:
    """Index of the input to listen on. `want` (substring) wins if present
    and not Bluetooth; else the default input unless it is Bluetooth; else
    the first wired input. None: nothing safe to hold open."""
    ins = [(i, d["name"]) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    safe = [(i, n) for i, n in ins if n not in bluetooth and "iphone" not in n.lower()]
    if want:
        for i, n in safe:
            if want.lower() in n.lower():
                return i
    for i, n in safe:
        if i == default_idx:
            return i
    return safe[0][0] if safe else None


# ----------------------------------------------------------- listener

class Listener:
    """Runs the spotter, VAD and machine against a live microphone and
    drives the overlay through the daemon's helpers."""

    def __init__(self, daemon, cfg: WakeConfig, prefix: str) -> None:  # noqa: ANN001
        self.d = daemon
        self.cfg = cfg
        self.prefix = prefix
        self.m = Machine(cfg)
        self.m.paused = PAUSE_FILE.exists()
        self.q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
        self.ring: list[np.ndarray] = []
        self.cap: list[np.ndarray] = []
        self.capturing = False
        self.stream = None
        self.rate = SR
        self.last_audio = time.time()
        self._lock = threading.Lock()
        self.stats = {"fires": 0, "rejected": 0, "sent": 0}
        self._end_t = 0.0
        self._listen_sent_at = 0.0
        self._first_audio_pending = False
        # second detector: a local Whisper check on the start of each utterance
        self._vad2 = None
        self._onset: float | None = None
        self._checked = False
        self._last_sp = 0.0
        self._onset_busy = False
        self._onset_hit: tuple | None = None

    # -- models
    def _spotter(self, strict: bool):
        import sentencepiece as spm
        import sherpa_onnx
        d = MODEL_DIR / KWS_NAME
        sp = spm.SentencePieceProcessor(model_file=str(d / "bpe.model"))
        boost, thr = self.cfg.thresholds(strict)
        lines = []
        for ph in self.cfg.phrases:
            lines.append(" ".join(sp.encode(ph.upper(), out_type=str)) + f" :{boost} #{thr} @WAKE")
        lines.append(" ".join(sp.encode(self.cfg.resume_phrase.upper(), out_type=str)) + f" :{boost} #{thr} @RESUME")
        kw = MODEL_DIR / ("keywords-strict.txt" if strict else "keywords.txt")
        kw.write_text("\n".join(lines) + "\n")
        sfx = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        return sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"), encoder=str(d / f"encoder-{sfx}"),
            decoder=str(d / f"decoder-{sfx}"), joiner=str(d / f"joiner-{sfx}"),
            keywords_file=str(kw), num_threads=1, keywords_score=boost,
            keywords_threshold=thr, num_trailing_blanks=2, max_active_paths=4)

    def _vad(self):
        import sherpa_onnx
        c = sherpa_onnx.VadModelConfig()
        c.silero_vad.model = str(MODEL_DIR / "silero_vad.onnx")
        c.silero_vad.threshold = 0.45
        c.silero_vad.min_silence_duration = 0.25
        c.silero_vad.min_speech_duration = 0.2
        c.sample_rate = SR
        c.num_threads = 1
        return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=40)

    # -- second detector: Whisper on utterance onsets
    # The 3.3M spotter caught 1 of 2 real "Hey Coop"s from Ary at any
    # sensitivity (10-14 recording); base.en on the first ~2 s of each
    # utterance caught 2 of 2 in ~220 ms. Runs only on speech onsets after
    # a pause (music and steady talk cost nothing), only while idle.
    def _onset_step(self, chunk: np.ndarray, t: float) -> None:
        if not getattr(self.cfg, "onsets", True) or self._vad2 is None:
            return
        hit, self._onset_hit = self._onset_hit, None
        if hit is not None:
            with self._lock:
                if self.m.state == "idle" and not (self.m.muted or self.m.paused or self.m.speaking):
                    log(f"spotted WAKE (onset check: {hit[1]!r})")
                    acts = self.m.keyword("wake", t)
                    pre = min(RING_CHUNKS * CHUNK / SR, t - hit[0] + 0.4)
                    acts = [("start", "wake", max(PREROLL_WAKE_S, pre)) if a[0] == "start" else a
                            for a in acts]
                    self._run(acts)
            self._onset = None
            return
        with self._lock:
            idle = self.m.state == "idle" and not (self.m.muted or self.m.paused or self.m.speaking)
        if not idle:
            self._onset = None
            return
        self._vad2.accept_waveform(chunk)
        sp = bool(self._vad2.is_speech_detected())
        while not self._vad2.empty():
            self._vad2.pop()
        if sp:
            if self._onset is None and t - self._last_sp >= 0.5:
                self._onset, self._checked = t - 0.3, False   # silero reports late
            self._last_sp = t
        if self._onset is not None and not self._checked:
            dur = t - self._onset
            ended = not sp and t - self._last_sp >= 0.3
            if dur >= 2.2 or (ended and dur >= 0.5):
                self._checked = True
                if not self._onset_busy:
                    n = int(min(dur + 0.4, 2.8) * SR / CHUNK) + 1
                    clip = np.concatenate(self.ring[-n:])
                    self._onset_busy = True
                    threading.Thread(target=self._check_onset, args=(clip, self._onset),
                                     daemon=True, name="wispr-wake-onset").start()
        if self._onset is not None and not sp and t - self._last_sp >= 0.6:
            self._onset = None

    def _check_onset(self, clip: np.ndarray, onset: float) -> None:
        try:
            m = _onset_model()
            rms = float(np.sqrt(np.mean(clip * clip))) if clip.size else 0.0
            g = max(1.0, min(8.0, 0.08 / max(rms, 1e-4)))
            segs, _ = m.transcribe(np.clip(clip * g, -1, 1).astype(np.float32), language="en",
                                   beam_size=1, vad_filter=False, initial_prompt="Hey Coop.",
                                   condition_on_previous_text=False, without_timestamps=True)
            txt = " ".join(x.text.strip() for x in segs).strip()
            w = [x.lower() for x in _words(txt)]
            names = {sound_key(p.split()[-1]) for p in self.cfg.phrases if p.split()}
            names |= {sound_key(a) for a in self.cfg.aliases}
            if len(w) >= 2 and w[0] in GREETINGS and sound_key(w[1]).rstrip("V") in names:
                self._onset_hit = (onset, txt)
            elif DEBUG_FILE.exists():
                log(f"debug: onset heard {txt!r}")
        except Exception as e:
            log(f"onset model: {e.__class__.__name__}: {e}")
        finally:
            self._onset_busy = False

    # -- level
    def _agc(self, chunk: np.ndarray) -> np.ndarray:
        """Bring soft speech up to the level the spotter was trained on.
        A soft voice a metre from the mic sits ~4-10x below it and the
        spotter misses it (measured 2026-10-13: 0/1 at 1x, 1/1 at 4x).
        Gain follows the loudest of the last ~1.5 s, capped at max_gain,
        so loud speech is never clipped and silence isn't pumped forever."""
        rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
        env = getattr(self, "_env", [])
        env.append(rms)
        if len(env) > 15:
            del env[:-15]
        self._env = env
        peak = max(env) if env else 0.0
        g = min(self.cfg_max_gain(), AGC_TARGET / max(peak, 1e-4))
        g = max(1.0, g)
        return np.clip(chunk * g, -1.0, 1.0).astype(np.float32)

    def cfg_max_gain(self) -> float:
        return float(getattr(self.cfg, "max_gain", 8.0) or 8.0)

    # -- audio
    def _cb(self, indata, frames, t, status) -> None:  # noqa: ANN001
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def _open(self) -> None:
        import sounddevice as sd

        cb = self._cb

        try:
            bt = bluetooth_device_names()
        except Exception:
            bt = set()
        devs = list(sd.query_devices())
        dev = pick_input(self.cfg.device, devs, sd.default.device[0], bt)
        if dev is None:
            raise RuntimeError("no wired input (only Bluetooth) — not holding a headset mic open")
        self.device_name = devs[dev]["name"]
        for attempt in range(2):
            if self._try_open(dev if attempt == 0 else None, devs):
                return
            self._reset_portaudio()
            devs = list(self._sd().query_devices())
            try:
                bt = bluetooth_device_names()
            except Exception:
                bt = set()
            dev = pick_input(self.cfg.device, devs, self._sd().default.device[0], bt)
            if dev is None:
                break
            self.device_name = devs[dev]["name"]
            if self._try_open(dev, devs):
                return
        raise RuntimeError("no input stream")

    def _sd(self):
        import sounddevice as sd
        return sd

    def _reset_portaudio(self) -> None:
        """PortAudio caches the device list per process: after a device
        appears/disappears (a Bluetooth speaker connecting) every open fails
        with -9986 until it is re-initialised. Only while dictation is idle."""
        for _ in range(50):
            if not getattr(self.d, "recording", False):
                break
            time.sleep(0.2)
        else:
            return
        try:
            sd = self._sd()
            sd._terminate()
            time.sleep(0.15)
            sd._initialize()
            log("PortAudio re-initialised (device list changed)")
        except Exception as e:
            log(f"PortAudio reinit failed: {e.__class__.__name__}: {e}")

    def _try_open(self, dev, devs) -> bool:  # noqa: ANN001
        if dev is None:
            return False
        sd = self._sd()
        cb = self._cb
        for rate in (SR, None):
            try:
                if rate is None:
                    rate = int(devs[dev]["default_samplerate"])
                self.stream = sd.InputStream(device=dev, samplerate=rate, channels=1, dtype="float32",
                                             blocksize=int(rate * CHUNK / SR), callback=cb)
                self.stream.start()
                self.rate = rate
                log(f"mic open at {rate} Hz ({self.device_name})")
                return True
            except Exception as e:
                log(f"mic open at {rate} failed: {e.__class__.__name__}: {e}")
        return False

    def _close(self) -> None:
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    # -- notes
    def note(self, kind: str, text: str = "") -> bool:
        from .daemon import _coop_note
        if kind == "listen":
            self._listen_sent_at = time.time()
        return _coop_note(kind, text, source="wake")

    def on_note(self, kind: str, info: dict | None) -> None:
        t = time.time()
        with self._lock:
            if kind == "speaking":
                acts = self.m.speaking_note(t)
                if self._end_t and t - self._end_t < 120:
                    log(f"latency: end of speech -> Coop answering {int((t - self._end_t) * 1000)} ms")
                    self._first_audio_pending = True
            elif kind == "spoke":
                acts = self.m.spoke(t)
                if self.m.state == "followup":
                    log(f"follow-up window open {self.cfg.followup_s:.0f}s")
            elif kind == "listen":
                if (info or {}).get("source") == "wake" or t - self._listen_sent_at < 0.3:
                    return
                acts = self.m.coop_key(t)
                self._clear_pause_file()
            else:
                return
            self._run(acts)

    def _observe(self) -> None:
        """Distributed notifications from the overlay, on our own runloop."""
        try:
            import objc
            from Foundation import NSDate, NSDistributedNotificationCenter, NSObject, NSRunLoop

            global _OBS_CLASS
            if _OBS_CLASS is None:
                class WisprWakeObserver(NSObject):
                    def on_(self, n):  # noqa: N802, ANN001
                        try:
                            kind = str(n.name()).rsplit(".", 1)[-1]
                            info = dict(n.userInfo()) if n.userInfo() else None
                            if _LISTENER is not None:
                                _LISTENER.on_note(kind, info)
                        except Exception as e:
                            log(f"note handler: {e.__class__.__name__}: {e}")
                _OBS_CLASS = WisprWakeObserver
            if _OBS_STARTED:
                return  # one observer thread per process; it always targets the live listener
            _set_obs_started()
            obs = _OBS_CLASS.alloc().init()
            c = NSDistributedNotificationCenter.defaultCenter()
            for k in ("speaking", "spoke", "listen"):
                c.addObserver_selector_name_object_(obs, objc.selector(obs.on_, signature=b"v@:@"),
                                                    f"{self.prefix}.{k}", None)
            self._obs = obs
            rl = NSRunLoop.currentRunLoop()
            # A runloop with no input source returns from runMode at once, so the
            # loop below would spin a full core. A Mach port keeps it blocking.
            from Foundation import NSMachPort
            rl.addPort_forMode_(NSMachPort.port(), "kCFRunLoopDefaultMode")
            while True:
                if not rl.runMode_beforeDate_("kCFRunLoopDefaultMode",
                                              NSDate.dateWithTimeIntervalSinceNow_(5.0)):
                    time.sleep(1.0)  # belt and braces: never busy-wait
        except Exception as e:
            log(f"notification observer stopped: {e.__class__.__name__}: {e} "
                f"(falling back to timed follow-up)")

    # -- actions
    def _run(self, acts: list) -> None:
        for a in acts:
            try:
                k = a[0]
                if k == "listen":
                    self.note("listen")
                    self.d.indicator.set_coop(True)
                    self.d.indicator.set("listening", "Coop")
                elif k == "cancel":
                    self.note("cancel")
                    self.d.indicator.hide()
                elif k == "start":
                    n = int(a[2] * SR / CHUNK)
                    self.cap = list(self.ring[-n:]) if n else []
                    self.capturing = True
                    self.stats["fires"] += a[1] == "wake"
                    log(f"{a[1]} -> capturing")
                elif k == "end":
                    audio = np.concatenate(self.cap) if self.cap else np.zeros(0, np.float32)
                    self.capturing, self.cap = False, []
                    self._end_t = time.time()
                    threading.Thread(target=self._process, args=(audio, a[1]), daemon=True,
                                     name="wispr-wake-stt").start()
                elif k == "abort":
                    self.capturing, self.cap = False, []
                    log("capture dropped (key or call took over)")
                elif k == "say":
                    self.note("say", a[1])
            except Exception as e:
                log(f"action {a[0]} failed: {e.__class__.__name__}: {e}")

    def _process(self, audio: np.ndarray, source: str) -> None:
        ok = False
        try:
            text = transcribe_for_coop(self.d, audio)
            found, rest = strip_wake(text, self.cfg)
            log(f"heard ({source}, {audio.size / SR:.1f}s): {text!r}")
            if source == "wake" and not found:
                self.stats["rejected"] += 1
                log("no wake phrase in the transcript — dropped as a false wake")
                rest = ""
            if rest and is_pause(rest, self.cfg):
                with self._lock:
                    self._run(self.m.pause(time.time()))
                self._write_pause_file()
                self.d.indicator.hide()
                return
            if rest:
                ok = self.note("hear", rest)
                self.stats["sent"] += ok
                self.d.indicator.set("coop" if ok else "error",
                                     f"{len(rest.split())}w" if ok else "Coop isn't running")
        except Exception as e:
            log(f"pipeline failed: {e.__class__.__name__}: {e}")
        finally:
            with self._lock:
                self._run(self.m.heard(ok, time.time()))
            if not ok:
                try:
                    self.d.indicator.hide()
                except Exception:
                    pass

    def pause_from_key(self) -> None:
        """'<name>, stop listening' said through the assistant key."""
        with self._lock:
            self._run(self.m.pause(time.time()))
        self._write_pause_file()

    def _write_pause_file(self) -> None:
        try:
            PAUSE_FILE.write_text(time.strftime("%Y-%m-%d %H:%M\n"))
        except OSError:
            pass
        log(f"paused — say '{self.cfg.resume_phrase}' or press the assistant key")

    def _clear_pause_file(self) -> None:
        try:
            PAUSE_FILE.unlink()
        except OSError:
            pass

    # -- main loop
    def run(self) -> None:
        threading.Thread(target=self._observe, daemon=True, name="wispr-wake-notes").start()
        kws = {False: self._spotter(False), True: self._spotter(True)}
        vad = self._vad()
        self._vad2 = self._vad()
        streams = {k: v.create_stream() for k, v in kws.items()}
        self._open()
        next_poll = 0.0
        was_muted = False
        log(f"listening for {self.cfg.phrases} (sensitivity {self.cfg.sensitivity}, "
            f"resume: '{self.cfg.resume_phrase}'{', PAUSED' if self.m.paused else ''})")
        while True:
            try:
                chunk = self.q.get(timeout=1.0)
            except queue.Empty:
                chunk = None
            t = time.time()
            if chunk is None:
                if t - self.last_audio > 5.0:
                    log("mic went quiet (stream wedged?) — reopening")
                    self._close()
                    time.sleep(0.5)
                    self._open()
                    self.last_audio = time.time()
                continue
            self.last_audio = t
            if self.rate != SR:
                from .daemon import _resample_to_16k
                chunk = _resample_to_16k(chunk, self.rate)
            self.ring.append(chunk)
            if len(self.ring) > RING_CHUNKS:
                del self.ring[:-RING_CHUNKS]
            if self.capturing:
                self.cap.append(chunk)
            with self._lock:
                acts = self.m.set_held(bool(getattr(self.d, "recording", False)), t)
                if t >= next_poll:
                    next_poll = t + CALL_POLL_S
                    try:
                        users = other_mic_users(set(self.cfg.ignore_mic_apps))
                    except Exception:
                        users = []
                    if bool(users) != self.m.call:
                        log(f"{'muted: mic in use by ' + ', '.join(users) if users else 'unmuted: mic free'}")
                    acts += self.m.set_call(bool(users), t)
                muted = self.m.muted
                strict = self.m.strict()
                want_vad = self.m.wants_vad()
                if self._first_audio_pending and self.m.speaking and t - self._end_t < 120:
                    if float(np.sqrt(np.mean(chunk * chunk))) > 0.01:
                        log(f"latency: end of speech -> Coop's voice in the room "
                            f"{int((t - self._end_t) * 1000)} ms")
                        self._first_audio_pending = False
            self._run(acts)
            if muted:
                was_muted = True
                continue
            if was_muted:
                streams = {k: v.create_stream() for k, v in kws.items()}
                vad.reset()
                was_muted = False
            k, s = kws[strict], streams[strict]
            chunk = self._agc(chunk)
            if DEBUG_FILE.exists():
                self._dbg_peak = max(getattr(self, "_dbg_peak", 0.0), float(np.abs(chunk).max()))
                if t - getattr(self, "_dbg_t", 0.0) > 5.0:
                    log(f"debug: state={self.m.state} strict={strict} peak(after gain)={self._dbg_peak:.3f}")
                    self._dbg_t, self._dbg_peak = t, 0.0
            s.accept_waveform(SR, chunk)
            hit = ""
            while k.is_ready(s):
                k.decode_stream(s)
                r = k.get_result(s)
                if r:
                    hit = r
                    k.reset_stream(s)
            with self._lock:
                if hit:
                    log(f"spotted {hit}{' (over Coop)' if strict else ''}")
                    acts = self.m.keyword("resume" if hit == "RESUME" else "wake", t)
                    if hit == "RESUME" and acts:
                        self._clear_pause_file()
                    self._run(acts)
                    want_vad = self.m.wants_vad()
                if want_vad:
                    vad.accept_waveform(chunk)
                    self._run(self.m.vad(bool(vad.is_speech_detected()), t))
                    while not vad.empty():
                        vad.pop()
                self._run(self.m.tick(t))
            try:
                self._onset_step(chunk, t)
            except Exception as e:
                log(f"onset check: {e.__class__.__name__}: {e}")


def transcribe_for_coop(daemon, audio: np.ndarray) -> str:  # noqa: ANN001
    """Same engines as an assistant-key take: cloud STT first with the local
    model decoding in parallel as the fallback, same vocabulary prompt, then
    the local rules cleanup (no LLM pass — the assistant is one already)."""
    from . import cleanup, cloud_stt, dictionary as dict_mod
    from .daemon import _STT_POOL
    if audio.size < SR * 0.3:
        return ""
    dur = audio.size / SR
    fut = None
    if cloud_stt.enabled():
        try:
            prompt = cloud_stt.build_prompt(dict_mod.prompt_terms(
                daemon.vocab, daemon.corrections, daemon.dictionary, limit=150))
            fut = _STT_POOL.submit(cloud_stt.transcribe, audio, prompt)
        except Exception as e:
            log(f"cloud submit failed: {e}")
    local = _STT_POOL.submit(daemon._local_transcribe, audio, None)
    raw = None
    if fut is not None:
        try:
            raw, why = fut.result(timeout=cloud_stt.wait_s(dur))
            if not raw:
                log(f"cloud stt -> local ({why})")
        except Exception:
            raw = None
    if not raw:
        raw = local.result()
    if not raw:
        return ""
    text, _ = cleanup.clean_text(raw, "default", daemon.vocab, daemon.corrections,
                                 daemon.snippets, llm_enabled=False)
    return text


_LISTENER: Listener | None = None
_ONSET_MODEL = None


def _onset_model():
    """faster-whisper base.en, int8, 2 threads; loaded on first onset."""
    global _ONSET_MODEL
    if _ONSET_MODEL is None:
        from faster_whisper import WhisperModel
        _ONSET_MODEL = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=2)
    return _ONSET_MODEL
_OBS_CLASS = None
_OBS_STARTED = False


def _set_obs_started() -> None:
    global _OBS_STARTED
    _OBS_STARTED = True


def listener() -> Listener | None:
    return _LISTENER


def start(daemon, prefix: str = "com.aryaman.coop.overlay") -> None:  # noqa: ANN001
    """Start the wake listener in the background if enabled. Never raises;
    restarts the listener with backoff if it dies."""
    global _LISTENER
    try:
        cfg = load_wake_config()
    except Exception as e:
        log(f"config unreadable, staying off: {e}")
        return
    if not cfg.enabled:
        log("off ([wake] enabled = false)")
        return
    if not (MODEL_DIR / KWS_NAME / "tokens.txt").exists() or not (MODEL_DIR / "silero_vad.onnx").exists():
        log(f"models missing under {MODEL_DIR} — run `wispr wake-setup`; staying off")
        return

    def loop() -> None:
        global _LISTENER
        delay = 2.0
        while True:
            try:
                _LISTENER = Listener(daemon, cfg, prefix)
                _LISTENER.run()
            except Exception as e:
                log(f"listener died: {e.__class__.__name__}: {e} — restarting in {delay:.0f}s")
                try:
                    if _LISTENER is not None:
                        _LISTENER._close()
                except Exception:
                    pass
            time.sleep(delay)
            delay = min(10.0, delay * 2)

    threading.Thread(target=loop, daemon=True, name="wispr-wake").start()


def intercept_hear(text: str) -> bool:
    """Called for every assistant-key `hear`: True if it was the pause
    phrase and has been handled here (so it must not reach the overlay)."""
    lst = _LISTENER
    if lst is None or not text:
        return False
    if is_pause(text, lst.cfg):
        lst.pause_from_key()
        return True
    return False


def setup_models() -> None:
    """Download the spotter + VAD models (~6 MB, Apache-2.0 / MIT) into
    ~/.wispr/wake. Idempotent."""
    import tarfile
    import urllib.request
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    base = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
    if not (MODEL_DIR / KWS_NAME / "tokens.txt").exists():
        tgz = MODEL_DIR / "kws.tar.bz2"
        urllib.request.urlretrieve(f"{base}/kws-models/{KWS_NAME}.tar.bz2", tgz)
        with tarfile.open(tgz) as t:
            t.extractall(MODEL_DIR, filter="data")
        tgz.unlink()
    if not (MODEL_DIR / "silero_vad.onnx").exists():
        urllib.request.urlretrieve(f"{base}/asr-models/silero_vad.onnx", MODEL_DIR / "silero_vad.onnx")
    print(f"wake models ready in {MODEL_DIR}")
