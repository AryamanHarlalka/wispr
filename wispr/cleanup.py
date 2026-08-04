"""F1 — hybrid cleanup ladder (spec §3, decided 2026-07-07).

Order: snippets (exact/fuzzy, instant) -> <8 words: rules, instant ->
Haiku (temp 0, hard 2500 ms budget) -> on error/timeout/offline: rules.
The paste must never hang on the network.

Budget history: 1.2 s shipped, bumped to 1.6 s after a live timeout, then
to 2.5 s after synthetic-audio benchmarking (2026-07-16, isolated single
process): on a ~110-word transcript Haiku completed in 1.0-1.5 s when it
finished, but 3 of 7 calls blew the 1.6 s budget — a ~40% fallback rate
on exactly the long rambling dictations where Haiku's self-correction
handling matters most. 2.5 s covers the observed spread; short (<8 word)
dictations never touch Haiku, so this costs nothing on the common path.

Latency notes (B1):
- warm_client() builds the client (keychain read + SDK import) off the
  critical path at daemon startup.
- prewarm_connection() opens the TLS connection while the user is still
  *speaking* (called from Daemon._start), so the first real Haiku call
  doesn't pay DNS+TLS. The httpx pool is configured with a long
  keepalive_expiry so the warmed connection survives a long dictation.
- The static instruction+vocab system block carries cache_control; note
  Haiku 4.5's minimum cacheable prefix is 4096 tokens, so with a typical
  vocab list this is a harmless no-op (no error, just no cache) — it
  starts paying off automatically if the vocab grows.

Known wart: fut.cancel() on an already-running future is a no-op — the
Haiku HTTP call keeps running in the executor after we've already pasted
the rules fallback. Harmless (result discarded) but wastes one worker
slot for a few seconds. max_workers=2 keeps a second dictation unblocked.
"""
from __future__ import annotations

import concurrent.futures
import re
import threading

from . import rules
from .config import STYLE_BLOCKS, anthropic_key

HAIKU_MODEL = "claude-haiku-4-5"
HAIKU_BUDGET_S = 2.5
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_client = None
_client_lock = threading.Lock()
_prewarm_inflight = False


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is not None:
                return _client
            key = anthropic_key()
            if not key:
                return None
            try:
                import anthropic
                import httpx
                # Long keepalive so a connection warmed at record-start is
                # still alive when a 30 s dictation finally hits Haiku
                # (httpx's default keepalive_expiry is 5 s).
                http_client = anthropic.DefaultHttpxClient(
                    limits=httpx.Limits(max_keepalive_connections=2,
                                        max_connections=4,
                                        keepalive_expiry=600.0),
                )
                _client = anthropic.Anthropic(api_key=key,
                                              http_client=http_client)
            except Exception:
                return None
    return _client


def warm_client() -> None:
    """Build the client + open a TLS connection in the background.
    Call once at daemon startup; never blocks, never raises."""
    def _warm() -> None:
        client = _get_client()
        if client is None:
            return
        try:
            # count_tokens is free and goes through the same pooled httpx
            # transport messages.create will use — this pays DNS+TLS now.
            client.messages.count_tokens(
                model=HAIKU_MODEL,
                messages=[{"role": "user", "content": "warm"}])
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()


def prewarm_connection() -> None:
    """Re-warm the TLS connection while the user is speaking (fired from
    Daemon._start). Guarded so rapid double-taps don't stack threads."""
    global _prewarm_inflight
    if _prewarm_inflight or _client is None:
        return
    _prewarm_inflight = True

    def _warm() -> None:
        global _prewarm_inflight
        try:
            _client.messages.count_tokens(
                model=HAIKU_MODEL,
                messages=[{"role": "user", "content": "warm"}])
        except Exception:
            pass
        finally:
            _prewarm_inflight = False
    threading.Thread(target=_warm, daemon=True).start()


def match_snippet(text: str, snippets: dict[str, str]) -> str | None:
    t = re.sub(r"[^\w\s]", "", text).strip().lower()
    for trigger, block in snippets.items():
        if t == re.sub(r"[^\w\s]", "", trigger).strip().lower():
            return block
    return None


