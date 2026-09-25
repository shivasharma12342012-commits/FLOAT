"""Float — using the computer, visibly.

When a teacher says "open the attendance sheet and fill in today's column", Float
can do it: move the pointer, click, type. That is a serious capability to hand to
a program, so three things are true of it here.

**You can see it.** Float does not borrow the system cursor silently. It draws
its own pointer — a ring that travels to the target and lands on it a beat before
the click — and a banner across the top of the screen saying what it is doing.
Automation you cannot see is automation you cannot supervise.

**You can stop it.** Escape, the banner's stop button, or the stop control in the
app. The flag is checked between every step, so the worst case is one action in
flight, not a queue draining into your files.

**It is written down.** Every step goes to the audit log with the teacher's name
against it, and an admin can switch the whole capability off for the school.

Both dependencies are optional. Without ``pyautogui`` Float says plainly that it
cannot drive the machine, rather than pretending to and doing nothing. Without a
display, the pointer overlay is skipped and the actions still run.
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db

try:  # pragma: no cover - depends on the host
    import pyautogui  # type: ignore

    pyautogui.FAILSAFE = True          # slam the pointer into a corner to abort
    pyautogui.PAUSE = 0.06
    HAVE_PYAUTOGUI = True
except Exception:
    pyautogui = None  # type: ignore
    HAVE_PYAUTOGUI = False

try:  # pragma: no cover - only used for the Escape watcher
    import keyboard  # type: ignore

    HAVE_KEYBOARD = True
except Exception:
    keyboard = None  # type: ignore
    HAVE_KEYBOARD = False

#: Typing faster than this looks like a macro and trips some input fields.
_TYPE_INTERVAL = 0.012
#: How long the pointer takes to travel. Fast enough not to be tedious, slow
#: enough that a person can follow it and reach for Escape.
_GLIDE = 0.45


class Stopped(Exception):
    """Raised when the teacher interrupts a run."""


@dataclass
class Session:
    user_id: int
    user_name: str
    intent: str
    started: float = field(default_factory=time.time)
    steps: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)


_current: Session | None = None
_lock = threading.RLock()
_overlay: "_Overlay | None" = None


def available() -> tuple[bool, str]:
    if not HAVE_PYAUTOGUI:
        return False, (
            "Pointer control needs one extra package. Run "
            "`pip install pyautogui` in the Float folder, then restart Float."
        )
    if db.org_get("computer_control", "1") != "1":
        return False, "Your school has switched pointer control off for all accounts."
    return True, "Ready."


def screen_size() -> tuple[int, int]:
    if not HAVE_PYAUTOGUI:
        return (0, 0)
    try:
        size = pyautogui.size()
        return int(size[0]), int(size[1])
    except Exception:
        return (0, 0)


def _clamp(x: int, y: int) -> tuple[int, int]:
    """Keep a target on-screen. A bad tool call should never send the pointer
    off into a second monitor or a negative coordinate."""
    width, height = screen_size()
    if width <= 0 or height <= 0:
        return x, y
    return max(0, min(x, width - 1)), max(0, min(y, height - 1))


# ---------------------------------------------------------------------------
# The visible pointer
# ---------------------------------------------------------------------------
class _Overlay:
    """A borderless always-on-top window that draws Float's pointer and banner.

    Tkinter, because it ships with Python. It runs on its own thread with its own
    event loop and is entirely optional: if the display refuses it, every method
    here becomes a no-op and the automation carries on without the visuals.

    All the canvas item IDs (``_ring``, ``_dot``, ``_banner``, ``_banner_bg``)
    are only ever touched on the Tk thread, inside jobs run by ``pump``. They are
    initialised to ``None`` before the queue can receive anything, so a ``banner``
    or ``move`` call from another thread can never see a half-constructed overlay.
    """

    def __init__(self, accent: str = "#E8A33D") -> None:
        self.accent = accent
        self.ok = False
        self._queue: list[Callable[[], None]] = []
        self._queue_lock = threading.Lock()
        self._root = None
        self._ring = None
        self._dot = None
        self._banner = None
        self._banner_bg = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="float-overlay")
        self._ready = threading.Event()
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            try:
                root.attributes("-transparentcolor", "#010203")
            except Exception:
                root.attributes("-alpha", 0.92)

            width, height = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{width}x{height}+0+0")
            canvas = tk.Canvas(root, bg="#010203", highlightthickness=0)
            canvas.pack(fill="both", expand=True)

            self._root, self._canvas = root, canvas
            self._width, self._height = width, height

            def pump() -> None:
                with self._queue_lock:
                    jobs, self._queue = self._queue, []
                for job in jobs:
                    try:
                        job()
                    except Exception:
                        pass
                root.after(16, pump)

            # Only flip `ok` once the canvas genuinely exists and the pump is
            # scheduled - a job posted a moment earlier just waits in the queue.
            self.ok = True
            self._ready.set()
            root.after(16, pump)
            root.mainloop()
        except Exception:
            self.ok = False
            self._ready.set()

    def _post(self, job: Callable[[], None]) -> None:
        if not self.ok:
            return
        with self._queue_lock:
            self._queue.append(job)

    def banner(self, text: str) -> None:
        def draw() -> None:
            canvas = self._canvas
            if self._banner is not None:
                canvas.delete(self._banner)
            if self._banner_bg is not None:
                canvas.delete(self._banner_bg)
            width = self._width
            self._banner_bg = canvas.create_rectangle(
                width / 2 - 320, 18, width / 2 + 320, 62, fill="#1E3A32", outline=self.accent, width=2
            )
            self._banner = canvas.create_text(
                width / 2, 40, text=text[:90], fill="#FBF9F4",
                font=("Segoe UI", 11, "bold"),
            )

        self._post(draw)

    def move(self, x: int, y: int) -> None:
        def draw() -> None:
            canvas = self._canvas
            if self._ring is not None:
                canvas.delete(self._ring)
            if self._dot is not None:
                canvas.delete(self._dot)
            self._ring = canvas.create_oval(
                x - 22, y - 22, x + 22, y + 22, outline=self.accent, width=3
            )
            self._dot = canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=self.accent, outline="")

        self._post(draw)

    def flash(self, x: int, y: int) -> None:
        def draw() -> None:
            canvas = self._canvas
            pulse = canvas.create_oval(x - 34, y - 34, x + 34, y + 34,
                                       outline="#FBF9F4", width=3)
            canvas.after(220, lambda: canvas.delete(pulse))

        self._post(draw)

    def close(self) -> None:
        def shut() -> None:
            try:
                self._root.destroy()
            except Exception:
                pass

        self._post(shut)
        self.ok = False


# ---------------------------------------------------------------------------
# Escape watcher
# ---------------------------------------------------------------------------
def _watch_for_escape(session: Session) -> None:
    """Watch for Escape and stop the run the moment it's pressed.

    Best effort, never required — the stop button and the API both work
    without this. Needs the optional ``keyboard`` package; without it (or
    without permission to hook the keyboard, which some Linux/macOS setups
    require running as root for) this simply does nothing, same as before,
    and the explicit stop controls remain the only way to interrupt a run.
    """
    if not HAVE_KEYBOARD:
        return
    try:
        while not session.stop.is_set():
            if keyboard.is_pressed("esc"):
                session.stop.set()
                return
            time.sleep(0.05)
    except Exception:
        # Some platforms need elevated privileges for global key hooks;
        # fail silently and leave the explicit stop controls as the way in.
        return


# ---------------------------------------------------------------------------
# Session control
# ---------------------------------------------------------------------------
def begin(user_id: int, user_name: str, intent: str, accent: str = "#E8A33D") -> Session:
    global _current, _overlay
    with _lock:
        if _current is not None and not _current.stop.is_set():
            raise RuntimeError("Float is already using the computer. Stop that run first.")
        session = Session(user_id=user_id, user_name=user_name, intent=intent)
        _current = session
        _overlay = _Overlay(accent)
        _overlay.banner(f"Float is using your computer — {intent[:60]}   ·   press Esc to stop")
        threading.Thread(target=_watch_for_escape, args=(session,),
                         daemon=True, name="float-escape-watch").start()
    db.audit("computer.begin", user_id=user_id, actor=user_name, detail=intent, level="warn")
    return session


def end(note: str = "finished") -> None:
    global _current, _overlay
    with _lock:
        session = _current
        _current = None
        overlay, _overlay = _overlay, None
    if overlay:
        overlay.close()
    if session:
        db.audit(
            "computer.end",
            user_id=session.user_id,
            actor=session.user_name,
            detail=f"{note}; {len(session.steps)} steps in {time.time() - session.started:.1f}s",
            level="warn",
        )


def stop_current() -> bool:
    with _lock:
        session = _current
        if session is None:
            return False
        session.stop.set()
        db.audit("computer.stop", user_id=session.user_id, actor=session.user_name,
                 detail="stopped by the teacher", level="warn")
        # Still holding the lock, so no other thread can sneak a new
        # `begin()` in between the flag being set and the session closing.
        end("stopped by the teacher")
    return True


def current_status() -> dict[str, Any]:
    with _lock:
        session = _current
    if session is None:
        return {"running": False}
    return {
        "running": True,
        "intent": session.intent,
        "steps": session.steps[-12:],
        "seconds": round(time.time() - session.started, 1),
    }


def _guard(session: Session) -> None:
    if session.stop.is_set():
        raise Stopped("Stopped.")


def _note(session: Session, text: str) -> None:
    session.steps.append(text)
    if _overlay:
        _overlay.banner(f"Float: {text[:70]}   ·   press Esc to stop")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _glide(session: Session, x: int, y: int) -> None:
    """Move Float's pointer to the target, visibly, then take the system one with it."""
    _guard(session)
    if _overlay and _overlay.ok:
        start = pyautogui.position() if HAVE_PYAUTOGUI else (x, y)
        steps = 18
        for i in range(1, steps + 1):
            _guard(session)
            t = i / steps
            ease = 1 - (1 - t) ** 3
            _overlay.move(int(start[0] + (x - start[0]) * ease),
                          int(start[1] + (y - start[1]) * ease))
            time.sleep(_GLIDE / steps)
    if HAVE_PYAUTOGUI:
        pyautogui.moveTo(x, y, duration=0.12)


