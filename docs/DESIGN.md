# Design notes

## Who this is for

One teacher, in an Indian school, most likely CBSE or a state board, six
periods a day, forty to fifty-five students a class, a chalkboard rather
than a smartboard, and a laptop that has to last. Float is built around that
person, not around a school IT department or a district rollout.

## Why a clean rebuild instead of extending JARVIS

The original project (J.A.R.V.I.S.) was a capable but general-purpose local
assistant bound tightly to Ollama, with a school-portal layer bolted on
top. Re-platforming it onto API-key providers, adding real auth, a proper
UI, and a teacher-specific toolkit touched nearly every file, so it was
faster and cleaner to keep the proven low-level choices — stdlib-only HTTP
server, SQLite, scrypt password hashing, OAuth PKCE on a loopback redirect —
and build the application layer fresh around teachers specifically.

## Visual direction

The look deliberately avoids the two defaults an AI-assisted design
tends toward: cream-and-terracotta warmth, or black-and-acid-green
"technical" chic. Instead: a blackboard green (`--board`), a paper-white
working surface (`--paper`), and a single warm accent (marigold,
`--accent`) reserved for actions — never decoration. The palette is meant
to feel like a staff room, not a startup.

Times New Roman appears in exactly one place with intent: the greeting,
and other display moments (modal headings, the sign-in page's pull-quote).
Everywhere else is a plain system sans-serif, because a teacher reading a
worksheet draft at 7 a.m. needs legibility, not personality.

## The "not a school portal" decision

Every screen is scoped to one teacher's own world — their classes, their
drafts, their files. There's no cross-teacher visibility, no shared gradebook,
no district reporting. The admin panel exists only to manage *accounts* and
*keys*, not to see into what teachers write. That boundary was treated as a
design constraint, not an afterthought: it's why classes/marks/attendance
live in `klasses`/`students`/`marks`/`attendance` tables scoped by
`user_id`, not by school.

## Anti-dependency, on purpose

Three things exist specifically so Float doesn't make a teacher's judgement
unnecessary:

1. **Assist levels** (`coach` / `draft` / `full`) change the system prompt's
   instructions, not just a UI label — "coach" genuinely withholds a
   finished draft and asks questions instead.
2. **The own-work ledger** (`own_work` table, `own_work_summary()`) tracks
   the ratio of AI-authored to teacher-authored characters in what gets
   saved, and surfaces it — gently, once a week, on a Friday — rather than
   hiding it.
3. **The default assist level is "draft", not "full".** A first version
   that clearly needs the teacher's read-through was chosen over one that
   invites rubber-stamping.

## Security choices worth explaining

- **Sessions die with the process.** A random `BOOT_NONCE` is generated at
  startup and mixed into every session token's fingerprint
  (`security.token_fingerprint`). Restarting Float — including just closing
  and reopening the app — invalidates every session, which is what "log out
  when you close the app" actually requires; a cookie expiry alone
  wouldn't do it.
- **No cookies, no CSRF surface.** The session token lives in the browser's
  `sessionStorage` and is sent as a bearer token. There's nothing for a
  cross-site request to ride on.
- **API keys are encrypted at rest** with a per-install device key
  (`data/.device_key`, mode 0600) using AES-256-GCM when `cryptography` is
  available, falling back to an HMAC-based keystream construction when it
  isn't — so the feature doesn't silently disappear on a machine where pip
  installs are restricted.
- **The `Host` header is checked** on every request as a DNS-rebinding
  defence, since the server binds to loopback and trusts requests that
  claim to be for it.

## Why hand-rolled docx/pdf/xlsx instead of a library

Zero third-party dependencies means Float works the moment Python is
installed, on a school laptop where `pip install` might be blocked by a
proxy or a policy. The OOXML and PDF writers in `float/artifacts.py` are
minimal but produce genuinely valid files — tested by opening the output in
Word, Excel and a PDF reader, not just by inspecting the XML.

## The hidden local runtime

See `PRIVATE_NOTES.md` (not included when this project is shared further)
for how it works. The design intent: it should be possible for the person
running Float to plug in a local Ollama model if they want to, without
that option ever appearing as a decision point for the teachers using it.
