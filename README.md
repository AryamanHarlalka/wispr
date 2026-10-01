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
| **Dictate** | Hold Right Option (or the Fn/globe key, see below), speak, release |
| **Hands-free** | Double-tap the hotkey; tap once to stop |
| **Teach it a word** | `wispr add Anika "Wispr Flow"` |
| **Fix a word it keeps mishearing** | `wispr fix "wrong" "right"` |
| **See what it knows** | `wispr words` · `wispr forget <term>` |
| **See recent dictations** | `wispr history` · `wispr last` |
| **Check what's wrong** | `wispr doctor` |
| **Restart it** | `wispr restart` |

The pill at the bottom of the screen shows what is happening — a live
waveform while you speak, then transcribing, cleaning, pasted. It floats over
full-screen apps too, without pulling you out of them.

### It learns

Wispr keeps a personal dictionary (`~/.wispr/dictionary.txt`) that biases
both the speech model and the cleanup pass toward *your* names, products and
jargon. It fills itself in three ways:

1. **You tell it** — `wispr add <word>` or `wispr fix <wrong> <right>`.
2. **You correct it** — a few seconds after a paste, Wispr looks at the text
   field it pasted into (Accessibility API, local, read-only). If you changed
   "Whisper" to "Wispr" or "docked X" to "docx", it remembers.
3. **The cleanup pass corrects it** — when Claude replaces a misheard word
   with one of your known terms, the pair is remembered so the speech model
   is biased toward it next time.

Learned pairs are hints, not blind replacements: they steer the speech model
and are shown to the cleanup pass, which applies them where the context fits.
Your own `wispr fix` entries are applied verbatim. `wispr words` shows all of
it; `wispr forget <term>` removes an entry.

### Fn / globe key as the hotkey

```toml
# ~/.wispr/config.toml
[hotkey]
key = "fn"
```

Then set System Settings → Keyboard → *Press globe key to* → **Do Nothing**,
or the emoji picker opens on every dictation (`wispr doctor` checks this).

### Latency vs. polish

By default the cleanup pass runs *before* the paste, under a hard 2-second
budget, so what lands is finished text. Short dictations (under four words)
skip it and land instantly; if the network is slow the local result pastes
instead. Would you rather have rougher text a second sooner?

```toml
[paste]
instant = true      # paste the local result immediately
revise = true       # …and let the cleanup pass rewrite it in place (guarded)
[cleanup]
wait_s = 2.0        # how long the paste may wait for the cleanup pass
```

---

## Making it yours

Everything personal lives in `~/.wispr/`, outside this repo, and is never
committed or uploaded.

| File | What it does |
|---|---|
| `config.toml` | hotkey, backend, model, paste and cleanup behaviour |
| `snippets.toml` | say "my email", get your email address |
| `dictionary.txt` | your names and jargon — `wispr add`, or learned |
| `corrections.tsv` | your verbatim fixes — `wispr fix` |
| `learned.tsv` | mishearings Wispr observed, used as hints |
| `seed-terms.txt` | extra terms to seed the vault vocabulary (one per line) |
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
wedged microphone, the globe-key setting, a broken model and an invalid key —
and prints the exact command to fix whatever it finds. Start here; it usually saves the debugging.

Logs, if you want them: `~/.wispr/logs/daemon.err.log`

### Uninstall

```bash
./install/uninstall.sh            # remove the service, keep your settings
./install/uninstall.sh --purge    # remove everything, including the key
```

---

## How it works

```
hotkey held  →  record (locally)
             →  transcribe (locally, Whisper, biased by your dictionary)
             →  clean up (Claude Haiku if you added a key; local rules otherwise)
             →  paste at your cursor
             →  learn from what you change
```

Transcription decodes *while you're still speaking*, in bounded chunks cut at
natural pauses, so a long dictation only pays for its last few seconds when you
let go. Cleanup runs under a hard time budget and falls back to local rules, so
the network can never hold your text hostage.

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

## Cloud speech model (default when a key is present)

Local `base.en` on an Intel CPU tops out around 15–20% word error rate on
accented speech. With an OpenAI key, Wispr sends each take to
`gpt-4o-transcribe`, primed with your personal dictionary and your last
dictation into the same app, while the local model decodes in parallel as
the offline fallback. On the bench clips that cut WER from 0.185 to 0.037
and was faster than local decoding (0.7–1.4 s vs 1.3–7 s).

```
security add-generic-password -U -s wispr-openai -a "$USER" -w   # paste key
```

`~/.wispr/config.toml`:

```toml
[stt]
cloud = true                      # false = fully on-device
cloud_model = "gpt-4o-transcribe" # or gpt-4o-mini-transcribe (half price)
speaker = "Sam is dictating in Indian-accented English, often softly"
```

Quiet takes are lifted toward −3 dBFS (gain capped at +24 dB) before upload.
The newest 150 takes are kept as WAVs in `~/.wispr/audio/` (local only) so
model changes can be measured on real speech. Every history record carries
`stt` (`cloud:…` or `local`) and `stt_note`.
