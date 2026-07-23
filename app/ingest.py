"""Event ingestion and the transaction projection.

Idempotency has two layers:

1. `events.event_id` is the PRIMARY KEY. A replayed event cannot be
   stored twice, full stop - that is a database-level guarantee, not an
   application convention.

2. The projection folds each event type with MIN() over its timestamp
   rather than overwriting a status field. Folding with MIN is
   idempotent (min(a, a) == a) and commutative (min(a, b) == min(b, a)),
   so replaying an event, or receiving events out of order, converges on
   the same transaction state.

The check-then-insert below is safe because the whole batch runs inside a
single `BEGIN IMMEDIATE`, which takes the SQLite write lock up front and
serialises it against every other writer.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Sequence

from .schemas import EventIn, IngestResult
from .timeutils import now_ts, to_minor_units

_EVENT_TYPE_TO_COLUMN = {
    "payment_initiated": "initiated_at",
    "payment_processed": "processed_at",
    "payment_failed": "failed_at",
    "settled": "settled_at",
}

_UPSERT_MERCHANT = """
INSERT INTO merchants (merchant_id, merchant_name, created_at, updated_at)
VALUES (:merchant_id, :merchant_name, :now, :now)
ON CONFLICT(merchant_id) DO UPDATE SET
    merchant_name = COALESCE(NULLIF(excluded.merchant_name, ''), merchants.merchant_name),
    updated_at    = excluded.updated_at
"""

# The fold. COALESCE on both sides of MIN is load-bearing: SQLite's
# scalar MIN() returns NULL if *any* argument is NULL, so a naive
# MIN(existing, incoming) would erase a known timestamp the first time an
# event of a different type arrived.
_UPSERT_TRANSACTION = """
INSERT INTO transactions (
    transaction_id, merchant_id, amount_minor, currency,
    initiated_at, processed_at, failed_at, settled_at,
    first_event_at, last_event_at, event_count, created_at, updated_at
) VALUES (
    :transaction_id, :merchant_id, :amount_minor, :currency,
    :initiated_at, :processed_at, :failed_at, :settled_at,
    :occurred_at, :occurred_at, 1, :now, :now
)
ON CONFLICT(transaction_id) DO UPDATE SET
    initiated_at = MIN(COALESCE(transactions.initiated_at, excluded.initiated_at),
                       COALESCE(excluded.initiated_at, transactions.initiated_at)),
    processed_at = MIN(COALESCE(transactions.processed_at, excluded.processed_at),
                       COALESCE(excluded.processed_at, transactions.processed_at)),
    failed_at    = MIN(COALESCE(transactions.failed_at, excluded.failed_at),
                       COALESCE(excluded.failed_at, transactions.failed_at)),
    settled_at   = MIN(COALESCE(transactions.settled_at, excluded.settled_at),
                       COALESCE(excluded.settled_at, transactions.settled_at)),
    first_event_at = MIN(transactions.first_event_at, excluded.first_event_at),
    last_event_at  = MAX(transactions.last_event_at,  excluded.last_event_at),
    event_count    = transactions.event_count + 1,
    has_amount_mismatch = CASE
        WHEN transactions.amount_minor <> excluded.amount_minor THEN 1
        ELSE transactions.has_amount_mismatch END,
    has_merchant_mismatch = CASE
        WHEN transactions.merchant_id <> excluded.merchant_id THEN 1
        ELSE transactions.has_merchant_mismatch END,
    updated_at = excluded.updated_at
"""

_INSERT_EVENT = """
INSERT INTO events (
    event_id, transaction_id, merchant_id, event_type,
    amount_minor, currency, occurred_at, received_at, raw_payload
) VALUES (
    :event_id, :transaction_id, :merchant_id, :event_type,
    :amount_minor, :currency, :occurred_at, :now, :raw_payload
)
"""

_SELECT_EXISTING_EVENT = """
SELECT event_id, transaction_id, merchant_id, event_type,
       amount_minor, currency, occurred_at
FROM events
WHERE event_id = ?
"""

_BUMP_DUPLICATE = """
UPDATE transactions
SET duplicate_event_count = duplicate_event_count + 1,
    updated_at = ?
WHERE transaction_id = ?
"""


def _to_row(event: EventIn, now: str) -> dict:
    params = {column: None for column in _EVENT_TYPE_TO_COLUMN.values()}
    params[_EVENT_TYPE_TO_COLUMN[event.event_type.value]] = event.timestamp
    params.update(
        event_id=event.event_id,
        transaction_id=event.transaction_id,
        merchant_id=event.merchant_id,
        merchant_name=event.merchant_name or event.merchant_id,
        event_type=event.event_type.value,
        amount_minor=to_minor_units(event.amount),
        currency=event.currency,
        occurred_at=event.timestamp,
        now=now,
        raw_payload=json.dumps(event.model_dump(mode="json"), sort_keys=True),
    )
    return params


def _payload_differs(existing: sqlite3.Row, params: dict) -> bool:
    return any(
        existing[field] != params[field]
        for field in (
            "transaction_id",
            "merchant_id",
            "event_type",
            "amount_minor",
            "currency",
            "occurred_at",
        )
    )


def ingest_events(
    conn: sqlite3.Connection,
    events: Sequence[EventIn] | Iterable[EventIn],
) -> tuple[list[IngestResult], set[str]]:
    """Ingest a batch atomically. Returns per-event results and the set of
    transaction ids whose state was affected."""
    now = now_ts()
    results: list[IngestResult] = []
    affected: set[str] = set()

    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    try:
        for event in events:
            params = _to_row(event, now)

            existing = cursor.execute(
                _SELECT_EXISTING_EVENT, (params["event_id"],)
            ).fetchone()

            if existing is not None:
                # Idempotent replay: the event is not re-applied. We only
                # bump an audit counter so operations can see that the
                # upstream system is redelivering.
                conflict = _payload_differs(existing, params)
                cursor.execute(_BUMP_DUPLICATE, (now, existing["transaction_id"]))
                results.append(
                    IngestResult(
                        event_id=params["event_id"],
                        transaction_id=existing["transaction_id"],
                        status="duplicate",
                        payload_conflict=conflict,
                        message=(
                            "event_id already ingested with a different payload; "
                            "the stored event was kept"
                            if conflict
                            else "event already ingested; ignored"
                        ),
                    )
                )
                continue

            cursor.execute(_UPSERT_MERCHANT, params)
            cursor.execute(_UPSERT_TRANSACTION, params)
            cursor.execute(_INSERT_EVENT, params)

            affected.add(params["transaction_id"])
            results.append(
                IngestResult(
                    event_id=params["event_id"],
                    transaction_id=params["transaction_id"],
                    status="created",
                )
            )

        cursor.execute("COMMIT")
    except Exception:
        cursor.execute("ROLLBACK")
        raise

    return results, affected
