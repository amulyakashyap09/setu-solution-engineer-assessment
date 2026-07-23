"""Read-side SQL.

Every filter, aggregation, sort and page is expressed in SQL. Nothing
here pulls a result set into Python to fold it - the largest thing this
module ever materialises is one page of rows.

Identifiers that end up spliced into SQL text (sort columns, group-by
dimensions) are only ever looked up from whitelists in this module.
Everything user-supplied is a bound parameter.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .timeutils import to_major_units

# ---------------------------------------------------------------------
# Whitelists
# ---------------------------------------------------------------------

TRANSACTION_SORT_COLUMNS = {
    "first_event_at": "t.first_event_at",
    "last_event_at": "t.last_event_at",
    "amount": "t.amount_minor",
    "status": "t.status",
    "merchant_id": "t.merchant_id",
    "transaction_id": "t.transaction_id",
    "settled_at": "t.settled_at",
    "event_count": "t.event_count",
}

DISCREPANCY_SORT_COLUMNS = {
    "severity": "severity_rank",
    "last_event_at": "last_event_at",
    "first_event_at": "first_event_at",
    "amount": "amount_minor",
    "transaction_id": "transaction_id",
    "age": "last_event_at",
}

DATE_FIELDS = {
    "first_event_at": "t.first_event_at",
    "last_event_at": "t.last_event_at",
    "initiated_at": "t.initiated_at",
    "settled_at": "t.settled_at",
}

GROUP_BY_DIMENSIONS = {
    "merchant": ("t.merchant_id", "merchant_id"),
    "date": ("t.txn_date", "date"),
    "status": ("t.status", "status"),
    "currency": ("t.currency", "currency"),
}

# Flag name -> (public discrepancy type, severity)
DISCREPANCY_FLAGS: dict[str, tuple[str, str]] = {
    "d_settled_after_failure": ("SETTLED_AFTER_FAILURE", "high"),
    "d_conflicting_payment_state": ("CONFLICTING_PAYMENT_STATE", "high"),
    "d_amount_mismatch": ("AMOUNT_MISMATCH", "high"),
    "d_merchant_mismatch": ("MERCHANT_MISMATCH", "high"),
    "d_settled_without_processing": ("SETTLED_WITHOUT_PROCESSING", "medium"),
    "d_processed_not_settled": ("PROCESSED_NOT_SETTLED", "medium"),
    "d_stuck_in_initiated": ("STUCK_IN_INITIATED", "low"),
    "d_duplicate_events": ("DUPLICATE_EVENTS", "low"),
}

TYPE_TO_FLAG = {public: flag for flag, (public, _) in DISCREPANCY_FLAGS.items()}

# DUPLICATE_EVENTS is an audit signal rather than a money problem: a
# redelivered event is ignored by design and does not corrupt state. It is
# excluded from the default report so the report stays actionable, and is
# available on request via ?type=DUPLICATE_EVENTS.
DEFAULT_DISCREPANCY_TYPES = [
    public for public, _ in DISCREPANCY_FLAGS.values() if public != "DUPLICATE_EVENTS"
]

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

# Columns selected for a transaction row, shared by list and detail.
_TXN_COLUMNS = """
    t.transaction_id, t.merchant_id, m.merchant_name, t.amount, t.currency,
    t.status, t.payment_status, t.settlement_status,
    t.initiated_at, t.processed_at, t.failed_at, t.settled_at,
    t.first_event_at, t.last_event_at, t.event_count,
    t.duplicate_event_count, t.txn_date
