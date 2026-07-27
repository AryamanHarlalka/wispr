# Murmur — latency and accuracy measurements

Last run: **2026-07-27**, macOS 15.7.7, Intel x86_64 (Iris Plus iGPU), faster-whisper
via CTranslate2 int8, `beam_size=1`.

---

## 1. Production latency (the number that matters)

Measured from `~/.murmur/history.jsonl` — **120 real dictations**, not synthetic clips.
This is release-key → text-pasted, end to end.

| | |
|---|---|
| Median | **4,400 ms** |
| p90 | 8,509 ms |
| p99 | 49,262 ms |
| Max | 104,263 ms |
| Over 3s | **100 / 120 (83%)** |
| Over 5s | 42 / 120 |

Split by utterance length:

| | Median | n |
|---|---|---|
| Short (<80 chars) | 3,962 ms | 39 |
| Long (≥200 chars) | 4,843 ms | 52 |

**The important line is that a short utterance costs 3,962 ms.** A two-word
dictation takes ~82% as long as a 200-character one, so latency is dominated by
**fixed overhead**, not audio length. Making long dictations faster is not the
problem; the floor is.

Cleanup path distribution:

| Path | Count |
|---|---|
| `haiku` | 78 |
| `fallback` (Haiku exceeded budget) | 22 |
| `rules` (short/snippet, by design) | 20 |

**22 fallbacks out of 100 Haiku attempts — an 18% timeout rate** against the 2.5s
budget. Every one of those is a dictation that waited the full budget and then
got the local-rules result anyway: worst of both.

---

## 2. Model comparison

Harness: `murmur bench` over `bench/clips/*.wav` against `bench/ground_truth.json`.

| Clip | Model | WER | whisper | cleanup | total | path |
|---|---|---|---|---|---|---|
| short (6s) | small.en | 37.5% | 2,555 ms | 2,506 ms | 5,061 ms | fallback |
| short (6s) | **base.en** | **12.5%** | **1,026 ms** | 2,186 ms | **3,212 ms** | haiku |
| medium (20s) | small.en | **11.1%** | 4,412 ms | 753 ms | 5,165 ms | haiku |
| medium (20s) | **base.en** | 18.5% | **1,125 ms** | 1,043 ms | **2,168 ms** | haiku |
| long (52s) | both | 100% | ~200 ms | 0 ms | ~200 ms | rules |

Model load (one-time, at daemon start): small.en 4.29s · base.en 2.46s.

**`base.en` is 2.5–3.9× faster on the whisper step** and cuts total latency roughly
in half on both usable clips.

Accuracy is genuinely mixed, and the comparison is thinner than it looks:

- On `short`, small.en's 37.5% is partly an artefact — its cleanup **timed out and
  fell back to rules**, so that row isn't comparing like with like.
- On `medium`, small.en is honestly better (11.1% vs 18.5%). base.en produced
  "loop an icon" where small.en produced "loop an IKEA" — both wrong for
  "loop in Anika". Neither model gets the proper noun; the `~/.murmur/vocab.txt`
  (125 terms) and `corrections.tsv` layers exist to catch exactly this.

### Recommendation, not a change

`base.en` is very likely the right default on this hardware — it roughly halves
the latency floor, and the vocab/corrections layers now cover the proper-noun
weakness that the old `config.py` note warned about (that note predates the vocab
expansion from 101 → 125 terms).

**It has deliberately not been switched.** Two usable clips is thin evidence for
changing the default on a tool used a hundred times a day, and accuracy regressions
are far more annoying than latency. Try it for a day — it's one line and reverts
just as easily:

```bash
echo base.en > ~/.murmur/whisper-model && launchctl kickstart -k gui/$UID/com.murmur.daemon
# revert:
rm ~/.murmur/whisper-model && launchctl kickstart -k gui/$UID/com.murmur.daemon
```

Then re-run the numbers in §1 against fresh history and compare on real usage
rather than three clips.

---

## 3. ⚠ `bench/clips/long.wav` is a bad recording — the harness is lying about it

The long clip reports 100% WER for **every** model, which looks like a model or
pipeline failure. It is neither.

```
long.wav: 52.0s @ 16000Hz  peak=1.0000  rms=0.04822
vad_filter=True     0.2s   chars=0    -> ''
vad_filter=False   12.7s   chars=41   -> 'Okay. Okay. Okay. Okay. Okay. Okay. Okay.'
```

The audio is **clipping** (peak 1.0) and its actual content is someone saying
"Okay" repeatedly — nothing like the long brain-dump in `ground_truth.json`. The
recording is wrong, not the transcription.

Consequences worth knowing:

1. **One third of the benchmark's signal is garbage**, and it reads as a model
   failure. Any future model comparison run against this harness is corrupted by
   it. This likely explains earlier confusing bench results.
2. VAD is behaving correctly by rejecting degenerate audio — but it returns an
   empty transcript in 0.2s with no distinct signal. In real use (a quiet room, a
   mic that didn't engage) the user gets silence and no explanation. Commit
   `a01bc73` added visible fallback for paste/cleanup failures; **VAD-returns-nothing
   does not appear to be covered.** Worth a distinct "heard nothing" state.

**Fix:** re-record with `murmur record-clip long 52` — it needs a real voice, so it
can't be automated. Until then, treat the long row as no data, not as a failure.

---

## 4. Where the ~4s floor actually goes

Roughly, for a short utterance on `small.en`:

```
whisper transcribe   ~2.5s     ← model choice; base.en cuts this to ~1.0s
Haiku cleanup        ~2.5s     ← hard budget, hit 18% of the time
────────────────────────────
total                ~5.0s
```

Both halves are addressable, and they're independent:

- **Whisper side:** `base.en` is the immediate lever (§2). Longer term, this is an
  Intel Mac — `config.py` already documents that mlx-whisper and whisper.cpp+Metal
  were ruled out *on this hardware* and would likely win on Apple Silicon. On an
  M-series machine this whole section changes.
- **Cleanup side:** cleanup runs **after** transcription completes. Since the local
  rules result is available immediately, an alternative is to paste the rules
  output at once and revise in place if Haiku returns within budget — turning a
  2.5s wait into a 0s wait plus an occasional correction. That's a design change,
  not a tuning change, and it's the single biggest available win on the floor.
