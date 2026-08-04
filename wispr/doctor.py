"""`wispr doctor` — one command that answers "why isn't this working?".

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

from .config import (WISPR_HOME, VAULT, anthropic_key, hotkey, revise_window_s,
                     paste_instant, paste_revise, stt_backend, stt_model)

LABEL = "com.wispr.daemon"

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

    A substring match on "-m wispr" is not good enough: it also catches
    `-m wispr doctor` (this very process), the shell that launched it, and
    any wrapper whose argv happens to quote the command. The daemon is the
    invocation with NO subcommand, i.e. argv ending exactly in `-m wispr`,
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
        if len(toks) >= 3 and toks[-2] == "-m" and toks[-1] == "wispr":
            out.append(f"{pid} {cmd}")
    return out


def _python_app_binary() -> str | None:
    """Path to the Python.app the daemon actually runs as.

    A framework Python re-execs into this bundle whenever the process needs
    to talk to the window server -- which the menu-bar indicator and
    CoreAudio both do. `ps` confirms it: the LaunchAgent names
    .venv/bin/python, the live process is Python.app/Contents/MacOS/Python.
    """
    base = getattr(sys, "_base_executable", None) or sys.executable
    version_dir = Path(os.path.realpath(base)).parent.parent
    app = version_dir / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return str(app) if app.exists() else None


def _python_bundle_id() -> str:
    """Code-signing identity TCC keys microphone consent on.

    Consent follows the signing identity, not the file path -- which is why
    the venv symlink and the framework binary share one grant, and why the
    reset can be scoped to Python alone instead of clearing every app on
    the machine. Must be read from Python.app: the bare interpreters are
    signed "python3", which is not the identity TCC stored.
    """
    for candidate in (_python_app_binary(), sys.executable):
        if not candidate:
            continue
        _, out = _sh("codesign", "-dv", candidate)
        for line in out.splitlines():
            if line.startswith("Identifier="):
                ident = line.split("=", 1)[1].strip()
                if ident and ident != "python3":
                    return ident
    return "org.python.python"


# Our own launchd jobs, and only ours. A loose "wispr" substring match is
# NOT safe: an unrelated commercial app, Wispr Flow, registers jobs under
# com.electron.wispr-flow (e.g. ...ShipIt, Squirrel's auto-updater, which
# never opens an input device). Matching those once reported a perfectly
# healthy one-daemon system as a hard failure, with printed counts
# ("1 daemons running, 1 LaunchAgent(s)") that contradicted the verdict.
# The com.wispr. prefix covers every label this project has ever used and
# nothing that Wispr Flow installs.
_OUR_LABEL_PREFIX = "com.wispr."


def _is_ours(name: str) -> bool:
    return name.lower().startswith(_OUR_LABEL_PREFIX)


def check_duplicates() -> None:
    """The 2026-07-30 bug. Two daemons on one input device produce
    intermittent CoreAudio failures that look like random breakage.

    Scoped to our own com.wispr.* labels only -- see _is_ours() above for
    why a bare substring match is wrong. Genuine microphone contention is
    a separate concern and is reported as a warning by
    check_mic_contention().
    """
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plists = sorted(p.name for p in agents_dir.glob("*.plist")
                    if _is_ours(p.name))
    _, out = _sh("launchctl", "list")
    loaded = sorted({ln.split()[-1] for ln in out.splitlines()
                     if _is_ours(ln.split()[-1])})
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
              "launchctl list | grep -i com.wispr")


# Apps that hold the input device for long stretches. Not errors -- macOS
# permits sharing -- but they are the usual cause of an intermittently dead
# hotkey, and naming them saves the "why does it only sometimes work" hunt.
_MIC_HOGS = {
    "Wispr Flow": "wispr flow",
    "zoom.us": "zoom.us",
    "Microsoft Teams": "microsoft teams",
    "Google Meet": "google meet",
}


def check_mic_contention() -> None:
    """Report other processes currently holding the microphone."""
    _, out = _sh("ps", "-Axo", "comm")
    running = out.lower()
    hogs = [name for name, needle in _MIC_HOGS.items() if needle in running]
    if hogs:
        _emit("warn", f"Also using the microphone: {', '.join(hogs)}",
              "macOS lets processes share an input device, but contention is\n"
              "the usual cause of dictation that works only sometimes.",
              "Quit them and retry if dictation is intermittent.")
    else:
        _emit("ok", "No other known microphone users running")


def check_listener_alive() -> None:
    """A daemon can hold its PID while its hotkey listener thread is dead —
    the exact 'alive but deaf' failure. The daemon logs a line on every
    listener (re)start, so a recent 'daemon running' with no trailing
    traceback is the signal that it is actually listening."""
    err = WISPR_HOME / "logs" / "daemon.err.log"
    out = WISPR_HOME / "logs" / "daemon.out.log"
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


def _launchd_mic_probe(timeout: float = 75.0) -> tuple[str, str]:
    """Open the microphone with the *daemon's* TCC identity.

    macOS attributes microphone consent to the responsible process, not to
    the binary that calls into CoreAudio. Run from a shell, the responsible
    process is Terminal -- which already has consent -- so an in-process
    probe passes even while the launchd-started daemon receives nothing but
    zeros. That false negative cost five days (2026-07-27 to 2026-08-01) of
    silent captures that every other check called healthy.

    The only faithful test is one launchd starts, so this writes a
    throwaway LaunchAgent, runs it once, and removes it.

    Returns (verdict, detail) where verdict is "ok" | "silent" | "unknown".
    """
    import json
    import tempfile
    import time

    label = "com.wispr.micprobe"
    home = Path.home()
    plist = home / "Library" / "LaunchAgents" / f"{label}.plist"
    outp = Path(tempfile.gettempdir()) / "wispr-micprobe.json"
    script = Path(tempfile.gettempdir()) / "wispr-micprobe.py"
    # Probe the binary the LaunchAgent runs, not this shell's interpreter.
    # They are different identities, and that difference is the whole bug.
    runner = _daemon_runner() or sys.executable

    script.write_text(
        "import json, sys\n"
        "import numpy as np, sounddevice as sd\n"
        "try:\n"
        "    r = sd.rec(24000, samplerate=16000, channels=1, dtype='float32')\n"
        "    sd.wait()\n"
        "    a = np.abs(r)\n"
        "    out = {'ok': True, 'rms': float(a.mean()), 'peak': float(a.max())}\n"
        "except Exception as e:\n"
        "    out = {'ok': False, 'err': f'{type(e).__name__}: {e}'}\n"
        f"open({str(outp)!r}, 'w').write(json.dumps(out))\n")

    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        f'    <string>{runner}</string>\n'
        f'    <string>{script}</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><true/>\n'
        '  <key>EnvironmentVariables</key><dict>\n'
        f'    <key>PYTHONHOME</key><string>{os.environ.get("PYTHONHOME", sys.base_prefix)}</string>\n'
        f'    <key>PYTHONPATH</key><string>{os.pathsep.join(_probe_pythonpath())}</string>\n'
        '  </dict>\n'
        '</dict></plist>\n')

    try:
        outp.unlink(missing_ok=True)
        _sh("launchctl", "unload", str(plist))
        rc, err = _sh("launchctl", "load", str(plist))
        if rc != 0:
            return "unknown", f"could not load probe agent: {err[:80]}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if outp.exists():
                break
            time.sleep(0.4)
        else:
            return "unknown", "probe did not finish in time"
        data = json.loads(outp.read_text())
        if not data.get("ok"):
            return "unknown", data.get("err", "?")
        if data["peak"] == 0.0:
            return "silent", f"rms={data['rms']:.8f} peak={data['peak']:.8f}"
        return "ok", f"rms={data['rms']:.6f} peak={data['peak']:.6f}"
    except Exception as e:
        return "unknown", f"{type(e).__name__}: {e}"
    finally:
        _sh("launchctl", "unload", str(plist))
        plist.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
        outp.unlink(missing_ok=True)


def _probe_pythonpath() -> list[str]:
    """Import paths the launchd probe needs.

    Must be selected, not sliced: the probe runs from an app bundle whose
    interpreter cannot find the venv by relative path, so if site-packages
    is missing the probe dies on `import numpy` and the check reports
    "could not verify" forever instead of failing loudly.
    """
    paths = [q for q in sys.path if q and "site-packages" in q]
    paths += [q for q in sys.path if q and q not in paths and Path(q).is_dir()]
    return paths[:6]


def _daemon_runner() -> str | None:
    """The executable the LaunchAgent launches.

    Read from the plist rather than assumed, so doctor keeps telling the truth
    if the daemon is ever repointed at a different interpreter or bundle.
    """
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if not plist.exists():
        return None
    try:
        import plistlib
        args = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
        return args[0] if args else None
    except Exception:
        return None


def check_mic_entitlement() -> None:
    """Catch the interpreter that can never be granted the microphone.

    A hardened-runtime binary (codesign flags 0x10000) needs
    com.apple.security.device.audio-input to touch CoreAudio, and needs
    NSMicrophoneUsageDescription for macOS to have any text to put in a
    consent dialog. The stock python.org Python.app has the hardened runtime
    and neither of the other two. The result is not an error and not a
    prompt -- it is an open stream that returns zeros forever, which is
    indistinguishable from a broken microphone and cost five days here.
    """
    runner = _daemon_runner()
    if not runner:
        _emit("warn", "No LaunchAgent found", "Cannot tell what the daemon runs.")
        return

    _, sig = _sh("codesign", "-d", "--verbose=4", runner)
    hardened = "flags=0x10000(runtime)" in sig
    _, ents = _sh("codesign", "-d", "--entitlements", "-", runner)
    has_audio = "device.audio-input" in ents

    bundle = Path(runner).parent.parent          # Contents/
    info = bundle / "Info.plist"
    has_usage = False
    if info.exists():
        try:
            import plistlib
            has_usage = "NSMicrophoneUsageDescription" in plistlib.loads(
                info.read_bytes())
        except Exception:
            pass

    name = Path(runner).name
    if hardened and not has_audio:
        _emit("fail", f"{name} can never be granted the microphone",
              "It is signed with the hardened runtime but has no\n"
              "com.apple.security.device.audio-input entitlement, so macOS\n"
              "denies audio outright"
              + ("" if has_usage else " -- and with no NSMicrophoneUsage-\n"
                                       "Description it cannot even show a prompt")
              + ".\nThe stream still opens and returns silence, which is why\n"
                "every other check passes.",
              "Run the daemon from Wispr.app instead:\n"
              "  bash install/build_app.sh && bash install/install.sh")
    elif not has_usage:
        _emit("warn", f"{name} has no NSMicrophoneUsageDescription",
              "macOS cannot show a consent dialog without one. If access was\n"
              "granted previously this still works; a reset would not recover.")
    else:
        _emit("ok", f"{name} is allowed to request the microphone")


def check_microphone() -> None:
    """Assert the capability: open an input stream, confirm samples are not
    all zero, and confirm it under the daemon's own launch identity.

    Opening is not enough. A process macOS has not granted microphone
    consent opens the stream successfully and is then fed pure silence
    indefinitely -- no exception, no warning. Asserting only "it opened"
    is the check that reported a healthy microphone while the daemon had
    been deaf for five days."""
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
        import numpy as np
        default_in = sd.query_devices(kind="input").get("name", "?")
        rec = sd.rec(int(0.7 * 16000), samplerate=16000, channels=1,
                     dtype="float32")
        sd.wait()
        peak = float(np.max(np.abs(rec))) if rec.size else 0.0
        if peak == 0.0:
            _emit("fail", f"Microphone is silent ({default_in})",
                  "The stream opened but every sample was zero. macOS does\n"
                  "this -- rather than erroring -- when the process has no\n"
                  "microphone consent.",
                  "Grant Microphone: System Settings → Privacy & Security →\n"
                  "Microphone. If Python is not listed, reset so it re-asks:\n"
                  f"  tccutil reset Microphone {_python_bundle_id()}\n"
                  f"  launchctl kickstart -k gui/$(id -u)/{LABEL}")
            return
        _emit("ok", f"Microphone delivers audio ({default_in}, peak={peak:.4f})")

        # The check above ran with *this* process's identity. The daemon's
        # is different, and that difference is the entire 2026-08-01 bug.
        verdict, detail = _launchd_mic_probe()
        if verdict == "ok":
            _emit("ok", f"Microphone works under launchd too ({detail})")
        elif verdict == "silent":
            _emit("fail", "Microphone is SILENT when launchd starts it",
                  f"Probe under the daemon's own identity captured {detail}.\n"
                  "This shell has microphone consent and the daemon does not,\n"
                  "so every other check here passes while dictation records\n"
                  "nothing. macOS attributes consent to the responsible\n"
                  "process: from a terminal that is Terminal, under launchd\n"
                  "it is Python itself.",
                  "Reset microphone consent so Python is asked directly:\n"
                  f"  tccutil reset Microphone {_python_bundle_id()}\n"
                  f"  launchctl kickstart -k gui/$(id -u)/{LABEL}\n"
                  "Then dictate once and click Allow when macOS prompts.\n"
                  "Scoped to Python's signing identity, so no other app\n"
                  "loses its microphone consent. Drop the identifier to\n"
                  "reset every app, only if the scoped reset does nothing.")
        else:
            _emit("warn", "Could not verify the microphone under launchd",
                  f"{detail}\n"
                  "Falling back to the in-process result above, which can\n"
                  "pass while the daemon is silent.")
    except Exception as e:
        _emit("fail", "Cannot open the microphone", f"{e.__class__.__name__}: {str(e)[:140]}",
              "Grant Microphone: System Settings → Privacy & Security → Microphone\n"
              "If already granted, another app may be holding the device — or a\n"
              "second wispr daemon is running (see the duplicate check above).")


def check_key() -> None:
    key = anthropic_key()
    if not key:
        _emit("warn", "No Anthropic API key — local rules cleanup only",
              "Transcription still works fully offline; text is just less polished.",
              "wispr set-key")
        return
    if not key.startswith("sk-ant-"):
        _emit("warn", "Key found but does not look like an Anthropic key",
              "Expected it to start with sk-ant-.", "wispr set-key")
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
              "Check the key is valid and has credit: wispr set-key")


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
    vocab = WISPR_HOME / "vocab.txt"
    n = len(vocab.read_text().splitlines()) if vocab.exists() else 0
    _emit("ok", f"Instant paste: {'on' if paste_instant() else 'off'} · "
                f"revise: {'on' if paste_revise() else 'off'} "
                f"({revise_window_s():.1f}s window)")
    _emit("ok", f"Vocabulary: {n} terms" + (f" · vault: {VAULT}" if VAULT else ""))


def main() -> None:
    print(f"\n{BOLD}wispr doctor{RST}\n")
    for fn in (check_duplicates, check_listener_alive, check_accessibility,
               check_mic_contention, check_mic_entitlement,
               check_microphone, check_model,
               check_key, check_config):
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
        _key = "Fn" if hotkey() == "fn" else "Right Option"
        print(f"  {GRN}{BOLD}All good.{RST} Hold {_key} and speak.\n")


if __name__ == "__main__":
    main()
