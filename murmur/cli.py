"""murmur CLI: daemon (default) · fix · vocab · history · menubar."""
from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "daemon"

    if cmd == "daemon":
        from .daemon import main as run
        run()
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
    else:
        print("usage: murmur [daemon|fix <wrong> <right>|vocab|history [n]|menubar]")
        sys.exit(1)


if __name__ == "__main__":
    main()
