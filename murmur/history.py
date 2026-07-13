"""F7 — history: append-only ~/.murmur/history.jsonl, local only."""
from __future__ import annotations

import json
import time

from .config import MURMUR_HOME, ensure_home


def append(app: str, mode: str, raw: str, cleaned: str, path: str,
           latency_ms: int) -> None:
    ensure_home()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app": app, "mode": mode, "raw": raw, "cleaned": cleaned,
        "path": path, "latency_ms": latency_ms,
    }
    with open(MURMUR_HOME / "history.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent(n: int = 5) -> list[dict]:
    ensure_home()
    lines = (MURMUR_HOME / "history.jsonl").read_text().splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return list(reversed(out))
