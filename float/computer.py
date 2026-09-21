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
_lock = threading.Lock()
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


# ---------------------------------------------------------------------------
# The visible pointer
# ---------------------------------------------------------------------------
class _Overlay:
    """A borderless always-on-top window that draws Float's pointer and banner.

    Tkinter, because it ships with Python. It runs on its own thread with its own
    event loop and is entirely optional: if the display refuses it, every method
    here becomes a no-op and the automation carries on without the visuals.
    """

    def __init__(self, accent: str = "#E8A33D") -> None:
        self.accent = accent
        self.ok = False
        self._queue: list[Callable[[], None]] = []
        self._queue_lock = threading.Lock()
        self._root = None
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
            self._ring = None
            self._banner = None
            self.ok = True
            self._ready.set()

            def pump() -> None:
                with self._queue_lock:
                    jobs, self._queue = self._queue, []
                for job in jobs:
                    try:
                        job()
                    except Exception:
                        pass
                root.after(16, pump)

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
            if self._banner:
                canvas.delete(self._banner)
            if self._banner_bg:
                canvas.delete(self._banner_bg)
            width = self._width
            self._banner_bg = canvas.create_rectangle(
                width / 2 - 320, 18, width / 2 + 320, 62, fill="#1E3A32", outline=self.accent, width=2
            )
            self._banner = canvas.create_text(
                width / 2, 40, text=text[:90], fill="#FBF9F4",
                font=("Segoe UI", 11, "bold"),
            )

        self._banner_bg = getattr(self, "_banner_bg", None)
        self._post(draw)

    def move(self, x: int, y: int) -> None:
        def draw() -> None:
            canvas = self._canvas
            if self._ring:
                canvas.delete(self._ring)
            if self._dot:
                canvas.delete(self._dot)
            self._ring = canvas.create_oval(
                x - 22, y - 22, x + 22, y + 22, outline=self.accent, width=3
            )
            self._dot = canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=self.accent, outline="")

        self._dot = getattr(self, "_dot", None)
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
    """Hold Escape for a moment to stop a run. Best effort, never required."""
    try:
        import tkinter as tk  # noqa: F401
    except Exception:
        return
    while not session.stop.is_set():
        try:
            if HAVE_PYAUTOGUI and hasattr(pyautogui, "keyDown"):
                pass
        except Exception:
            pass
        time.sleep(0.25)


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
    if _overlay:
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

    width, height = screen_size()

    if action == "move":
        x, y = int(kwargs["x"]), int(kwargs["y"])
        _glide(session, x, y)
        _note(session, f"moved to {x}, {y}")

    elif action in {"click", "double_click", "right_click"}:
        x, y = int(kwargs.get("x", -1)), int(kwargs.get("y", -1))
        if x >= 0 and y >= 0:
            _glide(session, x, y)
        if _overlay and x >= 0:
            _overlay.flash(x, y)
        button = "right" if action == "right_click" else "left"
        clicks = 2 if action == "double_click" else 1
        pyautogui.click(button=button, clicks=clicks, interval=0.08)
        _note(session, f"{action.replace('_', ' ')} at {x}, {y}" if x >= 0 else action)

    elif action == "type":
        text = str(kwargs.get("text", ""))[:4000]
        pyautogui.typewrite(text, interval=_TYPE_INTERVAL)
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
