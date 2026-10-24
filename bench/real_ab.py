"""Compare the cloud transcript stored in history against local base.en on
the same saved take (~/.wispr/audio). Usage: real_ab.py [N]"""
import json, sys, wave, difflib
from pathlib import Path
import numpy as np
from faster_whisper import WhisperModel
from wispr import vocab as vm, dictionary as dm
from wispr.config import load_vocab, load_corrections
H = Path.home()/".wispr"
recs = {}
for l in (H/"history.jsonl").open():
    if l.strip():
        r = json.loads(l); recs.setdefault(r.get("id"), {}).update(r)
m = WhisperModel("base.en", device="cpu", compute_type="int8")
prompt = vm.initial_prompt(load_vocab(), load_corrections(), dm.load_dictionary())
wavs = sorted((H/"audio").glob("*.wav"), key=lambda p: p.stat().st_mtime)[-int(sys.argv[1] if len(sys.argv)>1 else 3):]
for w in wavs:
    r = recs.get(w.stem, {})
    with wave.open(str(w)) as f:
        a = np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32)/32768
    segs,_ = m.transcribe(a, language="en", beam_size=1, vad_filter=True, initial_prompt=prompt, condition_on_previous_text=False)
    local = " ".join(s.text.strip() for s in segs)
    cloud = r.get("raw","")
    print(f"\n=== {r.get('ts')} {len(a)/16000:.0f}s  stt={r.get('stt')}")
    lw, cw = local.split(), cloud.split()
    for op,i1,i2,j1,j2 in difflib.SequenceMatcher(None,[x.lower().strip('.,?!') for x in lw],[x.lower().strip('.,?!') for x in cw]).get_opcodes():
        if op!="equal": print(f"  local: {' '.join(lw[i1:i2])!r:40}  cloud: {' '.join(cw[j1:j2])!r}")
