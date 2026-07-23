"""GET /reconciliation/summary and GET /reconciliation/discrepancies."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from .. import queries
from ..config import settings
from ..db import get_db
from ..errors import ApiError
from ..schemas import (
    DiscrepancyOut,
    DiscrepancyResponse,
    DiscrepancyType,
    PageMeta,
    SummaryResponse,
    SummaryRow,
    SummaryTotals,
)
from ..timeutils import (
    TimestampError,
    format_ts,
    parse_ts,
    range_lower_bound,
    range_upper_bound,
)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

Status = Literal["initiated", "processed", "failed", "settled"]


def _resolve_window(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    try:
        lower = range_lower_bound(date_from) if date_from else None
        upper = range_upper_bound(date_to) if date_to else None
    except TimestampError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    if lower and upper and lower > upper:
        raise ApiError(422, "validation_error", "date_from must not be after date_to")
    return lower, upper


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="Aggregated reconciliation position, grouped by any dimension(s)",
)
def get_summary(
    conn: sqlite3.Connection = Depends(get_db),
    group_by: Annotated[str, Query(
        description="Comma-separated dimensions: merchant, date, status, currency. "
                    "Pass an empty string for totals only.")] = "merchant",
    merchant_id: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[Status] | None, Query()] = None,
    currency: str | None = None,
    date_from: Annotated[str | None, Query(description="Inclusive, YYYY-MM-DD or ISO-8601")] = None,
    date_to: Annotated[str | None, Query(description="Inclusive, YYYY-MM-DD or ISO-8601")] = None,
    stale_after_hours: Annotated[int, Query(ge=0, le=8760)] = settings.stale_after_hours,
    limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SummaryResponse:
    """One GROUP BY, computed entirely in SQL.

    `group_by` accepts several dimensions at once, e.g.
    `group_by=merchant,date` produces a per-merchant-per-day position.
    Dates group on `txn_date`, the date of the transaction's first event.

    Amounts are summed over integer minor units, so the totals are exact.
    """
    dimensions = [d.strip() for d in group_by.split(",") if d.strip()]
    unknown = [d for d in dimensions if d not in queries.GROUP_BY_DIMENSIONS]
    if unknown:
        raise ApiError(
            422,
            "validation_error",
            f"Unsupported group_by dimension(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(queries.GROUP_BY_DIMENSIONS)}",
        )
    if len(set(dimensions)) != len(dimensions):
        raise ApiError(422, "validation_error", "group_by contains duplicate dimensions")

    lower, upper = _resolve_window(date_from, date_to)

    where, params = queries.build_transaction_filters(
        merchant_id=merchant_id,
        status=status,
        currency=currency.upper() if currency else None,
        date_from=lower,
        date_to=upper,
        date_field="first_event_at",
    )

    cutoff = format_ts(datetime.now(timezone.utc) - timedelta(hours=stale_after_hours))
    totals, groups, total_groups = queries.reconciliation_summary(
        conn, where, params,
        group_by=dimensions, cutoff=cutoff, limit=limit, offset=offset,
    )

    return SummaryResponse(
        group_by=dimensions,
        filters={
            "merchant_id": merchant_id,
            "status": status,
            "currency": currency,
            "date_from": lower,
            "date_to": upper,
            "stale_after_hours": stale_after_hours,
            "group_count": total_groups,
            "limit": limit,
            "offset": offset,
        },
        totals=SummaryTotals(**totals),
        groups=[SummaryRow(**row) for row in groups],
    )


@router.get(
    "/discrepancies",
    response_model=DiscrepancyResponse,
    summary="Transactions where the payment leg and settlement leg disagree",
)
def get_discrepancies(
    conn: sqlite3.Connection = Depends(get_db),
    type: Annotated[list[DiscrepancyType] | None, Query(
        description="Repeat to select several types. Defaults to every type "
                    "except DUPLICATE_EVENTS.")] = None,
    merchant_id: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[Literal["high", "medium", "low"] | None, Query()] = None,
    date_from: Annotated[str | None, Query(description="Inclusive, YYYY-MM-DD or ISO-8601")] = None,
    date_to: Annotated[str | None, Query(description="Inclusive, YYYY-MM-DD or ISO-8601")] = None,
    stale_after_hours: Annotated[int, Query(
        ge=0, le=8760,
        description="How long a transaction may sit unsettled before it counts "
                    "as stale.")] = settings.stale_after_hours,
    as_of: Annotated[str | None, Query(
        description="Evaluate staleness as at this instant. Defaults to now.")] = None,
    sort_by: Literal[
        "severity", "last_event_at", "first_event_at", "amount", "transaction_id", "age"
    ] = "severity",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=settings.max_page_size)] = settings.default_page_size,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DiscrepancyResponse:
    """Detects the following, all in one SQL pass:

    | Type | Meaning | Severity |
    |---|---|---|
    | `SETTLED_AFTER_FAILURE` | Money settled against a payment the rail reported as failed | high |
    | `CONFLICTING_PAYMENT_STATE` | Both a processed and a failed event exist | high |
    | `AMOUNT_MISMATCH` | Events for one transaction disagreed on the amount | high |
    | `MERCHANT_MISMATCH` | Events for one transaction disagreed on the merchant | high |
    | `SETTLED_WITHOUT_PROCESSING` | Settled with no processed event ever seen | medium |
    | `PROCESSED_NOT_SETTLED` | Processed, still unsettled past the staleness window | medium |
    | `STUCK_IN_INITIATED` | Initiated and never progressed past the staleness window | low |
    | `DUPLICATE_EVENTS` | Replayed events were received (audit signal, opt-in) | low |

    `counts_by_type` is computed over the whole filtered population, not
    just the returned page.
    """
    try:
        as_of_ts = format_ts(parse_ts(as_of)) if as_of else format_ts(datetime.now(timezone.utc))
    except TimestampError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc

    cutoff = format_ts(parse_ts(as_of_ts) - timedelta(hours=stale_after_hours))
    lower, upper = _resolve_window(date_from, date_to)

    selected_types = (
        [t.value for t in type] if type else list(queries.DEFAULT_DISCREPANCY_TYPES)
    )

    where, params = queries.build_transaction_filters(
        merchant_id=merchant_id,
        date_from=lower,
        date_to=upper,
        date_field="first_event_at",
    )

    items, total, counts_by_type = queries.discrepancies(
        conn, where, params,
        types=selected_types, as_of=as_of_ts, cutoff=cutoff,
        sort_by=sort_by, sort_order=sort_order,
        limit=limit, offset=offset,
    )

    if severity:
        # Severity is derived from the set of types actually selected, so
        # it is applied after shaping rather than in SQL.
        items = [item for item in items if item["severity"] == severity]

    return DiscrepancyResponse(
        pagination=PageMeta(
            limit=limit,
            offset=offset,
            total=total,
            returned=len(items),
            has_more=offset + len(items) < total,
        ),
        as_of=as_of_ts,
        stale_after_hours=stale_after_hours,
        counts_by_type={
            key: value for key, value in counts_by_type.items()
            if key in set(selected_types)
        },
        items=[DiscrepancyOut(**item) for item in items],
    )
