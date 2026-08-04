"""0a — benchmark harness. `wispr bench` runs the current STT + cleanup
pipeline over 3 fixed voice clips (bench/clips/*.wav) against hand-written
ground truth (bench/ground_truth.json) and prints WER + release->text
latency per clip. Everything in Task 0 is judged on this harness.

Clips are real voice recordings containing vault proper nouns and are
gitignored -- personal audio never leaves this machine or gets committed.
`wispr record-clip` records them.
"""
from __future__ import annotations

import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

BENCH_DIR = Path(__file__).resolve().parent.parent / "bench"
CLIPS_DIR = BENCH_DIR / "clips"
GROUND_TRUTH = BENCH_DIR / "ground_truth.json"
SAMPLE_RATE = 16000


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE, \
            f"{path} is {w.getframerate()}Hz, expected {SAMPLE_RATE}Hz"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def record_clip(name: str, seconds: float) -> Path:
    import sounddevice as sd
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    for i in (3, 2, 1):
        print(f"  recording in {i}…", flush=True)
        time.sleep(1)
    print("  🔴 speak now", flush=True)
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                    channels=1, dtype="float32")
    sd.wait()
    print("  ⏹ done", flush=True)
    dest = CLIPS_DIR / f"{name}.wav"
    pcm16 = np.clip(audio.flatten() * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16.tobytes())
    return dest


_PUNCT_RE = re.compile(r"[^\w\s]")


def _tokens(text: str) -> list[str]:
    return _PUNCT_RE.sub("", text.lower()).split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Standard WER: word-level Levenshtein edit distance / len(reference)."""
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    # dp[i][j] = edit distance between ref[:i] and hyp[:j]
    dp = [[0] * (len(hyp) + 1) for _ in range(len(ref) + 1)]
    for i in range(len(ref) + 1):
        dp[i][0] = i
    for j in range(len(hyp) + 1):
        dp[0][j] = j
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hyp) + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[-1][-1] / len(ref)


def run(backend_override: str | None = None, model_override: str | None = None,
        clean: bool = True) -> None:
    from . import cleanup, stt, vocab as vocab_mod
    from .config import (load_config, load_corrections, load_snippets,
                         load_vocab, stt_backend, stt_beam_size, stt_model)

    if not GROUND_TRUTH.exists():
        print(f"[wispr] no ground truth at {GROUND_TRUTH} -- "
              f"run `wispr record-clip <short|medium|long> <seconds>` "
              f"and write bench/ground_truth.json first", file=sys.stderr)
        sys.exit(1)
    truths: dict[str, str] = json.loads(GROUND_TRUTH.read_text())

    backend_name = backend_override or stt_backend()
    model_name = model_override or stt_model()
    beam_size = stt_beam_size()

    print(f"[wispr bench] backend={backend_name} model={model_name} "
          f"beam_size={beam_size} cleanup={'on' if clean else 'off'}")
    t0 = time.time()
    backend = stt.build_backend(backend_name, model_name)
    print(f"  model load: {time.time() - t0:.2f}s (one-time daemon-startup "
          f"cost, not counted below)\n")

    vocab = load_vocab()
    corrections = load_corrections()
    snippets = load_snippets() if clean else {}
    prompt = vocab_mod.initial_prompt(vocab, corrections)

    rows = []
    for name, truth in truths.items():
        clip = CLIPS_DIR / f"{name}.wav"
        if not clip.exists():
            print(f"  [skip] {name}: no clip at {clip}")
            continue
        audio = _load_wav(clip)
        t0 = time.time()
        segments = backend.transcribe(
            audio, language="en", initial_prompt=prompt, beam_size=beam_size,
            vad_filter=True, condition_on_previous_text=False)
        raw = " ".join(s.text.strip() for s in segments).strip()
        t_whisper = time.time()
        wer = word_error_rate(truth, raw)
        cleaned, path = (raw, "n/a")
        if clean:
            cleaned, path = cleanup.clean_text(
                raw, "neutral", vocab, corrections, snippets)
        t_end = time.time()
        rows.append({
            "clip": name,
            "duration_s": len(audio) / SAMPLE_RATE,
            "wer": wer,
            "whisper_ms": (t_whisper - t0) * 1000,
            "cleanup_ms": (t_end - t_whisper) * 1000,
            "total_ms": (t_end - t0) * 1000,
            "path": path,
            "raw": raw,
            "cleaned": cleaned,
        })

    print(f"{'clip':<8} {'dur':>6} {'WER':>6} {'whisper':>9} "
          f"{'cleanup':>9} {'total':>8}  path")
    for r in rows:
        print(f"{r['clip']:<8} {r['duration_s']:>5.1f}s {r['wer']:>5.1%} "
              f"{r['whisper_ms']:>7.0f}ms {r['cleanup_ms']:>7.0f}ms "
              f"{r['total_ms']:>6.0f}ms  {r['path']}")
    for r in rows:
        print(f"\n--- {r['clip']} ---\nraw:     {r['raw']}\ncleaned: {r['cleaned']}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "record":
        if len(args) != 3:
            print("usage: wispr bench record <short|medium|long> <seconds>")
            sys.exit(1)
        path = record_clip(args[1], float(args[2]))
        print(f"[wispr] wrote {path}")
        return
    backend = model = None
    clean = True
    i = 0
    while i < len(args):
        if args[i] == "--backend":
            backend = args[i + 1]; i += 2
        elif args[i] == "--model":
            model = args[i + 1]; i += 2
        elif args[i] == "--no-cleanup":
            clean = False; i += 1
        else:
            i += 1
    run(backend, model, clean)


if __name__ == "__main__":
    main()
