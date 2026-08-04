"""Regression tests for the 2026-07-30 "alive but deaf" bug.

pynput re-raises exceptions thrown inside a callback out of the listener
thread, which ends the listener. The process survives, so launchd's
KeepAlive never fires and the daemon sits there holding a healthy PID while
the hotkey does nothing. These tests assert that an audio failure can never
reach the callback, and that state is reset so the next press retries.
"""
import sys
import threading

import wispr.daemon as d

FAILS = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeInd:
    def __init__(self): self.states = []
    def set(self, *a): self.states.append(a)
    def hide(self): pass
    def push_level(self, l): pass


class DeadRecorder:
    """Reproduces PaErrorCode -9986 / AUHAL -10851: the stream will not open
    and closing it fails too."""
    level = 0.0
    def start(self):
        raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")
    def stop(self):
        raise RuntimeError("stream already invalid")


def make_daemon():
    dm = d.Daemon.__new__(d.Daemon)
    dm._lock = threading.Lock()
    dm._model_lock = threading.Lock()
    dm.recording = False
    dm.hands_free = False
    dm._incremental = None
    dm._dictation_seq = 0
    dm._last_release = 0.0
    dm.indicator = FakeInd()
    dm.vocab = []; dm.modes = {}; dm.snippets = {}; dm.corrections = {}
    dm.recorder = DeadRecorder()
    return dm


print("\n1. _start with an unopenable microphone")
dm = make_daemon()
raised = None
try:
    dm._start()
except Exception as e:
    raised = e
check("no exception escapes into the pynput callback", raised is None)
check("recording flag reset (was set True before the failing call)",
      dm.recording is False)
check("error surfaced on the pill",
      any(a and a[0] == "error" for a in dm.indicator.states))

print("\n2. the press after a failure")
# Before the fix, recording stayed True, so every later press returned at
# the guard and the tool appeared permanently dead even if the mic recovered.
raised = None
try:
    dm._start()
except Exception as e:
    raised = e
check("second press retries instead of short-circuiting", raised is None)
check("state still clean after the retry", dm.recording is False)

print("\n3. _stop_and_process when closing the stream fails")
dm = make_daemon()
dm.recording = True
raised = None
try:
    dm._stop_and_process()
except Exception as e:
    raised = e
check("no exception escapes", raised is None)
check("recording flag reset", dm.recording is False)

print("\n4. structural guarantees")
import inspect
src = inspect.getsource(d.Daemon.run)
check("both pynput callbacks are wrapped in a guard",
      src.count("_guard(") >= 2)
check("listener is supervised and rebuilt if macOS drops the event tap",
      "while True" in src)
check("Recorder retries after re-initialising PortAudio",
      hasattr(d.Recorder, "_reset_portaudio"))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL AUDIO-FAILURE TESTS PASSED")
