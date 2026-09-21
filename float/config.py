"""Float — configuration.

Everything tunable lives here. Values come from the environment, from a ``.env``
file beside the project root, and from the organisation settings row in SQLite
(which the admin panel writes). Precedence, highest first:

    admin panel  >  environment  >  .env  >  the defaults in this file

No third-party settings library: this reads plain text and casts it, so the app
installs from a three-line requirements file and starts in under a second on a
school laptop.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "Float"
APP_TAGLINE = "Your teaching, lighter."
VERSION = "1.0.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("FLOAT_DATA_DIR") or (PROJECT_ROOT / "data"))
WEB_ROOT = Path(__file__).resolve().parent / "web"

DB_PATH = DATA_DIR / "float.sqlite3"
FILES_DIR = DATA_DIR / "files"
LOG_PATH = DATA_DIR / "float.log"
DEVICE_KEY_PATH = DATA_DIR / ".device_key"


# ----------------------------------------------------------------------------
# .env loading. Six lines, no dependency.
# ----------------------------------------------------------------------------
def _load_dotenv() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# ----------------------------------------------------------------------------
# Model providers.
#
# Three are offered in the interface, each keyed by the school's own API key.
# A fourth transport exists for offline development and is not listed anywhere
# a teacher can see; see ``float/_runtime_ext.py``.
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    context: int
    #: Rupees per million tokens, in, out. Used only for the admin cost estimate.
    cost_in: float = 0.0
    cost_out: float = 0.0
    vision: bool = False


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    key_prefix: str
    key_help: str
    console_url: str
    models: tuple[ModelSpec, ...]
    default_model: str

    def model(self, model_id: str) -> ModelSpec | None:
        for spec in self.models:
            if spec.id == model_id:
                return spec
        return None


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        label="Anthropic",
        key_prefix="sk-ant-",
        key_help="Create a key at console.anthropic.com → API keys.",
        console_url="https://console.anthropic.com/settings/keys",
        default_model="claude-sonnet-4-5",
        models=(
            ModelSpec("claude-sonnet-4-5", "Claude Sonnet 4.5", 200_000, 250, 1250, True),
            ModelSpec("claude-opus-4-1", "Claude Opus 4.1", 200_000, 1250, 6250, True),
            ModelSpec("claude-haiku-4-5", "Claude Haiku 4.5", 200_000, 80, 400, True),
        ),
    ),
    "openai": Provider(
        id="openai",
        label="OpenAI",
        key_prefix="sk-",
        key_help="Create a key at platform.openai.com → API keys.",
        console_url="https://platform.openai.com/api-keys",
        default_model="gpt-4.1-mini",
        models=(
            ModelSpec("gpt-4.1", "GPT-4.1", 128_000, 165, 660, True),
            ModelSpec("gpt-4.1-mini", "GPT-4.1 mini", 128_000, 33, 132, True),
            ModelSpec("gpt-4o", "GPT-4o", 128_000, 210, 840, True),
            ModelSpec("gpt-4o-mini", "GPT-4o mini", 128_000, 12, 50, True),
        ),
    ),
    "google": Provider(
        id="google",
        label="Google",
        key_prefix="AIza",
        key_help="Create a key at aistudio.google.com → Get API key.",
        console_url="https://aistudio.google.com/app/apikey",
        default_model="gemini-2.0-flash",
        models=(
            ModelSpec("gemini-2.0-flash", "Gemini 2.0 Flash", 1_000_000, 8, 33, True),
            ModelSpec("gemini-2.5-pro", "Gemini 2.5 Pro", 1_000_000, 105, 840, True),
            ModelSpec("gemini-2.5-flash", "Gemini 2.5 Flash", 1_000_000, 25, 210, True),
        ),
    ),
}

#: Order the picker shows them in.
PROVIDER_ORDER = ("anthropic", "openai", "google")


# ----------------------------------------------------------------------------
# Local runtime gate.
#
# Off unless this machine says otherwise. Three ways to say so, all of them
# local to the box: an environment variable, a marker file, or the passphrase
# typed into the unlock field. The passphrase is compared as a hash so the
# phrase itself is not in the repository.
#
# Default passphrase: see PRIVATE_NOTES.md (delete that file before sharing).
# ----------------------------------------------------------------------------
_RT_MARKER = DATA_DIR / ".rt"
_RT_PHRASE_SHA256 = env(
    "FLOAT_RT_HASH",
    "5899cdb92a335b13a30161c9c3f9d614bcf289ba421473e97432f93742d615c9",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rt_phrase_matches(phrase: str) -> bool:
    """True when ``phrase`` is the local-runtime passphrase for this install."""
    return bool(phrase) and _sha(phrase.strip()) == _RT_PHRASE_SHA256


def rt_enabled() -> bool:
    """Whether the offline transport is available on this machine."""
    if env_bool("FLOAT_RT", False):
        return True
    return _RT_MARKER.exists()


def rt_set(enabled: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if enabled:
        _RT_MARKER.write_text("1", encoding="utf-8")
    elif _RT_MARKER.exists():
        _RT_MARKER.unlink()


RT_HOST = env("FLOAT_RT_HOST", "http://127.0.0.1:11434")
RT_MODEL = env("FLOAT_RT_MODEL", "llama3.1:8b")


# ----------------------------------------------------------------------------
# Server and session policy.
# ----------------------------------------------------------------------------
@dataclass
class Settings:
    host: str = field(default_factory=lambda: env("FLOAT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: env_int("FLOAT_PORT", 8765))

    school_name: str = field(default_factory=lambda: env("FLOAT_SCHOOL", "Your School"))

    #: Sessions die when the process does — see security.BOOT_NONCE. This is the
    #: ceiling while it is running.
    session_hours: float = field(default_factory=lambda: env_float("FLOAT_SESSION_HOURS", 10.0))
    #: An idle tab is signed out after this long, whatever the ceiling says.
    idle_minutes: float = field(default_factory=lambda: env_float("FLOAT_IDLE_MINUTES", 45.0))

    max_attempts: int = field(default_factory=lambda: env_int("FLOAT_MAX_ATTEMPTS", 7))
    lockout_seconds: float = field(default_factory=lambda: env_float("FLOAT_LOCKOUT", 90.0))

    max_upload_bytes: int = field(default_factory=lambda: env_int("FLOAT_MAX_UPLOAD", 12_000_000))
    request_timeout: float = field(default_factory=lambda: env_float("FLOAT_TIMEOUT", 180.0))

    google_client_id: str = field(default_factory=lambda: env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: env("GOOGLE_CLIENT_SECRET"))

    #: School-wide kill switch for pointer control. The admin panel writes this.
    computer_control: bool = field(default_factory=lambda: env_bool("FLOAT_COMPUTER_CONTROL", True))

    open_browser: bool = field(default_factory=lambda: env_bool("FLOAT_OPEN_BROWSER", True))

    def google_ready(self) -> bool:
        return bool(self.google_client_id)


settings = Settings()


def ensure_dirs() -> None:
    for path in (DATA_DIR, FILES_DIR, LOG_PATH.parent):
        path.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# Indian school context. Used by the teaching tools so their output is not
# generically American.
# ----------------------------------------------------------------------------
BOARDS = ("CBSE", "ICSE / ISC", "State Board", "IB", "Cambridge (IGCSE/A-Level)", "NIOS")
GRADES = tuple(str(n) for n in range(1, 13))
SUBJECTS = (
    "English", "Hindi", "Mathematics", "Science", "Physics", "Chemistry", "Biology",
    "Social Science", "History", "Geography", "Civics / Political Science", "Economics",
    "Computer Science", "Information Technology", "Sanskrit", "Marathi", "Bengali",
    "Tamil", "Telugu", "Kannada", "Gujarati", "Punjabi", "Urdu",
    "Accountancy", "Business Studies", "Physical Education", "Art", "Music",
    "Environmental Studies (EVS)", "General Knowledge",
)
LANGUAGES = {
    "en": "English",
    "hi": "हिन्दी",
    "bn": "বাংলা",
    "mr": "मराठी",
    "ta": "தமிழ்",
    "te": "తెలుగు",
    "gu": "ગુજરાતી",
    "kn": "ಕನ್ನಡ",
    "ml": "മലയാളം",
    "pa": "ਪੰਜਾਬੀ",
    "or": "ଓଡ଼ିଆ",
    "as": "অসমীয়া",
    "ur": "اردو",
}

#: How much of the work Float does for you. The point of the ladder is that the
#: top rung is not the default — see docs/DESIGN.md.
ASSIST_LEVELS = {
    "coach": "Coach — ask me questions, give me a frame, let me write it",
    "draft": "Draft — give me something to edit",
    "full": "Finish — give me the complete version",
}
DEFAULT_ASSIST = "draft"
