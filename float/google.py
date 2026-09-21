"""Float — Google sign-in and Google Drive.

Both are optional. Float works completely without either: a school with no Google
Workspace gets local accounts and the Desktop, and never sees a Google button.

**Sign-in** is OAuth 2.0 with PKCE on a loopback redirect, which is the flow
Google documents for installed applications. Float exchanges the authorisation
code itself, directly with Google's token endpoint over TLS, so the ID token
arrives over an authenticated channel; its claims — audience, issuer, expiry,
verified address — are still checked here. That is what keeps this inside the
standard library rather than dragging in a JWT stack for one signature.

**Drive** reuses the same consent, asking additionally for ``drive.file``, which
is the narrow scope: Float can only see and touch files Float created. It cannot
read the teacher's existing Drive, and saying so plainly in the consent screen is
the point of choosing that scope.

Tokens live in memory only, keyed by user id, and die with the process along with
everything else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
TIMEOUT = 25.0

SCOPE_IDENTITY = "openid email profile"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive.file"


class GoogleError(Exception):
    pass


@dataclass
class Flow:
    verifier: str
    state: str
    scope: str
    redirect: str
    user_id: int | None = None
    created: float = field(default_factory=time.time)


_flows: dict[str, Flow] = {}
_tokens: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()


def configured() -> bool:
    return bool(config.settings.google_client_id)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def start(redirect: str, want_drive: bool = False, user_id: int | None = None) -> str:
    """Create a flow and return the URL to send the browser to."""
    if not configured():
        raise GoogleError("Google sign-in is not set up for this installation.")

    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    scope = SCOPE_IDENTITY + (" " + SCOPE_DRIVE if want_drive else "")

    with _lock:
        _flows[state] = Flow(verifier=verifier, state=state, scope=scope,
                             redirect=redirect, user_id=user_id)
        for key in [k for k, v in _flows.items() if time.time() - v.created > 600]:
            _flows.pop(key, None)

    params = {
        "client_id": config.settings.google_client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent" if want_drive else "select_account",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise GoogleError(f"Google refused the request: {body[:200]}") from None
    except urllib.error.URLError as exc:
        raise GoogleError(f"Could not reach Google: {exc.reason}") from None


def _decode_id_token(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception as exc:
        raise GoogleError("Google's identity token could not be read.") from exc


def finish(state: str, code: str) -> dict[str, Any]:
    """Exchange the code. Returns the verified claims plus the tokens."""
    with _lock:
        flow = _flows.pop(state, None)
    if flow is None:
        raise GoogleError("That sign-in attempt expired. Try again.")

    fields = {
        "client_id": config.settings.google_client_id,
        "code": code,
        "code_verifier": flow.verifier,
        "grant_type": "authorization_code",
        "redirect_uri": flow.redirect,
    }
    if config.settings.google_client_secret:
        fields["client_secret"] = config.settings.google_client_secret

    payload = _post_form(TOKEN_URL, fields)
    id_token = payload.get("id_token", "")
    if not id_token:
        raise GoogleError("Google did not return an identity token.")

    claims = _decode_id_token(id_token)
    if claims.get("aud") != config.settings.google_client_id:
        raise GoogleError("That token was issued for a different application.")
    if claims.get("iss") not in ISSUERS:
        raise GoogleError("That token did not come from Google.")
    if float(claims.get("exp", 0)) < time.time():
        raise GoogleError("That token has already expired.")
    if not claims.get("email_verified"):
        raise GoogleError("That Google account has no verified email address.")

    return {
        "sub": claims.get("sub", ""),
        "email": (claims.get("email") or "").lower(),
        "name": claims.get("name") or claims.get("email", "").split("@")[0].title(),
        "picture": claims.get("picture", ""),
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": time.time() + float(payload.get("expires_in", 3000)),
        "scope": payload.get("scope", ""),
        "user_id": flow.user_id,
    }


def remember(user_id: int, tokens: dict[str, Any]) -> None:
    with _lock:
        _tokens[user_id] = tokens


def forget(user_id: int) -> None:
    with _lock:
        _tokens.pop(user_id, None)


def has_drive(user_id: int) -> bool:
    with _lock:
        entry = _tokens.get(user_id)
    return bool(entry and SCOPE_DRIVE in entry.get("scope", ""))


def _access_token(user_id: int) -> str:
    with _lock:
        entry = _tokens.get(user_id)
    if not entry:
        raise GoogleError("Not connected to Google Drive. Connect it in Settings → Google.")
    if entry["expires_at"] > time.time() + 60:
        return entry["access_token"]

    refresh = entry.get("refresh_token")
    if not refresh:
        raise GoogleError("The Drive connection expired. Connect it again in Settings → Google.")
    fields = {
        "client_id": config.settings.google_client_id,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }
    if config.settings.google_client_secret:
        fields["client_secret"] = config.settings.google_client_secret
    payload = _post_form(TOKEN_URL, fields)
    entry["access_token"] = payload.get("access_token", "")
    entry["expires_at"] = time.time() + float(payload.get("expires_in", 3000))
    with _lock:
        _tokens[user_id] = entry
    return entry["access_token"]


def _folder_id(user_id: int, name: str = "Float") -> str | None:
    """Find or create the Float folder. Only touches folders Float made."""
    token = _access_token(user_id)
    query = urllib.parse.quote(
        f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    request = urllib.request.Request(
        f"{FILES_URL}?q={query}&fields=files(id,name)&spaces=drive",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            found = json.loads(response.read().decode("utf-8")).get("files", [])
        if found:
            return found[0]["id"]
    except urllib.error.URLError:
        return None

    body = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder"}).encode()
    create = urllib.request.Request(
        FILES_URL + "?fields=id",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(create, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8")).get("id")
    except urllib.error.URLError:
        return None


def upload(user_id: int, filename: str, data: bytes, mime: str) -> dict[str, Any]:
    """Multipart upload into the Float folder. Returns name and a link to open it."""
    token = _access_token(user_id)
    metadata: dict[str, Any] = {"name": filename}
    folder = _folder_id(user_id)
    if folder:
        metadata["parents"] = [folder]

    boundary = "float" + secrets.token_hex(12)
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        json.dumps(metadata).encode("utf-8"),
        f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ])

    request = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GoogleError(
            f"Drive refused the upload: {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from None
    except urllib.error.URLError as exc:
        raise GoogleError(f"Could not reach Drive: {exc.reason}") from None
