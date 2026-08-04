"""Static guard against calling a function that does not exist.

Written after 2026-08-04, when a refactor deleted _send_cmd_v() but left
paste_text() calling it. Every dictation then died with

    NameError: name '_send_cmd_v' is not defined

and the only thing the user saw was the pill reporting "paste failed".

The behavioural suites could not catch this. They stub the keystroke layer
precisely so tests never type into the real machine, which means the real
call site is never executed and a missing name is invisible. Coverage of
behaviour and coverage of existence are different things; this file supplies
the second.

Deliberately static -- no imports of the daemon, no side effects. It runs in
about a second and would have turned an evening of "why is paste broken"
into one line of output.
"""
import pathlib
import subprocess
import sys

FAILS = []


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "wispr"

print("\n1. pyflakes finds no undefined names")

try:
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(PKG)],
        capture_output=True, text=True, timeout=120)
    available = "No module named" not in proc.stderr
except Exception as e:  # pragma: no cover
    proc, available = None, False
    print(f"  could not run pyflakes: {e}")

if not available:
    print("  SKIP  pyflakes not installed (pip install pyflakes)")
else:
    lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    undefined = [l for l in lines if "undefined name" in l]
    for l in undefined:
        print("        " + l)
    check("no undefined names in wispr/", not undefined)

    # Unused imports are noise, not breakage -- report but never fail on them.
    unused = [l for l in lines if "imported but unused" in l]
    if unused:
        print(f"  note: {len(unused)} unused import(s), not a failure")

print("\n2. every module compiles")
for src in sorted(PKG.glob("*.py")):
    proc = subprocess.run([sys.executable, "-m", "py_compile", str(src)],
                          capture_output=True, text=True)
    check(f"{src.name} compiles", proc.returncode == 0)
    if proc.returncode != 0:
        print("        " + proc.stderr.strip()[:200])

print("\n3. the paste path's helpers actually exist")
# Named explicitly rather than discovered, so deleting one is a test failure
# rather than a silently smaller test.
import ast  # noqa: E402

tree = ast.parse((PKG / "daemon.py").read_text())
defined = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

for fn in ("_send_cmd_v", "_send_backspaces", "paste_text",
           "_pasteboard_read", "_pasteboard_write"):
    check(f"daemon.{fn} is defined", fn in defined)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("ALL STATIC-INTEGRITY TESTS PASSED")
