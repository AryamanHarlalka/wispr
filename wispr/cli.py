"""wispr CLI: daemon (default) · doctor · set-key · fix · vocab · history ·
last · menubar · bench · restart."""
from __future__ import annotations

import sys

USAGE = """wispr — local voice dictation for macOS

  wispr                     run the daemon (normally launchd does this)
  wispr doctor              diagnose why it isn't working, with fixes
  wispr mic-test [secs]     live input meter — is the mic actually working?
  wispr set-key             store an Anthropic API key in your keychain
  wispr set-key --clear     remove it (falls back to local rules cleanup)
  wispr restart             restart the background service
  wispr add <word> [...]    add words to your personal dictionary
  wispr fix <wrong> <right> teach it a word it keeps mishearing
  wispr words               show your dictionary and learned corrections
  wispr forget <term>       remove a dictionary word or a correction
  wispr vocab               rebuild the vault vocabulary list
  wispr history [n]         show recent dictations
  wispr last                reprint the last dictation and copy it back
  wispr last --no-copy      print it without touching the clipboard
  wispr menubar             run the optional menu-bar companion
  wispr bench               measure accuracy and latency
"""


def _set_key(args: list[str]) -> None:
    from .config import clear_anthropic_key, set_anthropic_key
    if args and args[0] in ("--clear", "-c"):
        print("[wispr] key removed" if clear_anthropic_key()
              else "[wispr] no key was stored")
        return
    print("Wispr transcribes locally and never uploads audio.")
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
        print("\n[wispr] cancelled")
        return
    key = key.strip()
    if not key:
        print("[wispr] skipped — local rules cleanup will be used")
        return
    if not key.startswith("sk-ant-"):
        print("[wispr] that doesn't look like an Anthropic key "
              "(expected sk-ant-...) — not saved")
        return
    set_anthropic_key(key)
    print("[wispr] key saved to your macOS keychain (never to disk or git)")
    print("[wispr] restart to pick it up:  wispr restart")


def _mic_test(seconds: float = 6.0) -> None:
    """Live input-level meter.

    Exists because 'the pill says it heard nothing' has two unrelated
    causes -- no audio reaching the process, or audio that failed to
    transcribe -- and they are indistinguishable from the UI. This shows
    the raw signal, so the answer takes seconds instead of an evening.
    """
    import numpy as np
    import sounddevice as sd

    from .daemon import SAMPLE_RATE, _resample_to_16k

    dev = sd.query_devices(kind="input")
    native = int(dev["default_samplerate"])
    print(f"\nInput device : {dev['name']}")
    print(f"Native rate  : {native} Hz    (Wispr wants {SAMPLE_RATE} Hz)")

    rate = SAMPLE_RATE
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=1)
        print(f"16 kHz open  : OK")
    except Exception as e:
        rate = native
        print(f"16 kHz open  : REFUSED ({str(e)[:60]})")
        print(f"               -> capturing at {native} Hz and resampling")

    print(f"\nSpeak normally for {seconds:.0f} seconds…\n")
    frames = []

    def cb(indata, n, t, status):  # noqa: ANN001
        frames.append(indata.copy())
        lvl = float(np.abs(indata).mean())
        bars = min(int(lvl * 600), 50)
        print(f"\r  [{'#' * bars:<50}] {lvl:.5f}", end="", flush=True)

    try:
        with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                            callback=cb):
            import time as _t
            _t.sleep(seconds)
    except Exception as e:
        print(f"\n\n  COULD NOT OPEN THE MICROPHONE: "
              f"{e.__class__.__name__}: {e}")
        print("  Grant Microphone in System Settings → Privacy & Security.")
        return

    print("\n")
    if not frames:
        print("  No audio callbacks fired at all — the device never started.")
        return
    audio = _resample_to_16k(np.concatenate(frames).flatten(), rate)
    rms = float(np.sqrt(np.mean(np.square(audio))))
    peak = float(np.max(np.abs(audio)))
    print(f"  captured {audio.size / SAMPLE_RATE:.1f}s   rms={rms:.6f}   peak={peak:.6f}")
    if rms < 1e-4:
        print("\n  SILENT. The stream opened but no sound arrived.")
        print("  This is a permission or device problem, not a Wispr problem:")
        print("   1. System Settings → Privacy & Security → Microphone —")
        print("      make sure Python (and/or Terminal) is listed AND toggled on.")
        print("   2. Check the input device and level in Sound settings.")
        print("   3. Check nothing else is holding the mic (Zoom, Teams, Meet).")
    elif rms < 0.005:
        print("\n  Very quiet, but not silent. Move closer or raise the input")
        print("  volume in System Settings → Sound → Input.")
    else:
        print("\n  Microphone is working properly. If dictation still fails,")
        print("  the problem is downstream — run: wispr doctor")


