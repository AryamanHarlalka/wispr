# Setting up Murmur on your Mac

Hi Aman — this is Murmur, a voice dictation tool. Hold **Right Option**, talk,
let go, and the text appears wherever your cursor is. It works in any app.

Your audio never leaves your Mac. Transcription runs locally.

---

## Install

```bash
git clone <repo-url> ~/code/murmur
cd ~/code/murmur
./install/install.sh
```

The script does everything: dependencies, the speech model, and a background
service so Murmur starts automatically when you log in. **You never need to
keep a terminal open.**

It takes 5–15 minutes the first time, mostly downloading the speech model.

### Requirements

- macOS 12 or newer
- Python 3.10+ — check with `python3 -V`. If it's older or missing:
  `brew install python@3.12`
- ~2GB free disk

---

## The two permissions

macOS won't let any app read your keyboard or microphone without you saying so,
and this genuinely can't be automated. The installer prints the exact steps and
copies the path you need to your clipboard.

**Microphone** — you'll get a popup the first time you dictate. Click OK.

**Accessibility** — System Settings → Privacy & Security → Accessibility → **+**,
then Cmd-Shift-G and paste the path the installer copied. Toggle it on.

> If dictation does nothing at all, Accessibility is almost always the reason.
> The daemon runs fine without it; it just can't see the hotkey.

---

## Using it

| Action | What happens |
|---|---|
| Hold Right Option, speak, release | Text pastes at your cursor |
| Double-tap Right Option | Hands-free mode — tap once more to stop |
| A pill appears on screen | Shows listening / transcribing / done |

Murmur adapts its tone to the app you're in — casual in Slack and Messages,
proper sentences in Mail, and it leaves code and file paths alone in editors.

---

## Optional: better cleanup

Murmur tidies up filler words and punctuation using local rules. If you add an
[Anthropic API key](https://console.anthropic.com/), it uses Claude Haiku
instead, which is noticeably better on longer dictation.

Only the **transcribed text** is sent, never audio. There's a hard timeout; if
the API is slow or offline it silently falls back to local rules.

```bash
security add-generic-password -U -s murmur-anthropic -a $USER -w sk-ant-YOURKEY
launchctl kickstart -k gui/$UID/com.murmur.daemon
```

Costs a few cents a month at normal use. Skip it if you'd rather not.

## Optional: teach it your vocabulary

If it keeps mishearing names or jargon, add them one per line to
`~/.murmur/seed-terms.txt`, then run:

```bash
~/code/murmur/.venv/bin/python -m murmur vocab
```

This file stays on your machine.

---

## If something breaks

```bash
# Is it running?
launchctl print gui/$UID/com.murmur.daemon | grep state

# What went wrong?
tail -30 ~/.murmur/logs/daemon.err.log

# Restart
launchctl kickstart -k gui/$UID/com.murmur.daemon
```

**Nothing happens when I hold Right Option** → Accessibility permission.
**Slow on an Intel Mac** → use a smaller model:
```bash
echo base.en > ~/.murmur/whisper-model
launchctl kickstart -k gui/$UID/com.murmur.daemon
```
**Wrong words** → add them to `~/.murmur/seed-terms.txt` and rebuild vocab.

---

## Uninstall

```bash
~/code/murmur/install/uninstall.sh          # stop and remove the service
~/code/murmur/install/uninstall.sh --purge  # also delete settings and history
```

---

## What's on your machine

- `~/code/murmur` — the code
- `~/.murmur/` — your settings, vocabulary, and dictation history
- `~/Library/LaunchAgents/com.murmur.daemon.plist` — the startup entry

`~/.murmur/history.jsonl` keeps a local log of your dictations so you can
recover anything that failed to paste. Delete it whenever you like.
