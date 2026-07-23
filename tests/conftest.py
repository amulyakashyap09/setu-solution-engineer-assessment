"""Shared fixtures.

Every test gets its own SQLite file under pytest's tmp_path, so tests are
isolated and can run in any order. The app is exercised through
TestClient against that file - no mocking of the database layer, because
the behaviour under test (idempotency, generated columns, SQL
aggregation) lives in the database.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import connect, init_db
from app.ingest import ingest_events
from app.main import create_app
from app.schemas import EventIn
from app.timeutils import format_ts

SAMPLE_FILE = Path(__file__).resolve().parent.parent / "data" / "sample_events.json"


def _point_settings_at(path: str) -> None:
    # Settings is a frozen dataclass; object.__setattr__ is the supported
    # escape hatch and keeps the override explicit.
    object.__setattr__(settings, "database_path", path)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh database per test, and startup seeding switched off so tests
    assert against exactly the data they created."""
    original_path = settings.database_path
    original_seed = settings.seed_on_startup
    object.__setattr__(settings, "seed_on_startup", False)

    path = str(tmp_path / "test.db")
    _point_settings_at(path)
    init_db(path)
    yield path

    _point_settings_at(original_path)
    object.__setattr__(settings, "seed_on_startup", original_seed)


@pytest.fixture
def conn(isolated_db):
    connection = connect(isolated_db)
    yield connection
    connection.close()


@pytest.fixture
def client(isolated_db):
    # raise_server_exceptions=False so that the registered 500 handler
    # actually runs and can be asserted on, instead of TestClient
    # re-raising the exception into the test.
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------
# Timestamp helpers - most reconciliation behaviour is time-relative, so
# tests express times as offsets from "now" rather than hardcoded dates.
# ---------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def hours_ago(hours: float, base: datetime | None = None) -> str:
    return format_ts((base or datetime.now(timezone.utc)) - timedelta(hours=hours))


def minutes_ago(minutes: float) -> str:
    return format_ts(datetime.now(timezone.utc) - timedelta(minutes=minutes))


# ---------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------

_COUNTER = {"n": 0}

# Sentinel so that an explicitly-passed falsy value (e.g. event_id="")
# reaches the API instead of being swapped for a generated default.
_UNSET = object()


def make_event(
    event_type: str = "payment_initiated",
    *,
    event_id=_UNSET,
    transaction_id: str = "txn-1",
    merchant_id: str = "merchant_1",
    merchant_name: str | None = "QuickMart",
    amount: float = 100.0,
    currency: str = "INR",
    timestamp=_UNSET,
    **extra,
) -> dict:
    _COUNTER["n"] += 1
    payload = {
        "event_id": f"evt-{_COUNTER['n']}" if event_id is _UNSET else event_id,
        "event_type": event_type,
        "transaction_id": transaction_id,
        "merchant_id": merchant_id,
        "amount": amount,
        "currency": currency,
        "timestamp": hours_ago(1) if timestamp is _UNSET else timestamp,
    }
    if merchant_name is not None:
        payload["merchant_name"] = merchant_name
    payload.update(extra)
    return payload


def lifecycle(
    transaction_id: str,
    types: list[str],
    *,
    start_hours_ago: float = 72,
    spacing_hours: float = 1,
    **kwargs,
) -> list[dict]:
    """Build a chronologically spaced sequence of events for one transaction."""
    return [
        make_event(
            event_type,
            transaction_id=transaction_id,
            timestamp=hours_ago(start_hours_ago - index * spacing_hours),
            **kwargs,
        )
        for index, event_type in enumerate(types)
    ]


def post(client: TestClient, payload):
    return client.post("/events", json=payload)


def ingest(client: TestClient, payload) -> dict:
    response = post(client, payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


# ---------------------------------------------------------------------
# The real 10k-event dataset, seeded once per session and copied per test
# ---------------------------------------------------------------------

@pytest.fixture(scope="session")
def sample_events() -> list[dict]:
    if not SAMPLE_FILE.exists():
        pytest.skip(f"{SAMPLE_FILE} not present")
    return json.loads(SAMPLE_FILE.read_text())


@pytest.fixture(scope="session")
def seeded_db_file(tmp_path_factory, sample_events) -> str:
    path = str(tmp_path_factory.mktemp("seed") / "seeded.db")
    init_db(path)
    connection = connect(path)
    parsed = [EventIn.model_validate(record) for record in sample_events]
    for start in range(0, len(parsed), 2000):
        ingest_events(connection, parsed[start:start + 2000])
    connection.close()
    return path


@pytest.fixture
def seeded_client(seeded_db_file, tmp_path):
    destination = tmp_path / "seeded_copy.db"
    shutil.copy(seeded_db_file, destination)
    _point_settings_at(str(destination))
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client
