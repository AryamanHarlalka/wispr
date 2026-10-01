"""Quality regressions (2026-09-06): the rules layer must never eat words,
the learner must only learn real mishearings, and incremental decoding
must stay bounded no matter how long the dictation is.

Run:  PYTHONPATH=$PWD .venv/bin/python tests/test_quality.py
"""
import os as _os; _os.environ["WISPR_CLOUD_STT"] = "0"  # tests never call the cloud
import os
import sys
import tempfile
import threading
import time
import types

os.environ.setdefault("WISPR_HOTKEY", "right_option")
os.environ["WISPR_HOME"] = tempfile.mkdtemp(prefix="wispr-test-")

import numpy as np  # noqa: E402

import wispr.dictionary as dictionary  # noqa: E402
import wispr.learn as learn  # noqa: E402
import wispr.rules as rules  # noqa: E402
import wispr.daemon as d  # noqa: E402
from wispr.config import load_corrections  # noqa: E402

FAILS = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


print("\n1. rules never eat the user's words")
c = rules.clean
check("'I mean' at sentence end survives",
      c("Yeah, this is what I mean.") == "Yeah, this is what I mean.")
check("'actually' is not a self-correction trigger",
      c("I actually like the fact that it was separated.")
      == "I actually like the fact that it was separated.")
check("'you know' survives (judgement call, left to the LLM)",
      "you know" in c("remove any bolding which is, you know, ugly."))
check("um/uh are removed", c("So um I think uh we should go.") == "So I think we should go.")
check("removing a filler leaves no orphan punctuation",
      c("which is, um, fine.") == "Which is, fine.")
check("explicit 'no wait' correction applies",
      c("It's Tuesday, no wait, Wednesday.") == "It's Wednesday.")
check("'scratch that' correction applies",
      c("we need two, scratch that, three people") == "We need three people.")
check("stutter repeat collapses", c("I I want the the file") == "I want the file.")
check("intentional repeats stay", "very very" in c("it is very very good"))
check("'period' is not turned into punctuation",
      c("over a period of time it changed") == "Over a period of time it changed.")
check("multi-word corrections apply case-insensitively",
      c("send the Docked X file", {"docked x": "docx"}) == "Send the docx file.")
check("legitimate doubles survive ('had had', 'that that')",
      c("he had had enough, and that that was it") == "He had had enough, and that that was it.")
check("a filler before a sentence end keeps the period",
      c("It was, um. Then we left.") == "It was. Then we left.")
check("corrections are whole-word",
      c("the whisperer spoke softly", {"whisper": "Wispr"}) == "The whisperer spoke softly.")


print("\n2. the learner learns mishearings and nothing else")
# The rewording gate uses the system word list; pin a small one so the
# test behaves the same on every machine.
learn._ENGLISH = {"the", "report", "reports", "ship", "skip", "it", "now",
                  "send", "good", "great", "is", "hello", "hi", "team",
                  "whisper", "icon", "an", "thing", "install", "connector"}
S = learn.substitutions
check("single-word respelling", S("open Whisper now please", "open Wispr now please")
      == [("Whisper", "Wispr")])
check("two tokens merged into one", S("give me a docked X file", "give me a docx file")
      == [("docked X", "docx")])
check("similar proper noun", S("at Carlton University", "at Carleton University")
      == [("Carlton", "Carleton")])
check("a rewrite (different word) is NOT learned",
      S("the report is good", "the report is great") == [])
check("stop words are never learned", S("send it to the team", "send it to a team") == [])
check("sentence-initial capital is grammar, not a term",
      S("Hello team, send the report today", "Hi team, send the report today") == [])
check("a plural edit is not a mishearing", S("send the report", "send the reports") == [])
check("a similar-looking verb swap is not learned", S("ship it now", "skip it now") == [])
check("a one-token paste can still be learned from",
      learn.substitutions("Firecral", "Firecrawl") == [("Firecral", "Firecrawl")])
check("case-only change is not a mishearing", S("the atlas deck", "the Atlas deck") == [])
check("whole-sentence rewrite is ignored (alignment gate)",
      S("please fix the login bug today", "can you look at authentication tomorrow") == [])
check("numbers are never learned", S("we need 2 more", "we need 3 more") == [])
check("edit inside a larger field still aligns",
      S("ping Anika about it", "Earlier text.\nping Anika about it\nMore text")
      == [])
check("edit inside a larger field: the changed word is found",
      S("ping an icon about it", "Earlier text.\nping Anika about it\nMore text")
      == [("an icon", "Anika")])

learned = learn.learn_from_edit("install Firecall Connector", "install Firecrawl Connector")
check("edit learning persists the pair as a hint, not a verbatim correction",
      learned == [("Firecall", "Firecrawl")]
      and dictionary.load_learned().get("Firecall") == "Firecrawl"
      and "Firecall" not in load_corrections())
check("the right-hand side joins the dictionary (has a capital)",
      "Firecrawl" in dictionary.load_dictionary())
check("a lowercase respelling is a hint but not a dictionary word",
      learn.learn_from_edit("the wisper thing", "the whispr thing") == [("wisper", "whispr")]
      and "whispr" not in dictionary.load_dictionary())
check("learning the same pair twice is a no-op",
      learn.learn_from_edit("install Firecall Connector", "install Firecrawl Connector") == [])

dictionary.add_words(["Anika", "Northwind"])
got = learn.learn_from_llm("loop an icon on the review, tell the northwind numbers",
                           "Loop in Anika on the review, tell the Northwind numbers.",
                           dictionary.prompt_terms([], {}, limit=1000))
check("LLM substitution toward a known term is learned",
      got == [("an icon", "in Anika")])
