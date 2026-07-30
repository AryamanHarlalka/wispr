"""murmur CLI: daemon (default) · doctor · set-key · fix · vocab · history ·
menubar · bench · restart."""
from __future__ import annotations

import sys

USAGE = """murmur — local voice dictation for macOS

  murmur                     run the daemon (normally launchd does this)
  murmur doctor              diagnose why it isn't working, with fixes
  murmur set-key             store an Anthropic API key in your keychain
  murmur set-key --clear     remove it (falls back to local rules cleanup)
  murmur restart             restart the background service
  murmur fix <wrong> <right> teach it a word it keeps mishearing
  murmur vocab               rebuild the vocabulary list
  murmur history [n]         show recent dictations
  murmur menubar             run the optional menu-bar companion
  murmur bench               measure accuracy and latency
"""


def _set_key(args: list[str]) -> None:
    from .config import clear_anthropic_key, set_anthropic_key
    if args and args[0] in ("--clear", "-c"):
        print("[murmur] key removed" if clear_anthropic_key()
              else "[murmur] no key was stored")
        return
    print("Murmur transcribes locally and never uploads audio.")
    print("An Anthropic API key enables a fast cleanup pass on the TEXT only")
    print("(filler removal, punctuation, spoken self-corrections).")
    print("Get one at https://console.anthropic.com/settings/keys")
    print()
    try:
        import getpass
        # getpass keeps the key off the screen and out of shell history —
        # the reason this command exists rather than documenting a raw
        # `security add-generic-password` line with the key in argv.
        key = getpass.getpass("Paste your key (sk-ant-...), or Enter to skip: ")
    except (EOFError, KeyboardInterrupt):
        print("\n[murmur] cancelled")
        return
    key = key.strip()
    if not key:
        print("[murmur] skipped — local rules cleanup will be used")
        return
    if not key.startswith("sk-ant-"):
        print("[murmur] that doesn't look like an Anthropic key "
              "(expected sk-ant-...) — not saved")
        return
    set_anthropic_key(key)
    print("[murmur] key saved to your macOS keychain (never to disk or git)")
    print("[murmur] restart to pick it up:  murmur restart")


def _restart() -> None:
    import os
    import subprocess
    label = "com.murmur.daemon"
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[murmur] restarted {label}")
    else:
        print(f"[murmur] could not restart: {r.stderr.strip()[:160]}")
        print("[murmur] is it installed?  ./install/install.sh")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "daemon"

    if cmd == "daemon":
        from .daemon import main as run
        run()
    elif cmd in ("doctor", "status"):
        from .doctor import main as run_doctor
        run_doctor()
    elif cmd == "set-key":
        _set_key(args[1:])
    elif cmd == "restart":
        _restart()
    elif cmd == "fix":
        if len(args) != 3:
            print("usage: murmur fix <wrong> <right>")
            sys.exit(1)
        from .config import add_correction
        add_correction(args[1], args[2])
        print(f"[murmur] correction saved: '{args[1]}' -> '{args[2]}'")
    elif cmd == "vocab":
        from .vocab import write_vocab
        p = write_vocab()
        n = len(p.read_text().splitlines())
        print(f"[murmur] wrote {n} terms -> {p}")
    elif cmd == "history":
        from .history import recent
        for r in recent(int(args[1]) if len(args) > 1 else 5):
            print(f"{r['ts']}  [{r['mode']}/{r['path']}  {r['latency_ms']}ms]  "
                  f"{r['cleaned'][:80]}")
    elif cmd == "menubar":
        from .menubar import main as run_mb
        run_mb()
    elif cmd == "bench":
        from .bench import main as run_bench
        sys.argv = [sys.argv[0]] + args[1:]
        run_bench()
    elif cmd in ("-h", "--help", "help"):
        print(USAGE)
    else:
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
