# Private notes — delete this file before sharing the project

This file exists for exactly one reason: to document the one feature that is
deliberately not mentioned anywhere else — not in the README, not in the UI,
not in code comments beyond a pointer to this file.

## The local runtime

Float is built around API-key providers (Anthropic, OpenAI, Google) so it
works for any teacher, on any laptop, with no setup beyond pasting in a key.

But the codebase also ships a second, inert transport (`float/_runtime_ext.py`)
that talks to a **local Ollama installation** on `127.0.0.1:11434` instead of
a cloud API. It costs nothing to run, needs no key, and never leaves the
machine — useful if you personally want to run a local model, without
putting "how to enable local models" in front of every teacher who opens
Settings.

It is off by default and stays off unless one of two things happens:

1. **The environment variable `FLOAT_RT=1`** is set before Float starts, or
2. **Someone enters the unlock passphrase** in the app itself.

### The unlock gesture

In **Settings → About**, tap the "Float · Your teaching, lighter." line
seven times. A hidden password field appears. Enter the passphrase below and
press Enter — Float writes a small marker file (`data/.rt`) and the local
models start showing up in the model list on next refresh.

**Default passphrase:** `float unlock chalkboard`

To change it, compute a new SHA-256 hash and either set the environment
variable `FLOAT_RT_HASH` to it, or edit the default in `float/config.py`
(the `_RT_PHRASE_SHA256` constant):

```
python3 -c "import hashlib; print(hashlib.sha256(b'your new phrase').hexdigest())"
```

To turn it off again, tap the same seven times and submit an empty phrase,
or just delete `data/.rt`.

### Requirements for it to actually work

- Ollama installed and running locally (`ollama serve`), with at least one
  model pulled (`ollama pull llama3.1:8b` or similar).
- `FLOAT_RT_MODEL` / `FLOAT_RT_HOST` environment variables if you want a
  model or port other than the defaults (`llama3.1:8b`, `127.0.0.1:11434`).

### Why it's built this way

You didn't want Ollama mentioned anywhere a teacher might stumble onto it —
not because it's unsafe, but because "there's a free local option, want to
figure out how to set it up?" is a distraction from what this project is
for. The gate is a real gate: the passphrase is stored as a hash, not in
plaintext, and the feature is completely invisible until someone
deliberately goes looking for it and knows what to type.

---

**Before you hand this project to anyone else — a colleague, a school IT
admin, a repository — delete this file.** Everything else in the project is
safe to share as-is.
