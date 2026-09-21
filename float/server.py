"""Float — the server.

A threaded ``http.server`` bound to loopback. No framework, for the same reason
the rest of Float has no dependencies: the machine this runs on is a school
laptop, and the install has to work on the first try, offline, behind a proxy
that blocks half of PyPI.

What guards it:

* It binds to 127.0.0.1. Nothing on the school network can reach it.
* The ``Host`` header is checked, which closes the DNS-rebinding version of the
  same problem — a page on the internet resolving a name to 127.0.0.1 and
  talking to this port from the teacher's own browser.
* Authentication is a bearer token in a header, never a cookie, so there is no
  CSRF surface to defend: a cross-origin page cannot make the browser attach it.
* The token is kept in ``sessionStorage``, which the browser discards when the
  tab closes, and the session it names is bound to this process's boot nonce, so
  closing Float invalidates it server-side too.
"""

from __future__ import annotations

import io
import json
import mimetypes
import posixpath
import re
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import agent, artifacts, computer, config, db, google, greeting, providers, security, tools

Handler = Callable[["Route"], Any]
_routes: list[tuple[str, re.Pattern[str], Handler, str]] = []


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Route:
    """Everything a handler needs: the request, the user, and the parsed body."""

    def __init__(self, request: "FloatHandler", params: dict[str, str]) -> None:
        self.request = request
        self.params = params
        self.user: dict[str, Any] | None = None
        self.token: str = ""

    @property
    def body(self) -> dict[str, Any]:
        return self.request.json_body()

    @property
    def query(self) -> dict[str, str]:
        return self.request.query

    @property
    def ip(self) -> str:
        return self.request.client_address[0]

    def require_user(self) -> dict[str, Any]:
        if self.user is None:
            raise HttpError(401, "Sign in to continue.")
        return self.user

    def require_admin(self) -> dict[str, Any]:
        user = self.require_user()
        if user["role"] != "admin":
            raise HttpError(403, "That area is for admins.")
        return user


def route(method: str, pattern: str, auth: str = "user") -> Callable[[Handler], Handler]:
    """auth: 'none' | 'user' | 'admin'."""
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorate(handler: Handler) -> Handler:
        _routes.append((method, regex, handler, auth))
        return handler

    return decorate


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@route("POST", "/api/auth/state", auth="none")
def auth_state(ctx: Route) -> dict[str, Any]:
    return {
        "setup_needed": db.count_users() == 0,
        "google": google.configured(),
        "self_signup": db.org_get("self_signup", "0") == "1",
        "school": db.org_get("school_name", config.settings.school_name),
        "app": {"name": config.APP_NAME, "version": config.VERSION, "tagline": config.APP_TAGLINE},
    }


