# Float

*Your teaching, lighter.*

Float is a working companion for schoolteachers — one teacher at a time, or a
whole staff room. It drafts lesson plans, question papers, worksheets,
parent messages and report remarks; it keeps your classes and to-dos; and it
can, if you let it, use your computer for the repetitive parts of a task.

It runs entirely on your own computer. There is no Float server anywhere —
you bring your own API key (Anthropic, OpenAI or Google), and your data
never leaves the laptop except to that one provider, for that one request.

## Starting it

You need Python 3.10 or later. From this folder:

```
pip install -r requirements.txt      # optional, but recommended — see below
python main.py
```

A browser tab opens at `http://127.0.0.1:8765`. The first account you create
becomes the school's admin.

Everything Float needs lives in `data/`, next to this file: `float.sqlite3`
(accounts, classes, conversations) and `data/files` (documents Float has
made, until you save them to your Desktop or Drive). Delete `data/` to start
completely fresh.

### What the optional packages buy you

- `cryptography` — saved API keys are encrypted with AES-256-GCM instead of
  the plain-stdlib fallback. Install it unless you have a reason not to.
- `pyautogui` — lets Float actually move the mouse and type, for the
  "use the computer" tool. Skip it if you never plan to use that feature;
  everything else works without it.

## Signing in

The first person to open Float sets up the school and becomes its admin.
After that, teachers either sign up themselves (if the admin turns that on)
or the admin adds them from the admin panel, which hands out a temporary
password.

Every teacher can set a short PIN for quick sign-in between periods — the
full password still works everywhere. **Closing the browser tab, or closing
Float itself, signs you out.** There is no "remember me" that survives a
restart; that's deliberate.

## Giving Float a model to use

Float doesn't come with a model of its own. Open **Settings → Model & keys**
and paste in a key from Anthropic, OpenAI or Google — whichever you already
have, or whichever is cheapest for your school. An admin can also set a
single school-wide key so teachers don't need their own.

## The toolkit

Nineteen ready-made workflows live behind the toolkit button: lesson plans,
question papers with a marking scheme, worksheets in three tiers, marking
help, report-card remarks, parent messages in thirteen languages, bilingual
explainers, low-cost activity ideas, remedial plans, syllabus pacing,
class analysis, notices, letters, slide outlines, seating plans, doubt
explainers, assembly content and NEP holistic-progress descriptors. Each
one is a short form — fill it in, and Float turns it into a proper first
draft as a Word document, PDF, or spreadsheet.

## Files

When Float makes something, it shows up as a file card with three buttons:
**Desktop**, **Drive**, and **Download**. Nothing saves anywhere on its own —
you decide where it goes.

## Assist levels

Under Settings → How Float helps, there are three levels:

- **Coach me** — Float asks questions and reacts to what you write. You do
  the writing.
- **Draft it** (default) — Float writes a first version and says where it
  guessed, so you know what to check.
- **Do it all** — Float finishes routine paperwork outright.

Float also shows you, once a week, roughly how much of what went out was in
your own words versus drafted — not to police you, but because a teacher
who edits everything usually teaches better than one who forwards it.

## The admin panel

Visit `/admin` (or the shield icon in the rail, if you're an admin) for:
a spend and usage overview, the teacher list with reset/deactivate/delete,
school-wide settings (which providers are allowed, whether computer control
is on, self sign-up), notices that appear for every teacher, an audit log,
and a usage export to Excel.

## Running it as a service

Float has no installer beyond `python main.py`. If you want it to start
automatically:

- **Windows**: create a shortcut to `pythonw main.py` in the Startup folder.
- **macOS**: a small LaunchAgent plist pointing at `main.py` works well.
- **Linux**: a systemd `--user` unit running `python3 main.py --no-browser`.

## A note on dependency

Float is built to help you get the paperwork done faster — not to write
your teaching for you. The assist levels, the own-work ledger, and the
"Draft it" default (rather than "Do it all") are there on purpose. Nothing
in Float will nag you about this; it just tries not to make the easy thing
also the thoughtless thing.

---

See `docs/DESIGN.md` for the design reasoning, and `PRIVATE_NOTES.md`
(delete before sharing this project) for the one setting not in this file.