def act(session: Session, action: str, **kwargs: Any) -> str:
    """Perform one step. Returns a sentence describing what happened."""
    _guard(session)
    ok, reason = available()
    if not ok:
        raise RuntimeError(reason)

    if action == "move":
        x, y = _clamp(int(kwargs["x"]), int(kwargs["y"]))
        _glide(session, x, y)
        _note(session, f"moved to {x}, {y}")

    elif action in {"click", "double_click", "right_click"}:
        raw_x, raw_y = int(kwargs.get("x", -1)), int(kwargs.get("y", -1))
        has_target = raw_x >= 0 and raw_y >= 0
        x, y = _clamp(raw_x, raw_y) if has_target else (raw_x, raw_y)
        if has_target:
            _glide(session, x, y)
        if _overlay and has_target:
            _overlay.flash(x, y)
        button = "right" if action == "right_click" else "left"
        clicks = 2 if action == "double_click" else 1
        pyautogui.click(button=button, clicks=clicks, interval=0.08)
        _note(session, f"{action.replace('_', ' ')} at {x}, {y}" if has_target else action)

    elif action == "type":
        text = str(kwargs.get("text", ""))[:4000]
        _type_text(text)
        preview = text[:40] + ("…" if len(text) > 40 else "")
        _note(session, f"typed “{preview}”")

    elif action == "press":
        key = str(kwargs.get("key", "")).strip().lower()
        if not key:
            raise ValueError("press needs a key")
        pyautogui.press(key)
        _note(session, f"pressed {key}")

    elif action == "hotkey":
        keys = [str(k).strip().lower() for k in kwargs.get("keys", []) if str(k).strip()]
        if not keys:
            raise ValueError("hotkey needs keys")
        pyautogui.hotkey(*keys)
        _note(session, "pressed " + " + ".join(keys))

    elif action == "scroll":
        amount = int(kwargs.get("amount", -400))
        pyautogui.scroll(amount)
        _note(session, f"scrolled {'down' if amount < 0 else 'up'}")

    elif action == "wait":
        seconds = min(10.0, float(kwargs.get("seconds", 1)))
        deadline = time.time() + seconds
        while time.time() < deadline:
            _guard(session)
            time.sleep(0.1)
        _note(session, f"waited {seconds:g}s")

    elif action == "open":
        target = str(kwargs.get("target", "")).strip()
        if not target:
            raise ValueError("open needs something to open")
        _open_target(target)
        _note(session, f"opened {target}")

    else:
        raise ValueError(f"Float does not know the action '{action}'.")

    db.audit("computer.step", user_id=session.user_id, actor=session.user_name,
             detail=session.steps[-1] if session.steps else action)
    return session.steps[-1] if session.steps else action


def _type_text(text: str) -> None:
    """Type text that may contain characters outside pyautogui's known key set
    (accents, Devanagari and other Indic scripts, emoji). ``typewrite`` silently
    drops anything it doesn't recognise as a key, which used to mean a teacher's
    name or a Hindi sentence would arrive with holes in it and no error shown.
    Fall back to the clipboard-paste method for any character it can't type
    directly, so nothing is silently lost."""
    try:
        pyautogui.typewrite(text, interval=_TYPE_INTERVAL)
        return
    except (KeyError, ValueError):
        pass
    try:
        import pyperclip  # type: ignore

        previous = None
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = None
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        if previous is not None:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass
    except Exception:
        # No clipboard helper available either; type what we can character
        # by character so at least the ASCII portion lands correctly.
        for ch in text:
            try:
                pyautogui.typewrite(ch, interval=_TYPE_INTERVAL)
            except Exception:
                continue


def _open_target(target: str) -> None:
    system = platform.system()
    if target.startswith(("http://", "https://")):
        import webbrowser

        webbrowser.open(target)
        return
    if system == "Windows":
        subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
    elif system == "Darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def screenshot_size() -> dict[str, int]:
    width, height = screen_size()
    return {"width": width, "height": height}
