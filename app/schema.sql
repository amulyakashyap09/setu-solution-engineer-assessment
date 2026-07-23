-- =====================================================================
-- Payment reconciliation service - SQLite schema
--
-- Design notes
-- ------------
-- 1. `events` is an immutable, append-only log. `event_id` is the PRIMARY
--    KEY, which is what makes ingestion idempotent: a replayed event hits
--    the uniqueness constraint and is skipped instead of re-applied.
--
-- 2. `transactions` is a *projection* of the event log, maintained on
--    write so that list/summary queries never have to fold the log at
--    read time.
--
-- 3. The projection stores the FIRST timestamp observed for each event
--    type (initiated_at / processed_at / failed_at / settled_at) rather
--    than a mutable "current status" string. Folding with MIN() makes the
--    projection commutative: events may arrive out of order, be replayed,
--    or be backfilled and the resulting state is identical. There is no
--    "last write wins" race.
--
-- 4. Status is therefore never written by the application - it is derived
--    from those four columns by STORED generated columns, so the status
--    can never drift from the underlying event history, and can still be
--    indexed for fast filtering.
--
-- 5. Money is stored as INTEGER minor units (paise). Floating point is
--    not safe for currency; SUM() over INTEGER is exact. The REAL major
--    unit is exposed as a generated column purely for convenience.
-- =====================================================================

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id   TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS transactions (
    transaction_id       TEXT PRIMARY KEY,
    merchant_id          TEXT NOT NULL REFERENCES merchants(merchant_id),
    amount_minor         INTEGER NOT NULL CHECK (amount_minor >= 0),
    currency             TEXT NOT NULL,

    -- First observed occurrence of each lifecycle event (UTC ISO-8601).
    initiated_at         TEXT,
    processed_at         TEXT,
    failed_at            TEXT,
    settled_at           TEXT,

    -- Log bookkeeping.
    first_event_at       TEXT NOT NULL,
    last_event_at        TEXT NOT NULL,
    event_count          INTEGER NOT NULL DEFAULT 0,
    duplicate_event_count INTEGER NOT NULL DEFAULT 0,

    -- Data-quality flags raised at ingest when a later event contradicts
    -- what earlier events said about the same transaction.
    has_amount_mismatch   INTEGER NOT NULL DEFAULT 0 CHECK (has_amount_mismatch IN (0, 1)),
    has_merchant_mismatch INTEGER NOT NULL DEFAULT 0 CHECK (has_merchant_mismatch IN (0, 1)),

    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,

    amount REAL GENERATED ALWAYS AS (amount_minor / 100.0) STORED,

    -- Payment leg: what the payment rail said.
    payment_status TEXT GENERATED ALWAYS AS (
        CASE
            WHEN failed_at IS NOT NULL AND processed_at IS NOT NULL THEN 'conflicted'
            WHEN failed_at    IS NOT NULL THEN 'failed'
            WHEN processed_at IS NOT NULL THEN 'processed'
            ELSE 'initiated'
        END
    ) STORED,

    -- Settlement leg: what the settlement file said. Deliberately tracked
    -- as an independent axis - that separation is what makes a
    -- "settled but failed" case representable rather than overwritten.
    settlement_status TEXT GENERATED ALWAYS AS (
        CASE WHEN settled_at IS NOT NULL THEN 'settled' ELSE 'pending' END
    ) STORED,

    -- Single rolled-up status for ops filtering. A failure is considered
    -- terminal and outranks a settlement, so a settled-after-failure
    -- transaction reads as 'failed' here and is reported as a
    -- discrepancy rather than being silently counted as good money.
    status TEXT GENERATED ALWAYS AS (
        CASE
            WHEN failed_at    IS NOT NULL THEN 'failed'
            WHEN settled_at   IS NOT NULL THEN 'settled'
            WHEN processed_at IS NOT NULL THEN 'processed'
            ELSE 'initiated'
        END
    ) STORED,

    -- Business date of the transaction (date of its first event), stored
    -- so that GROUP BY date is an index scan rather than a per-row
    -- substr() over the whole table.
    txn_date TEXT GENERATED ALWAYS AS (substr(first_event_at, 1, 10)) STORED
);


CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    merchant_id    TEXT NOT NULL,
    event_type     TEXT NOT NULL CHECK (
        event_type IN ('payment_initiated', 'payment_processed', 'payment_failed', 'settled')
    ),
    amount_minor   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,   -- event timestamp, as reported by the source
    received_at    TEXT NOT NULL,   -- when we ingested it
    raw_payload    TEXT             -- original JSON, for audit / replay
);


-- ---------------------------------------------------------------------
-- Indexes. Each one is here to serve a specific query in the API.
-- ---------------------------------------------------------------------

-- GET /transactions?merchant_id=&date_from=&date_to=  (+ default sort)
CREATE INDEX IF NOT EXISTS idx_txn_merchant_date
    ON transactions (merchant_id, first_event_at DESC);

-- GET /transactions?status=&date_from=&date_to=
CREATE INDEX IF NOT EXISTS idx_txn_status_date
    ON transactions (status, first_event_at DESC);

-- Unfiltered listing sorted by recency, and date-range scans.
CREATE INDEX IF NOT EXISTS idx_txn_first_event_at
    ON transactions (first_event_at DESC);

-- GET /reconciliation/summary?group_by=merchant,date
CREATE INDEX IF NOT EXISTS idx_txn_date_merchant
    ON transactions (txn_date, merchant_id);

-- GET /reconciliation/discrepancies - the settlement-vs-payment scan.
CREATE INDEX IF NOT EXISTS idx_txn_recon
    ON transactions (settlement_status, payment_status, first_event_at);

-- Partial indexes: the discrepancy queries are all "column IS NOT NULL"
-- probes over a small minority of rows, so partial indexes keep them tiny.
CREATE INDEX IF NOT EXISTS idx_txn_settled_at
    ON transactions (settled_at) WHERE settled_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_txn_processed_unsettled
    ON transactions (processed_at) WHERE settled_at IS NULL AND failed_at IS NULL;

-- GET /transactions/{id} - event history, already in chronological order.
CREATE INDEX IF NOT EXISTS idx_events_txn_time
    ON events (transaction_id, occurred_at);

-- Merchant-scoped event feeds / audit.
CREATE INDEX IF NOT EXISTS idx_events_merchant_time
    ON events (merchant_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_type_time
    ON events (event_type, occurred_at DESC);
