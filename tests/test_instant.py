"""Guard tests for instant paste + in-place revision.

These assert the SAFE behaviour: that a revision refuses to fire whenever
it cannot prove it owns the cursor. A false negative costs slightly rougher
text; a false positive deletes the user's own characters.
"""
import os
import sys
import threading
import time
import types

# Same reasoning as test_doubletap: never read or write real user state.
os.environ.setdefault("WISPR_HOTKEY", "right_option")
os.environ.setdefault("WISPR_HOME", "/tmp/wispr-test-home")

import wispr.cleanup as cleanup
import wispr.daemon as d
import wispr.history as history

FAILS = []

# Do NOT write to the real ~/.wispr/history.jsonl. _revise_in_place() calls
# history.append() on the happy path, and an earlier version of this file let
# that through -- writing fake dictations into the user's actual history.
# A test that touches live user data is a bug in the test.
_history_writes = []
history.append = lambda *a, **k: _history_writes.append(a)
d.history = history


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeInd:
    def __init__(self): self.states = []
    def set(self, *a): self.states.append(a)
    def hide(self): pass
    def push_level(self, l): pass


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
    dm.recorder = types.SimpleNamespace(level=0.0, start=lambda: None,
                                        stop=lambda: None)
    return dm


def done_future(value):
    f = __import__("concurrent.futures", fromlist=["Future"]).Future()
    f.set_result(value)
    return f


# ---- track whether keystrokes were sent -------------------------------
sent = {"backspaces": 0, "pasted": []}
d._send_backspaces = lambda n: sent.__setitem__("backspaces", sent["backspaces"] + n)
d.paste_text = lambda text, app=None: sent["pasted"].append(text)
d._frontmost_pid = lambda: 4242  # same app still frontmost by default
# 2026-08-04: the revision now stages the replacement on the clipboard
# before it deletes anything, and re-asserts focus first. Both are real
# system calls, so stub them for the same reason history.append is stubbed
# above -- an unstubbed run would rewrite the user's actual clipboard.
d._pasteboard_read = lambda: None
d._pasteboard_write = lambda text: None
d._activate_target = lambda app=None: None
# paste.revise now defaults to OFF (see config.paste_revise). These tests
# are about what the revision does when it is enabled, so state that here
# instead of inheriting whatever the user's config happens to say.
d.paste_revise = lambda: True


print("\n1. instant_result splits correctly")
long_text = "so um i was thinking we should probably ship the thing on friday ok"
txt, path, fut = cleanup.instant_result(long_text, "neutral", [], {}, {},
                                        llm_enabled=False)
check("long + no LLM -> rules, no future", path == "rules" and fut is None and txt)
txt, path, fut = cleanup.instant_result("hello there", "neutral", [], {}, {})
check("short -> rules, no future", path == "rules" and fut is None)
txt, path, fut = cleanup.instant_result("my email", "neutral", [], {},
                                        {"my email": "me@example.com"})
check("snippet -> snippet, no future", path == "snippet"
      and txt == "me@example.com" and fut is None)

print("\n2. await_revision never raises")
bad = __import__("concurrent.futures", fromlist=["Future"]).Future()
bad.set_exception(RuntimeError("api exploded"))
got, p = cleanup.await_revision(bad, 0.2)
check("failed future -> (None, rules)", got is None and p == "rules")
got, p = cleanup.await_revision(None, 0.2)
check("no future -> (None, rules)", got is None)
got, p = cleanup.await_revision(done_future("  better text "), 0.2)
check("good future -> stripped text", got == "better text" and p == "haiku")

print("\n3. revision guards")
base = "the rules version of the text"

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm._revise_in_place(done_future("the improved version"), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
check("happy path DOES revise", sent["backspaces"] == len(base)
      and sent["pasted"] == ["the improved version"])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm._revise_in_place(done_future("the improved version"), base, FakeApp(),
                    "com.apple.Terminal", "technical", "raw", time.time())
check("technical mode does NOT revise (autocomplete/auto-indent)",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm._dictation_seq = 5  # a newer dictation started after we captured seq
orig_await = cleanup.await_revision
def bump_then_return(fut, timeout):
    dm._dictation_seq += 1
    return orig_await(fut, timeout)
cleanup.await_revision = bump_then_return
dm._revise_in_place(done_future("the improved version"), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
cleanup.await_revision = orig_await
check("new dictation started -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
d._frontmost_pid = lambda: 9999  # user switched apps
dm._revise_in_place(done_future("the improved version"), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
d._frontmost_pid = lambda: 4242
check("focus moved to another app -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
huge = "x" * 5000
dm._revise_in_place(done_future("improved"), huge, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
check("over the char limit -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm._revise_in_place(done_future(base), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
check("identical text -> does NOT touch the keyboard",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm._revise_in_place(done_future("improved"), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw",
                    time.time() - 60)  # window long expired
check("outside the revise window -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])

dm = make_daemon()
sent["backspaces"] = 0; sent["pasted"] = []
dm.recording = True  # user already holding the key again
dm._revise_in_place(done_future("improved"), base, FakeApp(),
                    "com.apple.Notes", "neutral", "raw", time.time())
check("recording again -> does NOT revise",
      sent["backspaces"] == 0 and sent["pasted"] == [])

print("\n4. the tests themselves stayed out of real user data")
check("history.append was stubbed, not live", len(_history_writes) >= 1)
import pathlib, json
_real = pathlib.Path.home() / ".wispr" / "history.jsonl"
_polluted = 0
if _real.exists():
    for _ln in _real.read_text(errors="ignore").splitlines():
        try:
            if json.loads(_ln).get("cleaned") == "the improved version":
                _polluted += 1
        except Exception:
            pass
check("no test fixtures leaked into ~/.wispr/history.jsonl", _polluted == 0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL GUARD TESTS PASSED")