check("LLM case-only change toward a known term is not a correction",
      "northwind" not in dictionary.load_learned())
check("a learned hint is NOT applied verbatim by the rules layer",
      "an icon" in rules.clean("click an icon on the desktop", load_corrections()))
got = learn.learn_from_llm("send it to Sam please", "Send it to Lee please.",
                           ["Lee", "Sam"])
check("LLM swapping one known term for another is NOT learned", got == [])
check("a dictionary word can never become a 'wrong' side",
      dictionary.record_correction("Anika", "Anka") is False)
check("reinforcing a learned pair bumps its count instead of duplicating",
      learn.learn_from_edit("Firecall thing", "Firecrawl thing") == []
      and [r for r in dictionary._learned_rows() if r[0] == "Firecall"][0][3] == "2")
check("wispr forget removes a learned pair",
      dictionary.remove_learned("Firecall") and "Firecall" not in dictionary.load_learned())
check("manual fix is verbatim", dictionary.record_correction("docked x", "docx")
      and load_corrections()["docked x"] == "docx")
check("wispr forget removes a word",
      dictionary.remove_word("Firecrawl") and "Firecrawl" not in dictionary.load_dictionary())
check("prompt terms: dictionary first, then corrections, then vocab, de-duplicated",
      dictionary.prompt_terms(["Claude", "anika"], {"wispa": "Wispr"}, ["Anika"], learned={})
      == ["Anika", "Wispr", "Claude"])


print("\n3. incremental decoding stays bounded")
SR = d.SAMPLE_RATE
rng = np.random.default_rng(0)


def speech(seconds):
    return (rng.standard_normal(int(seconds * SR)) * 0.05).astype("float32")


def silence(seconds):
    return (rng.standard_normal(int(seconds * SR)) * 0.0002).astype("float32")


audio = np.concatenate([speech(4), silence(0.6), speech(3), silence(0.5), speech(2)])
cut = d.find_cut(audio, 0, len(audio) - int(0.8 * SR))
check("find_cut lands in the last silence gap",
      cut is not None and 7.6 * SR < cut < 8.1 * SR)
check("find_cut returns None with no gap", d.find_cut(speech(6), 0, 6 * SR) is None)

decodes = []


class FakeModel:
    def transcribe(self, audio, **kw):
        decodes.append(len(audio) / SR)
        time.sleep(0.01)
        return [types.SimpleNamespace(text=f"[{len(audio) / SR:.0f}s]", start=0.0,
                                      end=len(audio) / SR)]


# 60 s of fluent speech with a short breath every ~6 s
parts = []
for _ in range(10):
    parts += [speech(5.5), silence(0.5)]
long_audio = np.concatenate(parts)
rec = types.SimpleNamespace(snapshot=lambda: long_audio)
inc = d.IncrementalTranscriber(FakeModel(), threading.Lock(), [], {}, [])
d.vocab_mod = types.SimpleNamespace(initial_prompt=lambda *a, **k: "")
# drive the worker synchronously: call the chunk step as the timer would
for _ in range(20):
    inc._maybe_commit_chunk(rec)
out = inc.finalize(long_audio)
check("every background decode is a bounded chunk (max %.1fs)" % max(decodes),
      max(decodes) <= d.MAX_CHUNK_AUDIO_S + 1)
check("the tail decoded on release is short",
      decodes[-1] < d.MIN_CHUNK_AUDIO_S + d.TRAILING_GUARD_S + 1)
check("all audio was consumed exactly once",
      abs(sum(decodes) - len(long_audio) / SR) < 0.05)
check("transcript is the concatenation of the chunks", out and out.count("[") == len(decodes))

# no pauses at all: MAX_CHUNK forces a cut
decodes.clear()
fluent = speech(50)
inc = d.IncrementalTranscriber(FakeModel(), threading.Lock(), [], {}, [])
rec = types.SimpleNamespace(snapshot=lambda: fluent)
for _ in range(10):
    inc._maybe_commit_chunk(rec)
inc.finalize(fluent)
check("without any pause the chunks are still capped at MAX_CHUNK_AUDIO_S",
      max(decodes) <= d.MAX_CHUNK_AUDIO_S + 0.1 and len(decodes) >= 3)

# a decode failure degrades to whole-decode, never to a lost dictation
class BoomModel:
    def transcribe(self, *a, **k):
        raise RuntimeError("model exploded")


inc = d.IncrementalTranscriber(BoomModel(), threading.Lock(), [], {}, [])
try:
    inc._maybe_commit_chunk(types.SimpleNamespace(snapshot=lambda: fluent))
except Exception:
    inc._failed = True
check("a failing decoder makes finalize() return None (caller whole-decodes)",
      inc.finalize(fluent) is None)


print("\n4. paste target is never Wispr itself")
import wispr.modes as modes  # noqa: E402


class App:
    def __init__(self, bid, pid): self._b, self._p = bid, pid
    def bundleIdentifier(self): return self._b
    def processIdentifier(self): return self._p


claude = App("com.anthropic.claudefordesktop", 111)
modes._raw_frontmost = lambda: claude
check("a foreign app is returned and remembered",
      modes.frontmost_app() is claude and modes.TRACKER.last_foreign() is claude)
modes._raw_frontmost = lambda: App("com.wispr.dictation", 222)
check("Wispr in front -> the last foreign app is returned instead",
      modes.frontmost_app() is claude)
modes._raw_frontmost = lambda: App("org.python.python", os.getpid())
check("our own pid in front -> the last foreign app is returned instead",
      modes.frontmost_app() is claude)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL QUALITY TESTS PASSED")
