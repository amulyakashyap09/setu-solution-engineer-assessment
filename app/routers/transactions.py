"""GET /transactions and GET /transactions/{transaction_id}."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query

from .. import queries
from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..schemas import (
    MerchantOut,
    PageMeta,
    TransactionDetail,
    TransactionListResponse,
    TransactionOut,
)
from ..timeutils import (
    TimestampError,
    format_ts,
    range_lower_bound,
    range_upper_bound,
    to_minor_units,
)

router = APIRouter(tags=["transactions"])

SortField = Literal[
    "first_event_at", "last_event_at", "amount", "status",
    "merchant_id", "transaction_id", "settled_at", "event_count",
]
DateField = Literal["first_event_at", "last_event_at", "initiated_at", "settled_at"]
Status = Literal["initiated", "processed", "failed", "settled"]
PaymentStatusLiteral = Literal["initiated", "processed", "failed", "conflicted"]
SettlementStatusLiteral = Literal["pending", "settled"]


@router.get(
    "/transactions",
    response_model=TransactionListResponse,
    summary="List transactions with filtering, sorting and pagination",
)
def get_transactions(
    conn: sqlite3.Connection = Depends(get_db),
    merchant_id: Annotated[list[str] | None, Query(
        description="Repeat to filter on several merchants")] = None,
    status: Annotated[list[Status] | None, Query(
        description="Rolled-up transaction status. Repeat for multiple.")] = None,
    payment_status: Annotated[list[PaymentStatusLiteral] | None, Query()] = None,
    settlement_status: Annotated[list[SettlementStatusLiteral] | None, Query()] = None,
    currency: str | None = None,
    date_from: Annotated[str | None, Query(
        description="Inclusive lower bound. `YYYY-MM-DD` or full ISO-8601.")] = None,
    date_to: Annotated[str | None, Query(
        description="Inclusive upper bound. A bare date covers the whole day.")] = None,
    date_field: Annotated[DateField, Query(
        description="Which timestamp the date range applies to.")] = "first_event_at",
    min_amount: Annotated[float | None, Query(ge=0)] = None,
    max_amount: Annotated[float | None, Query(ge=0)] = None,
    has_duplicates: Annotated[bool | None, Query(
        description="Only transactions that did (or did not) receive replayed events.")] = None,
    sort_by: SortField = "first_event_at",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = settings.default_page_size,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionListResponse:
    """Filtering, sorting and pagination all happen in SQL.

    Sort keys are resolved against a whitelist, and `transaction_id` is
    always appended as a tiebreaker so paging is stable even when sorting
    on a non-unique column.
    """
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise ApiError(422, "validation_error",
                       "min_amount must not be greater than max_amount")

    try:
        lower = range_lower_bound(date_from) if date_from else None
        upper = range_upper_bound(date_to) if date_to else None
    except TimestampError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc

    if lower and upper and lower > upper:
        raise ApiError(422, "validation_error",
                       "date_from must not be after date_to")

    where, params = queries.build_transaction_filters(
        merchant_id=merchant_id,
        status=status,
        payment_status=payment_status,
        settlement_status=settlement_status,
        currency=currency.upper() if currency else None,
        date_from=lower,
        date_to=upper,
        date_field=date_field,
        min_amount_minor=to_minor_units(min_amount) if min_amount is not None else None,
        max_amount_minor=to_minor_units(max_amount) if max_amount is not None else None,
        has_duplicates=has_duplicates,
    )

    total = queries.count_transactions(conn, where, params)
    rows = queries.list_transactions(
        conn, where, params,
        sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset,
    )

    return TransactionListResponse(
        pagination=PageMeta(
            limit=limit,
            offset=offset,
            total=total,
            returned=len(rows),
            has_more=offset + len(rows) < total,
        ),
        items=[TransactionOut(**row) for row in rows],
    )


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetail,
    summary="Fetch a transaction with its merchant and full event history",
    responses={404: {"description": "No such transaction"}},
)
def get_transaction_detail(
    transaction_id: Annotated[str, Path(min_length=1, max_length=128)],
    conn: sqlite3.Connection = Depends(get_db),
    stale_after_hours: Annotated[int, Query(ge=0, le=8760)] = settings.stale_after_hours,
) -> TransactionDetail:
    """Returns the projected transaction state, the merchant, any
    discrepancies, and every event ever received for it in chronological
    order - including replays, which appear once because they were never
    stored twice."""
    row = queries.get_transaction(conn, transaction_id)
    if row is None:
        raise ApiError(
            404, "not_found", f"Transaction {transaction_id!r} was not found"
        )

    cutoff = format_ts(datetime.now(timezone.utc) - timedelta(hours=stale_after_hours))
    events = queries.get_transaction_events(conn, transaction_id)
    discrepancy_types = queries.discrepancy_types_for_transaction(row, cutoff)

    merchant = MerchantOut(
        merchant_id=row["merchant_id"],
        merchant_name=row["merchant_name"] or row["merchant_id"],
    )
    payload = {k: v for k, v in row.items()
               if k not in ("has_amount_mismatch", "has_merchant_mismatch")}

    return TransactionDetail(
        **payload,
        merchant=merchant,
        discrepancies=discrepancy_types,
        events=events,
    )
