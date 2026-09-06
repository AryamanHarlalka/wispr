"""Learning from use — the part of "it doesn't learn words" that is
actually about learning.

Two evidence sources feed dictionary.py:

1. **Your edits.** After a paste, Wispr looks (via the Accessibility API,
   locally, read-only) at the text field it pasted into a few seconds
   later. If you changed a word — "Whisper" to "Wispr", "docked X" to
   "docx", "Carlton" to "Carleton" — that is a correction, and it is saved
   so the same mishearing is fixed automatically next time. This is what
   Wispr Flow calls learning from corrections.

2. **The cleanup model's substitutions.** When the LLM pass replaces a
   word with one of your known dictionary terms ("Firecall" -> "Firecrawl"),
   the pair is stored too, so the fast local rules and the speech model's
   own bias catch it even when the LLM is offline or skipped.

Everything is guarded: only one- or two-token substitutions, both sides
must be word-like, never a stop word, and the replacement must either
look like the original (character similarity) or be a proper term (a
known dictionary word / capitalised). And what is learned is a HINT (see
dictionary.py): biased in the speech model and shown to the cleanup
model, never blindly substituted — so a coincidence cannot rewrite a
future dictation. `wispr words` shows what was learned; `wispr forget
<wrong>` removes an entry.
"""
from __future__ import annotations

import difflib
import re
import threading
import time

from . import dictionary

_TOKEN_RE = re.compile(r"\S+")
_STRIP = "\"'“”‘’()[]{}<>,.;:!?…-—–*_`~"
_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "is", "are", "was", "were", "be", "been", "it", "its", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they", "me",
    "my", "your", "our", "their", "his", "her", "not", "no", "yes", "so",
    "if", "then", "than", "as", "by", "from", "up", "down", "out", "just",
    "do", "does", "did", "have", "has", "had", "can", "could", "would",
    "should", "will", "there", "here", "what", "which", "who", "how", "when",
    "where", "why", "all", "any", "some", "more", "very", "also", "into",
    "about", "like", "get", "got", "one", "two", "three", "okay", "ok",
}
MIN_SIMILARITY = 0.5      # char-level ratio, spaces removed
MIN_SIMILARITY_TERM = 0.25  # when the replacement is a proper term
MAX_SPAN = 2              # tokens on either side of a substitution
MIN_ALIGNMENT = 0.6       # share of pasted tokens still present, in order


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "")]


def _key(tok: str) -> str:
    return tok.strip(_STRIP).lower()


def _clean(tok: str) -> str:
    return tok.strip(_STRIP)


def _similar(a: str, b: str) -> float:
    a = re.sub(r"\s+", "", a.lower())
    b = re.sub(r"\s+", "", b.lower())
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _word_like(s: str) -> bool:
    return bool(re.match(r"^[\w][\w'’.\-/&+]*(?: [\w][\w'’.\-/&+]*)?$", s)) \
        and any(ch.isalpha() for ch in s)


_ENGLISH: set[str] | None = None


def _english_words() -> set[str]:
    """The system word list (/usr/share/dict/words on macOS), lowercased.
    Used as a rewording gate: when both sides of an edit are ordinary
    English words, the user rephrased — that is not a mishearing to learn.
    Empty when the file is missing, which disables the gate."""
    global _ENGLISH
    if _ENGLISH is None:
        words: set[str] = set()
        for path in ("/usr/share/dict/words", "/usr/dict/words"):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    words = {ln.strip().lower() for ln in f if ln.strip()}
                break
            except OSError:
                continue
        _ENGLISH = words
    return _ENGLISH


def _plain_english(s: str) -> bool:
    """Lowercase, and every token is in the system word list."""
    eng = _english_words()
    if not eng or s != s.lower():
        return False
    return all(w in eng for w in _key(s).split())


def is_term(s: str, known: set[str], sentence_initial: bool = False) -> bool:
    """A proper term: in the known set, or carries a digit / identifier
    punctuation / an inner capital (NASA, iPhone, docx-like ids), or is
    capitalised somewhere other than the start of a sentence — where a
    capital is just grammar, not a name."""
    k = _key(s)
    if k in known or any(w in known for w in k.split()):
        return True
    if re.search(r"[0-9._\-/]", s) or re.search(r"\w[A-Z]", s):
        return True
    return (not sentence_initial) and any(w[:1].isupper() for w in s.split())


def substitutions(before: str, after: str, *,
                  require_alignment: bool = True,
                  known: set[str] | None = None) -> list[tuple[str, str]]:
    """Word-level substitutions that turn `before` into (part of) `after`.

    Returns (wrong, right) pairs that pass the safety gates. `after` may
    contain more than `before` (the field had other text); alignment is
    on the shared region.
    """
    return [(w, r) for w, r, _ in substitutions_ex(
        before, after, require_alignment=require_alignment, known=known)]


