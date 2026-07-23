"""POST /events - idempotent ingestion of payment lifecycle events."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Union

from fastapi import APIRouter, Body, Depends, Query, Response

from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..ingest import ingest_events
from ..schemas import EventBatch, EventIn, IngestResponse
from ..timeutils import range_lower_bound, range_upper_bound, TimestampError

router = APIRouter(tags=["events"])

_EXAMPLE = {
    "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
    "event_type": "payment_initiated",
    "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "merchant_id": "merchant_2",
    "merchant_name": "FreshBasket",
    "amount": 15248.29,
    "currency": "INR",
    "timestamp": "2026-01-08T12:11:58.085567+00:00",
}


@router.post(
    "/events",
    response_model=IngestResponse,
    status_code=201,
    summary="Ingest one or more payment lifecycle events",
    responses={
        200: {"description": "Accepted, but every event was a duplicate replay"},
        201: {"description": "At least one new event was stored"},
    },
)
def post_events(
    response: Response,
    payload: Annotated[
        Union[EventIn, EventBatch, list[EventIn]],
        Body(openapi_examples={
            "single": {"summary": "One event", "value": _EXAMPLE},
            "array": {"summary": "Array of events", "value": [_EXAMPLE]},
            "wrapped": {"summary": "Wrapped batch", "value": {"events": [_EXAMPLE]}},
        }),
    ],
    conn: sqlite3.Connection = Depends(get_db),
) -> IngestResponse:
    """Accepts a single event, a bare array, or `{"events": [...]}`.

    Ingestion is idempotent on `event_id`. Replaying an event never
    creates a second record and never re-applies a state transition; the
    replay is reported back as `status: "duplicate"`.

    The batch is applied in one database transaction: either every new
    event in the request lands, or none of them do.
    """
    if isinstance(payload, EventIn):
        events = [payload]
    elif isinstance(payload, EventBatch):
        events = payload.events
    else:
        events = list(payload)

    if not events:
        raise ApiError(422, "validation_error", "At least one event is required")

    if len(events) > settings.max_batch_size:
        raise ApiError(
            413,
            "payload_too_large",
            f"Batch size {len(events)} exceeds the maximum of "
            f"{settings.max_batch_size} events per request",
        )

    results, affected = ingest_events(conn, events)
    created = sum(1 for r in results if r.status == "created")

    # 201 only when something was actually created; a pure replay is a
    # 200 so callers can tell the difference without parsing the body.
    response.status_code = 201 if created else 200

    return IngestResponse(
        received=len(results),
        created=created,
        duplicates=len(results) - created,
        transactions_affected=len(affected),
        results=results,
    )


@router.get("/events", summary="List raw ingested events (audit view)")
def list_events(
    conn: sqlite3.Connection = Depends(get_db),
    transaction_id: str | None = None,
    merchant_id: str | None = None,
    event_type: str | None = None,
    date_from: str | None = Query(None, description="Inclusive, YYYY-MM-DD or ISO-8601"),
    date_to: str | None = Query(None, description="Inclusive, YYYY-MM-DD or ISO-8601"),
    limit: int = Query(settings.default_page_size, ge=1, le=settings.max_page_size),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """The immutable event log, for auditing what was actually received."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if transaction_id:
        clauses.append("transaction_id = :transaction_id")
        params["transaction_id"] = transaction_id
    if merchant_id:
        clauses.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id
    if event_type:
        clauses.append("event_type = :event_type")
        params["event_type"] = event_type

    try:
        if date_from:
            clauses.append("occurred_at >= :date_from")
            params["date_from"] = range_lower_bound(date_from)
        if date_to:
            clauses.append("occurred_at <= :date_to")
            params["date_to"] = range_upper_bound(date_to)
    except TimestampError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc

    where = " AND ".join(clauses) if clauses else "1=1"
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM events WHERE {where}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"""SELECT event_id, event_type, transaction_id, merchant_id,
                   amount_minor / 100.0 AS amount, currency,
                   occurred_at, received_at
            FROM events WHERE {where}
            ORDER BY occurred_at DESC, event_id ASC
            LIMIT :limit OFFSET :offset""",
        dict(params, limit=limit, offset=offset),
    )
    items = [dict(row) for row in rows]

    return {
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "returned": len(items),
            "has_more": offset + len(items) < total,
        },
        "items": items,
    }
