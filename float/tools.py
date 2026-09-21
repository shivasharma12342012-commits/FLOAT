"""Float — what the model knows, and what it can do.

Three things live here.

**The system prompt**, assembled per request from the teacher's own context: what
they teach, which board, which classes, which language, and — the part that
matters most — how much of the work they want done for them.

**The tools**, in JSON Schema, which every provider is given in its own dialect
by ``providers.py``. Kept deliberately few. A model with six good tools uses them;
a model with twenty picks the wrong one.

**The toolkit**, which is the product: nineteen teaching workflows with the
fields an Indian school teacher would actually be asked for, and a prompt written
for each. These are not chat suggestions — each one is a small form that produces
a document.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import artifacts, computer, config, db

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
_BASE = """You are Float, a working assistant for schoolteachers. You are used between \
periods, in staff rooms, on laptops with weak connections, by people who are tired and \
busy. Answer accordingly: get to the point, and make the first draft good enough to use.

How you write:
- Plain, warm, specific. No preamble, no "Certainly!", no restating the question.
- Use Markdown headings, tables and lists — Float turns them into Word, PDF and Excel files.
- Indian classroom reality by default: 40-55 students in a room, shared textbooks, one \
projector for the floor, chalk and a blackboard, low-cost materials for activities, and \
a syllabus that has to be finished before the board exam.
- Never invent a page number, a textbook line, a circular, or a board rule. If you are \
not sure what the prescribed text says, say so and describe the concept instead.
- Marks, weightings and blueprints must add up. Check them before you answer.

What you are careful about:
- You do not assess a child's ability, diagnose a condition, or write anything that \
labels a student. You describe work, not children.
- Anything that goes to a parent or into a record is a draft for the teacher to approve. \
Say so once, briefly, at the end — not in every paragraph.
- The teacher's judgement is the point of the job. Where a real decision sits with them \
— a mark, a remark, whether a child is struggling — put the decision back in their hands \
with the information they need, rather than making it for them."""

_ASSIST = {
    "coach": """
This teacher has asked you to COACH, not to finish the work.
Give them a structure, the two or three decisions that actually matter, and one worked \
example. Ask one focused question where their context genuinely changes the answer. \
Leave the writing to them. Do not produce the finished artefact unless they ask again \
explicitly.""",
    "draft": """
This teacher has asked for a DRAFT they will edit.
Write the whole thing, properly, and finish with one short line naming the single place \
their own knowledge of the class should change what you wrote. One line — not a checklist.""",
    "full": """
This teacher has asked for a FINISHED version.
Write it complete and ready to use. No questions back, no placeholders like [insert name]. \
If something is genuinely unknown, choose a sensible default and note it in one clause.""",
}