def substitutions_ex(before: str, after: str, *,
                     require_alignment: bool = True,
                     known: set[str] | None = None
                     ) -> list[tuple[str, str, bool]]:
    """As substitutions(), plus whether the replacement is a proper term
    (name, product, identifier) worth adding to the dictionary."""
    bt, at = _tokens(before), _tokens(after)
    if not bt or not at:
        return []
    bk = [_key(t) for t in bt]
    ak = [_key(t) for t in at]
    sm = difflib.SequenceMatcher(None, bk, ak, autojunk=False)
    blocks = sm.get_matching_blocks()
    matched = sum(b.size for b in blocks)
    if require_alignment and len(bk) > 1 and matched < MIN_ALIGNMENT * len(bk):
        return []
    known = known or set()
    out: list[tuple[str, str, bool]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        if (i2 - i1) > MAX_SPAN or (j2 - j1) > MAX_SPAN:
            continue
        wrong = " ".join(_clean(t) for t in bt[i1:i2]).strip()
        right = " ".join(_clean(t) for t in at[j1:j2]).strip()
        if not wrong or not right:
            continue
        if wrong.lower() == right.lower():
            continue  # case-only edits are not mishearings
        if not _word_like(wrong) or not _word_like(right):
            continue
        if _key(wrong) in _STOP or _key(right) in _STOP:
            continue
        if all(w in _STOP for w in _key(wrong).split()) or \
                all(w in _STOP for w in _key(right).split()):
            continue
        if wrong.replace(" ", "").isdigit() or right.replace(" ", "").isdigit():
            continue
        if len(_key(wrong)) < 3 or len(_key(right)) < 2:
            continue
        initial = j1 == 0 or at[j1 - 1][-1:] in ".!?\n"
        term = is_term(right, known, initial)
        if not term and _plain_english(wrong) and _plain_english(right):
            continue  # a rephrase (report -> reports, ship -> skip)
        sim = _similar(wrong, right)
        if sim < (MIN_SIMILARITY_TERM if term else MIN_SIMILARITY):
            continue
        out.append((wrong, right, term))
    return out


def learn_from_edit(pasted: str, field_text: str) -> list[tuple[str, str]]:
    """Corrections the user made to a paste, saved. Returns what was
    learned (already persisted)."""
    if not pasted or not field_text or pasted.strip() == field_text.strip():
        return []
    known = {t.lower() for t in dictionary.load_dictionary()}
    learned = []
    for wrong, right, term in substitutions_ex(pasted, field_text, known=known):
        # Only a real term joins the dictionary (it goes into the speech
        # prompt); a plain respelled word is kept as a hint only.
        if dictionary.learn(wrong, right, source="edit", add_term=term):
            learned.append((wrong, right))
    return learned


def learn_from_llm(raw: str, cleaned: str, known_terms: list[str]) -> list[tuple[str, str]]:
    """Corrections the cleanup model applied *toward a known term*. Only
    those: a free rewrite by the model is style, not evidence."""
    if not raw or not cleaned:
        return []
    known = {t.lower() for t in known_terms}
    learned = []
    for wrong, right in substitutions(raw, cleaned, require_alignment=False,
                                      known=known):
        rk = _key(right)
        if rk not in known and not any(w in known for w in rk.split()):
            continue
        wk = _key(wrong)
        if wk in known or any(w in known for w in wk.split()):
            continue
        if dictionary.learn(wrong, right, source="llm", add_term=True):
            learned.append((wrong, right))
    return learned


# --- reading the text field back (macOS Accessibility, local, read-only) --

def read_focused_text(pid: int | None) -> str | None:
    """The value of the focused text element in app `pid` (or the system
    focus when pid is None). None when there is no text focus, the app
    does not expose it, or Accessibility is not granted."""
    try:
        import ApplicationServices as AS
    except Exception:
        return None
    try:
        if pid:
            elem = AS.AXUIElementCreateApplication(int(pid))
        else:
            elem = AS.AXUIElementCreateSystemWide()
        err, focused = AS.AXUIElementCopyAttributeValue(
            elem, AS.kAXFocusedUIElementAttribute, None)
        if err != 0 or focused is None:
            return None
        err, value = AS.AXUIElementCopyAttributeValue(
            focused, AS.kAXValueAttribute, None)
        if err != 0 or value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return str(value)
        except Exception:
            return None
    except Exception:
        return None


class EditWatcher:
    """Looks back at a pasted field a few seconds later and learns from
    what changed. One watcher per dictation; cancelled by the next one.

    `checks` are delays (s) after the paste. Each check reads the field;
    if the paste is still there and has been edited, the edits are
    learned and the watcher stops. If the paste is gone (message sent,
    window closed) nothing is learned, which is the safe outcome.
    """

    def __init__(self, pasted: str, pid: int | None, is_current,
                 checks=(4.0, 9.0), on_learn=None) -> None:
        self._pasted = pasted
        self._pid = pid
        self._is_current = is_current
        self._checks = tuple(checks)
        self._on_learn = on_learn
        self._cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True,
                         name="wispr-learn").start()

    def cancel(self) -> None:
        self._cancel.set()

    def _run(self) -> None:
        t0 = time.time()
        for delay in self._checks:
            wait = t0 + delay - time.time()
            if wait > 0 and self._cancel.wait(wait):
                return
            if self._cancel.is_set() or not self._is_current():
                return
            text = read_focused_text(self._pid)
            if not text:
                continue
            ptoks = [_key(t) for t in _tokens(self._pasted)]
            need = min(2, len(ptoks))
            if need and difflib.SequenceMatcher(
                    None, ptoks, [_key(t) for t in _tokens(text)],
                    autojunk=False).find_longest_match().size < need:
                continue  # our text is not in this field at all
            learned = learn_from_edit(self._pasted, text)
            if learned:
                if self._on_learn:
                    try:
                        self._on_learn(learned)
                    except Exception:
                        pass
                return