@route("POST", "/api/auth/signup", auth="none")
def auth_signup(ctx: Route) -> dict[str, Any]:
    body = ctx.body
    email = str(body.get("email", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    password = str(body.get("password", ""))

    first_user = db.count_users() == 0
    if not first_user and db.org_get("self_signup", "0") != "1":
        raise HttpError(403, "Your school creates accounts from the admin panel. Ask your admin to add you.")
    if "@" not in email or len(email) < 6:
        raise HttpError(400, "That email address does not look right.")
    if db.get_user_by_email(email):
        raise HttpError(409, "There is already an account with that email. Sign in instead.")
    problems = security.password_problems(password)
    if problems:
        raise HttpError(400, " ".join(problems))

    user = db.create_user(email, name, password, role="admin" if first_user else "teacher",
                          school=str(body.get("school", "")).strip())
    if first_user and body.get("school"):
        db.org_set("school_name", str(body["school"]).strip())
    db.audit("auth.signup", user_id=int(user["id"]), actor=user["name"],
             detail="first account, made admin" if first_user else "self sign-up", ip=ctx.ip)
    token = db.create_session(int(user["id"]), ctx.ip, ctx.request.headers.get("User-Agent", ""))
    db.touch_user(int(user["id"]), count_sign_in=True)
    return {"token": token, "user": _public_user(user)}


@route("POST", "/api/auth/signin", auth="none")
def auth_signin(ctx: Route) -> dict[str, Any]:
    body = ctx.body
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    pin = str(body.get("pin", ""))

    allowed, wait = security.sign_in_limiter.check(f"{ctx.ip}:{email}")
    if not allowed:
        raise HttpError(429, f"Too many attempts. Try again in {int(wait)} seconds.")

    user = db.get_user_by_email(email)
    failed = user is None or not user["active"]
    if not failed:
        if pin and user["pin_hash"]:
            failed = not security.verify_password(pin, user["pin_hash"])
        elif password and user["password_hash"]:
            failed = not security.verify_password(password, user["password_hash"])
        else:
            failed = True

    if failed:
        db.audit("auth.fail", user_id=user["id"] if user else None, actor=email,
                 detail="wrong credentials" if user else "unknown account", ip=ctx.ip, level="warn")
        raise HttpError(401, "That email and password don't match. Check both, or ask your admin to reset it.")

    security.sign_in_limiter.reset(f"{ctx.ip}:{email}")
    token = db.create_session(int(user["id"]), ctx.ip, ctx.request.headers.get("User-Agent", ""))
    db.touch_user(int(user["id"]), count_sign_in=True)
    db.audit("auth.signin", user_id=int(user["id"]), actor=user["name"],
             detail="pin" if pin else "password", ip=ctx.ip)
    return {"token": token, "user": _public_user(user), "must_change": bool(user["must_change"])}


@route("POST", "/api/auth/signout")
def auth_signout(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.end_session(ctx.token)
    google.forget(int(user["id"]))
    db.audit("auth.signout", user_id=int(user["id"]), actor=user["name"], ip=ctx.ip)
    return {"ok": True}


@route("GET", "/api/auth/google/start", auth="none")
def google_start(ctx: Route) -> dict[str, Any]:
    if not google.configured():
        raise HttpError(400, "Google sign-in is not set up for this installation.")
    want_drive = ctx.query.get("drive") == "1"
    user_id = None
    if want_drive:
        user = ctx.require_user()
        user_id = int(user["id"])
    redirect = f"http://{config.settings.host}:{ctx.request.server.server_port}/auth/google/callback"
    return {"url": google.start(redirect, want_drive, user_id)}


@route("GET", "/auth/google/callback", auth="none")
def google_callback(ctx: Route) -> Any:
    error = ctx.query.get("error")
    if error:
        return _close_window(f"Google sign-in was cancelled ({error}).", ok=False)
    try:
        result = google.finish(ctx.query.get("state", ""), ctx.query.get("code", ""))
    except google.GoogleError as exc:
        return _close_window(str(exc), ok=False)

    # Connecting Drive to an account that is already signed in.
    if result.get("user_id"):
        google.remember(int(result["user_id"]), result)
        db.audit("google.drive", user_id=int(result["user_id"]), detail="Drive connected", ip=ctx.ip)
        return _close_window("Google Drive connected. You can close this tab.", ok=True)

    user = db.get_user_by_google(result["sub"]) or db.get_user_by_email(result["email"])
    if user is None:
        if db.count_users() == 0:
            user = db.create_user(result["email"], result["name"], role="admin",
                                  google_sub=result["sub"])
        elif db.org_get("self_signup", "0") == "1":
            user = db.create_user(result["email"], result["name"], google_sub=result["sub"])
        else:
            return _close_window(
                "There is no Float account for that Google address. Ask your admin to add you.",
                ok=False,
            )
    elif not user["google_sub"]:
        db.update_user(int(user["id"]), google_sub=result["sub"])

    if not user["active"]:
        return _close_window("That account has been switched off. Speak to your admin.", ok=False)

    google.remember(int(user["id"]), result)
    token = db.create_session(int(user["id"]), ctx.ip, ctx.request.headers.get("User-Agent", ""))
    db.touch_user(int(user["id"]), count_sign_in=True)
    db.audit("auth.signin", user_id=int(user["id"]), actor=user["name"], detail="google", ip=ctx.ip)
    return _close_window("Signed in. You can close this tab.", ok=True, token=token)


def _close_window(message: str, ok: bool, token: str = "") -> tuple[str, str]:
    payload = json.dumps({"ok": ok, "message": message, "token": token})
    page = f"""<!doctype html><meta charset="utf-8"><title>Float</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;background:#1E3A32;color:#FBF9F4;
display:grid;place-items:center;height:100vh;margin:0;text-align:center;padding:2rem}}
p{{max-width:28rem}} strong{{font-family:"Times New Roman",serif;font-size:1.6rem;display:block;margin-bottom:.5rem}}</style>
<p><strong>{'Done' if ok else 'Not signed in'}</strong>{message}</p>
<script>
  try {{ window.opener && window.opener.postMessage({payload}, "*"); }} catch (e) {{}}
  setTimeout(() => window.close(), {1200 if ok else 4000});
</script>"""
    return ("html", page)


# ---------------------------------------------------------------------------
# Me
# ---------------------------------------------------------------------------
def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "school": user["school"],
        "board": user["board"],
        "subjects": _as_list(user["subjects"]),
        "grades": _as_list(user["grades"]),
        "language": user["language"],
        "assist_level": user["assist_level"],
        "accent": user["accent"],
        "appearance": user["appearance"],
        "has_pin": bool(user["pin_hash"]),
        "sign_ins": user["sign_ins"],
    }


def _as_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


@route("GET", "/api/me")
def me(ctx: Route) -> dict[str, Any]:
    return {"user": _public_user(ctx.require_user())}


@route("POST", "/api/me")
def me_update(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    body = ctx.body
    updates: dict[str, Any] = {}
    for key in ("name", "school", "board", "language", "accent", "appearance"):
        if key in body:
            updates[key] = str(body[key])[:120]
    if "assist_level" in body and body["assist_level"] in config.ASSIST_LEVELS:
        updates["assist_level"] = body["assist_level"]
    for key in ("subjects", "grades"):
        if key in body and isinstance(body[key], list):
            updates[key] = json.dumps([str(v)[:60] for v in body[key]][:30])
    db.update_user(int(user["id"]), **updates)
    return {"user": _public_user(db.get_user(int(user["id"])) or user)}


@route("POST", "/api/me/password")
def me_password(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    body = ctx.body
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if user["password_hash"] and not security.verify_password(current, user["password_hash"]):
        raise HttpError(403, "That current password is not right.")
    problems = security.password_problems(new)
    if problems:
        raise HttpError(400, " ".join(problems))
    db.set_password(int(user["id"]), new)
    db.audit("auth.password", user_id=int(user["id"]), actor=user["name"], ip=ctx.ip)
    return {"ok": True}


@route("POST", "/api/me/pin")
def me_pin(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    body = ctx.body
    pin = str(body.get("pin", ""))
    if pin:
        if not security.verify_password(str(body.get("password", "")), user["password_hash"] or ""):
            raise HttpError(403, "Enter your password to set a quick PIN.")
        problems = security.pin_problems(pin)
        if problems:
            raise HttpError(400, " ".join(problems))
    db.set_pin(int(user["id"]), pin)
    db.audit("auth.pin", user_id=int(user["id"]), actor=user["name"],
             detail="set" if pin else "removed", ip=ctx.ip)
    return {"ok": True, "has_pin": bool(pin)}


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
@route("GET", "/api/bootstrap")
def bootstrap(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    user_id = int(user["id"])
    can_drive = google.has_drive(user_id)
    control_ok, control_why = computer.available()

    models: list[dict[str, Any]] = []
    for provider_id in db.allowed_providers():
        spec = config.PROVIDERS[provider_id]
        has_key = bool(db.get_key(provider_id, user_id))
        for model in spec.models:
            models.append({
                "id": f"{provider_id}:{model.id}",
                "provider": provider_id,
                "provider_label": spec.label,
                "label": model.label,
                "ready": has_key,
            })
    if config.rt_enabled():
        from . import _runtime_ext

        for name in _runtime_ext.models() or [config.RT_MODEL]:
            models.append({"id": f"local:{name}", "provider": "local",
                           "provider_label": "On this machine", "label": name, "ready": True})

    return {
        "user": _public_user(user),
        "greeting": greeting.build(user),
        "reflection": greeting.week_reflection(user_id),
        "conversations": db.list_conversations(user_id),
        "toolkit": tools.TOOLKIT,
        "toolkit_groups": tools.TOOLKIT_GROUPS,
        "classes": db.list_classes(user_id),
        "tasks": db.list_tasks(user_id),
        "artifacts": db.list_artifacts(user_id, 24),
        "notices": db.active_notices(),
        "models": models,
        "keys": db.key_status(user_id),
        "providers": [
            {"id": p, "label": config.PROVIDERS[p].label, "help": config.PROVIDERS[p].key_help,
             "console": config.PROVIDERS[p].console_url}
            for p in db.allowed_providers()
        ],
        "options": {
            "boards": list(config.BOARDS),
            "subjects": list(config.SUBJECTS),
            "grades": list(config.GRADES),
            "languages": config.LANGUAGES,
            "assist": config.ASSIST_LEVELS,
        },
        "capabilities": {
            "drive": can_drive,
            "google": google.configured(),
            "computer": control_ok,
            "computer_why": control_why,
            "screen": computer.screenshot_size(),
            "teacher_own_keys": db.org_get("teacher_own_keys", "1") == "1",
        },
        "usage": db.usage_for_user(user_id),
        "own_work": db.own_work_summary(user_id),
    }


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@route("POST", "/api/chat")
def chat(ctx: Route) -> Any:
    user = ctx.require_user()
    body = ctx.body
    allowed, wait = security.chat_limiter.check(str(user["id"]))
    if not allowed:
        raise HttpError(429, f"Slow down a moment — try again in {int(wait)}s.")

    message = str(body.get("message", ""))
    conv_id = body.get("conv_id")
    if not conv_id:
        conv_id = db.new_conversation(int(user["id"]), agent.title_for(message),
                                      str(body.get("tool", "chat")))
    conv_id = int(conv_id)

    stream = agent.run(
        user,
        conv_id,
        message,
        model_choice=str(body.get("model", "")),
        allow_computer=bool(body.get("allow_computer", True)),
        attachments=body.get("attachments") or [],
    )
    return ("sse", stream)


@route("GET", "/api/conversations")
def conversations(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    return {"conversations": db.list_conversations(int(user["id"]))}


@route("GET", "/api/conversations/{conv_id}")
def conversation_detail(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    conv_id = int(ctx.params["conv_id"])
    conversation = db.get_conversation(conv_id, int(user["id"]))
    if conversation is None:
        raise HttpError(404, "No such conversation.")
    messages = [
        {"role": m["role"], "content": m["content"], "created_at": m["created_at"],
         "meta": {k: v for k, v in m["meta"].items() if k != "blocks"}}
        for m in db.get_messages(conv_id)
        if m["role"] in {"user", "assistant"} and m["content"].strip()
    ]
    return {"conversation": conversation, "messages": messages,
            "artifacts": [a for a in db.list_artifacts(int(user["id"]), 200)
                          if a["conv_id"] == conv_id]}


@route("POST", "/api/conversations/{conv_id}")
def conversation_update(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    conv_id = int(ctx.params["conv_id"])
    body = ctx.body
    if "title" in body:
        db.rename_conversation(conv_id, int(user["id"]), str(body["title"]))
    for flag in ("pinned", "archived"):
        if flag in body:
            db.set_conversation_flag(conv_id, int(user["id"]), flag, 1 if body[flag] else 0)
    return {"ok": True}


@route("DELETE", "/api/conversations/{conv_id}")
def conversation_delete(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.delete_conversation(int(ctx.params["conv_id"]), int(user["id"]))
    return {"ok": True}


@route("POST", "/api/toolkit/{entry_id}")
def toolkit_run(ctx: Route) -> dict[str, Any]:
    """Turn a filled-in toolkit form into a prompt and a fresh conversation."""
    user = ctx.require_user()
    entry = tools.toolkit_entry(ctx.params["entry_id"])
    if entry is None:
        raise HttpError(404, "No such tool.")
    prompt = tools.fill_prompt(entry, ctx.body.get("values") or {})
    title = f"{entry['name']}" + (
        f" — {ctx.body.get('values', {}).get('topic') or ctx.body.get('values', {}).get('klass', '')}"
        if ctx.body.get("values") else ""
    )
    conv_id = db.new_conversation(int(user["id"]), title.strip(" —")[:120], entry["id"])
    return {"conv_id": conv_id, "prompt": prompt, "title": title}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@route("GET", "/api/artifacts")
def artifacts_list(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    return {"artifacts": db.list_artifacts(int(user["id"]))}


@route("GET", "/api/artifacts/{artifact_id}/download")
def artifact_download(ctx: Route) -> Any:
    user = ctx.require_user()
    artifact = db.get_artifact(int(ctx.params["artifact_id"]), int(user["id"]))
    if artifact is None:
        raise HttpError(404, "No such file.")
    data = artifacts.artifact_bytes(artifact)
    mime = artifacts.KINDS.get(artifact["kind"], ("", "application/octet-stream"))[1]
    return ("file", (artifact["name"], mime, data))


@route("POST", "/api/artifacts/{artifact_id}/save")
def artifact_save(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    artifact = db.get_artifact(int(ctx.params["artifact_id"]), int(user["id"]))
    if artifact is None:
        raise HttpError(404, "No such file.")
    where = str(ctx.body.get("where", "desktop"))

    if where == "desktop":
        try:
            path = artifacts.save_to_desktop(artifact)
        except OSError as exc:
            raise HttpError(500, f"Could not write to the Desktop: {exc}") from None
        db.mark_artifact_saved(int(artifact["id"]), str(path))
        db.audit("file.save", user_id=int(user["id"]), actor=user["name"], detail=f"desktop: {path}")
        artifacts.reveal(path)
        return {"ok": True, "where": "desktop", "path": str(path),
                "message": f"Saved to {path.parent.name}/{path.name}."}

    if where == "drive":
        if not google.has_drive(int(user["id"])):
            raise HttpError(400, "Connect Google Drive first — Settings → Google.")
        mime = artifacts.KINDS.get(artifact["kind"], ("", "application/octet-stream"))[1]
        try:
            result = google.upload(int(user["id"]), artifact["name"],
                                   artifacts.artifact_bytes(artifact), mime)
        except google.GoogleError as exc:
            raise HttpError(502, str(exc)) from None
        link = result.get("webViewLink", "")
        db.mark_artifact_saved(int(artifact["id"]), link or "drive")
        db.audit("file.save", user_id=int(user["id"]), actor=user["name"], detail="drive")
        return {"ok": True, "where": "drive", "link": link,
                "message": "Saved to your Drive, in the Float folder."}

    raise HttpError(400, "Save where? 'desktop' or 'drive'.")


# ---------------------------------------------------------------------------
# Computer control
# ---------------------------------------------------------------------------
@route("GET", "/api/computer/status")
def computer_status(ctx: Route) -> dict[str, Any]:
    ctx.require_user()
    ok, why = computer.available()
    return {"available": ok, "why": why, **computer.current_status()}


@route("POST", "/api/computer/stop")
def computer_stop(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    stopped = computer.stop_current()
    if stopped:
        db.audit("computer.stop", user_id=int(user["id"]), actor=user["name"], level="warn")
    return {"stopped": stopped}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
@route("POST", "/api/keys")
def keys_put(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    body = ctx.body
    provider = str(body.get("provider", ""))
    secret = str(body.get("key", "")).strip()
    scope = str(body.get("scope", "user"))

    if provider not in config.PROVIDERS:
        raise HttpError(400, "Unknown provider.")
    if scope == "org" and user["role"] != "admin":
        raise HttpError(403, "Only an admin can set the school's key.")
    if scope == "user" and db.org_get("teacher_own_keys", "1") != "1":
        raise HttpError(403, "Your school has teachers use the school key. Ask your admin.")
    if not secret:
        raise HttpError(400, "Paste the key.")

    expected = config.PROVIDERS[provider].key_prefix
    if expected and not secret.startswith(expected):
        raise HttpError(400, f"That does not look like a {config.PROVIDERS[provider].label} key — "
                             f"they start with {expected}.")

    ok, detail = providers.verify_key(provider, secret)
    if not ok:
        raise HttpError(400, detail)

    db.put_key(provider, secret, scope, None if scope == "org" else int(user["id"]),
               added_by=int(user["id"]))
    db.audit("key.save", user_id=int(user["id"]), actor=user["name"],
             detail=f"{provider} ({scope})", level="warn")
    return {"ok": True, "detail": detail, "keys": db.key_status(int(user["id"]))}


@route("DELETE", "/api/keys/{provider}")
def keys_drop(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    provider = ctx.params["provider"]
    scope = ctx.query.get("scope", "user")
    if scope == "org" and user["role"] != "admin":
        raise HttpError(403, "Only an admin can remove the school's key.")
    db.drop_key(provider, scope, int(user["id"]))
    db.audit("key.remove", user_id=int(user["id"]), actor=user["name"],
             detail=f"{provider} ({scope})", level="warn")
    return {"ok": True, "keys": db.key_status(int(user["id"]))}


@route("POST", "/api/unlock")
def unlock(ctx: Route) -> dict[str, Any]:
    """The local transport. Not advertised; the passphrase is the whole gate."""
    user = ctx.require_user()
    phrase = str(ctx.body.get("phrase", ""))
    allowed, wait = security.sign_in_limiter.check(f"unlock:{ctx.ip}")
    if not allowed:
        raise HttpError(429, f"Try again in {int(wait)}s.")
    if not config.rt_phrase_matches(phrase):
        raise HttpError(403, "Not recognised.")
    enable = bool(ctx.body.get("enable", True))
    config.rt_set(enable)
    db.audit("runtime.toggle", user_id=int(user["id"]), actor=user["name"],
             detail="on" if enable else "off", level="warn")
    return {"ok": True, "enabled": config.rt_enabled()}


# ---------------------------------------------------------------------------
# Classes, students, marks, attendance, tasks, timetable
# ---------------------------------------------------------------------------
@route("GET", "/api/classes")
def classes_list(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    return {"classes": db.list_classes(int(user["id"]))}


@route("POST", "/api/classes")
def classes_add(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    body = ctx.body
    name = str(body.get("name", "")).strip()
    if not name:
        raise HttpError(400, "Give the class a name, like '8B'.")
    class_id = db.add_class(int(user["id"]), name, str(body.get("grade", "")),
                            str(body.get("section", "")), str(body.get("subject", "")),
                            str(body.get("board", user["board"])))
    roster = body.get("roster", body.get("students"))
    added = 0
    if isinstance(roster, str) and roster.strip():
        added = db.add_students(class_id, _parse_roster(roster))
    elif isinstance(roster, list):
        added = db.add_students(class_id, roster)
    return {"ok": True, "id": class_id, "students": added,
            "classes": db.list_classes(int(user["id"]))}


def _parse_roster(text: str) -> list[dict[str, str]]:
    """Accept '1, Aarav Sharma' or 'Aarav Sharma' or '1\tAarav', one per line."""
    entries: list[dict[str, str]] = []
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,\t]|\s{2,}", line, maxsplit=1)]
        if len(parts) == 2 and re.fullmatch(r"\d{1,4}", parts[0]):
            entries.append({"roll": parts[0], "name": parts[1]})
        else:
            match = re.match(r"^(\d{1,4})[.)]\s+(.*)$", line)
            if match:
                entries.append({"roll": match.group(1), "name": match.group(2)})
            else:
                entries.append({"roll": str(len(entries) + 1), "name": line})
    return entries


@route("DELETE", "/api/classes/{class_id}")
def classes_delete(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.delete_class(int(ctx.params["class_id"]), int(user["id"]))
    return {"ok": True, "classes": db.list_classes(int(user["id"]))}


@route("GET", "/api/classes/{class_id}/students")
def students_list(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    class_id = int(ctx.params["class_id"])
    if not db.owns_class(class_id, int(user["id"])):
        raise HttpError(404, "No such class.")
    return {"students": db.list_students(class_id),
            "marks": db.class_marks(class_id),
            "attendance": db.attendance_range(class_id)}


@route("POST", "/api/classes/{class_id}/students")
def students_add(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    class_id = int(ctx.params["class_id"])
    if not db.owns_class(class_id, int(user["id"])):
        raise HttpError(404, "No such class.")
    roster = ctx.body.get("roster", ctx.body.get("students"))
    entries = _parse_roster(roster) if isinstance(roster, str) else (roster or [])
    added = db.add_students(class_id, entries)
    return {"ok": True, "added": added, "students": db.list_students(class_id)}


@route("POST", "/api/classes/{class_id}/marks")
def marks_add(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    class_id = int(ctx.params["class_id"])
    if not db.owns_class(class_id, int(user["id"])):
        raise HttpError(404, "No such class.")
    count = db.record_marks(ctx.body.get("marks") or [])
    return {"ok": True, "saved": count}


@route("POST", "/api/classes/{class_id}/attendance")
def attendance_save(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    class_id = int(ctx.params["class_id"])
    if not db.owns_class(class_id, int(user["id"])):
        raise HttpError(404, "No such class.")
    body = ctx.body
    db.save_attendance(class_id, str(body.get("date", "")),
                       [int(i) for i in body.get("present") or []],
                       [int(i) for i in body.get("absent") or []])
    return {"ok": True}


@route("GET", "/api/tasks")
def tasks_list(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    return {"tasks": db.list_tasks(int(user["id"]), ctx.query.get("all") == "1")}


@route("POST", "/api/tasks")
def tasks_add(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    title = str(ctx.body.get("title", "")).strip()
    if not title:
        raise HttpError(400, "A task needs a title.")
    db.add_task(int(user["id"]), title, str(ctx.body.get("due", "")))
    return {"ok": True, "tasks": db.list_tasks(int(user["id"]))}


@route("POST", "/api/tasks/{task_id}")
def tasks_update(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.set_task_done(int(ctx.params["task_id"]), int(user["id"]), bool(ctx.body.get("done")))
    return {"ok": True, "tasks": db.list_tasks(int(user["id"]))}


@route("DELETE", "/api/tasks/{task_id}")
def tasks_delete(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.delete_task(int(ctx.params["task_id"]), int(user["id"]))
    return {"ok": True, "tasks": db.list_tasks(int(user["id"]))}


@route("GET", "/api/timetable")
def timetable_get(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    return {"periods": db.list_periods(int(user["id"]))}


@route("POST", "/api/timetable")
def timetable_set(ctx: Route) -> dict[str, Any]:
    user = ctx.require_user()
    db.set_periods(int(user["id"]), ctx.body.get("periods") or [])
    return {"ok": True, "periods": db.list_periods(int(user["id"])),
            "greeting": greeting.build(db.get_user(int(user["id"])) or user)}


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@route("GET", "/api/admin/overview", auth="admin")
def admin_overview(ctx: Route) -> dict[str, Any]:
    ctx.require_admin()
    rows = db.usage_summary(30)
    total_paise = sum(r["paise"] for r in rows)
    return {
        "org": db.org_all(),
        "users": rows,
        "sessions": db.active_sessions(),
        "by_day": db.usage_by_day(14),
        "totals": {
            "teachers": len(rows),
            "active": len([r for r in rows if r["active"]]),
            "signed_in": len(db.active_sessions()),
            "calls": sum(r["calls"] for r in rows),
            "tokens": sum(r["tin"] + r["tout"] for r in rows),
            "rupees": round(total_paise / 100, 2),
        },
        "providers": [
            {"id": p, "label": config.PROVIDERS[p].label,
             "key": bool(db.get_key(p)), "allowed": p in db.allowed_providers()}
            for p in config.PROVIDER_ORDER
        ],
        "notices": db.active_notices(),
    }


@route("POST", "/api/admin/users", auth="admin")
def admin_add_user(ctx: Route) -> dict[str, Any]:
    admin = ctx.require_admin()
    body = ctx.body
    email = str(body.get("email", "")).strip().lower()
    name = str(body.get("name", "")).strip()
    password = str(body.get("password", "")) or security.new_token(9)
    if "@" not in email:
        raise HttpError(400, "That email address does not look right.")
    if db.get_user_by_email(email):
        raise HttpError(409, "There is already an account with that email.")
    user = db.create_user(email, name, password, role=str(body.get("role", "teacher")),
                          must_change=True, school=db.org_get("school_name"))
    db.audit("admin.user.add", user_id=int(admin["id"]), actor=admin["name"],
             detail=f"added {email}", level="warn", ip=ctx.ip)
    return {"ok": True, "temp_password": password, "users": db.usage_summary(30)}


@route("POST", "/api/admin/users/{user_id}", auth="admin")
def admin_update_user(ctx: Route) -> dict[str, Any]:
    admin = ctx.require_admin()
    user_id = int(ctx.params["user_id"])
    target = db.get_user(user_id)
    if target is None:
        raise HttpError(404, "No such teacher.")
    body = ctx.body
    action = str(body.get("action", ""))

    if action == "deactivate":
        if user_id == int(admin["id"]):
            raise HttpError(400, "You cannot switch off your own account.")
        db.update_user(user_id, active=0)
        db.end_all_sessions(user_id)
        detail = "deactivated"
    elif action == "activate":
        db.update_user(user_id, active=1)
        detail = "activated"
    elif action == "make_admin":
        db.update_user(user_id, role="admin")
        detail = "made admin"
    elif action == "make_teacher":
        if _admin_count() <= 1 and target["role"] == "admin":
            raise HttpError(400, "There has to be at least one admin.")
        db.update_user(user_id, role="teacher")
        detail = "made teacher"
    elif action == "reset_password":
        temporary = security.new_token(9)
        db.set_password(user_id, temporary)
        db.update_user(user_id, must_change=1)
        db.set_pin(user_id, "")
        db.end_all_sessions(user_id)
        db.audit("admin.user.reset", user_id=int(admin["id"]), actor=admin["name"],
                 detail=f"reset {target['email']}", level="warn", ip=ctx.ip)
        return {"ok": True, "temp_password": temporary, "users": db.usage_summary(30)}
    elif action == "sign_out":
        ended = db.end_all_sessions(user_id)
        detail = f"signed out of {ended} session(s)"
    elif action == "delete":
        if user_id == int(admin["id"]):
            raise HttpError(400, "You cannot delete your own account.")
        if target["role"] == "admin" and _admin_count() <= 1:
            raise HttpError(400, "There has to be at least one admin.")
        db.delete_user(user_id)
        detail = "deleted, with all their data"
    else:
        raise HttpError(400, "Unknown action.")

    db.audit("admin.user", user_id=int(admin["id"]), actor=admin["name"],
             detail=f"{target['email']}: {detail}", level="warn", ip=ctx.ip)
    return {"ok": True, "detail": detail, "users": db.usage_summary(30)}


def _admin_count() -> int:
    return len([u for u in db.list_users() if u["role"] == "admin" and u["active"]])


@route("POST", "/api/admin/org", auth="admin")
def admin_org(ctx: Route) -> dict[str, Any]:
    admin = ctx.require_admin()
    body = ctx.body
    changed: list[str] = []
    for key in ("school_name", "computer_control", "teacher_own_keys", "self_signup",
                "monthly_paise_budget", "require_pin"):
        if key in body:
            db.org_set(key, str(body[key]))
            changed.append(key)
    if "allowed_providers" in body and isinstance(body["allowed_providers"], list):
        db.org_set("allowed_providers", json.dumps(
            [p for p in body["allowed_providers"] if p in config.PROVIDERS]))
        changed.append("allowed_providers")
    if "computer_control" in body:
        config.settings.computer_control = str(body["computer_control"]) == "1"
    db.audit("admin.org", user_id=int(admin["id"]), actor=admin["name"],
             detail=", ".join(changed), level="warn", ip=ctx.ip)
    return {"ok": True, "org": db.org_all()}


@route("GET", "/api/admin/audit", auth="admin")
def admin_audit(ctx: Route) -> dict[str, Any]:
    ctx.require_admin()
    return {"entries": db.audit_tail(
        limit=min(500, int(ctx.query.get("limit", "200"))),
        level=ctx.query.get("level", ""),
        user_id=int(ctx.query["user_id"]) if ctx.query.get("user_id") else None,
    )}


@route("POST", "/api/admin/notice", auth="admin")
def admin_notice(ctx: Route) -> dict[str, Any]:
    admin = ctx.require_admin()
    body = ctx.body
    if body.get("retire"):
        db.retire_notice(int(body["retire"]))
    else:
        title = str(body.get("title", "")).strip()
        if not title:
            raise HttpError(400, "A notice needs a title.")
        db.add_notice(title, str(body.get("body", "")), str(body.get("level", "info")),
                      int(admin["id"]))
    db.audit("admin.notice", user_id=int(admin["id"]), actor=admin["name"], ip=ctx.ip)
    return {"ok": True, "notices": db.active_notices()}


@route("GET", "/api/admin/export", auth="admin")
def admin_export(ctx: Route) -> Any:
    ctx.require_admin()
    rows: list[list[Any]] = [
        ["Teacher", "Email", "Role", "Active", "Sign-ins", "Requests",
         "Tokens in", "Tokens out", "Estimated ₹", "Avg latency (ms)"]
    ]
    for entry in db.usage_summary(30):
        rows.append([
            entry["name"], entry["email"], entry["role"], "Yes" if entry["active"] else "No",
            entry["sign_ins"], entry["calls"], entry["tin"], entry["tout"],
            round(entry["paise"] / 100, 2), round(entry["latency"]),
        ])
    return ("file", ("float-usage.xlsx", artifacts.KINDS["xlsx"][1], artifacts.build_xlsx(rows)))


# ---------------------------------------------------------------------------
# The HTTP layer
# ---------------------------------------------------------------------------
class FloatHandler(BaseHTTPRequestHandler):
    server_version = f"Float/{config.VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # the audit log is the log

    def json_body(self) -> dict[str, Any]:
        if getattr(self, "_body_cache", None) is not None:
            return self._body_cache
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._body_cache = {}
        elif length > config.settings.max_upload_bytes:
            raise HttpError(413, "That is larger than Float accepts.")
        else:
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                self._body_cache = parsed if isinstance(parsed, dict) else {"value": parsed}
            except ValueError:
                raise HttpError(400, "The request body was not valid JSON.") from None
        return self._body_cache

    @property
    def query(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in {"127.0.0.1", "localhost", "[::1]", "::1", config.settings.host}

    # -- verbs -------------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        self._body_cache = None
        try:
            if not self._host_ok():
                raise HttpError(403, "Refused: unexpected Host header.")

            path = urllib.parse.urlparse(self.path).path
            for verb, pattern, handler, auth in _routes:
                if verb != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                ctx = Route(self, match.groupdict())
                ctx.token = self._bearer()
                if ctx.token:
                    ctx.user = db.resolve_session(ctx.token)
                if auth != "none" and ctx.user is None:
                    raise HttpError(401, "Sign in to continue.")
                if auth == "admin":
                    ctx.require_admin()
                self._send_result(handler(ctx))
                return

            if method == "GET":
                self._serve_static(path)
                return
            raise HttpError(404, "No such endpoint.")

        except HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            self._send_json({"error": f"Float hit an unexpected problem: {exc}"}, 500)

    def _bearer(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        return self.query.get("token", "")

    # -- responses ---------------------------------------------------------
    def _send_result(self, result: Any) -> None:
        if isinstance(result, tuple) and len(result) == 2:
            kind, payload = result
            if kind == "sse":
                self._send_sse(payload)
                return
            if kind == "file":
                name, mime, data = payload
                self._send_bytes(data, mime, filename=name)
                return
            if kind == "html":
                self._send_bytes(payload.encode("utf-8"), "text/html; charset=utf-8")
                return
        self._send_json(result if result is not None else {"ok": True})

    def _headers(self, status: int, mime: str, length: int | None, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, mime: str, filename: str = "") -> None:
        extra = {}
        if filename:
            quoted = urllib.parse.quote(filename)
            extra["Content-Disposition"] = f'attachment; filename="{quoted}"; filename*=UTF-8\'\'{quoted}'
        self._headers(200, mime, len(data), extra)
        self.wfile.write(data)

    def _send_sse(self, stream: Any) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for event in stream:
                frame = f"data: {json.dumps(event, default=str)}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass   # the teacher closed the tab mid-answer; nothing to clean up
        except Exception as exc:  # pragma: no cover
            try:
                self.wfile.write(
                    f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n".encode()
                )
            except Exception:
                pass

    # -- static ------------------------------------------------------------
    _PAGES = {"/": "index.html", "/login": "login.html", "/admin": "admin.html"}

    def _serve_static(self, path: str) -> None:
        if path in self._PAGES:
            target = Path(config.WEB_ROOT) / self._PAGES[path]
        else:
            clean = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
            target = (Path(config.WEB_ROOT) / clean).resolve()
            if not str(target).startswith(str(Path(config.WEB_ROOT).resolve())):
                raise HttpError(403, "Refused.")
        if not target.is_file():
            raise HttpError(404, "Not found.")

        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        data = target.read_bytes()
        extra = {
            "Content-Security-Policy":
                "default-src 'self'; img-src 'self' data: blob: https:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        } if target.suffix == ".html" else {}
        self._headers(200, mime, len(data), extra)
        self.wfile.write(data)


class FloatServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str | None = None, port: int | None = None) -> FloatServer:
    config.ensure_dirs()
    db.init()
    db.purge_stale_sessions()
    server = FloatServer((host or config.settings.host, port or config.settings.port), FloatHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="float-http").start()
    return server
