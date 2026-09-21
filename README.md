<div align="center">

# Float

**Your teaching, lighter.**

*A working companion for schoolteachers — the paperwork gets faster, the teaching stays yours.*

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/required%20dependencies-zero-2E7D5B?style=flat-square)
![Data](https://img.shields.io/badge/your%20data-stays%20on%20your%20computer-1E3A32?style=flat-square)
![Providers](https://img.shields.io/badge/works%20with-Anthropic%20%7C%20OpenAI%20%7C%20Google-E8A33D?style=flat-square)

</div>

---

Float is not a school portal, and it isn't trying to run your school. It's built around one teacher at a time — the six periods, the hundred and twenty exercise books, the parent messages that all need to sound a little different, the report card remarks due Friday. It drafts the paperwork, keeps your classes straight, and — if you let it — can even move the mouse for the tedious on-screen parts of a task. Everything runs on your own laptop, on your own API key. There is no Float server anywhere collecting anything.

<br>

## Contents

- [Why Float exists](#why-float-exists)
- [Getting started](#getting-started)
- [The toolkit](#the-toolkit)
- [Choosing a model](#choosing-a-model)
- [How much Float does for you](#how-much-float-does-for-you)
- [Files, Drive, and Desktop](#files-drive-and-desktop)
- [Security and privacy](#security-and-privacy)
- [The admin panel](#the-admin-panel)
- [Project layout](#project-layout)
- [Running it as a background service](#running-it-as-a-background-service)
- [Troubleshooting](#troubleshooting)
- [License](#license)

<br>

## Why Float exists

Most classroom software is built for the institution — attendance systems, gradebooks, timetabling engines. Float is built for the person standing at the board. It doesn't ask which vendor your school has a contract with, doesn't need an IT rollout, and doesn't put anyone above you in the account hierarchy except the one admin your own school chooses.

It also tries hard not to make you worse at your job. An assistant that quietly does all your thinking for you isn't actually helping — so Float defaults to handing back a **first draft that clearly wants your read-through**, not a finished product you'd never think to check. More on that in [How much Float does for you](#how-much-float-does-for-you).

<br>

## Getting started

You need **Python 3.10 or later**. Nothing else is required — Float runs entirely on the standard library.

```bash
git clone <your-repo-url> float
cd float
pip install -r requirements.txt   # optional — see below
python main.py
```

A browser tab opens at `http://127.0.0.1:8765`. The first account you create becomes your school's admin.

> **On Windows**, once it's set up, you can skip the terminal entirely — see [`Start Float.vbs`](#running-it-as-a-background-service) for a proper double-click launcher with no console window.

### What the optional packages buy you

| Package | Without it | With it |
|---|---|---|
| `cryptography` | API keys are still encrypted, with a stdlib fallback | Encrypted with AES-256-GCM instead |
| `pyautogui` | Everything else works normally | Float can move the mouse and type — the "use my computer" tool |

Skip either one freely. Nothing else in Float depends on them.

### Where your data lives

Everything Float needs sits in a `data/` folder next to `main.py`:

```
data/
├── float.sqlite3      accounts, classes, conversations, tasks
└── files/             documents Float has made, until you save them elsewhere
```

Delete `data/` at any point to start completely fresh.

<br>

## The toolkit

Nineteen ready-made workflows, each a short form that turns into a real first draft — a Word document, a PDF, or a spreadsheet, depending on what makes sense.

<table>
<tr><td valign="top">

**Planning**
- Lesson plan
- Syllabus pacing
- Seating and groups

**Teaching**
- Explain in two languages
- Classroom activity
- Remedial plan
- Lesson slides
- Solve a doubt

</td><td valign="top">

**Assessment**
- Question paper
- Worksheet
- Mark an answer
- Rubric

**Reporting**
- Report card remarks
- Marks analysis
- Holistic progress descriptors

</td><td valign="top">

**Communication**
- Message to a parent
- Notice or circular
- Letter to the principal
- Assembly speech

</td></tr>
</table>

Every one of these understands Indian classroom reality by default: CBSE, ICSE, state boards, IB and Cambridge; question papers with blueprints and marking schemes that actually add up; parent messages in **thirteen Indian languages**; activities that work with fifty students and no equipment budget.

<br>

## Choosing a model

Float doesn't ship with a model of its own — open **Settings → Model & keys** and paste in a key from whichever provider you already use.

| Provider | Fast, cheap default | Best available | Get a key |
|---|---|---|---|
| **Anthropic** | Claude Sonnet 4.5 | Claude Opus 4.1 | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| **OpenAI** | GPT-4.1 mini | GPT-4.1 | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Google** | Gemini 2.0 Flash | Gemini 2.5 Pro | [aistudio.google.com](https://aistudio.google.com/app/apikey) |

*Approximate cost, per million tokens, is shown to you in Settings and on the admin dashboard — check each provider's own pricing page for the current rate.*

An admin can also add a single **school-wide key** so individual teachers never have to bring their own.

<br>

## How much Float does for you

Under **Settings → How Float helps**, three levels change how Float actually behaves — not just a label, the underlying instructions it works from:

| Level | What happens |
|---|---|
| **Coach me** | Float asks questions and reacts to what you write. You do the writing. |
| **Draft it** *(default)* | Float writes a first version and says plainly where it guessed, so you know what to check. |
| **Do it all** | Float finishes routine paperwork outright — useful for notices, seating charts, mark sheets. |

Once a week, Float also shows you roughly how much of what went out was in your own words versus drafted for you — not to police you, but because a teacher who edits everything usually teaches better than one who forwards it unread.

<br>

## Files, Drive, and Desktop

When Float makes something, it shows up as a file card with three choices — **Desktop**, **Drive**, **Download**. Nothing saves anywhere on its own; you decide where it goes, every time.

<br>

## Security and privacy

- **Sessions end when you close the app.** A fresh random key is generated every time Float starts, and it's mixed into every session token. Restarting the app invalidates every open session — the way "signed out when I close the window" should actually work, not just a cookie timer.
- **No cookies, no CSRF surface.** The session token lives in the browser tab's own memory and travels as a bearer token. There's nothing for a cross-site request to ride on.
- **API keys are encrypted at rest**, using a device-specific key that never leaves your machine.
- **The admin panel manages accounts, not content.** There's no cross-teacher visibility into what anyone writes — only who has access and what it's costing.
- **Your files, your call.** Nothing is written to Drive or the Desktop unless you press the button.

<br>

## The admin panel

Visit `/admin`, or the shield icon in the sidebar if your account is an admin, for:

- Usage and estimated spend, per teacher and for the school
- Adding, deactivating, or removing teacher accounts
- A school-wide API key, if you'd rather not have teachers bring their own
- Notices that appear for everyone
- An audit log
- A one-click usage export to Excel

<br>

## Project layout

```
float/
├── main.py                CLI entry point — start here
├── requirements.txt
├── float/
│   ├── config.py          providers, models, Indian-schools context
│   ├── security.py        password hashing, session tokens, encryption
│   ├── db.py               SQLite schema and every query
│   ├── providers.py        streaming client for each AI provider
│   ├── artifacts.py        builds real .docx / .pdf / .xlsx files, zero dependencies
│   ├── computer.py         mouse and keyboard control, with an on-screen indicator
│   ├── greeting.py         the opening screen
│   ├── tools.py            the 19-entry toolkit and the tool-calling system prompt
│   ├── agent.py            the conversation loop
│   ├── google.py           OAuth sign-in and Drive upload
│   ├── server.py           the HTTP server and every API route
│   └── web/                the interface: login, main app, admin panel
├── docs/DESIGN.md          the reasoning behind the design and security choices
└── tests/test_float.py     the test suite
```

<br>

## Running it as a background service

Float has no installer beyond `python main.py`. To have it start automatically:

- **Windows** — a `Start Float.vbs` launcher is included: double-click it and Float opens with no visible console window. Right-click it → *Create shortcut* to put an icon on your desktop or pin it to the taskbar. Drop it in your Startup folder (`Win + R` → `shell:startup`) to have it open on login.
- **macOS** — a small `LaunchAgent` plist pointing at `main.py` works well.
- **Linux** — a `systemd --user` unit running `python3 main.py --no-browser`.

<br>

## Troubleshooting

<details>
<summary><strong>Float says the address is already in use</strong></summary>
<br>

It's probably already running — check for an existing browser tab first. If not, something else is using port 8765; start Float on a different one:

```bash
python main.py --port 9000
```
</details>

<details>
<summary><strong>I forgot my password</strong></summary>
<br>

Ask your school's admin to reset it from the admin panel, or run this from a terminal in the project folder:

```bash
python main.py set-password you@school.edu.in
```
</details>

<details>
<summary><strong>Nothing happens when I double-click the launcher</strong></summary>
<br>

Run `Start Float.bat` directly instead of the `.vbs` shortcut — that one shows its output, so you can read the actual error. It's almost always Python not being installed, or not added to `PATH` during setup.
</details>

<br>

## License

No license file is included yet. If you plan to share this project beyond your own school, add one — [choosealicense.com](https://choosealicense.com) is a good starting point.

---

<div align="center">

*Float · Your teaching, lighter.*

</div>
