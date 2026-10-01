"""Cloud speech-to-text — the accuracy upgrade (2026-10-01).

Why: base.en (74M params, CPU) is the accuracy ceiling on this Intel Mac.
A/B on the owner's own recordings (bench/ab_cloud.py, 2026-10-01):

    clip      base.en (local)          gpt-4o-transcribe (cloud)
    short     WER 0.375, 1311 ms       WER 0.125,  829 ms
    medium    WER 0.185, 1672 ms       WER 0.037, 1239 ms   (both proper nouns right)
    29 s      7072 ms                  1375 ms

So the cloud model is ~5x more accurate on the owner's voice AND faster on this
machine. It takes a free-text `prompt`, so the personal dictionary biases
recognition itself instead of being guessed back in by the cleanup step.

Fail-safe contract: the daemon always keeps the local model decoding in
parallel. Any error, timeout, missing key or implausible output here
returns None and the local transcript is used. Offline still works.

Key: env OPENAI_API_KEY, else macOS keychain item 'wispr-openai'.
"""
from __future__ import annotations

import io
import re
import subprocess
import threading
import time
import wave

import numpy as np

from .config import _bool_knob, _float_knob, load_config

KEYCHAIN_SERVICE = "wispr-openai"
URL = "https://api.openai.com/v1/audio/transcriptions"
SAMPLE_RATE = 16000

_client = None
_key: str | None = None
_lock = threading.Lock()


def enabled() -> bool:
    return _bool_knob("WISPR_CLOUD_STT", "stt", "cloud", True) and bool(api_key())


def model() -> str:
    v = str(load_config().get("stt", {}).get("cloud_model", "")).strip()
    return v or "gpt-4o-transcribe"


def speaker_hint() -> str:
    """Optional one-liner about the speaker, kept in the user's config so
    nothing personal lives in the repo, e.g.
        [stt]
        speaker = "Sam is dictating in Indian-accented English, often softly"
    """
    return str(load_config().get("stt", {}).get("speaker", "")).strip().rstrip(".")


def wait_s(duration_s: float) -> float:
    """How long to wait for the cloud before using the local transcript.
    Measured 0.7-1.4 s for 2-30 s clips; budget scales with length."""
    base = _float_knob("WISPR_CLOUD_WAIT_S", "stt", "cloud_wait_s", 4.0)
    return min(12.0, base + 0.06 * duration_s)


def api_key() -> str | None:
    global _key
    if _key:
        return _key
    import os
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s",
                                KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=3)
            k = r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            k = None
    _key = k or None
    return _key


def _get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import httpx
                _client = httpx.Client(
                    timeout=httpx.Timeout(20.0, connect=4.0),
                    limits=httpx.Limits(max_keepalive_connections=2,
                                        keepalive_expiry=600.0))
    return _client


def prewarm() -> None:
    """Open the TLS connection while the user is still speaking."""
    if not enabled():
        return

    def _w() -> None:
        try:
            _get_client().get("https://api.openai.com/v1/models",
                              headers={"Authorization": f"Bearer {api_key()}"},
                              timeout=5)
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


def normalize(audio: np.ndarray) -> np.ndarray:
    """Quiet-voice fix. Remove DC offset and, only when the take is quiet,
    lift it toward -3 dBFS (gain capped at +24 dB so room hiss isn't
    turned into words). Loud takes pass through untouched — measured: the
    model gains nothing from boosting audio that is already full-scale."""
    if audio.size == 0:
        return audio
    a = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(a)))
    if peak < 1e-4 or peak >= 0.35:
        return a.astype(np.float32)
    gain = min(0.7 / peak, 15.85)
    return np.clip(a * gain, -1.0, 1.0).astype(np.float32)


def _wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def build_prompt(terms: list[str], context: str = "") -> str:
    """The prompt reads like preceding text, which is how the model uses
    it: recent text first (continuity, casing), then the spelling list."""
    parts = [f"{speaker_hint() or 'Dictation'}. Spell names and products "
             "exactly as listed."]
    if terms:
        parts.append("Names and terms: " + ", ".join(terms[:150]) + ".")
    if context:
        parts.append("Previous text: " + context[-300:])
    return " ".join(parts)


_WORD = re.compile(r"[a-z0-9']+")


def plausible(text: str, prompt: str, duration_s: float) -> bool:
    """Reject the two known failure shapes of prompted cloud STT: echoing
    the prompt back, and inventing text on near-silence."""
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    if "names and terms:" in low or "spell names and products" in low:
        return False
    words = _WORD.findall(low)
    # ~6 words/s is faster than anyone speaks; far beyond it is invention.
    if duration_s > 0 and len(words) > 8 + 6 * duration_s:
        return False
    # Long runs copied verbatim from the term list = prompt leakage.
    plist = _WORD.findall(prompt.lower())
    if len(words) >= 8:
        grams = {" ".join(plist[i:i + 6]) for i in range(len(plist) - 5)}
        hits = sum(1 for i in range(len(words) - 5)
                   if " ".join(words[i:i + 6]) in grams)
        if hits >= 2:
            return False
    return True


def transcribe(audio: np.ndarray, prompt: str) -> tuple[str | None, str]:
    """Returns (text or None, reason). Never raises."""
    try:
        key = api_key()
        if not key:
            return None, "no key"
        dur = audio.size / SAMPLE_RATE
        if dur < 0.3:
            return None, "too short"
        a = normalize(audio)
        if float(np.sqrt(np.mean(np.square(a)))) < 2e-4:
            return None, "silence"
        data = {"model": model(), "language": "en",
                "response_format": "json", "temperature": "0"}
        if prompt:
            data["prompt"] = prompt
        t0 = time.time()
        r = _get_client().post(
            URL, headers={"Authorization": f"Bearer {key}"}, data=data,
            files={"file": ("dictation.wav", _wav_bytes(a), "audio/wav")})
        if r.status_code != 200:
            return None, f"http {r.status_code}: {r.text[:120]}"
        text = (r.json().get("text") or "").strip()
        if not plausible(text, prompt, dur):
            return None, "implausible output"
        return text, f"ok {int((time.time() - t0) * 1000)}ms"
    except Exception as e:
        return None, f"{e.__class__.__name__}: {str(e)[:100]}"
