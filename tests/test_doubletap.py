"""Regression tests for double-tap hands-free.

The original handler committed a full transcribe/clean/paste cycle on the
FIRST release, synchronously, on the pynput callback thread. The second press
of a double-tap could not be delivered until that finished, so the 0.4 s
window had always expired by the time it arrived: hands-free was documented
in the README and impossible to trigger, and every attempt left a spurious
"heard nothing" pill from ~100 ms of audio.

These tests drive the real handlers -- pulled out of Daemon.run() through a
fake pynput Listener -- and assert the four behaviours that matter:

  1. a held press still stops on release, with no added latency
  2. two quick taps latch hands-free and keep recording
  3. a tap while hands-free stops and commits
  4. a lone quick tap still stops, just deferred

Timings are scaled off the real constants, so retuning them cannot silently
invalidate the suite.
"""
import os as _os; _os.environ["WISPR_CLOUD_STT"] = "0"  # tests never call the cloud
import os
import sys
import threading
import time
import types

# These tests drive the pynput code path through a fake Listener, so they
# must not be at the mercy of ~/.wispr/config.toml: with [hotkey] key =
# "fn" set, Daemon.run() goes into the Quartz tap loop instead and the
# suite hangs forever with no output. Pin the backend and point WISPR_HOME
# at a scratch dir so nothing here can read or write real user state.
os.environ.setdefault("WISPR_HOTKEY", "right_option")
os.environ.setdefault("WISPR_HOME", "/tmp/wispr-test-home")

FAILS = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# --- fake pynput, installed before wispr.daemon imports it ----------------
class _Key:
    alt_r = "alt_r"
    alt_l = "alt_l"


class _Listener:
    """Captures the handlers instead of hooking the real event tap."""
    captured = {}

    def __init__(self, on_press=None, on_release=None):
        _Listener.captured["press"] = on_press
        _Listener.captured["release"] = on_release

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def join(self):
        raise SystemExit  # break out of run()'s supervision loop

    @property
    def running(self):
        return True

    def stop(self):
        pass


fake = types.ModuleType("pynput")
fake.keyboard = types.SimpleNamespace(Key=_Key, Listener=_Listener)
sys.modules["pynput"] = fake
sys.modules["pynput.keyboard"] = fake.keyboard

import wispr.daemon as d  # noqa: E402


class FakeInd:
    def __init__(self):
        self.states = []

    def set(self, *a):
        self.states.append(a)

    def hide(self):
        pass

    def push_level(self, level):
        pass


def make_daemon():
    dm = d.Daemon.__new__(d.Daemon)
    dm._lock = threading.Lock()
    dm._model_lock = threading.Lock()
    dm.recording = False
    dm.hands_free = False
    dm._incremental = None
    dm._dictation_seq = 0
    dm._last_release = 0.0
    dm._press_t = 0.0
    dm._last_tap = 0.0
    dm._pending_stop = None
    dm.indicator = FakeInd()
    dm.vocab = []
    dm.modes = {}
    dm.snippets = {}
    dm.corrections = {}
    dm.dictionary = []; dm._edit_watcher = None
    dm._front_at_press = None; dm._front_at_press_t = 0.0

    dm.started = 0
    dm.stopped = 0

    def _start():
        dm.started += 1
        dm.recording = True

    def _stop():
        dm.stopped += 1
        dm.recording = False

    dm._start = _start
    dm._stop_and_process = _stop
    return dm


def handlers(dm):
    try:
        dm.run()
    except SystemExit:
        pass
    p, r = _Listener.captured["press"], _Listener.captured["release"]

    # Handlers now only stamp the time and queue the event; a worker does
    # the work. Give it a beat before asserting on state.
    def press(k):
        p(k); time.sleep(0.03)

    def release(k):
        r(k); time.sleep(0.03)
    return press, release


HELD = d.TAP_MAX_S + 0.15      # unambiguously a hold
TAP = d.TAP_MAX_S / 3          # unambiguously a tap
SETTLE = d.DOUBLE_TAP_S + 0.25  # long enough for a deferred stop to fire

print("\n1. a held press behaves exactly as before")
dm = make_daemon()
press, release = handlers(dm)
press(_Key.alt_r)
time.sleep(HELD)
release(_Key.alt_r)
check("recording started on press", dm.started == 1)
check("stopped immediately on release, not deferred", dm.stopped == 1)
check("no deferred stop left pending", dm._pending_stop is None)
check("did not enter hands-free", dm.hands_free is False)

print("\n2. double-tap latches hands-free")
dm = make_daemon()
press, release = handlers(dm)
press(_Key.alt_r)
time.sleep(TAP)
release(_Key.alt_r)          # first tap -- stop is deferred, not committed
check("still recording after the first tap", dm.recording is True)
check("first tap did NOT commit a dictation", dm.stopped == 0)
press(_Key.alt_r)            # second tap arrives inside the window
time.sleep(TAP)
release(_Key.alt_r)
check("hands-free engaged", dm.hands_free is True)
check("recording continued, was never restarted", dm.started == 1)
check("nothing was transcribed on the way in", dm.stopped == 0)
time.sleep(SETTLE)
check("deferred stop was cancelled, not merely delayed", dm.stopped == 0)
check("still recording after the window elapsed", dm.recording is True)

print("\n3. a tap while hands-free stops and commits")
press(_Key.alt_r)
time.sleep(TAP)
release(_Key.alt_r)
check("hands-free cleared", dm.hands_free is False)
check("dictation committed exactly once", dm.stopped == 1)
check("no stray timer left behind", dm._pending_stop is None)
time.sleep(SETTLE)
check("no second commit from a leftover timer", dm.stopped == 1)

print("\n4. a lone quick tap still works, just deferred")
dm = make_daemon()
press, release = handlers(dm)
press(_Key.alt_r)
time.sleep(TAP)
release(_Key.alt_r)
check("not committed instantly", dm.stopped == 0)
time.sleep(SETTLE)
check("committed once the window passed", dm.stopped == 1)
check("did not enter hands-free", dm.hands_free is False)

print("\n6. a slow mic start cannot turn a tap into a hold (2026-10-02)")
# Opening the mic can take ~0.5 s. The whole double-tap lands while
# _start() is still running; the key-ups queue behind it. Hold time must
# come from when the keys really moved, not when the worker got to them.
dm = make_daemon()
_raw_start = dm._start
def slow_start():
    time.sleep(0.5)
    _raw_start()
dm._start = slow_start
handlers(dm)
p, r = _Listener.captured["press"], _Listener.captured["release"]
p(_Key.alt_r); time.sleep(0.08); r(_Key.alt_r)
time.sleep(0.12)
p(_Key.alt_r); time.sleep(0.08); r(_Key.alt_r)
time.sleep(0.5 + SETTLE)
check("hands-free engaged despite the slow start", dm.hands_free is True)
check("first tap was NOT transcribed as push-to-talk", dm.stopped == 0)
check("still recording", dm.recording is True)

print("\n5. other keys are ignored")
dm = make_daemon()
press, release = handlers(dm)
press(_Key.alt_l)
release(_Key.alt_l)
check("left alt does nothing", dm.started == 0 and dm.stopped == 0)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL DOUBLE-TAP TESTS PASSED")