"""


# ---------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------

def _in_clause(column: str, values: Sequence[str], prefix: str,
               params: dict[str, Any]) -> str:
    placeholders = []
    for index, value in enumerate(values):
        key = f"{prefix}{index}"
        params[key] = value
        placeholders.append(f":{key}")
    return f"{column} IN ({', '.join(placeholders)})"


def build_transaction_filters(
    *,
    merchant_id: Sequence[str] | None = None,
    status: Sequence[str] | None = None,
    payment_status: Sequence[str] | None = None,
    settlement_status: Sequence[str] | None = None,
    currency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_field: str = "first_event_at",
    min_amount_minor: int | None = None,
    max_amount_minor: int | None = None,
    has_duplicates: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if merchant_id:
        clauses.append(_in_clause("t.merchant_id", merchant_id, "merchant_", params))
    if status:
        clauses.append(_in_clause("t.status", status, "status_", params))
    if payment_status:
        clauses.append(_in_clause("t.payment_status", payment_status, "pstatus_", params))
    if settlement_status:
        clauses.append(_in_clause("t.settlement_status", settlement_status, "sstatus_", params))
    if currency:
        clauses.append("t.currency = :currency")
        params["currency"] = currency

    column = DATE_FIELDS[date_field]
    if date_from:
        clauses.append(f"{column} >= :date_from")
        params["date_from"] = date_from
    if date_to:
        clauses.append(f"{column} <= :date_to")
        params["date_to"] = date_to

    if min_amount_minor is not None:
        clauses.append("t.amount_minor >= :min_amount")
        params["min_amount"] = min_amount_minor
    if max_amount_minor is not None:
        clauses.append("t.amount_minor <= :max_amount")
        params["max_amount"] = max_amount_minor

    if has_duplicates is True:
        clauses.append("t.duplicate_event_count > 0")
    elif has_duplicates is False:
        clauses.append("t.duplicate_event_count = 0")

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


# ---------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------

def count_transactions(conn: sqlite3.Connection, where: str,
                       params: dict[str, Any]) -> int:
    sql = f"SELECT COUNT(*) AS n FROM transactions t WHERE {where}"
    return conn.execute(sql, params).fetchone()["n"]


def list_transactions(
    conn: sqlite3.Connection,
    where: str,
    params: dict[str, Any],
    *,
    sort_by: str,
    sort_order: str,
    limit: int,
    offset: int,
) -> list[dict]:
    column = TRANSACTION_SORT_COLUMNS[sort_by]
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"
    # transaction_id is appended as a tiebreaker so that paging through a
    # non-unique sort key (status, merchant_id, a shared timestamp) is
    # stable and cannot repeat or skip rows between pages.
    sql = f"""
        SELECT {_TXN_COLUMNS}
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        WHERE {where}
        ORDER BY {column} {direction} NULLS LAST, t.transaction_id ASC
        LIMIT :limit OFFSET :offset
    """
    query_params = dict(params, limit=limit, offset=offset)
    return [dict(row) for row in conn.execute(sql, query_params)]


def get_transaction(conn: sqlite3.Connection, transaction_id: str) -> dict | None:
    sql = f"""
        SELECT {_TXN_COLUMNS},
               t.has_amount_mismatch, t.has_merchant_mismatch
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        WHERE t.transaction_id = :transaction_id
    """
    row = conn.execute(sql, {"transaction_id": transaction_id}).fetchone()
    return dict(row) if row else None


def get_transaction_events(conn: sqlite3.Connection, transaction_id: str) -> list[dict]:
    sql = """
        SELECT event_id, event_type, transaction_id, merchant_id,
               amount_minor / 100.0 AS amount, currency,
               occurred_at, received_at
        FROM events
        WHERE transaction_id = :transaction_id
        ORDER BY occurred_at ASC, received_at ASC
    """
    return [dict(row) for row in conn.execute(sql, {"transaction_id": transaction_id})]


def list_merchants(conn: sqlite3.Connection) -> list[dict]:
    sql = """
        SELECT m.merchant_id, m.merchant_name,
               COUNT(t.transaction_id) AS transaction_count,
               COALESCE(SUM(t.amount_minor), 0) AS total_amount_minor
        FROM merchants m
        LEFT JOIN transactions t ON t.merchant_id = m.merchant_id
        GROUP BY m.merchant_id, m.merchant_name
        ORDER BY m.merchant_id
    """
    rows = []
    for row in conn.execute(sql):
        item = dict(row)
        item["total_amount"] = to_major_units(item.pop("total_amount_minor"))
        rows.append(item)
    return rows


# ---------------------------------------------------------------------
# Reconciliation summary
# ---------------------------------------------------------------------

_METRICS = """
    COUNT(*) AS transaction_count,
    COALESCE(SUM(t.amount_minor), 0) AS total_amount_minor,
    COALESCE(SUM(t.status = 'initiated'), 0) AS initiated_count,
    COALESCE(SUM(t.status = 'processed'), 0) AS processed_count,
    COALESCE(SUM(t.status = 'failed'), 0)    AS failed_count,
    COALESCE(SUM(t.status = 'settled'), 0)   AS settled_count,
    COALESCE(SUM(CASE WHEN t.status = 'settled' THEN t.amount_minor ELSE 0 END), 0)
        AS settled_amount_minor,
    COALESCE(SUM(CASE WHEN t.status = 'failed' THEN t.amount_minor ELSE 0 END), 0)
        AS failed_amount_minor,
    COALESCE(SUM(CASE WHEN t.settled_at IS NULL AND t.failed_at IS NULL
                      THEN t.amount_minor ELSE 0 END), 0)
        AS unsettled_amount_minor,
    COALESCE(SUM(CASE WHEN {discrepancy_predicate} THEN 1 ELSE 0 END), 0)
        AS discrepancy_count
