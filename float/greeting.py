"""Float — the line the teacher sees first.

A greeting is worth writing properly because it is the only part of the interface
every teacher reads every day. Three rules it follows:

*It is built from their day, not from a list.* The time, the weekday, what is on
their timetable in the next hour, what they left unfinished. "Good morning" is
not information; "Period 3 starts in 20 minutes — Class 8B, Science" is.

*It costs nothing.* No model call, no network. It is assembled locally in about a
millisecond, so the window is useful before anything has loaded.

*It says one thing.* A greeting, then at most one fact, then at most one nudge.
A dashboard of six numbers at 7:40 in the morning is not a welcome.

The front end sets this in Times New Roman, which is the one typographic
instruction the brief pinned down.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from . import db

#: Hindi/regional salutations are offered to teachers whose language is set to
#: something other than English. They are greetings, not translations of the
#: whole line — mixing scripts mid-sentence reads badly.
SALUTATIONS = {
    "en": {"morning": "Good morning", "afternoon": "Good afternoon", "evening": "Good evening"},
    "hi": {"morning": "सुप्रभात", "afternoon": "नमस्कार", "evening": "शुभ संध्या"},
    "bn": {"morning": "শুভ সকাল", "afternoon": "নমস্কার", "evening": "শুভ সন্ধ্যা"},
    "mr": {"morning": "शुभ सकाळ", "afternoon": "नमस्कार", "evening": "शुभ संध्याकाळ"},
    "ta": {"morning": "காலை வணக்கம்", "afternoon": "வணக்கம்", "evening": "மாலை வணக்கம்"},
    "te": {"morning": "శుభోదయం", "afternoon": "నమస్కారం", "evening": "శుభ సాయంత్రం"},
    "gu": {"morning": "સુપ્રભાત", "afternoon": "નમસ્તે", "evening": "શુભ સંધ્યા"},
    "kn": {"morning": "ಶುಭೋದಯ", "afternoon": "ನಮಸ್ಕಾರ", "evening": "ಶುಭ ಸಂಜೆ"},
    "ml": {"morning": "സുപ്രഭാതം", "afternoon": "നമസ്കാരം", "evening": "ശുഭ സായാഹ്നം"},
    "pa": {"morning": "ਸ਼ੁਭ ਸਵੇਰ", "afternoon": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "evening": "ਸ਼ੁਭ ਸ਼ਾਮ"},
    "or": {"morning": "ଶୁଭ ସକାଳ", "afternoon": "ନମସ୍କାର", "evening": "ଶୁଭ ସନ୍ଧ୍ୟା"},
    "as": {"morning": "শুভ ৰাতিপুৱা", "afternoon": "নমস্কাৰ", "evening": "শুভ সন্ধিয়া"},
    "ur": {"morning": "صبح بخیر", "afternoon": "السلام علیکم", "evening": "شام بخیر"},
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: Said once, quietly, under the greeting. Never more than one.
OPENERS = (
    "What are we working on?",
    "Where would you like to start?",
    "What's first today?",
    "Tell me what you need.",
)


def _part_of_day(hour: int) -> str:
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _first_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    parts = name.replace(".", " ").split()
    # "Mrs Anita Sharma" → "Anita"; a lone honorific is not a name.
    honorifics = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sir", "madam"}
    for part in parts:
        if part.lower().strip(".") not in honorifics:
            return part
    return parts[-1]


def _parse_clock(value: str) -> dt.time | None:
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%H.%M"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def next_period(user_id: int, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """The teacher's next scheduled period today, if there is one."""
    now = now or dt.datetime.now()
    weekday = now.weekday()
    rows = db.list_periods(user_id, weekday)
    best: tuple[int, dict[str, Any]] | None = None
    for row in rows:
        start = _parse_clock(row["starts"])
        if start is None:
            continue
        starts_at = dt.datetime.combine(now.date(), start)
        minutes = int((starts_at - now).total_seconds() // 60)
        if -5 <= minutes <= 180 and (best is None or minutes < best[0]):
            best = (minutes, {**row, "in_minutes": minutes})
    return best[1] if best else None


def build(user: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    """Return ``{headline, detail, opener, period, tone}``."""
    now = now or dt.datetime.now()
    name = _first_name(user.get("name", ""))
    language = user.get("language", "en")
    salutation = SALUTATIONS.get(language, SALUTATIONS["en"])[_part_of_day(now.hour)]
    headline = f"{salutation}, {name}." if name else f"{salutation}."

    detail = ""
    tone = "calm"
    period = next_period(int(user["id"]), now)

    if period:
        label = period.get("label") or "your next class"
        minutes = period["in_minutes"]
        room = f" in {period['room']}" if period.get("room") else ""
        if minutes <= 0:
            detail = f"{label}{room} is on now."
            tone = "urgent"
        elif minutes <= 30:
            detail = f"{label}{room} starts in {minutes} minute{'s' if minutes != 1 else ''}."
            tone = "urgent"
        else:
            detail = f"Next up: {label}{room} at {period['starts']}."

    if not detail:
        open_tasks = db.list_tasks(int(user["id"]))
        today = now.date().isoformat()
        due_today = [t for t in open_tasks if t["due"] and t["due"] <= today]
        if due_today:
            first = due_today[0]["title"]
            extra = len(due_today) - 1
            detail = f"Due today: {first}" + (f", and {extra} more." if extra else ".")
            tone = "urgent" if len(due_today) > 2 else "calm"
        elif open_tasks:
            detail = f"{len(open_tasks)} thing{'s' if len(open_tasks) != 1 else ''} on your list."
        elif now.weekday() == 4 and now.hour >= 14:
            detail = "It's Friday afternoon. Worth closing the week off."
        elif now.weekday() >= 5:
            detail = f"It's {WEEKDAYS[now.weekday()]} — planning day, if you want it to be."
        elif user.get("sign_ins", 0) <= 1:
            detail = "First time here. Tell me what you teach and I'll fit around it."

    return {
        "headline": headline,
        "detail": detail,
        "opener": random.choice(OPENERS),
        "period": period,
        "tone": tone,
        "date": now.strftime("%A, %d %B"),
        "clock": now.strftime("%H:%M"),
    }


def week_reflection(user_id: int) -> str | None:
    """Friday's note about what the teacher built, not what Float did.

    Deliberately phrased around their contribution. A tool that reports its own
    output volume back at you every week is training you to measure the wrong
    thing.
    """
    now = dt.datetime.now()
    if now.weekday() != 4:
        return None
    summary = db.own_work_summary(user_id, days=7)
    if not summary["n"]:
        return None
    share = summary["own_share"]
    if share >= 50:
        return f"This week, {share}% of what went out was in your own words. That's the right shape."
    if share >= 20:
        return f"This week you reworked about {share}% of the drafts. Worth a look at the ones you didn't."
    return "Most of this week's drafts went out close to as written. Worth a read-through before Monday."
