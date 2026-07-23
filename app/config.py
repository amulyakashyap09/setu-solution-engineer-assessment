"""Runtime configuration, driven entirely by environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ":memory:" is supported for tests.
    database_path: str = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "payments.db"))

    # Largest batch accepted by POST /events in a single request.
    max_batch_size: int = _env_int("MAX_BATCH_SIZE", 5000)

    # Pagination guardrails.
    default_page_size: int = _env_int("DEFAULT_PAGE_SIZE", 50)
    max_page_size: int = _env_int("MAX_PAGE_SIZE", 500)

    # How long a transaction may sit in a non-terminal state before the
    # reconciliation report considers it stale. Overridable per request.
    stale_after_hours: int = _env_int("STALE_AFTER_HOURS", 48)

    # SQLite busy timeout in milliseconds.
    busy_timeout_ms: int = _env_int("SQLITE_BUSY_TIMEOUT_MS", 5000)

    # On a fresh deployment (Render/Fly free tiers have ephemeral disks)
    # an empty database makes the demo look broken. If the events table is
    # empty at boot and a seed file is present, load it once.
    seed_on_startup: bool = os.getenv("SEED_ON_STARTUP", "true").lower() in (
        "1", "true", "yes"
    )
    seed_file: str = os.getenv("SEED_FILE", str(BASE_DIR / "data" / "sample_events.json"))

    app_name: str = "Payment Reconciliation Service"
    version: str = "1.0.0"


settings = Settings()
