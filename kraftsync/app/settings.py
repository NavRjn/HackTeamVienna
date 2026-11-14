"""Runtime settings for the local KraftSync service. Env vars only (see .env.example)."""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_TOKEN_LEN = 24
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class SettingsError(ValueError):
    """Raised when the environment is missing something or is unsafe."""


def _path(value: str | None, default: str) -> Path:
    p = Path(value or default)
    return p if p.is_absolute() else REPO_ROOT / p


@dataclass(frozen=True)
class Settings:
    token: str
    host: str = "0.0.0.0"              # LAN-reachable on purpose (team on the same Wi-Fi); token required
    port: int = 8000
    plan_path: Path = REPO_ROOT / "data" / "demo" / "plan.json"
    events_path: Path = REPO_ROOT / "data" / "events.jsonl"
    log_path: Path | None = None       # compact call log file (console always); from_env default data/kraftsync.log
    webhook_url: str | None = None     # n8n webhook we call when there is a conflict
    webhook_token: str | None = None   # sent to n8n as X-KraftSync-Token (separate secret)
    confidence_min: float = 0.7
    demo_today: date | None = None
    redirect_chat_id: int | None = None
    docs: bool = False

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        try:
            return cls(
                token=e("KRAFTSYNC_TOKEN", ""),
                host=e("KRAFTSYNC_HOST") or "0.0.0.0",
                port=int(e("KRAFTSYNC_PORT") or 8000),
                plan_path=_path(e("PLAN_PATH"), "data/demo/plan.json"),
                events_path=_path(e("EVENTS_PATH"), "data/events.jsonl"),
                log_path=_path(e("LOG_PATH"), "data/kraftsync.log") if e("LOG_PATH") != "off" else None,
                webhook_url=e("N8N_CONFLICT_WEBHOOK_URL") or None,
                webhook_token=e("N8N_HOOK_TOKEN") or None,
                confidence_min=float(e("CONFIDENCE_MIN") or 0.7),
                demo_today=date.fromisoformat(e("DEMO_TODAY")) if e("DEMO_TODAY") else None,
                redirect_chat_id=int(e("DEMO_REDIRECT_CHAT_ID")) if e("DEMO_REDIRECT_CHAT_ID") else None,
                docs=e("KRAFTSYNC_DOCS") == "1",
            )
        except ValueError as exc:
            raise SettingsError(f"invalid value in environment: {exc}") from exc

    def validate(self) -> None:
        if len(self.token) < MIN_TOKEN_LEN:
            raise SettingsError(
                f"KRAFTSYNC_TOKEN must be set and at least {MIN_TOKEN_LEN} characters long."
            )
        if not 0 <= self.confidence_min <= 1:
            raise SettingsError("CONFIDENCE_MIN must be between 0 and 1.")
        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            raise SettingsError("N8N_CONFLICT_WEBHOOK_URL must start with http:// or https://")
