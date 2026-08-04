"""Regression tests for the 2026-08-04 "text appears, then vanishes" bug.

_revise_in_place used to send `_send_backspaces(len(pasted))` and only
THEN `paste_text(improved)` -- destructive first. When the second half
failed, or the backspace run walked past the start of the field (the macOS
error beep the user reported), the text was simply gone from the document.
The documented recovery -- "the improved text is on the clipboard, so
Cmd-V recovers" -- was false: paste_text spawned a thread that restored
the previous clipboard 0.6 s later, so by the time the user reached for
Cmd-V the dictation had been overwritten there too. The text existed
nowhere.

These tests assert the properties that make that impossible now:

  1. nothing is destroyed before the replacement is on the clipboard
  2. no restore thread may overwrite text that is the only copy left
  3. the transcript reaches history BEFORE the paste is attempted, and
     exactly once per dictation
  4. every original guard still refuses to fire when it cannot prove it
     owns the cursor

Nothing here sends a real keystroke, touches the real clipboard, or writes
to the real ~/.wispr -- a test that touches live user data is a bug in
the test.
"""
import os

# Before importing anything from wispr: pin the hotkey backend (a config
# with [hotkey] key = "fn" sends Daemon.run() into a real Quartz tap) and
# point WISPR_HOME at a scratch dir.
os.environ.setdefault("WISPR_HOTKEY", "right_option")
os.environ.setdefault("WISPR_HOME", "/tmp/wispr-test-home")

import sys           # noqa: E402
import threading     # noqa: E402
import time          # noqa: E402
import types         # noqa: E402

import numpy as np   # noqa: E402

import wispr.daemon as d      # noqa: E402
import wispr.history as history  # noqa: E402

FAILS = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# ---- a fake pasteboard: the real one belongs to the user ---------------
CLIP = {"text": None}
d._pasteboard_read = lambda: CLIP["text"]
d._pasteboard_write = lambda text: CLIP.__setitem__("text", text)
d._send_cmd_v = lambda: None
d._activate_target = lambda app=None: None
d._frontmost_pid = lambda: 4242  # same app still frontmost by default
# Real timings would make this suite take half a minute of sleeping.
d.RESTORE_DELAY_S = 0.05
d.RESTORE_GIVE_UP_S = 0.4
SETTLE = d.RESTORE_DELAY_S + d.RESTORE_GIVE_UP_S + 0.35

# ---- config-independent knobs -----------------------------------------
# paste.revise defaults to OFF now, and the user's own config sets it
# false. These tests are about what the revision does when enabled.
d.paste_revise = lambda: True
d.paste_instant = lambda: True
d.revise_window_s = lambda: 2.5
d.revise_max_chars = lambda: 1200

# ---- fake history: never the real history.jsonl ------------------------
STORE = []
# STORE is cleared between scenarios; this one only ever goes up, so the
# "did we actually stub it?" check at the end cannot be fooled by a reset.
WRITES = {"n": 0}


def fake_append(app, mode, raw, cleaned, path, latency_ms, stages=None,
                rec_id=None):
    WRITES["n"] += 1
    rid = rec_id or f"rec{len(STORE)}"
    STORE.append({"id": rid, "app": app, "mode": mode, "raw": raw,
                  "cleaned": cleaned, "path": path, "latency_ms": latency_ms})
    return rid


def fake_update(rec_id, **fields):
    WRITES["n"] += 1
    for r in STORE:
        if r["id"] == rec_id:
            r.update(fields)
            return
    STORE.append(dict(fields, id=rec_id))   # would mean a lost record


history.append = fake_append
history.update = fake_update
d.history = history


class FakeInd:
    def __init__(self): self.states = []
    def set(self, *a): self.states.append(a)
    def hide(self): pass
    def push_level(self, level): pass


class FakeApp:
    def __init__(self, pid=4242): self._pid = pid
    def processIdentifier(self): return self._pid


def make_daemon():
    dm = d.Daemon.__new__(d.Daemon)
    dm._lock = threading.Lock()
    dm._model_lock = threading.Lock()
    dm.recording = False
    dm.hands_free = False
    dm._incremental = None
    dm._dictation_seq = 1
    dm.indicator = FakeInd()
    dm.vocab = []; dm.modes = {}; dm.snippets = {}; dm.corrections = {}
    dm.recorder = types.SimpleNamespace(level=0.0, _capture_rate=16000,
                                        start=lambda: None, stop=lambda: None)
    dm.model = types.SimpleNamespace(
        transcribe=lambda *a, **k: [types.SimpleNamespace(text="hello world")])
    return dm


def done_future(value):
    f = __import__("concurrent.futures", fromlist=["Future"]).Future()
    f.set_result(value)
    return f


