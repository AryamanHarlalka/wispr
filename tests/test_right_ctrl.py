"""Right Control as the hotkey (Mac mini, 2026-10-13).

Right Control is also a modifier, so Ctrl+key shortcuts must keep working:
any key or click while it is held turns the press into a shortcut, which
throws the recording away without transcribing or pasting. Hold-to-talk
and double-tap hands-free must behave exactly as with Fn.

Drives the key-agnostic handlers directly -- the same calls the Quartz tap
worker makes -- with real event timestamps.
"""
import os as _os; _os.environ["WISPR_CLOUD_STT"] = "0"
import os, sys, threading, time
os.environ.setdefault("WISPR_HOTKEY", "right_option")
os.environ.setdefault("WISPR_HOME", "/tmp/wispr-test-home")

import wispr.daemon as d  # noqa: E402

FAILS = []
def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class FakeInd:
    def __init__(self): self.states, self.hidden = [], 0
    def set(self, *a): self.states.append(a)
    def hide(self): self.hidden += 1
    def push_level(self, level): pass


class FakeRec:
    def stop(self): return None


def make():
    dm = d.Daemon.__new__(d.Daemon)
    dm._lock = threading.Lock()
    dm.recording = False; dm.hands_free = False
    dm._incremental = None; dm._press_t = 0.0; dm._last_tap = 0.0
    dm._pending_stop = None; dm.indicator = FakeInd(); dm.recorder = FakeRec()
    dm.started = dm.stopped = 0
    def _start():
        dm.started += 1; dm.recording = True
    def _stop():
        dm.stopped += 1; dm.recording = False
    dm._start = _start; dm._stop_and_process = _stop
    return dm

HELD = d.TAP_MAX_S + 0.15
TAP = d.TAP_MAX_S / 3
SETTLE = d.DOUBLE_TAP_S + 0.25
t = time.time

print("\n1. hold Right Control, speak, release -> pastes")
dm = make()
t0 = t(); dm._hotkey_down(t0); dm._hotkey_up(t0 + HELD)
check("started once", dm.started == 1)
check("stopped and processed once", dm.stopped == 1)

print("\n2. Right Control + C is a shortcut, not a dictation")
dm = make()
t0 = t(); dm._hotkey_down(t0); dm._hotkey_combo(); dm._hotkey_up(t0 + HELD)
time.sleep(SETTLE)
check("nothing transcribed or pasted", dm.stopped == 0)
check("recording thrown away", dm.recording is False)
check("pill hidden", dm.indicator.hidden >= 1)

print("\n3. a quick Ctrl+C does not count as the first tap of a double-tap")
dm = make()
t0 = t(); dm._hotkey_down(t0); dm._hotkey_combo(); dm._hotkey_up(t0 + TAP)
t1 = t0 + TAP + 0.1; dm._hotkey_down(t1); dm._hotkey_up(t1 + TAP)
check("not hands-free", dm.hands_free is False)
time.sleep(SETTLE + 0.5)  # synthetic timestamps run ahead of the clock
check("the later lone tap behaves as a short press", dm.stopped == 1)

print("\n4. double-tap Right Control latches hands-free")
dm = make()
t0 = t(); dm._hotkey_down(t0); dm._hotkey_up(t0 + TAP)
t1 = t0 + TAP + 0.1; dm._hotkey_down(t1); dm._hotkey_up(t1 + TAP)
check("hands-free engaged", dm.hands_free is True)
check("recording kept running", dm.recording is True and dm.started == 1)

print("\n5. a Ctrl shortcut during hands-free leaves the session running")
t2 = t(); dm._hotkey_down(t2); dm._hotkey_combo(); dm._hotkey_up(t2 + TAP)
check("still hands-free", dm.hands_free is True)
check("still recording", dm.recording is True and dm.stopped == 0)

print("\n6. a plain tap then ends hands-free and commits")
t3 = t(); dm._hotkey_down(t3); dm._hotkey_up(t3 + TAP)
check("hands-free cleared", dm.hands_free is False)
check("committed once", dm.stopped == 1)

print("\n7. config: 'ctrl' maps to Left Control")
os.environ["WISPR_HOTKEY"] = "ctrl"
from wispr.config import hotkey, hotkey_label
check("hotkey() == left_ctrl", hotkey() == "left_ctrl")
check("label says Left Control", hotkey_label() == "Left Control")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS)); sys.exit(1)
print("ALL RIGHT-CONTROL TESTS PASSED")