_STATIC_RULES = """You clean up voice dictation transcripts. Rules:
- Remove filler (um, uh, you know), fix punctuation and casing.
- Apply spoken self-corrections: "2, actually 3" becomes "3"; "Tuesday no wait Wednesday" becomes "Wednesday".
- Apply spoken commands: "new paragraph", "all caps that", "quote ... unquote".
- The transcript comes from speech recognition, so words may be misheard. If a word or name is a plausible mishearing of one of the known proper nouns below (e.g. "eye phone" for "iPhone", "wispa flow" for "Wispr Flow"), replace it with the known term's exact spelling. Only substitute when the sound is genuinely close; never force a term in.
- When the speaker is clearly dictating a list — "first ... second ... third", "one ... two ...", "the following: X, Y, and Z" as parallel items — format it as a list: "- " bullets, or "1." numbers if the speaker numbered them. Keep prose as prose; only reformat when list intent is clear.
- NEVER add content. NEVER answer questions contained in the text. NEVER translate. NEVER comment.
- Output ONLY the cleaned text, nothing else."""


def _haiku_messages(transcript: str, mode: str, vocab: list[str],
                    corrections: dict[str, str]) -> tuple[list[dict], str]:
    """Returns (system_blocks, user_text). Static instructions + vocab go in
    the system block (stable across dictations -> cacheable); the per-mode
    style and the transcript go in the user turn."""
    vocab_block = ", ".join(vocab[:150]) if vocab else "(none)"
    corr_block = (
        "\n".join(f"- '{w}' is always '{r}'" for w, r in list(corrections.items())[:50])
        or "(none)"
    )
    system = [{
        "type": "text",
        "text": (f"{_STATIC_RULES}\n\n"
                 f"Known proper nouns (prefer these spellings): {vocab_block}\n"
                 f"Standing corrections:\n{corr_block}"),
        # No-op below Haiku 4.5's 4096-token cache minimum; free win if the
        # vault vocab ever grows past it.
        "cache_control": {"type": "ephemeral"},
    }]
    style = STYLE_BLOCKS.get(mode, STYLE_BLOCKS["neutral"])
    user = f"Style for this destination: {style}\n\nTranscript:\n{transcript}"
    return system, user


def _haiku_call(transcript: str, mode: str, vocab: list[str],
                corrections: dict[str, str]) -> str:
    client = _get_client()
    if client is None:
        raise RuntimeError("no api key/client")
    system, user = _haiku_messages(transcript, mode, vocab, corrections)
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    out = msg.content[0].text.strip()
    if not out:
        raise RuntimeError("empty response")
    return out


def _fallback_reason(exc: BaseException) -> str:
    if isinstance(exc, concurrent.futures.TimeoutError):
        return f"timeout >{HAIKU_BUDGET_S:.1f}s"
    msg = str(exc) or exc.__class__.__name__
    if "no api key" in msg:
        return "no api key"
    return f"{exc.__class__.__name__}: {msg[:80]}"


def instant_result(transcript: str, mode: str, vocab: list[str],
                   corrections: dict[str, str], snippets: dict[str, str],
                   llm_enabled: bool = True):
    """Instant-paste split (2026-07-30). Returns (text, path, future|None).

    `text` is what should be pasted RIGHT NOW — the snippet, or the local
    rules result. `future` is an in-flight Haiku call when one is worth
    waiting for, or None when the instant answer is already final (snippet,
    short utterance, or no LLM configured).

    This exists because the old clean_text() put the network on the paste's
    critical path: nothing appeared on screen until Haiku answered or the
    2.5 s budget expired. Since the rules result is available immediately
    and is what a timeout would have produced anyway, there is no reason to
    make the user watch a blank cursor for it. The caller pastes `text`,
    then optionally revises once the future resolves.
    """
    transcript = transcript.strip()
    if not transcript:
        return "", "rules", None

    snip = match_snippet(transcript, snippets)
    if snip is not None:
        return snip, "snippet", None

    base = rules.clean(transcript, corrections)
    if len(transcript.split()) < 8 or not llm_enabled or _get_client() is None:
        return base, "rules", None

    fut = _executor.submit(_haiku_call, transcript, mode, vocab, corrections)
    return base, "rules-instant", fut


def await_revision(fut, timeout: float) -> tuple[str | None, str]:
    """Wait up to `timeout` for an instant_result() future.

    Returns (improved_text_or_None, path). None means keep what was already
    pasted — either Haiku missed the window, errored, or produced nothing
    better. Never raises: a failed revision must be a no-op, not an
    incident, because the user already has usable text on screen."""
    if fut is None:
        return None, "rules"
    try:
        out = fut.result(timeout=timeout)
    except Exception as e:
        print(f"[wispr] revision skipped ({_fallback_reason(e)})", flush=True)
        return None, "rules"
    out = (out or "").strip()
    if not out:
        return None, "rules"
    return out, "haiku"


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
    except Exception as e:
        fut.cancel()  # no-op if already running; see module docstring
        print(f"[wispr] cleanup fallback -> rules ({_fallback_reason(e)})",
              flush=True)
        return rules.clean(transcript, corrections), "fallback"