def _last(args: list[str]) -> None:
    """Recovery affordance (2026-08-04).

    Exists because of the "text appears, then vanishes" incident: a paste
    or an in-place revision can fail in ways this tool cannot undo from
    inside the target app, and the user is then staring at an empty
    document with no idea whether the dictation survived. It did — the
    daemon writes the transcript to history before it attempts the paste
    — so the answer needs to be one command, not a jsonl file and a
    text editor.

    Copying is the default. Printing alone would still leave the user
    retyping something Wispr already has.
    """
    from .history import last
    rec = last()
    if rec is None:
        print("[wispr] no dictations recorded yet")
        return
    text = rec.get("cleaned") or rec.get("raw") or ""
    if not text:
        print(f"[wispr] the last entry ({rec.get('ts', '?')}) has no text")
        return
    print(text)
    if any(a in ("--no-copy", "-n") for a in args):
        return
    try:
        # Straight to NSPasteboard rather than through daemon.py: this
        # runs in a throwaway CLI process, so there is no restore thread
        # to coordinate with, and importing the daemon would drag in
        # numpy and the STT backend for a clipboard write.
        from AppKit import NSPasteboard, NSPasteboardTypeString
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
    except Exception as e:
        print(f"\n[wispr] could not copy to the clipboard: "
              f"{e.__class__.__name__}: {e}")
        return
    print(f"\n[wispr] copied to the clipboard — {rec.get('ts', '?')} "
          f"· {rec.get('app', '?')} · {rec.get('path', '?')}")


def _restart() -> None:
    import os
    import subprocess
    label = "com.wispr.daemon"
    r = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[wispr] restarted {label}")
    else:
        print(f"[wispr] could not restart: {r.stderr.strip()[:160]}")
        print("[wispr] is it installed?  ./install/install.sh")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "daemon"

    if cmd == "daemon":
        from .daemon import main as run
        run()
    elif cmd in ("doctor", "status"):
        from .doctor import main as run_doctor
        run_doctor()
    elif cmd in ("mic-test", "mictest"):
        _mic_test(float(args[1]) if len(args) > 1 else 6.0)
    elif cmd == "set-key":
        _set_key(args[1:])
    elif cmd == "restart":
        _restart()
    elif cmd in ("last", "recover"):
        _last(args[1:])
    elif cmd == "fix":
        if len(args) != 3:
            print("usage: wispr fix <wrong> <right>")
            sys.exit(1)
        from . import dictionary
        if dictionary.record_correction(args[1], args[2]):
            dictionary.add_words([args[2]])
            print(f"[wispr] correction saved: '{args[1]}' -> '{args[2]}' "
                  f"(applies from the next dictation)")
        else:
            print(f"[wispr] not saved — already known, or '{args[1]}' is "
                  f"itself a dictionary word")
    elif cmd == "add":
        if len(args) < 2:
            print("usage: wispr add <word> [<word> ...]   (quote multi-word terms)")
            sys.exit(1)
        from . import dictionary
        added = dictionary.add_words(args[1:])
        skipped = [w for w in args[1:] if w not in added]
        if added:
            print(f"[wispr] added: {', '.join(added)}")
        if skipped:
            print(f"[wispr] already there or not a word: {', '.join(skipped)}")
    elif cmd in ("words", "dictionary", "dict"):
        from . import dictionary
        from .config import load_corrections
        words = dictionary.load_dictionary()
        corr = load_corrections()
        learned = dictionary.load_learned()
        print(f"Dictionary ({len(words)}):")
        for w in words:
            print(f"  {w}")
        print(f"\nYour corrections, applied verbatim ({len(corr)}):")
        for wrong, right in corr.items():
            print(f"  {wrong!r:28} -> {right}")
        print(f"\nLearned from your edits and cleanup, used as hints "
              f"({len(learned)}):")
        for wrong, right in learned.items():
            print(f"  {wrong!r:28} -> {right}")
        print("\nadd: wispr add <word>   fix: wispr fix <wrong> <right>   "
              "remove: wispr forget <term>")
    elif cmd == "forget":
        if len(args) < 2:
            print("usage: wispr forget <term>")
            sys.exit(1)
        from . import dictionary
        term = " ".join(args[1:])
        a = dictionary.remove_word(term)
        b = dictionary.remove_correction(term)
        c = dictionary.remove_learned(term)
        print(f"[wispr] forgot '{term}'" if (a or b or c)
              else f"[wispr] '{term}' was not in the dictionary or corrections")
    elif cmd == "vocab":
        from .vocab import write_vocab
        p = write_vocab()
        n = len(p.read_text().splitlines())
        print(f"[wispr] wrote {n} terms -> {p}")
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
