"""Personal dictionary — the words Wispr must get right, and what it has
learned about how you say them.

Three local files under ~/.wispr, all plain text, none of which ever
leave the machine:

  dictionary.txt   one term per line: names, products, jargon. Pushed into
                   the speech model's bias prompt AND handed to the cleanup
                   model as "prefer these spellings". `wispr add <word>`;
                   Wispr also adds terms it learns.
  corrections.tsv  wrong<TAB>right — YOUR explicit fixes (`wispr fix`).
                   Applied verbatim, whole-word, by the offline rules
                   layer and listed for the cleanup model as standing
                   rules. Verbatim because you asked for it.
  learned.tsv      wrong<TAB>right<TAB>source<TAB>count — pairs Wispr
                   observed itself: a word you changed after a paste, or
                   a known term the cleanup model substituted in. These are
                   HINTS, not blind replacements: the right-hand side is
                   biased in the speech model, and the pair is shown to the
                   cleanup model as "X was misheard as Y before", which
                   applies it only where the context fits. A single
                   observation can be a coincidence ("an icon" really is a
                   phrase), so a learned pair never rewrites text on its
                   own — that is the difference between learning and
                   guessing.

`wispr words` lists all three; `wispr forget <term>` removes from any.
"""
from __future__ import annotations

import re
import time

from .config import WISPR_HOME, ensure_home, load_corrections

DICTIONARY_FILE = WISPR_HOME / "dictionary.txt"
LEARNED_FILE = WISPR_HOME / "learned.tsv"

_WORD_OK = re.compile(r"^[\w][\w'’.\-&/+ ]{0,39}$")


def _norm(t: str) -> str:
    return t.strip().lower()


# ---- dictionary.txt -------------------------------------------------------

def load_dictionary() -> list[str]:
    ensure_home()
    if not DICTIONARY_FILE.exists():
        return []
    try:
        lines = DICTIONARY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: dict[str, str] = {}
    for ln in lines:
        w = ln.strip()
        if w and not w.startswith("#"):
            out.setdefault(_norm(w), w)
    return list(out.values())


def _write_dictionary(words: list[str]) -> None:
    ensure_home()
    header = ("# Wispr personal dictionary — one term per line.\n"
              "# Names, products, jargon. Edit freely; local only.\n")
    DICTIONARY_FILE.write_text(
        header + "\n".join(words) + ("\n" if words else ""), encoding="utf-8")


def add_words(words: list[str]) -> list[str]:
    """Add terms; returns the ones that were actually new."""
    current = load_dictionary()
    have = {_norm(w) for w in current}
    added = []
    for w in words:
        w = w.strip()
        if not w or not _WORD_OK.match(w) or _norm(w) in have:
            continue
        current.append(w)
        have.add(_norm(w))
        added.append(w)
    if added:
        _write_dictionary(current)
    return added


def remove_word(word: str) -> bool:
    current = load_dictionary()
    keep = [w for w in current if _norm(w) != _norm(word)]
    if len(keep) == len(current):
        return False
    _write_dictionary(keep)
    return True


# ---- corrections.tsv (manual, verbatim) ----------------------------------

def record_correction(wrong: str, right: str) -> bool:
    """Persist an explicit wrong->right. Returns False when it is already
    known or is not a sensible pair."""
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right or _norm(wrong) == _norm(right):
        return False
    if len(wrong) > 60 or len(right) > 60:
        return False
    ensure_home()
    existing = load_corrections()
    if _norm(existing.get(wrong, "")) == _norm(right):
        return False
    # A correction whose left side is a dictionary word would "fix" a
    # word that is right. Refuse.
    if _norm(wrong) in {_norm(w) for w in load_dictionary()}:
        return False
    with open(WISPR_HOME / "corrections.tsv", "a", encoding="utf-8") as f:
        f.write(f"{wrong}\t{right}\n")
    return True


