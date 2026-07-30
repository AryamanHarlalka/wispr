# Giving Murmur to someone

This is the *owner's* checklist. Everything a new user needs is in
[README.md](README.md) — don't duplicate setup instructions here, or the two
will drift and one of them will be wrong.

---

## The whole handoff

Send them two lines:

```bash
git clone https://github.com/your-username/murmur.git ~/code/murmur
cd ~/code/murmur && ./install/install.sh
```

Then: *"read the README if you get stuck, and run `murmur doctor` if it
misbehaves — it'll tell you exactly what's wrong."*

That's it. The installer handles Python discovery, the venv, the right backend
for their hardware, the model download, the API key prompt, the LaunchAgent,
the permission walkthrough, and a verification pass.

---

## Repo access

The repo is **private**, so each person needs to be added:

```bash
gh repo add-collaborator your-username/murmur <their-github-username>
```

or GitHub → Settings → Collaborators → Add people. They'll need to be signed
in to git (`gh auth login` is the easiest route) before the clone works.

To go public instead, run the secret scan below first, then:

```bash
gh repo edit your-username/murmur --visibility public
```

---

## Before sharing with anyone new

**1. Confirm nothing personal is in tracked source.** Personal data lives in
`~/.murmur/` by design, but that boundary needs re-checking whenever the code
changes:

```bash
cd ~/code/murmur
git grep -nEi 'sk-ant-[A-Za-z0-9_-]{20}|@gmail|@example|/Users/[a-z]+/(Desktop|Documents)' \
  -- . ':!*.md'
```

Expect zero hits. Note the key pattern requires 20+ characters *after* the
prefix — a bare `sk-ant-` appears legitimately in prompts and validation, and
matching on the prefix alone produces false positives you'll learn to ignore,
which defeats the point of the check.

Things that have leaked before and are now deliberately kept out of git: real
email addresses (`config.py`), a personal network and private project codenames
(`vocab.py`), and a hardcoded vault path (`config.py`, replaced by
`~/.murmur/vault-path`).

**2. Confirm no audio is tracked.** `bench/dumps/` holds real recordings.

```bash
git ls-files | grep -Ei '\.(wav|m4a|mp3|flac|aiff)$'
```

Only `bench/clips/*` (deliberate test fixtures) should ever appear, and only if
you're comfortable shipping your own voice with the repo. `.gitignore` already
covers `bench/dumps/` and every audio extension.

**3. Confirm no key ever entered history.**

```bash
git log -p --all | grep -iE 'sk-ant-[a-z0-9]' | head
```

Must be empty. The key belongs in Keychain only. If this ever hits, scrub the
history before making anything public — rotating the key is not enough.

---

## What they get vs. what stays yours

| | Ships in the repo | Stays on your machine |
|---|---|---|
| Code, installer, docs | ✅ | |
| Generic seed terms (tooling words) | ✅ | |
| Your vocabulary / people's names | | `~/.murmur/vocab.txt` |
| Your snippets, corrections | | `~/.murmur/*` |
| Your API key | | Keychain |
| Your dictation history | | `~/.murmur/history.jsonl` |
| Your vault path | | `~/.murmur/vault-path` |

A fresh install starts with an empty vocabulary and the built-in seed terms.
Nobody inherits your names.

---

## Their API key

They use **their own**, billed to them. `murmur set-key` prompts for it, reads
it without echoing, validates the `sk-ant-` prefix, and stores it in their
Keychain. Never send them yours — it's a shared-billing and shared-blast-radius
problem, and revoking it later breaks their install.

Without a key everything still works; cleanup just falls back to local rules.
Worth saying explicitly, because people assume the key is required.

---

## If they report "it broke"

Ask for the output of one command:

```bash
murmur doctor
```

It covers the failure modes that have actually occurred: duplicate daemons
competing for the microphone, a dead hotkey listener inside a live process,
Accessibility not granted (or granted to a symlink rather than the resolved
binary), a wedged CoreAudio device, a missing model, and an invalid key. Each
failure prints its own fix.