"""


def _discrepancy_predicate(cutoff_param: str = ":cutoff") -> str:
    """A row-level predicate that is true when a transaction has at least
    one reconciliation discrepancy (excluding the informational
    duplicate-events signal)."""
    return f"""(
        (t.settled_at IS NOT NULL AND t.failed_at IS NOT NULL)
        OR (t.settled_at IS NOT NULL AND t.processed_at IS NULL AND t.failed_at IS NULL)
        OR (t.processed_at IS NOT NULL AND t.failed_at IS NULL
            AND t.settled_at IS NULL AND t.processed_at < {cutoff_param})
        OR (t.initiated_at IS NOT NULL AND t.processed_at IS NULL
            AND t.failed_at IS NULL AND t.settled_at IS NULL
            AND t.initiated_at < {cutoff_param})
        OR (t.processed_at IS NOT NULL AND t.failed_at IS NOT NULL)
        OR t.has_amount_mismatch = 1
        OR t.has_merchant_mismatch = 1
    )"""


def _shape_summary_row(row: dict) -> dict:
    count = row["transaction_count"] or 0
    settled = row["settled_count"] or 0
    shaped = {
        "transaction_count": count,
        "total_amount": to_major_units(row["total_amount_minor"]),
        "initiated_count": row["initiated_count"],
        "processed_count": row["processed_count"],
        "failed_count": row["failed_count"],
        "settled_count": settled,
        "settled_amount": to_major_units(row["settled_amount_minor"]),
        "failed_amount": to_major_units(row["failed_amount_minor"]),
        "unsettled_amount": to_major_units(row["unsettled_amount_minor"]),
        "discrepancy_count": row["discrepancy_count"],
        "settlement_rate": round(settled / count, 4) if count else 0.0,
    }
    for key in ("merchant_id", "merchant_name", "date", "status", "currency"):
        if key in row:
            shaped[key] = row[key]
    return shaped


def reconciliation_summary(
    conn: sqlite3.Connection,
    where: str,
    params: dict[str, Any],
    *,
    group_by: Sequence[str],
    cutoff: str,
    limit: int,
    offset: int,
) -> tuple[dict, list[dict], int]:
    metrics = _METRICS.format(discrepancy_predicate=_discrepancy_predicate())
    query_params = dict(params, cutoff=cutoff)

    totals_sql = f"SELECT {metrics} FROM transactions t WHERE {where}"
    totals = _shape_summary_row(dict(conn.execute(totals_sql, query_params).fetchone()))

    if not group_by:
        return totals, [], 0

    select_parts: list[str] = []
    group_parts: list[str] = []
    join = ""
    for dimension in group_by:
        expression, alias = GROUP_BY_DIMENSIONS[dimension]
        select_parts.append(f"{expression} AS {alias}")
        group_parts.append(expression)
        if dimension == "merchant":
            select_parts.append("m.merchant_name AS merchant_name")
            group_parts.append("m.merchant_name")
            join = "JOIN merchants m ON m.merchant_id = t.merchant_id"

    select_clause = ", ".join(select_parts)
    group_clause = ", ".join(group_parts)

    count_sql = f"""
        SELECT COUNT(*) AS n FROM (
            SELECT 1 FROM transactions t {join}
            WHERE {where} GROUP BY {group_clause}
        )
    """
    total_groups = conn.execute(count_sql, query_params).fetchone()["n"]

    groups_sql = f"""
        SELECT {select_clause}, {metrics}
        FROM transactions t
        {join}
        WHERE {where}
        GROUP BY {group_clause}
        ORDER BY {group_clause}
        LIMIT :limit OFFSET :offset
    """
    rows = conn.execute(groups_sql, dict(query_params, limit=limit, offset=offset))
    groups = [_shape_summary_row(dict(row)) for row in rows]
    return totals, groups, total_groups


# ---------------------------------------------------------------------
# Discrepancies
# ---------------------------------------------------------------------

_FLAGGED_CTE = """
WITH flagged AS (
    SELECT
        t.transaction_id, t.merchant_id, m.merchant_name, t.amount, t.amount_minor,
        t.currency, t.status, t.payment_status, t.settlement_status,
        t.initiated_at, t.processed_at, t.failed_at, t.settled_at,
        t.first_event_at, t.last_event_at, t.event_count, t.duplicate_event_count,
        CAST((julianday(:as_of) - julianday(t.last_event_at)) * 24.0 AS REAL) AS age_hours,
        CASE WHEN t.settled_at IS NOT NULL AND t.failed_at IS NOT NULL
             THEN 1 ELSE 0 END AS d_settled_after_failure,
        CASE WHEN t.settled_at IS NOT NULL AND t.processed_at IS NULL
                  AND t.failed_at IS NULL
             THEN 1 ELSE 0 END AS d_settled_without_processing,
        CASE WHEN t.processed_at IS NOT NULL AND t.failed_at IS NULL
                  AND t.settled_at IS NULL AND t.processed_at < :cutoff
             THEN 1 ELSE 0 END AS d_processed_not_settled,
        CASE WHEN t.initiated_at IS NOT NULL AND t.processed_at IS NULL
                  AND t.failed_at IS NULL AND t.settled_at IS NULL
                  AND t.initiated_at < :cutoff
             THEN 1 ELSE 0 END AS d_stuck_in_initiated,
        CASE WHEN t.processed_at IS NOT NULL AND t.failed_at IS NOT NULL
             THEN 1 ELSE 0 END AS d_conflicting_payment_state,
        t.has_amount_mismatch   AS d_amount_mismatch,
        t.has_merchant_mismatch AS d_merchant_mismatch,
        CASE WHEN t.duplicate_event_count > 0
             THEN 1 ELSE 0 END AS d_duplicate_events
    FROM transactions t
    JOIN merchants m ON m.merchant_id = t.merchant_id
    WHERE {where}
),
scored AS (
    SELECT *,
        CASE
            WHEN d_settled_after_failure = 1 OR d_conflicting_payment_state = 1
                 OR d_amount_mismatch = 1 OR d_merchant_mismatch = 1 THEN 3
            WHEN d_settled_without_processing = 1 OR d_processed_not_settled = 1 THEN 2
            ELSE 1
        END AS severity_rank
    FROM flagged
)
"""


def _selected_predicate(types: Sequence[str]) -> str:
    flags = [TYPE_TO_FLAG[t] for t in types]
    return " OR ".join(f"{flag} = 1" for flag in flags) if flags else "0"


def discrepancies(
    conn: sqlite3.Connection,
    where: str,
    params: dict[str, Any],
    *,
    types: Sequence[str],
    as_of: str,
    cutoff: str,
    sort_by: str,
    sort_order: str,
    limit: int,
    offset: int,
) -> tuple[list[dict], int, dict[str, int]]:
    cte = _FLAGGED_CTE.format(where=where)
    selected = _selected_predicate(types)
    query_params = dict(params, as_of=as_of, cutoff=cutoff)

    total = conn.execute(
        f"{cte} SELECT COUNT(*) AS n FROM scored WHERE {selected}", query_params
    ).fetchone()["n"]

    # Counts per type are computed across the whole filtered population,
    # not just the returned page, so the header numbers stay meaningful
    # while paging.
    count_columns = ", ".join(
        f"COALESCE(SUM({flag}), 0) AS {flag}" for flag in DISCREPANCY_FLAGS
    )
    count_row = conn.execute(
        f"{cte} SELECT {count_columns} FROM scored WHERE {selected}", query_params
    ).fetchone()
    counts_by_type = {
        DISCREPANCY_FLAGS[flag][0]: count_row[flag] for flag in DISCREPANCY_FLAGS
    }

    column = DISCREPANCY_SORT_COLUMNS[sort_by]
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"
    order_clause = (
        f"{column} {direction} NULLS LAST, severity_rank DESC, transaction_id ASC"
        if sort_by != "severity"
        else f"severity_rank {direction}, last_event_at DESC, transaction_id ASC"
    )

    rows = conn.execute(
        f"""{cte}
        SELECT * FROM scored
        WHERE {selected}
        ORDER BY {order_clause}
        LIMIT :limit OFFSET :offset
        """,
        dict(query_params, limit=limit, offset=offset),
    )

    items = [_shape_discrepancy_row(dict(row), types) for row in rows]
    return items, total, counts_by_type


def _shape_discrepancy_row(row: dict, types: Sequence[str]) -> dict:
    requested = set(types)
    present = [
        public
        for flag, (public, _) in DISCREPANCY_FLAGS.items()
        if row.get(flag) and public in requested
    ]
    severity = "low"
    for public in present:
        for flag, (name, level) in DISCREPANCY_FLAGS.items():
            if name == public and SEVERITY_RANK[level] > SEVERITY_RANK[severity]:
                severity = level
    return {
        "transaction_id": row["transaction_id"],
        "merchant_id": row["merchant_id"],
        "merchant_name": row["merchant_name"],
        "amount": row["amount"],
        "currency": row["currency"],
        "status": row["status"],
        "payment_status": row["payment_status"],
        "settlement_status": row["settlement_status"],
        "discrepancy_types": present,
        "severity": severity,
        "initiated_at": row["initiated_at"],
        "processed_at": row["processed_at"],
        "failed_at": row["failed_at"],
        "settled_at": row["settled_at"],
        "last_event_at": row["last_event_at"],
        "event_count": row["event_count"],
        "duplicate_event_count": row["duplicate_event_count"],
        "age_hours": round(row["age_hours"] or 0.0, 2),
    }


def discrepancy_types_for_transaction(row: dict, cutoff: str) -> list[str]:
    """Recompute discrepancy types for a single already-fetched
    transaction row, used by the transaction detail endpoint."""
    found: list[str] = []
    settled = row.get("settled_at")
    failed = row.get("failed_at")
    processed = row.get("processed_at")
    initiated = row.get("initiated_at")

    if settled and failed:
        found.append("SETTLED_AFTER_FAILURE")
    if settled and not processed and not failed:
        found.append("SETTLED_WITHOUT_PROCESSING")
    if processed and not failed and not settled and processed < cutoff:
        found.append("PROCESSED_NOT_SETTLED")
    if initiated and not processed and not failed and not settled and initiated < cutoff:
        found.append("STUCK_IN_INITIATED")
    if processed and failed:
        found.append("CONFLICTING_PAYMENT_STATE")
    if row.get("has_amount_mismatch"):
        found.append("AMOUNT_MISMATCH")
    if row.get("has_merchant_mismatch"):
        found.append("MERCHANT_MISMATCH")
    if row.get("duplicate_event_count", 0) > 0:
        found.append("DUPLICATE_EVENTS")
    return found
