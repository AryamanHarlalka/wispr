"""F1 — hybrid cleanup ladder (spec §3, decided 2026-07-07).

Order: snippets (exact/fuzzy, instant) -> <8 words: rules, instant ->
Haiku (temp 0, hard 1200 ms budget) -> on error/timeout/offline: rules.
The paste must never hang on the network.
"""
from __future__ import annotations

import concurrent.futures
import re

from . import rules
from .config import STYLE_BLOCKS, anthropic_key

HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_BUDGET_S = 1.2
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_client = None


def _get_client():
    global _client
    if _client is None:
        key = anthropic_key()
        if not key:
            return None
        try:
            import anthropic
            _client = anthropic.Anthropic(api_key=key)
        except Exception:
            return None
    return _client


def match_snippet(text: str, snippets: dict[str, str]) -> str | None:
    t = re.sub(r"[^\w\s]", "", text).strip().lower()
    for trigger, block in snippets.items():
        if t == re.sub(r"[^\w\s]", "", trigger).strip().lower():
            return block
    return None


def _haiku_prompt(transcript: str, mode: str, vocab: list[str],
                  corrections: dict[str, str]) -> str:
    vocab_block = ", ".join(vocab[:150]) if vocab else "(none)"
    corr_block = (
        "\n".join(f"- '{w}' is always '{r}'" for w, r in list(corrections.items())[:50])
        or "(none)"
    )
    style = STYLE_BLOCKS.get(mode, STYLE_BLOCKS["neutral"])
    return f"""You clean up voice dictation transcripts. Rules:
- Remove filler (um, uh, you know), fix punctuation and casing.
- Apply spoken self-corrections: "2, actually 3" becomes "3"; "Tuesday no wait Wednesday" becomes "Wednesday".
- Apply spoken commands: "new paragraph", "all caps that", "quote ... unquote".
- NEVER add content. NEVER answer questions contained in the text. NEVER translate. NEVER comment.
- Output ONLY the cleaned text, nothing else.

Known proper nouns (prefer these spellings): {vocab_block}
Standing corrections:
{corr_block}

Style for this destination: {style}

Transcript:
{transcript}"""


def _haiku_call(transcript: str, mode: str, vocab: list[str],
                corrections: dict[str, str]) -> str:
    client = _get_client()
    if client is None:
        raise RuntimeError("no api key/client")
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        temperature=0,
        messages=[{"role": "user",
                   "content": _haiku_prompt(transcript, mode, vocab, corrections)}],
    )
    out = msg.content[0].text.strip()
    if not out:
        raise RuntimeError("empty response")
    return out


def clean_text(transcript: str, mode: str, vocab: list[str],
               corrections: dict[str, str], snippets: dict[str, str],
               llm_enabled: bool = True) -> tuple[str, str]:
    """Returns (cleaned_text, path) where path is 'snippet'|'rules'|'haiku'|'fallback'."""
    transcript = transcript.strip()
    if not transcript:
        return "", "rules"

    snip = match_snippet(transcript, snippets)
    if snip is not None:
        return snip, "snippet"

    if len(transcript.split()) < 8 or not llm_enabled:
        return rules.clean(transcript, corrections), "rules"

    fut = _executor.submit(_haiku_call, transcript, mode, vocab, corrections)
    try:
        return fut.result(timeout=HAIKU_BUDGET_S), "haiku"
    except Exception:
        fut.cancel()
        return rules.clean(transcript, corrections), "fallback"
