"""Voice engine path (2026-11-05): the guards match the engine's shared contract,
a whisper loop is collapsed, and an unreachable engine reports itself as down
instead of raising into the daemon. Plain script, no pytest (see README)."""
import json
import os
import sys
from pathlib import Path

import numpy as np

from wispr import engine_stt, voice_engine

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("FAIL", msg)


vectors = Path.home() / "code" / "voice-engine" / "spec" / "guard-vectors.json"
if vectors.exists():
    for v in json.loads(vectors.read_text()):
        r = engine_stt.guard(v["in"], v["speech"], v["family"])
        check((r["text"], r["dropped"]) == (v["out"], v["dropped"]), f"vector {v['name']}: {r}")
else:
    print("skip: voice-engine repo not on this machine")

r = engine_stt.guard("I like... The note The note The note The note", None, "decoder")
check(r["text"] == "I like... The note", f"loop collapse: {r}")
check(engine_stt.guard("Thank you.", 0.0, "decoder")["dropped"], "silence thank-you dropped")

# Unreachable engine → disabled, and transcribe raises the typed error.
voice_engine.URL = "http://127.0.0.1:9"
engine_stt._health = (0.0, None)
check(engine_stt.status() is None, "status of a dead engine")
check(engine_stt.enabled() is False, "dead engine is disabled")
try:
    engine_stt.transcribe(np.zeros(1600, dtype="float32"))
    check(False, "transcribe on a dead engine should raise")
except engine_stt.EngineUnavailable:
    pass

os.environ["WISPR_ENGINE_STT"] = "0"
check(engine_stt.enabled() is False, "kill switch")

print("test_engine_stt:", "FAIL" if fails else "ok")
sys.exit(1 if fails else 0)
