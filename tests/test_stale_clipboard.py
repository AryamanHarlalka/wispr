"""Regression: an old dictation stuck on the clipboard must never be
restored over (and so never pasted instead of) a newer dictation."""
import os as _os; _os.environ["WISPR_CLOUD_STT"] = "0"
import time
import wispr.daemon as d

CLIP = {"text": "OLD DICTATION about sign-in pages"}
d._pasteboard_read = lambda: CLIP["text"]
d._pasteboard_write = lambda t: CLIP.__setitem__("text", t)
d._send_cmd_v = lambda: None
d._activate_target = lambda app=None: None
d._front_is = lambda app=None: True
d.RESTORE_DELAY_S = 0.05
fails = []

def check(name, ok):
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok: fails.append(name)

# 1. default: restore is off -> the new dictation stays on the clipboard
d.clipboard_restore = lambda: False
d.paste_text("NEW dictation")
time.sleep(0.3)
check("restore off: clipboard keeps the new dictation", CLIP["text"] == "NEW dictation")

# 2. restore on, but the 'prior' clipboard is one of our own past dictations
d.clipboard_restore = lambda: True
d._clip.update(gen=0, text=None, prior=None, armed=0, pending=0)  # as after a restart
CLIP["text"] = "OLD DICTATION about sign-in pages"
d._recent_dictations = lambda: {"OLD DICTATION about sign-in pages"}
d.paste_text("SECOND dictation")
time.sleep(0.4)
check("restore on: a stale dictation is never put back", CLIP["text"] == "SECOND dictation")

# 3. restore on, genuine user clipboard still comes back
d._clip.update(gen=0, text=None, prior=None, armed=0, pending=0)
CLIP["text"] = "https://user-copied.example"
d._recent_dictations = lambda: set()
d.paste_text("THIRD dictation")
time.sleep(0.4)
check("restore on: the user's real clipboard is restored", CLIP["text"] == "https://user-copied.example")

if fails:
    raise SystemExit(f"{len(fails)} FAILURE(S): " + ", ".join(fails))
print("ALL STALE-CLIPBOARD TESTS PASSED")
