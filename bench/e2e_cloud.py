"""Pipeline check without pasting: cloud STT -> Haiku cleanup on bench clips."""
import json, sys, time, wave
from pathlib import Path
import numpy as np
from wispr import cloud_stt, cleanup, dictionary as dm
from wispr.config import load_corrections, load_snippets, load_vocab
R = Path(__file__).parent
def load(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)/32768
vocab, corr = load_vocab(), load_corrections()
terms = dm.prompt_terms(vocab, corr, dm.load_dictionary(), limit=150)
prompt = cloud_stt.build_prompt(terms)
for p in [R/"clips/medium.wav", R/"clips/short.wav"] + sorted((R/"dumps").glob("*.wav"))[-2:]:
    a = load(p); t = time.time()
    quiet = a * 0.06   # simulate speaking softly (~ -24 dB)
    for label, x in (("normal", a), ("soft", quiet)):
        t = time.time(); raw, why = cloud_stt.transcribe(x, prompt); t1 = time.time()
        out, path = cleanup.clean_text(raw or "", "ai", vocab, corr, load_snippets(), budget_s=5)
        print(f"{p.name} [{label}] stt {why} | clean {path} {int((time.time()-t1)*1000)}ms\n  RAW: {raw}\n  OUT: {out}")
