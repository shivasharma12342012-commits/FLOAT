"""Float — credentials, sessions and at-rest encryption.

Four things live here.

**Passwords.** Salted scrypt. The password itself is never written anywhere, not
to the database, not to a log, not to the audit trail. ``verify`` is constant
time.

**Sessions that end when the app does.** Every session token is minted against
``BOOT_NONCE``, a random value generated when the process starts. Close Float and
the nonce is gone, so every token issued under it stops validating — which is
exactly the behaviour asked for: shutting the app signs the teacher out, on that
machine and on any tab they left open. Idle timeout and an absolute ceiling apply
on top of that while it is running.

**API keys at rest.** A school's OpenAI or Anthropic key is a spending
instrument, so it is encrypted before it touches SQLite, under a key derived from
a random device secret stored in ``data/.device_key`` with 0600 permissions.
Copying the database to another machine therefore yields nothing. AES-256-GCM is
used when ``cryptography`` is installed; without it the fallback is
encrypt-then-MAC over an HMAC-SHA256 counter keystream, which is a standard
construction and holds up on its own. Both are versioned in the ciphertext so an
install can move between them.

**Rate limiting.** A small in-memory sliding window, keyed by whatever the caller
wants to limit on — an email, an IP, a user id.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

# scrypt cost. 2**15 is about 100 ms on a laptop: unnoticeable once per sign-in,
# ruinous a billion times over.
_N, _R, _P, _DKLEN = 2**15, 8, 1, 32
_MAXMEM = 128 * _N * _R * 2

#: Regenerated every start. This is what makes "close the app" mean "sign out".
BOOT_NONCE = secrets.token_hex(16)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${_N}${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n_raw, salt_hex, expected = encoded.split("$", 3)
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_raw),
            r=_R,
            p=_P,
            dklen=_DKLEN,
            maxmem=_MAXMEM,
        )
        return hmac.compare_digest(key.hex(), expected)
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    """Plain-language reasons a password is not good enough, or an empty list.

    Length does most of the work, so the bar is length. One character class rule
    is kept because school machines are shared and 'aaaaaaaaaaaa' is a real thing
    people type.
    """
    problems: list[str] = []
    if len(password) < 10:
        problems.append("Use at least 10 characters.")
    if password.lower() in {"password12", "password123", "teacher123", "12345678910"}:
        problems.append("That one is on every guessing list. Pick something else.")
    if len(set(password)) < 5:
        problems.append("Use at least 5 different characters.")
    return problems


def pin_problems(pin: str) -> list[str]:
    problems: list[str] = []
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        problems.append("A quick PIN is 4 to 8 digits.")
    elif pin in {"0000", "1234", "1111", "123456", "000000"} or len(set(pin)) == 1:
        problems.append("Too easy to guess. Pick a different PIN.")
    return problems


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_fingerprint(token: str) -> str:
    """What goes in the database. The token itself never does."""
    return hashlib.sha256(f"{BOOT_NONCE}:{token}".encode("utf-8")).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# ---------------------------------------------------------------------------
# Device key and at-rest encryption
# ---------------------------------------------------------------------------
def _device_key() -> bytes:
    path = Path(config.DEVICE_KEY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    raw = secrets.token_bytes(32)
    path.write_bytes(raw)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600; best effort on Windows
    except OSError:
        pass
    return raw


def _subkey(purpose: str) -> bytes:
    return hashlib.blake2b(_device_key(), key=purpose.encode("utf-8"), digest_size=32).digest()


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        return AESGCM
    except Exception:  # pragma: no cover - absence is the normal case
        return None


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 in counter mode. Deterministic, and never reuses a block."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str, purpose: str = "apikey") -> str:
    """Encrypt a secret for storage. Returns an opaque, versioned string."""
    if not plaintext:
        return ""
    key = _subkey(purpose)
    data = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(12)

    aesgcm = _aesgcm()
    if aesgcm is not None:
        blob = aesgcm(key).encrypt(nonce, data, purpose.encode("utf-8"))
        return "v2." + base64.urlsafe_b64encode(nonce + blob).decode("ascii")

    stream_key = hashlib.blake2b(key, key=b"stream", digest_size=32).digest()
    mac_key = hashlib.blake2b(key, key=b"mac", digest_size=32).digest()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(stream_key, nonce, len(data))))
    tag = hmac.new(mac_key, purpose.encode("utf-8") + nonce + ct, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(nonce + tag + ct).decode("ascii")


def decrypt_secret(blob: str, purpose: str = "apikey") -> str:
    """Reverse ``encrypt_secret``. Returns "" on any tampering or key mismatch."""
    if not blob:
        return ""
    try:
        version, _, body = blob.partition(".")
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
        key = _subkey(purpose)

        if version == "v2":
            aesgcm = _aesgcm()
            if aesgcm is None:
                return ""
            nonce, payload = raw[:12], raw[12:]
            return aesgcm(key).decrypt(nonce, payload, purpose.encode("utf-8")).decode("utf-8")

        if version == "v1":
            nonce, tag, ct = raw[:12], raw[12:44], raw[44:]
            mac_key = hashlib.blake2b(key, key=b"mac", digest_size=32).digest()
            expected = hmac.new(mac_key, purpose.encode("utf-8") + nonce + ct, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                return ""
            stream_key = hashlib.blake2b(key, key=b"stream", digest_size=32).digest()
            return bytes(
                a ^ b for a, b in zip(ct, _keystream(stream_key, nonce, len(ct)))
            ).decode("utf-8")
    except Exception:
        return ""
    return ""


def mask_key(key: str) -> str:
    """What the interface shows instead of the key."""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "…"
    return f"{key[:7]}…{key[-4:]}"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
@dataclass
class _Window:
    hits: list[float]
    blocked_until: float = 0.0


class RateLimiter:
    """Sliding window. Not distributed, because Float is not."""

    def __init__(self, limit: int, window: float, penalty: float = 0.0) -> None:
        self.limit = limit
        self.window = window
        self.penalty = penalty
        self._lock = threading.Lock()
        self._keys: dict[str, _Window] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds_to_wait)."""
        now = time.time()
        with self._lock:
            state = self._keys.get(key)
            if state is None:
                state = self._keys[key] = _Window(hits=[])
            if state.blocked_until > now:
                return False, state.blocked_until - now
            state.hits = [t for t in state.hits if now - t < self.window]
            if len(state.hits) >= self.limit:
                if self.penalty:
                    state.blocked_until = now + self.penalty
                    return False, self.penalty
                return False, self.window - (now - state.hits[0])
            state.hits.append(now)
            return True, 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._keys.pop(key, None)

    def sweep(self, older_than: float = 3600.0) -> None:
        now = time.time()
        with self._lock:
            for key in [
                k for k, v in self._keys.items()
                if v.blocked_until < now and (not v.hits or now - v.hits[-1] > older_than)
            ]:
                self._keys.pop(key, None)


sign_in_limiter = RateLimiter(
    limit=config.settings.max_attempts,
    window=300.0,
    penalty=config.settings.lockout_seconds,
)
chat_limiter = RateLimiter(limit=60, window=60.0)