def system_prompt(user: dict[str, Any], tool_context: str = "") -> str:
    now = dt.datetime.now()
    subjects = _json_list(user.get("subjects"))
    grades = _json_list(user.get("grades"))
    language = config.LANGUAGES.get(user.get("language", "en"), "English")

    lines = [_BASE, _ASSIST.get(user.get("assist_level", "draft"), _ASSIST["draft"])]

    profile = [f"Today is {now.strftime('%A, %d %B %Y')}."]
    if user.get("name"):
        profile.append(f"You are working with {user['name']}.")
    if user.get("school"):
        profile.append(f"School: {user['school']}.")
    if user.get("board"):
        profile.append(f"Board: {user['board']}.")
    if subjects:
        profile.append(f"Subjects taught: {', '.join(subjects)}.")
    if grades:
        profile.append(f"Classes taught: {', '.join(grades)}.")
    if user.get("language", "en") != "en":
        profile.append(
            f"This teacher's language is {language}. Write in {language} when they write to you "
            "in it, and when the output is for students or parents. Keep technical terms and "
            "board terminology in English where that is what the school uses."
        )
    lines.append("\n".join(profile))

    classes = db.list_classes(int(user["id"]))
    if classes:
        listing = "; ".join(
            f"{c['name']} ({c['students']} students)" + (f", {c['subject']}" if c["subject"] else "")
            for c in classes[:12]
        )
        lines.append(f"Classes registered in Float: {listing}. Use look_up_class before "
                     "writing anything that needs real names or marks.")

    lines.append(
        "Files: when the teacher will print, send or keep something, call create_file. "
        "Do not ask permission first and do not paste a wall of text and then offer a file — "
        "make the file, and say in one line what it is. Word for documents, Excel for "
        "anything with marks or lists, PDF only when they ask for PDF. PDF cannot render "
        "Indian-language scripts, so use Word for those."
    )
    if tool_context:
        lines.append(tool_context)
    return "\n\n".join(lines)


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value or "[]")
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "create_file",
        "description": (
            "Create a real file the teacher can print, email or upload: Word, PDF, Excel, "
            "CSV, HTML or Markdown. Content is Markdown for documents (headings, tables, "
            "lists all work) and a list of rows for spreadsheets. Use this whenever the "
            "output is something they will keep rather than read once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename without extension, e.g. 'Class 8B Unit Test'."},
                "kind": {"type": "string", "enum": ["docx", "pdf", "xlsx", "csv", "html", "md", "txt"]},
                "title": {"type": "string", "description": "Heading printed at the top."},
                "subtitle": {"type": "string", "description": "One line under the title: class, date, marks."},
                "content": {"type": "string", "description": "Markdown body. For xlsx/csv, a Markdown table."},
            },
            "required": ["name", "kind", "content"],
        },
    },
    {
        "name": "look_up_class",
        "description": (
            "Read a class the teacher has registered: the student list, recent marks and "
            "attendance. Use before writing report remarks, seating plans, group lists or "
            "anything else that needs real names or real scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "class_name": {"type": "string", "description": "Name or grade+section, e.g. '8B'."},
                "include": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["students", "marks", "attendance"]},
                    "description": "What to fetch. Defaults to students.",
                },
            },
            "required": ["class_name"],
        },
    },
    {
        "name": "save_task",
        "description": (
            "Put something on the teacher's list in Float. Use when they say they need to "
            "remember something, or when a plan you wrote implies a deadline for them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due": {"type": "string", "description": "YYYY-MM-DD, or empty."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "use_computer",
        "description": (
            "Drive the teacher's computer: move Float's pointer, click, type, press keys, "
            "scroll, open an application or a file. Float shows its own pointer and a banner "
            "so the teacher can see and stop it. Only use this when the teacher has asked "
            "Float to do something on the machine — never to 'check' or 'look at' something "
            "you could ask about instead. Plan the whole sequence in one call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "One line the teacher will see on the banner."},
                "steps": {
                    "type": "array",
                    "description": "Steps in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["move", "click", "double_click", "right_click",
                                         "type", "press", "hotkey", "scroll", "wait", "open"],
                            },
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "text": {"type": "string"},
                            "key": {"type": "string"},
                            "keys": {"type": "array", "items": {"type": "string"}},
                            "amount": {"type": "integer"},
                            "seconds": {"type": "number"},
                            "target": {"type": "string", "description": "App name, file path or URL for 'open'."},
                        },
                        "required": ["action"],
                    },
                },
            },
            "required": ["intent", "steps"],
        },
    },
    {
        "name": "read_my_day",
        "description": "The teacher's timetable for today and their open tasks. Use when they ask about their day, or before planning their time.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def tools_for(user: dict[str, Any], allow_computer: bool) -> list[dict[str, Any]]:
    names = {"create_file", "look_up_class", "save_task", "read_my_day"}
    if allow_computer:
        names.add("use_computer")
    return [t for t in TOOL_SCHEMAS if t["name"] in names]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute(name: str, payload: dict[str, Any], user: dict[str, Any],
            conv_id: int | None, emit: Any = None) -> dict[str, Any]:
    """Run a tool. Returns ``{"text": <for the model>, "card": <for the UI or None>}``."""
    handler = {
        "create_file": _tool_create_file,
        "look_up_class": _tool_look_up_class,
        "save_task": _tool_save_task,
        "use_computer": _tool_use_computer,
        "read_my_day": _tool_read_my_day,
    }.get(name)
    if handler is None:
        return {"text": f"No tool called {name}.", "card": None}
    try:
        return handler(payload, user, conv_id, emit)
    except Exception as exc:
        return {"text": f"The tool failed: {exc}", "card": None}


def _tool_create_file(payload: dict[str, Any], user: dict[str, Any],
                      conv_id: int | None, emit: Any) -> dict[str, Any]:
    kind = str(payload.get("kind", "docx")).lower()
    if kind not in artifacts.KINDS:
        kind = "docx"
    name = str(payload.get("name") or "Document")
    content = payload.get("content", "")
    title = str(payload.get("title") or name)
    subtitle = str(payload.get("subtitle") or "")
    if not subtitle:
        subtitle = f"{user.get('name', '')} · {dt.datetime.now().strftime('%d %B %Y')}".strip(" ·")

    data = artifacts.render(kind, content, title, subtitle)
    record = artifacts.store(int(user["id"]), conv_id, name, kind, data)
    record["title"] = title
    db.log_own_work(int(user["id"]), record["name"], len(str(content)), 0)
    db.audit("file.create", user_id=int(user["id"]), actor=user.get("name", ""),
             detail=f"{record['name']} ({record['size']} bytes)")
    return {
        "text": f"Created {record['name']} ({record['label']}, {record['size']} bytes). "
                "The teacher can save it to their Desktop or Google Drive from the file card.",
        "card": {"type": "file", **record},
    }


def _tool_look_up_class(payload: dict[str, Any], user: dict[str, Any],
                        conv_id: int | None, emit: Any) -> dict[str, Any]:
    wanted = str(payload.get("class_name", "")).strip().lower()
    include = set(payload.get("include") or ["students"])
    classes = db.list_classes(int(user["id"]))
    match = None
    for klass in classes:
        haystack = f"{klass['name']} {klass['grade']}{klass['section']} {klass['subject']}".lower()
        if wanted and (wanted in haystack or haystack.startswith(wanted)):
            match = klass
            break
    if match is None:
        if not classes:
            return {"text": "No classes are registered in Float yet. The teacher can add one "
                            "under Classes, or paste the list of names here.", "card": None}
        names = ", ".join(c["name"] for c in classes)
        return {"text": f"No class matched '{wanted}'. Registered classes: {names}.", "card": None}

    report: list[str] = [f"Class {match['name']} — {match['subject'] or 'no subject set'}, "
                         f"{match['students']} students."]
    if "students" in include:
        students = db.list_students(int(match["id"]))
        report.append("Students: " + ", ".join(
            f"{s['roll']} {s['name']}".strip() for s in students) or "none")
    if "marks" in include:
        marks = db.class_marks(int(match["id"]))[:200]
        if marks:
            report.append("Recent marks: " + "; ".join(
                f"{m['name']} {m['assessment']} {m['score']}/{m['max_score']}" for m in marks[:60]))
        else:
            report.append("No marks recorded.")
    if "attendance" in include:
        rows = db.attendance_range(int(match["id"]), 20)
        if rows:
            report.append("Attendance: " + "; ".join(
                f"{r['on_date']}: {len(r['present'])} present, {len(r['absent'])} absent" for r in rows))
        else:
            report.append("No attendance recorded.")
    return {"text": "\n".join(report), "card": None}


def _tool_save_task(payload: dict[str, Any], user: dict[str, Any],
                    conv_id: int | None, emit: Any) -> dict[str, Any]:
    title = str(payload.get("title", "")).strip()
    if not title:
        return {"text": "A task needs a title.", "card": None}
    due = str(payload.get("due", "")).strip()
    db.add_task(int(user["id"]), title, due)
    return {"text": f"Added to the teacher's list: {title}" + (f" (due {due})" if due else ""),
            "card": {"type": "task", "title": title, "due": due}}


def _tool_read_my_day(payload: dict[str, Any], user: dict[str, Any],
                      conv_id: int | None, emit: Any) -> dict[str, Any]:
    today = dt.datetime.now()
    periods = db.list_periods(int(user["id"]), today.weekday())
    tasks = db.list_tasks(int(user["id"]))
    lines = [f"{today.strftime('%A %d %B')}."]
    if periods:
        lines.append("Timetable: " + "; ".join(
            f"{p['starts']}-{p['ends']} {p['label']}" + (f" ({p['room']})" if p['room'] else "")
            for p in periods))
    else:
        lines.append("No timetable saved for today.")
    if tasks:
        lines.append("Open tasks: " + "; ".join(
            f"{t['title']}" + (f" (due {t['due']})" if t["due"] else "") for t in tasks[:20]))
    else:
        lines.append("No open tasks.")
    return {"text": "\n".join(lines), "card": None}


def _tool_use_computer(payload: dict[str, Any], user: dict[str, Any],
                       conv_id: int | None, emit: Any) -> dict[str, Any]:
    ok, reason = computer.available()
    if not ok:
        return {"text": f"Cannot use the computer: {reason}", "card": None}

    intent = str(payload.get("intent", "a task"))[:120]
    steps = payload.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return {"text": "No steps were given.", "card": None}
    if len(steps) > 40:
        return {"text": "That is more than 40 steps. Break it into smaller runs.", "card": None}

    try:
        session = computer.begin(int(user["id"]), user.get("name", ""), intent,
                                 user.get("accent", "#E8A33D"))
    except RuntimeError as exc:
        return {"text": str(exc), "card": None}

    done: list[str] = []
    try:
        for step in steps:
            action = str(step.get("action", ""))
            kwargs = {k: v for k, v in step.items() if k != "action"}
            done.append(computer.act(session, action, **kwargs))
            if emit:
                emit({"type": "computer_step", "text": done[-1]})
    except computer.Stopped:
        computer.end("stopped")
        return {"text": f"The teacher stopped the run after {len(done)} steps: "
                        + "; ".join(done), "card": {"type": "computer", "steps": done, "stopped": True}}
    except Exception as exc:
        computer.end(f"failed: {exc}")
        return {"text": f"Stopped after {len(done)} steps because: {exc}. Completed: "
                        + "; ".join(done), "card": {"type": "computer", "steps": done, "stopped": True}}

    computer.end("finished")
    return {"text": f"Done: {'; '.join(done)}",
            "card": {"type": "computer", "steps": done, "stopped": False}}


# ---------------------------------------------------------------------------
# The teaching toolkit
#
# Each entry is a small form plus the prompt it fills. ``fields`` are rendered by
# the front end; ``{placeholders}`` in ``prompt`` are filled from them.
# ---------------------------------------------------------------------------
def _f(key: str, label: str, kind: str = "text", **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": kind, **extra}


_CLASS = _f("klass", "Class", "text", placeholder="8B")
_SUBJECT = _f("subject", "Subject", "text", placeholder="Science")
_TOPIC = _f("topic", "Chapter or topic", "text", placeholder="Force and Laws of Motion")
_BOARD = _f("board", "Board", "select", options=list(config.BOARDS))

TOOLKIT: list[dict[str, Any]] = [
    {
        "id": "lesson-plan",
        "group": "Planning",
        "name": "Lesson plan",
        "blurb": "A period-by-period plan with outcomes, activities and board work.",
        "icon": "plan",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT, _TOPIC, _BOARD,
            _f("periods", "Number of periods", "number", value=1, min=1, max=20),
            _f("minutes", "Minutes per period", "number", value=40, min=20, max=120),
            _f("strength", "Class strength", "number", value=45, min=5, max=100),
            _f("notes", "Anything specific", "textarea",
               placeholder="Half the class struggles with numericals. No projector this week."),
        ],
        "prompt": """Write a lesson plan for Class {klass} {subject}, {board}, on: {topic}.
{periods} period(s) of {minutes} minutes each. Class strength about {strength}.

Structure each period as: learning outcomes (observable, in student language) · prior \
knowledge to check in 2 minutes · the hook · main teaching with what goes on the \
blackboard · one activity that works with {strength} students and no special equipment · \
check for understanding with the exact questions to ask · homework · what to do if the \
period runs short.

Add: common misconceptions on this topic and how to catch them; two differentiated \
tasks, one for students who finish early and one for students who are behind; the \
materials list.

Teacher's notes: {notes}

Create it as a Word file named "{klass} {subject} — {topic} lesson plan".""",
    },
    {
        "id": "question-paper",
        "group": "Assessment",
        "name": "Question paper",
        "blurb": "Blueprint-based paper with a marking scheme that adds up.",
        "icon": "paper",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT, _BOARD,
            _f("chapters", "Chapters covered", "textarea", placeholder="Ch 8 Motion, Ch 9 Force and Laws of Motion"),
            _f("marks", "Total marks", "number", value=40, min=10, max=100),
            _f("duration", "Duration (minutes)", "number", value=90, min=30, max=180),
            _f("exam_type", "Type", "select",
               options=["Unit test", "Periodic test", "Half-yearly", "Pre-board", "Annual", "Practice paper"]),
            _f("difficulty", "Difficulty", "select", options=["Easy", "Moderate", "Challenging", "Board-level"]),
            _f("competency", "Competency-based questions", "select",
               options=["40% (CBSE current)", "25%", "50%", "None"]),
        ],
        "prompt": """Set a {exam_type} question paper for Class {klass} {subject}, {board}.
Chapters: {chapters}. Total {marks} marks, {duration} minutes. Difficulty: {difficulty}. \
Competency-based/application questions: {competency}.

Start with a blueprint table: section, question type, marks each, number of questions, \
total marks, and the Bloom's level. The section totals must sum exactly to {marks} — \
check this before writing the paper.

Then the paper itself: general instructions at the top, sections labelled the way \
{board} labels them, questions numbered continuously, marks in brackets at the right of \
each question. Include internal choice where the board's pattern has it.

Then, after a page break, the marking scheme: the expected answer or value points for \
every question, with the mark split shown (e.g. "formula 1 + substitution 1 + answer 1").

Create it as a Word file named "{klass} {subject} {exam_type}".""",
    },
    {
        "id": "worksheet",
        "group": "Assessment",
        "name": "Worksheet",
        "blurb": "Practice sheet at three levels, with the answer key.",
        "icon": "worksheet",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT, _TOPIC,
            _f("count", "Questions", "number", value=15, min=5, max=50),
            _f("tiers", "Levels", "select",
               options=["Three tiers (support / core / stretch)", "One level", "Two tiers"]),
            _f("style", "Style", "select",
               options=["Mixed", "MCQ only", "Numerical practice", "Short answer", "Diagram-based", "Case study"]),
        ],
        "prompt": """Make a {subject} worksheet for Class {klass} on {topic}. About {count} \
questions, {style}, arranged as: {tiers}.

Each tier gets a short line telling the teacher who it is for. Leave ruled space \
indicated for written answers. Number continuously.

After a page break, the answer key with working shown for anything numerical.

Create it as a Word file named "{klass} {subject} — {topic} worksheet".""",
    },
    {
        "id": "marking",
        "group": "Assessment",
        "name": "Mark an answer",
        "blurb": "Band, mark split and feedback for one student's answer — you decide the mark.",
        "icon": "mark",
        "fields": [
            _CLASS, _SUBJECT,
            _f("question", "The question", "textarea"),
            _f("max_marks", "Marks available", "number", value=5, min=1, max=20),
            _f("answer", "The student's answer", "textarea", rows=8),
            _f("scheme", "Marking scheme, if you have one", "textarea", required=False),
        ],
        "prompt": """Here is one Class {klass} {subject} answer to mark, out of {max_marks}.

Question: {question}
Marking scheme: {scheme}
Student's answer: {answer}

Give me: the mark split point by point, showing exactly which mark was earned where and \
which was not · a suggested total, clearly labelled as a suggestion · one sentence of \
feedback written to the student, naming the specific thing to fix · the one thing this \
answer shows they have genuinely understood.

Then stop. The mark is the teacher's to award — do not argue for it.""",
    },
    {
        "id": "rubric",
        "group": "Assessment",
        "name": "Rubric",
        "blurb": "Criteria and descriptors you can hand to students beforehand.",
        "icon": "rubric",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT,
            _f("task", "The task", "textarea", placeholder="Group presentation on water conservation"),
            _f("marks", "Total marks", "number", value=20, min=5, max=100),
            _f("levels", "Levels", "number", value=4, min=3, max=5),
        ],
        "prompt": """Build a marking rubric for this Class {klass} {subject} task: {task}. \
{marks} marks total, {levels} performance levels.

A table: criteria down the left, levels across the top, a descriptor in every cell \
written in language a student can read, and the marks for each criterion. The criteria \
marks must total {marks}.

Under it, a one-paragraph version the teacher can read out to the class before they start.

Create it as a Word file named "{klass} {subject} rubric".""",
    },
    {
        "id": "remarks",
        "group": "Reporting",
        "name": "Report card remarks",
        "blurb": "Specific, non-repeating remarks for a whole class.",
        "icon": "report",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT,
            _f("students", "Students and notes", "textarea", rows=10,
               placeholder="Aarav — good in theory, careless in numericals\nDiya — quiet, excellent written work\nKabir — improving, needs to finish homework"),
            _f("words", "Words per remark", "number", value=35, min=15, max=80),
            _f("tone", "Tone", "select", options=["Warm and honest", "Formal", "Encouraging", "Direct"]),
        ],
        "prompt": """Write report card remarks for Class {klass} {subject}. Tone: {tone}. \
About {words} words each.

Students and my notes on them:
{students}

Rules: every remark names something specific that student did — no remark should work \
for two different children. Say one thing they have done well and one thing to work on, \
with a concrete next step. Never label the child ("weak", "bright", "average"); describe \
the work. No two remarks may open with the same word.

Put them in a table: name, remark. Create it as a Word file named "{klass} {subject} remarks".""",
    },
    {
        "id": "parent-message",
        "group": "Communication",
        "name": "Message to a parent",
        "blurb": "WhatsApp-length, in English or your language, ready to send.",
        "icon": "message",
        "fields": [
            _f("student", "Student's name", "text"),
            _CLASS,
            _f("reason", "What it is about", "select",
               options=["Absence", "Homework not done", "Praise / good work", "Behaviour concern",
                        "Falling marks", "PTM invitation", "Fee reminder", "Medical / leave", "Other"]),
            _f("detail", "The specifics", "textarea",
               placeholder="Absent 4 days this week, no message from home"),
            _f("language", "Language", "select", options=list(config.LANGUAGES.values())),
            _f("channel", "Sending it by", "select", options=["WhatsApp", "School diary note", "Email", "SMS"]),
        ],
        "prompt": """Write a message to the parent of {student}, Class {klass}, about: {reason}. \
Details: {detail}. Language: {language}. Channel: {channel}.

Keep it to the length that channel deserves — a WhatsApp message is four or five lines, \
not a letter. Respectful, clear, no blame on the child, one specific ask or next step. \
Open with the teacher's name and the class so the parent knows who is writing.

Give me two versions: a warmer one and a more formal one. Nothing else.""",
    },
    {
        "id": "bilingual",
        "group": "Teaching",
        "name": "Explain in two languages",
        "blurb": "A concept side by side in English and your classroom language.",
        "icon": "languages",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT, _TOPIC,
            _f("language", "Second language", "select", options=list(config.LANGUAGES.values())),
            _f("depth", "For", "select", options=["The whole class", "Students who are behind", "Revision before the exam"]),
        ],
        "prompt": """Explain {topic} ({subject}, Class {klass}) side by side in English and \
{language}, for: {depth}.

A two-column table: English on the left, {language} on the right, matched line for line, \
so the teacher can read either column aloud. Keep the technical terms in English in both \
columns — that is what the exam will use — but explain each one in {language} the first \
time it appears.

Then: the three sentences worth writing on the blackboard, in both languages. Then five \
questions to ask the class, in both languages.

Create it as a Word file named "{topic} — bilingual notes".""",
    },
    {
        "id": "activity",
        "group": "Teaching",
        "name": "Classroom activity",
        "blurb": "Something that works with 50 students and no equipment.",
        "icon": "activity",
        "fields": [
            _CLASS, _SUBJECT, _TOPIC,
            _f("minutes", "Time available", "number", value=20, min=5, max=60),
            _f("strength", "Class strength", "number", value=48, min=5, max=100),
            _f("budget", "Materials", "select",
               options=["Nothing but chalk and blackboard", "Everyday items from home",
                        "Under ₹200 total", "School lab available", "One projector"]),
        ],
        "prompt": """Give me three classroom activities for {topic} ({subject}, Class {klass}). \
{minutes} minutes, {strength} students, materials: {budget}.

For each: what it is in one line · exactly how to run it with {strength} students in a \
room with fixed benches · what the teacher says to start it · how long each part takes · \
what to do when it goes wrong · what you actually see if they understood.

Rank them: the safest one first, the most interesting one last. Be honest about which one \
will be noisy.""",
    },
    {
        "id": "remedial",
        "group": "Teaching",
        "name": "Remedial plan",
        "blurb": "A two-week plan for students who are behind.",
        "icon": "remedial",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT,
            _f("gap", "What they're struggling with", "textarea",
               placeholder="Cannot balance chemical equations; shaky on valency"),
            _f("count", "How many students", "number", value=8, min=1, max=40),
            _f("time", "Time you have", "select",
               options=["15 minutes after class, twice a week", "One extra period a week",
                        "Zero session time — inside the regular lesson only", "Saturday remedial class"]),
        ],
        "prompt": """Write a two-week remedial plan for {count} Class {klass} students in \
{subject}. What they are struggling with: {gap}. Time available: {time}.

Work backwards from the real gap, not the current chapter — name the earlier skill that \
is actually missing. Session by session: what to do, what the student does, and the \
one-question check at the end that tells you whether to move on.

Add: what the teacher should stop doing, if anything, that is making it worse; what to \
send home that a parent with no {subject} background can still help with.

Create it as a Word file named "{klass} {subject} remedial plan".""",
    },
    {
        "id": "pacing",
        "group": "Planning",
        "name": "Syllabus pacing",
        "blurb": "Weeks to chapters, with exams and holidays taken out.",
        "icon": "calendar",
        "kind": "xlsx",
        "fields": [
            _CLASS, _SUBJECT, _BOARD,
            _f("chapters", "Chapters left", "textarea", rows=6),
            _f("from_date", "Starting", "date"),
            _f("to_date", "Must finish by", "date"),
            _f("periods_week", "Periods per week", "number", value=5, min=1, max=12),
            _f("blocked", "Weeks already gone", "textarea", required=False,
               placeholder="Sports week 12-16 Oct, half-yearly 20-30 Sep"),
        ],
        "prompt": """Build a pacing calendar for Class {klass} {subject} ({board}) from \
{from_date} to {to_date}, {periods_week} periods a week.

Chapters remaining:
{chapters}

Unavailable: {blocked}

Count the actual teaching periods available between those dates, subtract the \
unavailable weeks, then allocate periods to chapters in proportion to their weight in \
the board exam — say what weighting you assumed. Leave 12% slack, because a term \
without slack is a term that overruns.

Give it as a table: week starting, chapters, periods, what must be finished by the end \
of that week, and a running "periods left" column. Flag clearly, in words, if the \
syllabus does not fit — do not quietly compress it.

Create it as an Excel file named "{klass} {subject} pacing".""",
    },
    {
        "id": "analysis",
        "group": "Reporting",
        "name": "Marks analysis",
        "blurb": "Paste your marks; get the topics to reteach.",
        "icon": "analysis",
        "fields": [
            _CLASS, _SUBJECT,
            _f("data", "Paste marks", "textarea", rows=10,
               placeholder="Name, Q1(5), Q2(5), Q3(10), Q4(10)\nAarav, 4, 2, 7, 3\nDiya, 5, 5, 9, 8"),
            _f("topics", "What each question tested", "textarea", required=False,
               placeholder="Q1 valency, Q2 balancing, Q3 mole concept, Q4 numericals"),
        ],
        "prompt": """Analyse these Class {klass} {subject} marks.

{data}

Question topics: {topics}

Give me: the class average and the spread · which question the class did worst on, with \
the percentage · the specific misconception that question failure points to · the \
students who are more than one standard deviation below, listed by name, with what each \
one specifically got wrong · the students who did well on the hard question and badly on \
the easy one, because that usually means carelessness, not ability.

Then one paragraph: what to reteach, and how long it should take. Do not recommend \
reteaching the whole chapter unless the data actually says so.""",
    },
    {
        "id": "notice",
        "group": "Communication",
        "name": "Notice or circular",
        "blurb": "Official school wording, first time.",
        "icon": "notice",
        "kind": "docx",
        "fields": [
            _f("about", "What it's about", "textarea"),
            _f("audience", "For", "select",
               options=["Parents", "Students", "All staff", "One class", "Notice board"]),
            _f("tone", "Tone", "select", options=["Formal", "Friendly", "Urgent"]),
            _f("from_who", "Signed by", "text", placeholder="Class Teacher, VIII-B"),
        ],
        "prompt": """Write a school notice for {audience} about: {about}. Tone: {tone}. \
Signed by: {from_who}.

Standard Indian school format: school name line, "NOTICE" or "CIRCULAR" centred, \
reference number placeholder, date, subject line, body, signature block. Body in short \
numbered points if there is more than one instruction. Say clearly what the reader must \
do and by when.

Create it as a Word file.""",
    },
    {
        "id": "letter",
        "group": "Communication",
        "name": "Letter to the principal",
        "blurb": "Leave, requisition, permission — properly worded.",
        "icon": "letter",
        "kind": "docx",
        "fields": [
            _f("purpose", "What you're asking for", "select",
               options=["Casual leave", "Medical leave", "Permission for a trip",
                        "Requisition for materials", "Request for a substitute",
                        "Complaint / concern", "Other"]),
            _f("detail", "Details", "textarea"),
            _f("from_who", "Your name and designation", "text"),
        ],
        "prompt": """Write a formal letter to the Principal. Purpose: {purpose}. \
Details: {detail}. From: {from_who}.

Standard Indian official format: To, The Principal / school name / place · Subject line · \
Respected Sir/Madam · body in two or three short paragraphs, the ask stated plainly in \
the first one · Yours faithfully · name and designation.

Courteous, brief, and specific about dates and numbers. Create it as a Word file.""",
    },
    {
        "id": "slides",
        "group": "Teaching",
        "name": "Lesson slides",
        "blurb": "Slide-by-slide content you can paste into PowerPoint.",
        "icon": "slides",
        "kind": "docx",
        "fields": [
            _CLASS, _SUBJECT, _TOPIC,
            _f("count", "Number of slides", "number", value=10, min=4, max=30),
        ],
        "prompt": """Plan {count} slides for teaching {topic} ({subject}, Class {klass}).

For each slide: the title · at most four lines of on-screen text, written to be read \
from the back row · what the teacher says while it is up, in two or three sentences · \
what to draw or show if there is no image available.

Slide one is a question, not a title card. The last slide is the check for understanding.

Create it as a Word file named "{topic} slides" with one section per slide.""",
    },
    {
        "id": "seating",
        "group": "Planning",
        "name": "Seating and groups",
        "blurb": "Group lists that mix ability without announcing it.",
        "icon": "seating",
        "fields": [
            _CLASS,
            _f("students", "Names (or leave blank to use your class list)", "textarea", required=False),
            _f("size", "Group size", "number", value=4, min=2, max=8),
            _f("basis", "Mix by", "select",
               options=["Mixed ability", "Same ability", "Keep talkers apart", "Random", "Friendship groups avoided"]),
        ],
        "prompt": """Make groups of {size} for Class {klass}, on this basis: {basis}.
Students: {students}

If no names were given, look up the class first.

Give the groups as a table with a neutral label for each (Group A, B, …) — never a label \
that reveals the basis to the students. Add one line on where to seat each group in a \
room with fixed benches and a blackboard at the front, and which two groups should not \
be next to each other.""",
    },
    {
        "id": "doubt",
        "group": "Teaching",
        "name": "Solve a doubt",
        "blurb": "A worked answer, and how to explain it at the board.",
        "icon": "doubt",
        "fields": [
            _CLASS, _SUBJECT,
            _f("question", "The question", "textarea", rows=5),
            _f("level", "Explain for", "select",
               options=["A student who is behind", "The average student", "A student who wants more"]),
        ],
        "prompt": """A Class {klass} {subject} student asked: {question}

Answer it for: {level}.

Give me: the answer worked through step by step, with the reason for each step, not just \
the algebra · the one step students most often get wrong here · how to put it on the \
blackboard, including what to draw · one analogy that holds up, and where the analogy \
breaks (say so — a broken analogy taught as true costs more than no analogy) · one \
follow-up question to check they actually got it.""",
    },
    {
        "id": "assembly",
        "group": "Communication",
        "name": "Assembly speech",
        "blurb": "Two to three minutes, in a student's voice.",
        "icon": "assembly",
        "fields": [
            _f("occasion", "Occasion or theme", "text",
               placeholder="Republic Day / World Environment Day / Exam week"),
            _f("speaker", "Who is speaking", "select",
               options=["A student", "The class teacher", "The head boy / head girl"]),
            _CLASS,
            _f("minutes", "Minutes", "number", value=3, min=1, max=10),
        ],
        "prompt": """Write a school assembly speech for {occasion}, delivered by {speaker} \
from Class {klass}, {minutes} minutes long (about {minutes}50 words).

Written for the voice of a {speaker} — sentences short enough to say out loud without \
losing breath, no vocabulary they would not use. Open with something concrete rather \
than a definition. One idea, carried through. End on something the school can actually \
do this week, not a general sentiment.

Mark the places to pause.""",
    },
    {
        "id": "hpc",
        "group": "Reporting",
        "name": "Holistic progress descriptors",
        "blurb": "NEP-style descriptors across domains, not a single grade.",
        "icon": "holistic",
        "kind": "docx",
        "fields": [
            _CLASS,
            _f("student", "Student", "text"),
            _f("observations", "What you've noticed", "textarea", rows=8,
               placeholder="Finishes work fast but won't check it. Helps others. Goes quiet in group work."),
            _f("domains", "Domains", "select",
               options=["Academic + socio-emotional + physical", "Academic only",
                        "Socio-emotional and life skills only"]),
        ],
        "prompt": """Write holistic progress descriptors for {student}, Class {klass}, \
covering: {domains}.

What the teacher has observed: {observations}

For each domain: two or three sentences describing what this child does, in observable \
terms — what they were seen doing, not what they are. Then one thing that would help \
them next, addressed to the child. Then one line for the parent.

No grades, no ranking, no comparison with other children, no words that fix a trait in \
place. If the observations do not support a domain, say the teacher has not recorded \
enough yet rather than inventing it.

Create it as a Word file named "{student} — progress descriptors".""",
    },
]

TOOLKIT_GROUPS = ["Planning", "Teaching", "Assessment", "Reporting", "Communication"]


def toolkit_entry(entry_id: str) -> dict[str, Any] | None:
    for entry in TOOLKIT:
        if entry["id"] == entry_id:
            return entry
    return None


def fill_prompt(entry: dict[str, Any], values: dict[str, Any]) -> str:
    prompt = entry["prompt"]
    for field in entry["fields"]:
        key = field["key"]
        raw = values.get(key, "")
        text = str(raw).strip() or ("not specified" if field.get("required") is not False else "—")
        prompt = prompt.replace("{" + key + "}", text)
    # Any placeholder the form did not cover.
    for key, value in values.items():
        prompt = prompt.replace("{" + str(key) + "}", str(value))
    return prompt
