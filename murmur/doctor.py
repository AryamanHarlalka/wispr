"""`murmur doctor` — one command that answers "why isn't this working?".

Written after a 2026-07-30 incident where two LaunchAgents had been running
two daemons against the same microphone for two weeks. Nothing surfaced it:
`launchctl` reported a healthy process, the menu bar looked normal, and the
only symptom was dictation failing "randomly". Finding it required reading
raw `ps` output and knowing what to look for.

The lesson generalised: a health check that asks "is the process alive?"
misses every failure where the process is alive and not doing its job. Each
check below therefore asserts a capability, not a PID, and every failure
prints the exact command that fixes it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import (MURMUR_HOME, VAULT, anthropic_key, revise_window_s,
                     paste_instant, paste_revise, stt_backend, stt_model)

LABEL = "com.murmur.daemon"

GRN = "\033[32m"; RED = "\033[31m"; YLW = "\033[33m"
DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"

_results: list[tuple[str, str]] = []


def _emit(level: str, title: str, detail: str = "", fix: str = "") -> None:
    mark = {"ok": f"{GRN}✓{RST}", "warn": f"{YLW}!{RST}", "fail": f"{RED}✗{RST}"}[level]
    print(f"  {mark} {title}")
    if detail:
        for line in detail.splitlines():
            print(f"      {DIM}{line}{RST}")
    if fix:
        for line in fix.splitlines():
            print(f"      {BOLD}{line}{RST}")
    _results.append((level, title))


def _sh(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 1, str(e)


def _daemon_processes() -> list[str]:
    """Actual daemon processes only.

    A substring match on "-m murmur" is not good enough: it also catches
    `-m murmur doctor` (this very process), the shell that launched it, and
    any wrapper whose argv happens to quote the command. The daemon is the
    invocation with NO subcommand, i.e. argv ending exactly in `-m murmur`,
    so match on that and drop our own process and our parent."""
    _, ps_out = _sh("ps", "-Ao", "pid=,command=")
    mine = {os.getpid(), os.getppid()}
    out: list[str] = []
    for ln in ps_out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        pid_str, _, cmd = ln.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in mine:
            continue
        toks = cmd.split()
        if len(toks) >= 3 and toks[-2] == "-m" and toks[-1] == "murmur":
            out.append(f"{pid} {cmd}")
    return out


def check_duplicates() -> None:
    """The 2026-07-30 bug. Two daemons on one input device produce
    intermittent CoreAudio failures that look like random breakage."""
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plists = sorted(p.name for p in agents_dir.glob("*.plist")
                    if "murmur" in p.name.lower() or "wispr" in p.name.lower())
    _, out = _sh("launchctl", "list")
    loaded = sorted({ln.split()[-1] for ln in out.splitlines()
                     if "murmur" in ln.lower() or "wispr" in ln.lower()})
    procs = _daemon_processes()

    if len(plists) > 1 or len(loaded) > 1 or len(procs) > 1:
        extra = [p for p in plists if p != f"{LABEL}.plist"]
        _emit("fail", f"{len(procs)} daemons running, {len(plists)} LaunchAgent(s) installed",
              "More than one daemon competing for the same microphone causes\n"
              "intermittent 'error recording voice' and dead hotkeys.\n"
              + "\n".join(f"plist: {p}" for p in plists)
              + ("\n" + "\n".join(f"proc:  {p[:90]}" for p in procs) if procs else ""),
              "Remove the extras, keep " + LABEL + ":\n"
              + "\n".join(
                  f"launchctl bootout gui/$(id -u)/{p[:-6]}; "
                  f"rm ~/Library/LaunchAgents/{p}" for p in extra))
    elif len(procs) == 1 and len(plists) == 1:
        _emit("ok", "One daemon, one LaunchAgent")
    elif not procs:
        _emit("fail", "No daemon running",
              "The hotkey cannot work with nothing listening.",
              f"launchctl kickstart -k gui/$(id -u)/{LABEL}")
    else:
        _emit("warn", f"{len(procs)} process(es), {len(plists)} plist(s)",
              "Unexpected combination — check manually with:\n"
              "launchctl list | grep -i murmur")


def check_listener_alive() -> None:
    """A daemon can hold its PID while its hotkey listener thread is dead —
    the exact 'alive but deaf' failure. The daemon logs a line on every
    listener (re)start, so a recent 'daemon running' with no trailing
    traceback is the signal that it is actually listening."""
    err = MURMUR_HOME / "logs" / "daemon.err.log"
    out = MURMUR_HOME / "logs" / "daemon.out.log"
    if not out.exists():
        _emit("warn", "No daemon log yet",
              f"Expected {out} — it appears on first run.")
        return
    tail = out.read_text(errors="ignore").splitlines()[-25:]
    running = any("daemon running" in ln for ln in tail)
    ready = any("ready — hold" in ln or "ready --" in ln for ln in tail)
    if running and ready:
        _emit("ok", "Hotkey listener started and model loaded")
    elif ready:
        _emit("warn", "Model loaded but no 'daemon running' line",
              "The listener may not have started.")
    else:
        _emit("warn", "Daemon log has no recent ready/running line",
              f"Check: tail -20 {out}")

    if err.exists():
        etail = err.read_text(errors="ignore").splitlines()[-40:]
        if any("Traceback" in ln for ln in etail):
            _emit("warn", "Recent traceback in the error log",
                  "May predate the last restart — check timestamps.",
                  f"tail -40 {err}")


def check_accessibility() -> None:
    """Without Accessibility, pynput sees no keys and paste cannot fire.
    TCC attributes trust to the real binary path, so this process (same
    interpreter as the daemon) is a faithful proxy."""
    try:
        from ApplicationServices import AXIsProcessTrusted
    except Exception:
        _emit("warn", "Could not check Accessibility",
              "pyobjc ApplicationServices unavailable.")
        return
    real = os.path.realpath(sys.executable)
    if AXIsProcessTrusted():
        _emit("ok", "Accessibility granted")
    else:
        _emit("fail", "Accessibility NOT granted",
              "The hotkey cannot be detected and paste will fail.",
              "System Settings → Privacy & Security → Accessibility → [+]\n"
              "Press Cmd-Shift-G and paste this exact path:\n"
              f"{real}")


def check_microphone() -> None:
    """Assert the capability: actually open an input stream. A granted
    permission that still fails to open (device wedged, in use) is the
    failure mode that matters."""
    try:
        import sounddevice as sd
    except Exception as e:
        _emit("fail", "sounddevice not importable", str(e)[:120],
              "Re-run ./install/install.sh")
        return
    try:
        devs = sd.query_devices()
        ins = [d for d in devs if d.get("max_input_channels", 0) > 0]
        if not ins:
            _emit("fail", "No input device found",
                  "macOS reports no microphone.")
            return
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32"):
            pass
        default_in = sd.query_devices(kind="input").get("name", "?")
        _emit("ok", f"Microphone opens cleanly ({default_in})")
    except Exception as e:
        _emit("fail", "Cannot open the microphone", f"{e.__class__.__name__}: {str(e)[:140]}",
              "Grant Microphone: System Settings → Privacy & Security → Microphone\n"
              "If already granted, another app may be holding the device — or a\n"
              "second murmur daemon is running (see the duplicate check above).")


def check_key() -> None:
    key = anthropic_key()
    if not key:
        _emit("warn", "No Anthropic API key — local rules cleanup only",
              "Transcription still works fully offline; text is just less polished.",
              "murmur set-key")
        return
    if not key.startswith("sk-ant-"):
        _emit("warn", "Key found but does not look like an Anthropic key",
              "Expected it to start with sk-ant-.", "murmur set-key")
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(model="claude-haiku-4-5", max_tokens=4,
                               messages=[{"role": "user", "content": "hi"}])
        _emit("ok", "Anthropic key works (Haiku cleanup active)")
    except Exception as e:
        _emit("fail", "Anthropic key present but the API call failed",
              f"{e.__class__.__name__}: {str(e)[:140]}",
              "Check the key is valid and has credit: murmur set-key")


def check_model() -> None:
    backend, model = stt_backend(), stt_model()
    _emit("ok", f"Backend: {backend} · model: {model}",
          "Apple Silicon auto-selects the GPU path; Intel uses CPU."
          if backend == "faster-whisper" else "")
    try:
        if backend == "faster-whisper":
            from faster_whisper import WhisperModel
            WhisperModel(model, device="cpu", compute_type="int8")
        elif backend == "mlx-whisper":
            import mlx_whisper  # noqa: F401
        _emit("ok", "Speech model loads")
    except Exception as e:
        _emit("fail", "Speech model failed to load",
              f"{e.__class__.__name__}: {str(e)[:140]}",
              "Re-run ./install/install.sh to refetch it.")


def check_config() -> None:
    vocab = MURMUR_HOME / "vocab.txt"
    n = len(vocab.read_text().splitlines()) if vocab.exists() else 0
    _emit("ok", f"Instant paste: {'on' if paste_instant() else 'off'} · "
                f"revise: {'on' if paste_revise() else 'off'} "
                f"({revise_window_s():.1f}s window)")
    _emit("ok", f"Vocabulary: {n} terms" + (f" · vault: {VAULT}" if VAULT else ""))


def main() -> None:
    print(f"\n{BOLD}murmur doctor{RST}\n")
    for fn in (check_duplicates, check_listener_alive, check_accessibility,
               check_microphone, check_model, check_key, check_config):
        try:
            fn()
        except Exception as e:  # a broken check must not hide the others
            _emit("warn", f"Check {fn.__name__} errored",
                  f"{e.__class__.__name__}: {str(e)[:120]}")
    fails = sum(1 for lvl, _ in _results if lvl == "fail")
    warns = sum(1 for lvl, _ in _results if lvl == "warn")
    print()
    if fails:
        print(f"  {RED}{BOLD}{fails} problem(s) to fix{RST} "
              f"{DIM}({warns} warning(s)){RST}\n")
        sys.exit(1)
    if warns:
        print(f"  {YLW}Working, with {warns} optional improvement(s).{RST}\n")
    else:
        print(f"  {GRN}{BOLD}All good.{RST} Hold Right Option and speak.\n")


if __name__ == "__main__":
    main()
