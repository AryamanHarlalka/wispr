"""F7 — history: append-only ~/.wispr/history.jsonl, local only.

2026-08-04: this file stopped being a log and became a safety net. The
daemon now writes the transcript here BEFORE it tries to paste it, so a
paste that fails — or a revision that eats the text off the screen —
cannot destroy the dictation; `wispr last` reads it straight back out.

That means a record has to be updatable after the fact (latency, the
revised text, "paste_error"). The file stays strictly append-only, because
rewriting it in place would open a window where a crash loses every past
dictation to save one field. A revision is simply another line carrying
the same "id" and only the fields that changed; readers fold by id, last
write wins. One dictation is still one entry in `wispr history`.
"""
from __future__ import annotations

import json
import time
import uuid

from .config import WISPR_HOME, ensure_home


def append(app: str, mode: str, raw: str, cleaned: str, path: str,
           latency_ms: int, stages: dict | None = None,
           rec_id: str | None = None) -> str:
    """Write a new record and return its id, for later update().

    `stages` is optional per-stage latency, e.g.
    {"whisper_ms": 840, "cleanup_ms": 310, "paste_ms": 260} — total_ms is
    latency_ms. Old records without it (and without an id) stay readable.
    """
    ensure_home()
    rid = rec_id or uuid.uuid4().hex
    rec = {
        "id": rid,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": app, "mode": mode, "raw": raw, "cleaned": cleaned,
        "path": path, "latency_ms": latency_ms,
    }
    if stages:
        rec["stages"] = {k: int(v) for k, v in stages.items()}
    with open(WISPR_HOME / "history.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rid


def update(rec_id: str, **fields) -> None:
    """Revise an existing record without appending a second dictation.

    Appends a partial line for the same id — see the module docstring for
    why this is not an in-place edit. `ts` is deliberately not touched, so
    an entry keeps the time it was spoken; the revision carries `rev_ts`.
    """
    ensure_home()
    rec: dict = {"id": rec_id, "rev_ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for k, v in fields.items():
        if k == "stages":
            rec["stages"] = {kk: int(vv) for kk, vv in (v or {}).items()}
        else:
            rec[k] = v
    with open(WISPR_HOME / "history.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _fold(records: list[dict]) -> list[dict]:
    """Merge revision lines into the record they revise, oldest first.

    Records written before ids existed have no "id" and are never merged
    with anything — folding them together on a shared missing key would
    silently collapse a user's older history.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for i, r in enumerate(records):
        rid = r.get("id") or f"__legacy_{i}"
        if rid in merged:
            merged[rid].update(r)
        else:
            merged[rid] = dict(r)
            order.append(rid)
    return [merged[k] for k in order]


def recent(n: int = 5) -> list[dict]:
    """The n most recent dictations, newest first, revisions folded in."""
    ensure_home()
    lines = (WISPR_HOME / "history.jsonl").read_text(
        errors="ignore").splitlines()
    # Over-read: a dictation can span several lines now (the pre-paste
    # record plus its revisions), so the last n lines are not the last n
    # dictations. 4x + 40 covers the worst realistic case cheaply.
    out = []
    for line in lines[-(max(n, 1) * 4 + 40):]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return list(reversed(_fold(out)[-n:]))


def last() -> dict | None:
    """The most recent dictation, or None if there are none. This is what
    `wispr last` prints and copies back to the clipboard."""
    got = recent(1)
    return got[0] if got else None