def reset(user_clipboard=None):
    """Fresh clipboard state between scenarios."""
    CLIP["text"] = user_clipboard
    d._clip.update({"gen": 0, "text": None, "prior": None,
                    "armed": 0, "pending": 0})
    STORE.clear()


def boom_cmd_v():
    raise RuntimeError("Accessibility denied")


def boom_paste(text, app=None):
    raise RuntimeError("cmd-v never landed")


REAL_PASTE = d.paste_text
BASE = "the rules version of the text"
BETTER = "the improved version"


print("\n1. paste_text cannot lose the text it is pasting")
reset("USER CLIPBOARD")
REAL_PASTE("dictated text", None)
check("the dictation is on the clipboard the moment the paste is issued",
      CLIP["text"] == "dictated text")
time.sleep(SETTLE)
check("the user's own clipboard comes back after a successful paste",
      CLIP["text"] == "USER CLIPBOARD")

reset("USER CLIPBOARD")
d._send_cmd_v = boom_cmd_v
raised = None
try:
    REAL_PASTE("dictated text", None)
except Exception as e:
    raised = e
d._send_cmd_v = lambda: None
check("a failed keystroke is reported, not swallowed",
      isinstance(raised, RuntimeError))
check("the dictation is on the clipboard after a failed paste",
      CLIP["text"] == "dictated text")
check("that clipboard generation is armed as a recovery buffer",
      d._clip["armed"] == d._clip["gen"] and d._clip["armed"] != 0)
time.sleep(SETTLE)
check("no restore thread overwrote the only remaining copy",
      CLIP["text"] == "dictated text")


