"""Voice engine client + text guards for Python apps (dictation, agents, workers).

Copy this one file into any project. No dependencies beyond the standard library
(numpy optional for sending arrays).

    from voice_engine import transcribe, guard
    result = transcribe(samples)            # 16 kHz mono float32, via voiced on this Mac
    result = transcribe_file("meeting.m4a", profile="verbatim", diarize=True)
    clean = guard(text, speech_seconds=None, family="decoder")   # for any other model's output

The guard rules mirror Sources/VoiceText/Guards.swift; spec/guard-vectors.json is the contract
(run `python3 voice_engine.py --test path/to/guard-vectors.json`).
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request

URL = os.environ.get("VOICED_URL", "http://127.0.0.1:8178")

# ---------------------------------------------------------------- client


class EngineUnavailable(RuntimeError):
    pass


def _token() -> str:
    try:
        return open(os.path.expanduser("~/.voice-engine/token")).read().strip()
    except OSError:
        return ""


def _post(path: str, body: bytes, content_type: str, timeout: float,
          auth: bool = False) -> dict:
    headers = {"Content-Type": content_type, "X-Sample-Rate": "16000"}
    if auth:
        headers["Authorization"] = f"Bearer {_token()}"
    req = urllib.request.Request(URL + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise EngineUnavailable(f"voiced {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise EngineUnavailable(f"voiced unreachable: {e}") from e


def healthy(timeout: float = 0.5) -> dict | None:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=timeout) as r:
            d = json.loads(r.read())
            return d if d.get("ok") and d.get("models") else None
    except Exception:
        return None


def transcribe(samples, profile: str = "dictation", diarize: bool = False,
               timeout: float | None = None) -> dict:
    """16 kHz mono float32 samples (numpy array or bytes) → transcript dict:
    text, raw, model, audioSeconds, speechSeconds, ms, words[], turns[], notes[]."""
    if hasattr(samples, "astype"):
        body = samples.astype("<f4").tobytes()
    else:
        body = bytes(samples)
    secs = len(body) / 4 / 16000
    q = f"?profile={profile}" + ("&diarize=1" if diarize else "")
    return _post("/v1/transcribe" + q, body, "application/octet-stream",
                 timeout or (3.0 + secs / 15))


def transcribe_file(path: str, profile: str = "verbatim", diarize: bool = False,
                    timeout: float = 600) -> dict:
    body = json.dumps({"path": os.path.abspath(os.path.expanduser(path)),
                       "profile": profile, "diarize": diarize}).encode()
    return _post("/v1/transcribe-file", body, "application/json", timeout, auth=True)


def set_vocabulary(terms: list[str], aliases: dict[str, list[str]] | None = None,
                   timeout: float = 120) -> dict:
    body = json.dumps({"terms": terms, "aliases": aliases or {}}).encode()
    return _post("/v1/vocabulary", body, "application/json", timeout, auth=True)


# ---------------------------------------------------------------- guards

SILENCE_PHRASES = {
    "thanks for watching", "thank you for watching", "thanks for listening",
    "thank you for listening", "please subscribe", "like and subscribe",
    "subscribe to my channel", "we'll see you next time", "see you next time",
    "see you in the next video", "subtitles by the amara org community",
    "subtitles by the amaraorg community", "sous-titrage st' 501", "sous titrage st 501",
    "sous-titrage st 501", "transcribed by", "transcription by", "ding", "music", "applause",
    "you", "bye bye", "silence",
}
SHORT_REPLIES = {"thank you", "thank you very much", "thanks", "okay", "ok", "bye", "yes", "no",
                 "hmm"}
CREDIT_PATTERNS = [  # only text no one says aloud
    r"(?i)\bsous[- ]titrage(\s+st'?\s*\d+)?[.!?]?",
    r"(?i)\bsubtitles by the amara\.?org community[.!?]?",
]
NO_SPEECH_S = 0.2
SILENCE_PHRASE_MAX_SPEECH_S = 1.5


def key(s: str) -> str:
    out, last_space = [], True
    for ch in s.lower():
        if ch.isalnum() or ch in "'’":
            out.append("'" if ch == "’" else ch)
            last_space = False
        elif ch.isspace() or ch == "-":
            if not last_space:
                out.append(" ")
            last_space = True
    return "".join(out).strip()


_SILENCE_KEYS = {key(p) for p in SILENCE_PHRASES}
_SHORT_KEYS = {key(p) for p in SHORT_REPLIES}


def _trailing_punct(tok: str) -> str:
    i = len(tok)
    while i > 0 and unicodedata.category(tok[i - 1]).startswith("P") and tok[i - 1] != "'":
        i -= 1
    return tok[i:]


def collapse_loops(text: str, min_repeats: int = 4, max_n: int = 8) -> str:
    toks = text.split()
    if len(toks) < min_repeats:
        return text
    keys = [key(t) for t in toks]
    out, i = [], 0
    while i < len(toks):
        best_n = best_reps = 0
        n = 1
        while n <= max_n and i + n * min_repeats <= len(toks):
            unit = keys[i:i + n]
            if any(unit):
                reps = 1
                while i + (reps + 1) * n <= len(toks) and keys[i + reps * n:i + (reps + 1) * n] == unit:
                    reps += 1
                if reps >= min_repeats and n * reps > best_n * best_reps:
                    best_n, best_reps = n, reps
            n += 1
        if best_n:
            unit = toks[i:i + best_n]
            last = toks[i + best_n * best_reps - 1]
            p = _trailing_punct(unit[-1])
            unit[-1] = (unit[-1][:-len(p)] if p else unit[-1]) + _trailing_punct(last)
            out.extend(unit)
            i += best_n * best_reps
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out)


def guard(text: str, speech_seconds: float | None = None, family: str = "decoder") -> dict:
    """family: 'decoder' (Whisper, gpt-4o-transcribe) or 'transducer' (Parakeet)."""
    trimmed = " ".join((text or "").split())
    if not trimmed:
        return {"text": "", "dropped": False, "notes": []}
    # Decoders invent text on silence; a transducer's text goes only when the VAD heard nothing.
    if speech_seconds is not None and speech_seconds < (NO_SPEECH_S if family == "decoder" else 0.01):
        return {"text": "", "dropped": True, "notes": ["no speech in the audio"]}
    k = key(trimmed)
    phrase_limit = SILENCE_PHRASE_MAX_SPEECH_S if family == "decoder" else 0.5
    if k in _SILENCE_KEYS and (speech_seconds or 0) < phrase_limit:
        return {"text": "", "dropped": True, "notes": ["silence phrase"]}
    if (speech_seconds is not None and speech_seconds < 0.5 and k in _SHORT_KEYS
            and family == "decoder"):
        return {"text": "", "dropped": True, "notes": ["silence phrase"]}
    notes, out = [], trimmed
    if family == "decoder":
        stripped = out
        for p in CREDIT_PATTERNS:
            stripped = re.sub(p, "", stripped)
        stripped = " ".join(stripped.split())
        if stripped != out:
            notes.append("removed subtitle credit")
            out = stripped
    collapsed = collapse_loops(out, 3 if family == "decoder" else 4)
    if collapsed != out:
        notes.append("collapsed a repeating loop")
        out = collapsed
    if not out:
        return {"text": "", "dropped": True, "notes": notes}
    return {"text": out, "dropped": False, "notes": notes}


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        vectors = json.load(open(sys.argv[2]))
        bad = 0
        for v in vectors:
            r = guard(v["in"], v["speech"], v["family"])
            if r["text"] != v["out"] or r["dropped"] != v["dropped"]:
                bad += 1
                print(f"FAIL {v['name']}: {r}")
        print(f"{len(vectors) - bad}/{len(vectors)} guard vectors pass")
        sys.exit(1 if bad else 0)
    print(json.dumps(healthy() or {"ok": False}))
