"""Request and response models.

Validation lives here so that malformed input is rejected at the edge
with a 422 and a precise field path, before any SQL is issued.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timeutils import TimestampError, normalise_ts, to_minor_units

# One paisa short of 1e13 rupees. Guards against overflow and typos like
# an amount accidentally sent in minor units.
MAX_AMOUNT = 1_000_000_000.0


class EventType(str, Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class TransactionStatus(str, Enum):
    initiated = "initiated"
    processed = "processed"
    failed = "failed"
    settled = "settled"


class PaymentStatus(str, Enum):
    initiated = "initiated"
    processed = "processed"
    failed = "failed"
    conflicted = "conflicted"


class SettlementStatus(str, Enum):
    pending = "pending"
    settled = "settled"


class DiscrepancyType(str, Enum):
    settled_after_failure = "SETTLED_AFTER_FAILURE"
    settled_without_processing = "SETTLED_WITHOUT_PROCESSING"
    processed_not_settled = "PROCESSED_NOT_SETTLED"
    stuck_in_initiated = "STUCK_IN_INITIATED"
    conflicting_payment_state = "CONFLICTING_PAYMENT_STATE"
    amount_mismatch = "AMOUNT_MISMATCH"
    merchant_mismatch = "MERCHANT_MISMATCH"
    duplicate_events = "DUPLICATE_EVENTS"


# ---------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------

class EventIn(BaseModel):
    """A single inbound payment lifecycle event.

    `extra="allow"` is deliberate: upstream systems add fields over time,
    and rejecting an event because it carried an unrecognised key would
    drop real money data. Unknown fields are preserved in `raw_payload`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    event_id: str = Field(min_length=1, max_length=128)
    event_type: EventType
    transaction_id: str = Field(min_length=1, max_length=128)
    merchant_id: str = Field(min_length=1, max_length=128)
    merchant_name: str | None = Field(default=None, max_length=256)
    amount: float = Field(gt=0, le=MAX_AMOUNT)
    currency: str = Field(min_length=3, max_length=3)
    timestamp: str

    @field_validator("event_id", "transaction_id", "merchant_id")
    @classmethod
    def _strip_identifier(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if not code.isalpha():
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        return code

    @field_validator("timestamp")
    @classmethod
    def _normalise_timestamp(cls, value: Any) -> str:
        try:
            return normalise_ts(value)
        except TimestampError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, value: float) -> float:
        try:
            minor = to_minor_units(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if minor <= 0:
            raise ValueError("amount must be greater than zero")
        return value


class EventBatch(BaseModel):
    events: list[EventIn] = Field(min_length=1)


class IngestResult(BaseModel):
    event_id: str
    transaction_id: str
    status: Literal["created", "duplicate"]
    # True when a replayed event_id carried a payload that differs from the
    # one already stored - the replay is still ignored, but it is surfaced.
    payload_conflict: bool = False
    message: str | None = None


class IngestResponse(BaseModel):
    received: int
    created: int
    duplicates: int
    transactions_affected: int
    results: list[IngestResult]


# ---------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------

class MerchantOut(BaseModel):
    merchant_id: str
    merchant_name: str


class TransactionOut(BaseModel):
    transaction_id: str
    merchant_id: str
    merchant_name: str | None = None
    amount: float
    currency: str
    status: TransactionStatus
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    initiated_at: str | None = None
    processed_at: str | None = None
    failed_at: str | None = None
    settled_at: str | None = None
    first_event_at: str
    last_event_at: str
    event_count: int
    duplicate_event_count: int
    txn_date: str


class EventOut(BaseModel):
    event_id: str
    event_type: EventType
    transaction_id: str
    merchant_id: str
    amount: float
    currency: str
    occurred_at: str
    received_at: str


class TransactionDetail(TransactionOut):
    merchant: MerchantOut
    discrepancies: list[DiscrepancyType] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)


class PageMeta(BaseModel):
    limit: int
    offset: int
    total: int
    returned: int
    has_more: bool


class TransactionListResponse(BaseModel):
    pagination: PageMeta
    items: list[TransactionOut]


# ---------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------

class SummaryTotals(BaseModel):
    transaction_count: int
    total_amount: float
    initiated_count: int
    processed_count: int
    failed_count: int
    settled_count: int
    settled_amount: float
    failed_amount: float
    unsettled_amount: float
    discrepancy_count: int
    settlement_rate: float


class SummaryRow(SummaryTotals):
    merchant_id: str | None = None
    merchant_name: str | None = None
    date: str | None = None
    status: TransactionStatus | None = None
    currency: str | None = None


class SummaryResponse(BaseModel):
    group_by: list[str]
    filters: dict[str, Any]
    totals: SummaryTotals
    groups: list[SummaryRow]


class DiscrepancyOut(BaseModel):
    transaction_id: str
    merchant_id: str
    merchant_name: str | None = None
    amount: float
    currency: str
    status: TransactionStatus
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    discrepancy_types: list[DiscrepancyType]
    severity: Literal["high", "medium", "low"]
    initiated_at: str | None = None
    processed_at: str | None = None
    failed_at: str | None = None
    settled_at: str | None = None
    last_event_at: str
    event_count: int
    duplicate_event_count: int
    age_hours: float


class DiscrepancyResponse(BaseModel):
    pagination: PageMeta
    as_of: str
    stale_after_hours: int
    counts_by_type: dict[str, int]
    items: list[DiscrepancyOut]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: str
    events: int
    transactions: int
    merchants: int