print("\n2. the replacement is staged before anything is destroyed")
ORDER = []
d._send_backspaces = lambda n: ORDER.append(("backspace", n, CLIP["text"]))
d.paste_text = lambda text, app=None: ORDER.append(("paste", text))
reset("USER CLIPBOARD")
dm = make_daemon()
dm._revise_in_place(done_future(BETTER), BASE, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
check("the revision ran at all", [k for k, *_ in ORDER] == ["backspace", "paste"])
check("the replacement was already on the clipboard before the FIRST "
      "backspace", ORDER and ORDER[0][2] == BETTER)
check("it backspaces exactly the characters it pasted",
      ORDER and ORDER[0][1] == len(BASE))
check("and then pastes the improved text", ORDER[-1] == ("paste", BETTER))


print("\n3. a revision that fails mid-flight stays recoverable")
reset("USER CLIPBOARD")
d._send_backspaces = lambda n: None
d.paste_text = REAL_PASTE
# Exactly what _process does: instant paste, then hold restores while the
# revision is in flight.
REAL_PASTE(BASE, None)               # schedules a restore for gen 1
d._begin_revision_window()
d.paste_text = boom_paste            # the revise paste blows up
dm = make_daemon()
dm._revise_in_place(done_future(BETTER), BASE, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
d._end_revision_window()
check("the improved text is on the clipboard after the failure",
      CLIP["text"] == BETTER)
check("it is armed, so nothing may restore over it",
      d._clip["armed"] == d._clip["gen"])
check("the pill tells the user how to get it back",
      any(a[0] == "error" and "Cmd-V" in a[1] for a in dm.indicator.states))
time.sleep(SETTLE)
check("the instant paste's restore thread did NOT clobber it",
      CLIP["text"] == BETTER)


print("\n4. the transcript reaches history before the paste is attempted")
d.modes_mod = types.SimpleNamespace(
    bundle_id_of=lambda app: "com.apple.Notes",
    mode_for=lambda b, m: "neutral",
    frontmost_app=lambda: None)
d.vocab_mod = types.SimpleNamespace(initial_prompt=lambda v, c: "")
PENDING = {"fut": None}
d.cleanup = types.SimpleNamespace(
    instant_result=lambda raw, mode, v, c, s: ("Hello world.", "rules",
                                               PENDING["fut"]),
    clean_text=lambda *a, **k: ("Hello world.", "rules"),
    await_revision=lambda fut, t: ((fut.result(), "haiku") if fut is not None
                                   else (None, "rules")))
AUDIO = np.zeros(16000, dtype="float32")

reset("USER CLIPBOARD")
PENDING["fut"] = None
d.paste_text = boom_paste
dm = make_daemon()
dm._process(AUDIO, None, None)
check("history has the transcription even though the paste raised",
      len(STORE) == 1 and STORE[0]["cleaned"] == "Hello world.")
check("the raw transcript is kept too",
      len(STORE) == 1 and STORE[0]["raw"] == "hello world")
check("the record is marked as a paste error",
      len(STORE) == 1 and STORE[0]["path"] == "paste_error")
check("one dictation, one history entry — not one per attempt",
      len(STORE) == 1)

reset("USER CLIPBOARD")
PENDING["fut"] = done_future("Hello, world!")
d.paste_text = lambda text, app=None: None
d._send_backspaces = lambda n: None
dm = make_daemon()
dm._process(AUDIO, FakeApp(), None)
check("still exactly one history entry after a successful revision",
      len(STORE) == 1)
check("the entry was revised in place to the improved text",
      len(STORE) == 1 and STORE[0]["cleaned"] == "Hello, world!")
check("and no longer reads as a paste error",
      len(STORE) == 1 and STORE[0]["path"] == "haiku")


print("\n5. the original guards still refuse to touch the keyboard")
sent = {"backspaces": 0, "pasted": []}
d._send_backspaces = lambda n: sent.__setitem__("backspaces",
                                                sent["backspaces"] + n)
d.paste_text = lambda text, app=None: sent["pasted"].append(text)


def guard_case(name, **kw):
    """Run one guard scenario and assert nothing was typed."""
    reset("USER CLIPBOARD")
    sent["backspaces"] = 0
    sent["pasted"] = []
    dm = make_daemon()
    for k, v in kw.get("daemon", {}).items():
        setattr(dm, k, v)
    dm._revise_in_place(done_future(kw.get("improved", BETTER)),
                        kw.get("pasted", BASE), kw.get("app", FakeApp()),
                        "com.apple.Notes", kw.get("mode", "neutral"), "raw",
                        kw.get("t0", time.time()))
    check(name, sent["backspaces"] == 0 and sent["pasted"] == [])
    check(name + " — and nothing was staged on the clipboard",
          CLIP["text"] == "USER CLIPBOARD")


guard_case("over the char limit -> does NOT revise", pasted="x" * 5000)
guard_case("technical mode does NOT revise (autocomplete/auto-indent)",
           mode="technical")
guard_case("recording again -> does NOT revise", daemon={"recording": True})
guard_case("outside the revise window -> does NOT revise",
           t0=time.time() - 60)
guard_case("identical text -> does NOT touch the keyboard", improved=BASE)

# The dictation counter has to move WHILE Haiku is out, not before the
# call: _revise_in_place snapshots it on entry and compares afterwards.
reset("USER CLIPBOARD")
sent["backspaces"] = 0; sent["pasted"] = []
dm = make_daemon()
_orig_await = d.cleanup.await_revision


def _bump_then_return(fut, timeout):
    dm._dictation_seq += 1
    return _orig_await(fut, timeout)


d.cleanup.await_revision = _bump_then_return
dm._revise_in_place(done_future(BETTER), BASE, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
d.cleanup.await_revision = _orig_await
check("a newer dictation owns the cursor -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])
check("newer dictation -> nothing was staged on the clipboard either",
      CLIP["text"] == "USER CLIPBOARD")

reset("USER CLIPBOARD")
sent["backspaces"] = 0; sent["pasted"] = []
d._frontmost_pid = lambda: 9999   # user switched apps
dm = make_daemon()
dm._revise_in_place(done_future(BETTER), BASE, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
d._frontmost_pid = lambda: 4242
check("focus moved to another app -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])
check("focus moved -> nothing was staged on the clipboard either",
      CLIP["text"] == "USER CLIPBOARD")

# New guard: macOS text substitution changes the character count, so the
# one-backspace-per-character assumption is no longer safe.
guard_case("text macOS may have substituted -> does NOT revise",
           pasted="wait -- I mean this...")

# revise is off by default now; the daemon must honour that.
reset("USER CLIPBOARD")
sent["backspaces"] = 0; sent["pasted"] = []
d.paste_revise = lambda: False
dm = make_daemon()
dm._revise_in_place(done_future(BETTER), BASE, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
d.paste_revise = lambda: True
check("paste.revise disabled -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])


print("\n6. the tests themselves stayed out of real user data")
check("WISPR_HOME points at a scratch dir",
      os.environ["WISPR_HOME"] == "/tmp/wispr-test-home")
check("the pasteboard was stubbed, not live", CLIP["text"] is not None)
check("history was stubbed, not live", WRITES["n"] >= 1)
import pathlib, json  # noqa: E402
_real = pathlib.Path.home() / ".wispr" / "history.jsonl"
_polluted = 0
if _real.exists():
    for _ln in _real.read_text(errors="ignore").splitlines():
        try:
            if json.loads(_ln).get("cleaned") in (BETTER, "Hello, world!",
                                                  "Hello world."):
                _polluted += 1
        except Exception:
            pass
check("no test fixtures leaked into ~/.wispr/history.jsonl", _polluted == 0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASTE-SAFETY TESTS PASSED")
