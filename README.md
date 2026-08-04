# Wispr

Local voice dictation for macOS. Hold **Right Option**, speak, release — clean
text appears wherever your cursor is. Works in any app.

Your voice never leaves your Mac. Transcription runs entirely on your own
hardware, so there's no subscription, no per-word cost, and nothing to trust.

---

## Install

You need a Mac. Everything else the installer handles.

```bash
git clone https://github.com/your-username/wispr.git ~/code/wispr
cd ~/code/wispr && ./install/install.sh
```

It takes about five minutes, most of it downloading the speech model. The
installer will:

1. find a suitable Python (installing one is the only thing it can't do for you)
2. set up an isolated environment so nothing touches your system Python
3. pick the fastest transcription engine **for your specific Mac** — Apple
   Silicon gets the GPU backend, Intel gets an optimised CPU one
4. offer to store an Anthropic API key (optional — see below)
5. install it as a background service that starts at login
6. walk you through the two macOS permissions
7. verify the whole thing actually works, and tell you if it doesn't

After that there's no terminal, ever. It's just there.

### If you don't have Python 3.10+

```bash
brew install python@3.12
```

No Homebrew? Get it at [brew.sh](https://brew.sh), or download Python from
[python.org](https://www.python.org/downloads/macos/).

---

## The two permissions

macOS won't let *any* app read your keyboard or microphone without explicit
consent, and Apple deliberately makes this un-scriptable. So this is the one
part you do by hand. The installer walks you through it and puts the path you
need on your clipboard.

**Accessibility** — lets Wispr notice you're holding the hotkey, and paste the
result. System Settings → Privacy & Security → Accessibility → **[+]** → press
`Cmd-Shift-G` → paste the path → toggle it **on**.

**Microphone** — macOS prompts the first time you dictate. Click OK.

> The path must be the *real* Python binary, not a symlink — macOS attributes
> permissions to the resolved file. The installer resolves it for you; if you're
> doing it by hand, `wispr doctor` prints the exact path to use.

---

## The API key (optional)

Wispr always transcribes locally. A key only affects the **cleanup** step:

| | Without a key | With a key |
|---|---|---|
| Transcription | on your Mac | on your Mac |
| Audio uploaded | never | never |
| Cleanup | local rules | Claude Haiku |
| Filler removal | basic | thorough |
| Spoken corrections | no | "Tuesday, no wait, Wednesday" → "Wednesday" |
| Cost | free | a fraction of a cent per dictation, billed to you |

The key is **yours** — get one at
[console.anthropic.com](https://console.anthropic.com/settings/keys). It's
stored in your macOS Keychain, never on disk and never in this repo.

```bash
wispr set-key            # add or replace
wispr set-key --clear    # remove; falls back to local rules
```

Only the transcribed **text** is ever sent, and only when a key is present.

---

## Using it

| | |
|---|---|
| **Dictate** | Hold Right Option, speak, release |
| **Hands-free** | Double-tap Right Option; tap once to stop |
| **Fix a word it keeps mishearing** | `wispr fix "wrong" "right"` |
| **See recent dictations** | `wispr history` |
| **Check what's wrong** | `wispr doctor` |
| **Restart it** | `wispr restart` |

Text appears almost immediately — the local result pastes right away, then
quietly refines itself a moment later if the cleanup pass improves on it. You
generally won't notice the second step, and it turns itself off in code editors
and terminals, where auto-indent and autocomplete make rewriting unsafe.

Prefer the text to land once and never change?

```toml
# ~/.wispr/config.toml
[paste]
revise = false
```

---

## Making it yours

Everything personal lives in `~/.wispr/`, outside this repo, and is never
committed or uploaded.

| File | What it does |
|---|---|
| `config.toml` | backend, model, paste behaviour |
| `snippets.toml` | say "my email", get your email address |
| `corrections.tsv` | permanent fixes for words it mishears |
| `seed-terms.txt` | names and jargon to recognise (one per line) |
| `modes.toml` | per-app tone — casual in Slack, precise in editors |
| `history.jsonl` | every dictation, local only |

**Obsidian users:** point Wispr at your vault and it learns the proper nouns
you actually use, so it stops mangling names.

```bash
echo "/path/to/your/vault" > ~/.wispr/vault-path && wispr vocab
```

Only names are extracted — page titles, aliases, folder names. Never note
content.

---

## When something breaks

```bash
wispr doctor
```

It checks for duplicate daemons, a dead hotkey listener, missing permissions, a
wedged microphone, a broken model and an invalid key — and prints the exact
command to fix whatever it finds. Start here; it usually saves the debugging.

Logs, if you want them: `~/.wispr/logs/daemon.err.log`

### Uninstall

```bash
./install/uninstall.sh            # remove the service, keep your settings
./install/uninstall.sh --purge    # remove everything, including the key
```

---

## How it works

```
Right Option held  →  record (locally)
                   →  transcribe (locally, Whisper)
                   →  clean up (local rules; Haiku if you added a key)
                   →  paste at your cursor
```

Transcription decodes *while you're still speaking*, so a long dictation doesn't
pay for the whole recording when you let go. Cleanup happens after the paste,
not before it, so the network is never between you and your text.

**Backends are chosen per machine**, because the right answer is hardware
dependent — MLX on the Apple GPU where that exists, CTranslate2 on CPU where it
doesn't. Measured rather than assumed: see [BENCHMARKS.md](BENCHMARKS.md).

---

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.10+
- ~1GB disk for the speech model
- No internet needed to dictate — only for the optional cleanup pass

## Licence

MIT — see [LICENSE](LICENSE).
