# Murmur

Local, vault-aware voice dictation for the Mac. Hold **Right Option**
anywhere, speak, release — cleaned text pastes at the cursor.
Double-tap Right Option for hands-free mode (tap once to stop).

Audio never leaves the Mac. Transcription is local (faster-whisper
`small.en`). Cleanup is hybrid: snippets and short utterances go through
instant local rules; longer ones go to Claude Haiku (text only, temp 0,
hard 1.2 s budget) and fall back to rules on any error/timeout/offline.

## Install

```bash
cd ~/code/murmur
./install/install.sh
```

Optional extras:

```bash
# Haiku cleanup (recommended): store the API key once in the keychain
security add-generic-password -s murmur-anthropic -a murmur -w '<ANTHROPIC_API_KEY>'
# menu-bar state icon + recent-5 (separate process)
pip install rumps && python -m murmur menubar &
```

macOS permissions (System Settings → Privacy & Security):
- **Accessibility** — for the global hotkey + paste keystroke (grant to
  your terminal or the python binary in `.venv`)
- **Microphone** — first run will prompt

## Commands

| command | what |
|---|---|
| `python -m murmur` | run the daemon (PTT + hands-free) |
| `python -m murmur fix <wrong> <right>` | teach a correction (sticks everywhere) |
| `python -m murmur vocab` | regenerate vault vocab (run weekly / after big vault changes) |
| `python -m murmur history [n]` | show last n dictations |
| `python -m murmur menubar` | menu-bar companion (optional) |

## Config (`~/.murmur/`)

- `modes.toml` — bundle-id → mode (casual / email / technical / neutral)
- `snippets.toml` — spoken trigger → inserted block
- `corrections.tsv` — learned fixes (also fed to Whisper + Haiku)
- `vocab.txt` — vault-derived proper nouns (terms only, never facts)
- `history.jsonl` — every dictation (ts, app, raw, cleaned, path, latency)

## Privacy contract

- Audio: never leaves the Mac.
- API traffic: cleaned-up **text** only, and only when Haiku cleanup is on
  (no key in env/keychain = rules-only, silently).
- `vocab.txt` carries proper-noun *terms* from the vault, never facts/notes.

Spec: `ops/work/the-bench/voice-flow/murmur-v3-spec.md` in the vault.