def remove_correction(wrong: str) -> bool:
    ensure_home()
    p = WISPR_HOME / "corrections.tsv"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    keep = [ln for ln in lines
            if not ("\t" in ln and _norm(ln.split("\t", 1)[0]) == _norm(wrong))]
    if len(keep) == len(lines):
        return False
    p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return True


# ---- learned.tsv (observed, hints) ----------------------------------------

def load_learned() -> dict[str, str]:
    """wrong -> right, most recently reinforced last."""
    ensure_home()
    if not LEARNED_FILE.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for ln in LEARNED_FILE.read_text(encoding="utf-8").splitlines():
            if ln.startswith("#") or "\t" not in ln:
                continue
            parts = ln.split("\t")
            if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                out[parts[0].strip()] = parts[1].strip()
    except OSError:
        pass
    return out


def _learned_rows() -> list[list[str]]:
    if not LEARNED_FILE.exists():
        return []
    rows = []
    for ln in LEARNED_FILE.read_text(encoding="utf-8").splitlines():
        if ln.startswith("#") or "\t" not in ln:
            continue
        parts = (ln.split("\t") + ["", "", ""])[:4]
        rows.append(parts)
    return rows


def _write_learned(rows: list[list[str]]) -> None:
    ensure_home()
    header = ("# Wispr learned corrections: wrong<TAB>right<TAB>source<TAB>count\n"
              "# Hints for the speech and cleanup models, never blind replacements.\n")
    LEARNED_FILE.write_text(
        header + "".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")


def learn(wrong: str, right: str, source: str, add_term: bool = True) -> bool:
    """An observed correction. Stores/reinforces the pair and, when
    `add_term` and the right-hand side looks like a term, adds it to the
    dictionary so the speech model is biased toward it next time. Returns
    True when something new was stored (a fresh pair, or a new word)."""
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right or _norm(wrong) == _norm(right):
        return False
    if len(wrong) > 60 or len(right) > 60:
        return False
    if _norm(wrong) in {_norm(w) for w in load_dictionary()}:
        return False
    rows = _learned_rows()
    stamp = time.strftime("%Y-%m-%d")
    new = True
    for r in rows:
        if _norm(r[0]) == _norm(wrong):
            new = r[1].strip().lower() != right.lower()
            r[1] = right
            r[2] = source
            try:
                r[3] = str(int(r[3] or 0) + 1)
            except ValueError:
                r[3] = "1"
            break
    else:
        rows.append([wrong, right, f"{source}@{stamp}", "1"])
    _write_learned(rows)
    added = add_words([right]) if (add_term and _looks_like_term(right)) else []
    return new or bool(added)


def remove_learned(wrong: str) -> bool:
    rows = _learned_rows()
    keep = [r for r in rows if _norm(r[0]) != _norm(wrong)]
    if len(keep) == len(rows):
        return False
    _write_learned(keep)
    return True


def _looks_like_term(s: str) -> bool:
    """Something worth biasing the speech model toward: has a capital
    letter, a digit, or an internal symbol — a name, product or
    identifier rather than a plain lowercase English word."""
    s = s.strip()
    if not s or len(s) < 2 or len(s.split()) > 3:
        return False
    return bool(re.search(r"[A-Z0-9._\-/]", s))


# ---- merged views ----------------------------------------------------------

def prompt_terms(vocab: list[str], corrections: dict[str, str],
                 dictionary: list[str] | None = None, limit: int = 60,
                 learned: dict[str, str] | None = None) -> list[str]:
    """Merged, de-duplicated term list in priority order for the speech
    model's bias prompt: personal dictionary first, then the targets of
    manual and learned corrections, then vault-harvested vocab."""
    if dictionary is None:
        dictionary = load_dictionary()
    if learned is None:
        learned = load_learned()
    seen: dict[str, str] = {}
    for t in (list(dictionary) + list(corrections.values())
              + list(learned.values()) + list(vocab)):
        t = (t or "").strip()
        if t and _norm(t) not in seen:
            seen[_norm(t)] = t
    return list(seen.values())[:limit]
