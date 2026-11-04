"""On-device voice engine as Wispr's primary speech path (2026-11-05).

`voiced` (~/code/voice-engine) keeps Parakeet-family models warm on the Neural
Engine and answers on 127.0.0.1:8178. Dictation uses its `dictation` profile
(Bark 3.0 Clean: drops fillers, false starts and stutters in the model itself).

Why it replaced cloud-first: the cloud model took 1.5–6.4 s (p90 8.5 s with
cleanup), timed out under load, and every timeout fell back to base.en, which
looped ("The note The note…", "Semi, Semi…") and invented "Thank you." on
silence. A transducer model can't do either, and it answers in ~0.2 s offline.

Cloud and base.en remain the fallback, only when voiced is down.
Kill switch: [stt] engine = false in ~/.wispr/config.toml, then `wispr restart`.
"""
from __future__ import annotations

import threading
import time

from . import voice_engine
from .config import _bool_knob

EngineUnavailable = voice_engine.EngineUnavailable
guard = voice_engine.guard

_health: tuple[float, dict | None] = (0.0, None)
_refreshing = False
_STALE_S = 5.0


def _refresh() -> None:
    global _health, _refreshing
    try:
        h = voice_engine.healthy(timeout=0.4)
    except Exception:
        h = None
    _health = (time.time(), h)
    _refreshing = False


def status(block: bool = False) -> dict | None:
    """voiced's /health. Never blocks the hotkey path: a stale answer is refreshed in the
    background and the last known one is returned (block=True waits, max 0.4 s)."""
    global _refreshing
    t, h = _health
    if time.time() - t >= _STALE_S:
        if block:
            _refresh()
            return _health[1]
        if not _refreshing:
            _refreshing = True
            threading.Thread(target=_refresh, daemon=True).start()
    return h


def enabled() -> bool:
    if not _bool_knob("WISPR_ENGINE_STT", "stt", "engine", True):
        return False
    if _health[0] == 0.0:  # first use since start: one probe (instant when voiced is down)
        return status(block=True) is not None
    return status() is not None


def transcribe(audio) -> dict:
    """16 kHz mono float32 → transcript dict. Raises EngineUnavailable."""
    global _health
    try:
        r = voice_engine.transcribe(audio, profile="dictation")
        _health = (time.time(), _health[1] or {"ok": True})
        return r
    except EngineUnavailable:
        _health = (time.time(), None)
        raise
