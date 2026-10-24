"""Replay recent real raws through cleanup; flag big shrinkage."""
import json, sys, time
from pathlib import Path
from wispr import cleanup
from wispr.config import load_corrections, load_snippets, load_vocab
H = Path.home()/".wispr/history.jsonl"
recs = [json.loads(l) for l in H.open() if l.strip()]
recs = [r for r in recs if "raw" in r and len(r["raw"].split()) >= 25][-int(sys.argv[1] if len(sys.argv)>1 else 6):]
v, c, s = load_vocab(), load_corrections(), load_snippets()
for r in recs:
    t=time.time(); out, path = cleanup.clean_text(r["raw"], "ai", v, c, s, budget_s=7)
    ratio = len(out.split())/max(1,len(r["raw"].split()))
    print(f"--- {r['ts']} {path} {int((time.time()-t)*1000)}ms words {len(r['raw'].split())}->{len(out.split())} ({ratio:.2f})")
    print("RAW:", r["raw"][:400]); print("OUT:", out[:400])
