# Payment Reconciliation Service

A backend service that ingests payment lifecycle events, maintains transaction and
settlement state, and reports discrepancies between the payment leg and the
settlement leg.

**Python 3.12 · FastAPI · SQLite**

- Live demo: `https://setu-solution-engineer-assessment.onrender.com`
- Interactive API docs: `https://setu-solution-engineer-assessment.onrender.com/docs`
- Postman collection: [`postman_collection.json`](./postman_collection.json)

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Data model](#data-model)
- [API reference](#api-reference)
- [Testing](#testing)
- [Deployment](#deployment)
- [Assumptions and tradeoffs](#assumptions-and-tradeoffs)
- [AI tool disclosure](#ai-tool-disclosure)

---

## Quick start

Three commands, no database server to install:

```bash
pip install -r requirements-dev.txt
python -m scripts.seed --file data/sample_events.json --database data/payments.db
uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000/docs>.

If you skip the seed step the service seeds itself on first boot: when the events
table is empty and `data/sample_events.json` is present, it loads the file
automatically. Set `SEED_ON_STARTUP=false` to disable.

Verify it came up:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"1.0.0","events":10165,"transactions":3800,"merchants":5}
```

With Docker instead:

```bash
docker build -t payment-reconciliation .
docker run --rm -p 8000:8000 payment-reconciliation
```

Or via the Makefile: `make install`, `make seed`, `make run`, `make test`.

---

## Architecture

### The core idea

Two tables, and the relationship between them is the whole design:

```
POST /events
     │
     ▼
┌─────────────────────┐         ┌──────────────────────────────┐
│ events              │         │ transactions                 │
│ append-only log     │────────▶│ projection of the log        │
│ PK: event_id        │ on write│ PK: transaction_id           │
│ never updated       │         │ status = generated column    │
└─────────────────────┘         └──────────────────────────────┘
     │                                     │
     │ GET /events (audit)                 │ GET /transactions
     │ GET /transactions/{id}              │ GET /reconciliation/*
     ▼                                     ▼
```

`events` is immutable and append-only. `transactions` is a **projection** of that
log, maintained at write time so that reporting queries never fold the log at read
time.

### Idempotency

Three layers, in order of how much they are relied on:

1. **`events.event_id` is the PRIMARY KEY.** A replayed event cannot be stored
   twice. This is a database constraint, not an application convention — it holds
   even if the application logic above it has a bug.

2. **The projection folds with `MIN()`, it does not overwrite.** Instead of a
   mutable `status` column, `transactions` stores the first observed timestamp of
   each event type: `initiated_at`, `processed_at`, `failed_at`, `settled_at`.
   Each incoming event folds into its column with `MIN()`:

   ```sql
   initiated_at = MIN(COALESCE(transactions.initiated_at, excluded.initiated_at),
                      COALESCE(excluded.initiated_at, transactions.initiated_at))
   ```

   `MIN` is idempotent (`min(a, a) = a`) and commutative (`min(a, b) = min(b, a)`).
   So replaying an event, receiving events out of order, or backfilling history all
   converge on the same state. There is no last-write-wins race to lose.

   The `COALESCE` on both sides is load-bearing: SQLite's scalar `MIN()` returns
   `NULL` if *any* argument is `NULL`, so a naive `MIN(existing, incoming)` would
   erase a known timestamp the first time an event of a different type arrived.

3. **Each batch runs inside one `BEGIN IMMEDIATE`.** SQLite takes the write lock up
   front, which serialises concurrent writers and makes the check-then-insert safe.
   A batch is all-or-nothing.

Verified end to end: re-ingesting all 10,355 records from `sample_events.json`
produces an identical SHA-256 fingerprint of every transaction row, and the event
count stays at 10,165. See `tests/test_sample_data.py::test_reingesting_the_whole_file_changes_nothing`.

### Status is derived, never written

The application never writes a status. All three status columns are SQLite
**`STORED` generated columns** computed from the four timestamps:

```sql
status TEXT GENERATED ALWAYS AS (
    CASE WHEN failed_at    IS NOT NULL THEN 'failed'
         WHEN settled_at   IS NOT NULL THEN 'settled'
         WHEN processed_at IS NOT NULL THEN 'processed'
         ELSE 'initiated' END
) STORED
```

Two consequences worth the choice:

- Status **cannot drift** from the event history. There is no code path that could
  set a status the events do not support.
- Because they are `STORED` rather than `VIRTUAL`, they are **indexable**, so
  filtering on status is still an index seek.

### Two status axes, not one

The payment leg and the settlement leg are tracked independently:

| Column | Values |
|---|---|
| `payment_status` | `initiated`, `processed`, `failed`, `conflicted` |
| `settlement_status` | `pending`, `settled` |
| `status` (rolled up, for ops filtering) | `initiated`, `processed`, `failed`, `settled` |

This separation is the point. A single mutable status field would let a late
`settled` event overwrite a `failed` payment, and the discrepancy would silently
disappear — which is exactly the bug the assignment asks the service to detect. With
two axes, `payment_status = failed` alongside `settlement_status = settled` is a
representable, queryable state.

In the rolled-up `status`, **failure outranks settlement**, so a settled-after-failure
transaction reads as `failed` and is reported as a discrepancy rather than being
counted as good money. The sample data contains 95 of these.

### Request flow

Route handlers are declared as sync `def`, so FastAPI runs them in a worker
threadpool. That is the correct pairing for the stdlib `sqlite3` driver, which is
blocking — declaring them `async` would stall the event loop on every query.

### Layout

```
app/
  main.py           FastAPI app, lifespan, startup seeding
  config.py         environment-driven settings
  db.py             connection handling, WAL, pragmas
  schema.sql        tables, generated columns, indexes
  schemas.py        Pydantic request/response models and validation
  ingest.py         idempotent ingestion and the projection fold
  queries.py        all read-side SQL
  errors.py         one error envelope for every failure mode
  routers/          events, transactions, reconciliation, meta
scripts/seed.py     bulk loader
tests/              211 tests
```

---

## Data model

```sql
merchants(merchant_id PK, merchant_name, created_at, updated_at)

transactions(
    transaction_id PK,
    merchant_id FK -> merchants,
    amount_minor INTEGER,          -- canonical money, exact
    currency,
    initiated_at, processed_at, failed_at, settled_at,   -- first occurrence of each
    first_event_at, last_event_at,
    event_count, duplicate_event_count,
    has_amount_mismatch, has_merchant_mismatch,          -- data-quality flags
    amount            GENERATED (amount_minor / 100.0)  STORED,
    payment_status    GENERATED ...                     STORED,
    settlement_status GENERATED ...                     STORED,
    status            GENERATED ...                     STORED,
    txn_date          GENERATED (substr(first_event_at,1,10)) STORED
)

events(
    event_id PK,                   -- the idempotency key
    transaction_id FK -> transactions,
    merchant_id, event_type, amount_minor, currency,
    occurred_at,                   -- when it happened, per the source
    received_at,                   -- when we ingested it
    raw_payload                    -- original JSON, for audit and replay
)
```

### Money is stored as integers

`amount_minor` holds paise, and conversion goes through `Decimal`, not float:

```python
round(8.115 * 100)                      # 811  ← wrong
int(Decimal("8.115") * 100)             # 812  ← correct
```

Binary floats cannot represent most 2-decimal values exactly, and the error
compounds under `SUM()`. All aggregation happens over `INTEGER`, so reported totals
are exact. `amount` is exposed as a generated `REAL` purely for API convenience.
`tests/test_units.py` pins the known float traps (`8.115`, `1.005`, `2.675`) and
asserts that 1,000 transactions of ₹0.01 total exactly ₹10.00.

### Timestamps

Stored as fixed-width UTC ISO-8601 strings: `2026-01-08T12:11:58.085567+00:00`.

SQLite has no native datetime type, so range filters and `ORDER BY` are
lexicographic string comparisons. Those only equal chronological comparisons if
every stored value has identical width and timezone — which is why the code uses an
explicit `strftime` format rather than `datetime.isoformat()`, since the latter
silently drops the microsecond component when it is zero.

Inputs are accepted in any ISO-8601 form (`Z`, explicit offset, or naive treated as
UTC) and normalised on the way in.

### Indexes

Every index exists to serve a specific endpoint:

| Index | Serves |
|---|---|
| `idx_txn_merchant_date (merchant_id, first_event_at DESC)` | `GET /transactions?merchant_id=&date_from=` |
| `idx_txn_status_date (status, first_event_at DESC)` | `GET /transactions?status=` |
| `idx_txn_first_event_at (first_event_at DESC)` | default listing and date scans |
| `idx_txn_date_merchant (txn_date, merchant_id)` | `GET /reconciliation/summary?group_by=merchant,date` |
| `idx_txn_recon (settlement_status, payment_status, first_event_at)` | discrepancy scan |
| `idx_txn_settled_at` **partial** `WHERE settled_at IS NOT NULL` | settlement checks |
| `idx_txn_processed_unsettled` **partial** `WHERE settled_at IS NULL AND failed_at IS NULL` | unsettled backlog |
| `idx_events_txn_time (transaction_id, occurred_at)` | event history, pre-sorted |
| `idx_events_merchant_time`, `idx_events_type_time` | event log filters |

The two partial indexes matter: the discrepancy queries are `IS NOT NULL` probes
over a small minority of rows, so a partial index stays tiny and stays in cache.

Confirmed with `EXPLAIN QUERY PLAN` — no query in the API does a full table scan:

```
list by merchant+date   SEARCH t USING INDEX idx_txn_merchant_date (merchant_id=? AND first_event_at>?)
list by status          SEARCH t USING INDEX idx_txn_status_date (status=?)
group by date           SCAN t USING INDEX idx_txn_date_merchant
event history           SEARCH events USING INDEX idx_events_txn_time (transaction_id=?)
processed unsettled     SEARCH t USING INDEX idx_txn_processed_unsettled (processed_at>?)
```

---

## API reference

Full interactive documentation at `/docs`. Every error, from any layer, uses one
envelope:

```json
{ "error": { "code": "validation_error", "message": "...", "details": [ ... ] } }
```

### `POST /events`

Accepts a single event, a bare array, or `{"events": [...]}`.

```bash
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
    "event_type": "payment_initiated",
    "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "merchant_id": "merchant_2",
    "merchant_name": "FreshBasket",
    "amount": 15248.29,
    "currency": "INR",
    "timestamp": "2026-01-08T12:11:58.085567+00:00"
  }'
```

```json
{
  "received": 1, "created": 1, "duplicates": 0, "transactions_affected": 1,
  "results": [
    { "event_id": "b768e3a7-...", "transaction_id": "2f86e94c-...",
      "status": "created", "payload_conflict": false, "message": null }
  ]
}
```

- **`201`** when at least one event was created; **`200`** when every event was a
  duplicate replay. Callers can tell the difference without parsing the body.
- Replaying an event returns `status: "duplicate"` and changes nothing.
- If a replayed `event_id` arrives with a *different* payload, the stored event
  wins and `payload_conflict: true` is returned — a redelivery is never allowed to
  rewrite history silently.
- Unknown extra fields are accepted and preserved in `raw_payload`. Dropping a real
  payment event because it carried an unrecognised key would be worse than storing it.
- `413` above `MAX_BATCH_SIZE` (default 5,000).

### `GET /transactions`

| Parameter | Notes |
|---|---|
| `merchant_id` | repeatable |
| `status` | `initiated` `processed` `failed` `settled`, repeatable |
| `payment_status` | `initiated` `processed` `failed` `conflicted` |
| `settlement_status` | `pending` `settled` |
| `currency` | |
| `date_from`, `date_to` | inclusive; `YYYY-MM-DD` covers the whole day, or full ISO-8601 |
| `date_field` | which timestamp the range applies to (default `first_event_at`) |
| `min_amount`, `max_amount` | compared in exact minor units |
| `has_duplicates` | transactions that did or did not receive replays |
| `sort_by`, `sort_order` | whitelisted column + `asc`/`desc` |
| `limit`, `offset` | default 50, max 500 |

```json
{
  "pagination": { "limit": 50, "offset": 0, "total": 3800, "returned": 50, "has_more": true },
  "items": [ { "transaction_id": "...", "status": "settled", "payment_status": "processed",
               "settlement_status": "settled", "amount": 15248.29, ... } ]
}
```

`transaction_id` is always appended as a sort tiebreaker, so paging over a
non-unique key (status, merchant) cannot repeat or skip rows.

### `GET /transactions/{transaction_id}`

Returns transaction state, both status axes, the merchant, any discrepancies, and
the full chronological event history. `404` with the standard envelope if unknown.

### `GET /reconciliation/summary`

`group_by` accepts a comma-separated list of `merchant`, `date`, `status`,
`currency` — e.g. `group_by=merchant,date` gives a per-merchant-per-day position.
An empty `group_by` returns totals only.

Each group and the grand total carry: `transaction_count`, `total_amount`,
per-status counts, `settled_amount`, `failed_amount`, `unsettled_amount`,
`discrepancy_count`, `settlement_rate`.

Everything is one `GROUP BY` with conditional aggregation. Totals always cover the
whole filtered set, never just the returned page.

### `GET /reconciliation/discrepancies`

| Type | Meaning | Severity |
|---|---|---|
| `SETTLED_AFTER_FAILURE` | money settled against a payment the rail reported as failed | high |
| `CONFLICTING_PAYMENT_STATE` | both a processed and a failed event exist | high |
| `AMOUNT_MISMATCH` | events for one transaction disagreed on the amount | high |
| `MERCHANT_MISMATCH` | events for one transaction disagreed on the merchant | high |
| `SETTLED_WITHOUT_PROCESSING` | settled with no processed event ever seen | medium |
| `PROCESSED_NOT_SETTLED` | processed, still unsettled past the staleness window | medium |
| `STUCK_IN_INITIATED` | initiated and never progressed past the window | low |
| `DUPLICATE_EVENTS` | replayed events received (audit signal, opt-in) | low |

Filters: `type` (repeatable), `merchant_id`, `severity`, `date_from`/`date_to`,
`stale_after_hours` (default 48), `as_of`, `sort_by`, `sort_order`, `limit`, `offset`.

`as_of` re-runs the report as it would have looked at a past instant, which makes
the output reproducible rather than dependent on wall-clock time.

`counts_by_type` is computed over the whole filtered population, not the current
page, so the header numbers stay meaningful while paging.

`DUPLICATE_EVENTS` is excluded by default. A redelivered event is absorbed by design
and is not a money problem; including it would make the report noisier without
making it more actionable. Request it explicitly with `?type=DUPLICATE_EVENTS`.

### `GET /events`, `GET /merchants`, `GET /health`

The raw audit log with filters and pagination; merchants with volume; liveness with
row counts.

---

## Sample data

`data/sample_events.json` is the file supplied with the assignment, used unmodified.
Independently profiled before any code was written:

| | |
|---|---|
| Records in file | 10,355 |
| Unique `event_id`s | 10,165 |
| **Exact duplicate records** | **190** |
| Transactions | 3,800 |
| Merchants | 5 |
| Date range | 2026-01-08 → 2026-04-08 |

Lifecycle shapes present:

| Shape | Count | Resulting state |
|---|---|---|
| initiated → processed → settled | 2,565 | clean `settled` |
| initiated → failed | 570 | clean `failed` |
| initiated → processed | 380 | `PROCESSED_NOT_SETTLED` |
| initiated only | 190 | `STUCK_IN_INITIATED` |
| **initiated → failed → settled** | **95** | **`SETTLED_AFTER_FAILURE`** |

After ingestion the service reports exactly 2,565 settled / 665 failed / 380
processed / 190 initiated, and 665 discrepancies (95 + 380 + 190). These numbers are
asserted as ground truth in `tests/test_sample_data.py`.

Seeding all 10,355 records takes **~1.0s**.

---

## Testing

```bash
pytest                    # 211 tests, ~11s
pytest -v
pytest tests/test_idempotency.py
```

Each test gets its own SQLite file under `tmp_path`, so tests are isolated and
order-independent. Nothing is mocked — the behaviour under test (idempotency,
generated columns, SQL aggregation) lives in the database, so the tests exercise the
real one.

| File | Tests | Covers |
|---|---|---|
| `test_idempotency.py` | 18 | replays, in-batch duplicates, conflicting payloads, **all 6 permutations of event arrival order**, batch atomicity, concurrent submission |
| `test_validation.py` | 55 | every required field, bad enums, amount and currency rules, timestamp forms, query-param bounds, **SQL-injection attempts on sort and filter params**, error envelope |
| `test_transactions.py` | 36 | every filter, multi-value filters, date boundaries, sorting, null ordering, pagination invariants, detail and event history |
| `test_reconciliation.py` | 36 | every group-by dimension, totals reconciliation, **every discrepancy type**, staleness window, `as_of`, severity, counts-vs-page |
| `test_units.py` | 36 | money precision traps, exact summation, timestamp normalisation and ordering |
| `test_meta.py` | 13 | health, merchants, raw event log, OpenAPI |
| `test_sample_data.py` | 17 | the real 10k dataset: exact counts, full-file replay fingerprint, performance guard rails |

Cases worth calling out:

- **Order independence** is parametrised over all six permutations of
  `initiated`/`processed`/`settled` and asserts identical final state.
- **Concurrency**: eight threads submit the same event simultaneously; exactly one
  is created and seven are reported as duplicates.
- **Batch atomicity**: a failure injected mid-batch leaves zero rows behind.
- **Injection**: `sort_by=first_event_at; DROP TABLE transactions;--` returns `422`
  and the table is still there; filter values containing `' OR 1=1 --` are bound
  parameters and match nothing.
- **Full-file replay**: all 10,355 records re-ingested, transaction fingerprint
  unchanged.

All 40 requests in the Postman collection were executed against a running instance
loaded with the full dataset; the slowest reporting query returned in 13ms.

---

## Deployment

The image runs as a non-root user, exposes `/health` for platform health checks, and
seeds itself on first boot.

**Render** — push the repo, then New → Blueprint. [`render.yaml`](./render.yaml) is
included. On the free plan, remove the `disk:` block (disks are paid); the service
will re-seed from `sample_events.json` on each cold start, which is fine for a demo.

**Fly.io** — [`fly.toml`](./fly.toml) is included with a mounted volume:

```bash
fly launch --no-deploy
fly volumes create sqlite_data --size 1
fly deploy
```

**Any Docker host**

```bash
docker build -t payment-reconciliation .
docker run -d -p 8000:8000 -v $(pwd)/data:/app/data payment-reconciliation
```

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_PATH` | `./data/payments.db` | SQLite file location |
| `SEED_ON_STARTUP` | `true` | load the sample file when the DB is empty |
| `SEED_FILE` | `./data/sample_events.json` | seed source |
| `MAX_BATCH_SIZE` | `5000` | largest accepted `POST /events` batch |
| `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE` | `50` / `500` | pagination guard rails |
| `STALE_AFTER_HOURS` | `48` | default reconciliation staleness window |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | lock wait before erroring |

The container runs a **single uvicorn worker on purpose**. SQLite is a single-writer
database; WAL lets readers proceed during a write, but adding worker processes does
not add write throughput. The point at which one worker is not enough is the point
at which you move to Postgres, not the point at which you add workers.

---

## Assumptions and tradeoffs

### Assumptions

- **`event_id` is globally unique and is the idempotency key.** The assignment's
  sample data supports this: all 190 duplicate records are byte-identical replays.
- **The first observed timestamp of an event type is authoritative.** If two
  distinct `payment_processed` events arrive for one transaction, the earlier one is
  the real transition; the later is a retry artefact. Both are kept in the log.
- **A settled event after a failed event is a discrepancy, not a correction.** The
  service does not assume settlement retroactively repairs a failed payment; it
  flags the pair for a human. Reversing that assumption is a one-line change to the
  `status` generated column.
- **Amounts are immutable per transaction.** The first amount seen is the
  transaction's amount; a later disagreement raises `AMOUNT_MISMATCH` rather than
  overwriting.
- **All timestamps are convertible to UTC**, and events may arrive arbitrarily late
  or out of order.
- **Merchants are discovered from events** rather than pre-registered, since the
  sample data has no separate merchant feed. The latest `merchant_name` wins.

### Tradeoffs

**SQLite over Postgres.** The assignment allows any SQL database and weights
reviewer setup time heavily. SQLite means zero setup, a reproducible demo, and one
fewer moving part in deployment. The cost is real: single writer, no network
concurrency, no native `NUMERIC` or `TIMESTAMPTZ`. The schema is deliberately
portable — generated columns, partial indexes and `ON CONFLICT` all exist in
Postgres — so migrating is mostly a driver swap plus `TEXT` → `TIMESTAMPTZ` and
`INTEGER` → `BIGINT`.

**Write-time projection over read-time aggregation.** Folding the event log on every
read would be simpler and always correct by construction, but it makes every list
and summary O(events). Projecting on write costs a little ingest latency and adds
the obligation to keep the projection correct — which is why the fold is
commutative and idempotent, and why status is generated rather than assigned.

**Offset pagination.** Simple, and it gives clients a `total`, which ops dashboards
want. It degrades on deep pages (`OFFSET 100000` still walks 100,000 rows). At that
scale I would add keyset pagination on `(first_event_at, transaction_id)` and keep
offset only for the UI's page-number control.

**Denormalised `merchant_id` on `events`.** Redundant with `transactions`, but it
lets the audit log be filtered by merchant without a join, and preserves what the
event actually claimed — which is what makes `MERCHANT_MISMATCH` detectable.

**`raw_payload` stored per event.** Roughly doubles the events table size. Worth it:
without the original payload, an ingestion bug is unrecoverable and there is nothing
to replay from.

**Staleness is a request parameter, not a constant.** `PROCESSED_NOT_SETTLED` is
inherently time-relative, and different merchants have different settlement SLAs. A
hardcoded threshold would make the report wrong for someone. Hence
`stale_after_hours` plus `as_of` for reproducible point-in-time runs.

**Duplicates bump a counter.** `duplicate_event_count` is the one thing a replay
mutates. It is an audit signal — noisy upstream redelivery is worth seeing — and it
touches no state that affects reconciliation.

### What I would do next, with more time

1. **Postgres plus Alembic migrations.** The current schema is created with
   `CREATE TABLE IF NOT EXISTS` and has no migration path — fine for a take-home,
   not for anything that accumulates real data.
2. **Keyset pagination** on the transaction list, for deep pages.
3. **Per-merchant settlement SLAs** stored on the merchant row, so the staleness
   window is per-merchant instead of global.
4. **Structured JSON logging with a request id**, and metrics on ingest lag and
   discrepancy counts.
5. **Authentication.** There is none. Every endpoint is public, which is correct for
   a reviewable demo and wrong for anything else.
6. **A reconciliation run table** — persist each report execution so trends over
   time are queryable, rather than recomputing on demand.
7. **Load testing at 10M events** to find where the projection approach stops
   holding up.

---

## AI tool disclosure

I used **Claude (Anthropic)** while building this, in these ways:

- **Data profiling.** Before writing any code, I had it analyse
  `sample_events.json` to establish ground truth — duplicate counts, lifecycle
  shapes, and the 95 settled-after-failure cases. Those numbers became the assertions
  in `tests/test_sample_data.py`.
- **Schema and code drafting.** The generated-column approach, the `MIN()`/`COALESCE`
  fold, and the index set were worked out in discussion and then drafted with its
  help.
- **Test suite generation.** Most of the 211 test cases were drafted with Claude,
  then reviewed and corrected by me. Two failures during development were bugs in the
  test helpers rather than the application, which I fixed.
- **README and Postman collection.**

Everything was reviewed, executed, and verified by me: the full suite passes, the
`EXPLAIN QUERY PLAN` output was checked by hand, and all 40 Postman requests were run
against a live instance loaded with the full dataset.
