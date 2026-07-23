"""End-to-end tests against the real `sample_events.json`.

These assert on the actual shape of the provided dataset, so they catch
regressions that a hand-built fixture would not: 10,355 records
containing 190 exact duplicates, 3,800 transactions across 5 merchants,
and every discrepancy scenario the assignment describes.
"""

from __future__ import annotations

import time

import pytest

from app.db import connect
from app.ingest import ingest_events
from app.schemas import EventIn

# Ground truth, derived independently from the raw JSON file.
TOTAL_RECORDS = 10_355
UNIQUE_EVENTS = 10_165
DUPLICATE_RECORDS = 190
TRANSACTIONS = 3_800
MERCHANTS = 5

EXPECTED_STATUS_COUNTS = {
    "settled": 2_565,     # initiated -> processed -> settled
    "failed": 665,        # 570 clean failures + 95 settled-after-failure
    "processed": 380,     # processed, never settled
    "initiated": 190,     # never progressed
}

SETTLED_AFTER_FAILURE = 95
PROCESSED_NOT_SETTLED = 380
STUCK_IN_INITIATED = 190


def test_sample_file_matches_expected_shape(sample_events):
    assert len(sample_events) == TOTAL_RECORDS
    assert len({event["event_id"] for event in sample_events}) == UNIQUE_EVENTS
    assert len({event["transaction_id"] for event in sample_events}) == TRANSACTIONS
    assert len({event["merchant_id"] for event in sample_events}) == MERCHANTS


def test_full_dataset_ingests_with_expected_counts(seeded_client):
    body = seeded_client.get("/health").json()
    assert body["events"] == UNIQUE_EVENTS
    assert body["transactions"] == TRANSACTIONS
    assert body["merchants"] == MERCHANTS


def test_duplicates_in_the_source_file_were_absorbed(seeded_client):
    """The file contains 190 repeated event_ids. None became a second row."""
    body = seeded_client.get(
        "/reconciliation/discrepancies?type=DUPLICATE_EVENTS&limit=1").json()
    assert body["counts_by_type"]["DUPLICATE_EVENTS"] == DUPLICATE_RECORDS


def test_status_distribution(seeded_client):
    groups = seeded_client.get(
        "/reconciliation/summary?group_by=status").json()["groups"]
    counts = {group["status"]: group["transaction_count"] for group in groups}
    assert counts == EXPECTED_STATUS_COUNTS


def test_discrepancy_counts_match_the_scenarios_in_the_data(seeded_client):
    body = seeded_client.get("/reconciliation/discrepancies?limit=1").json()
    assert body["counts_by_type"]["SETTLED_AFTER_FAILURE"] == SETTLED_AFTER_FAILURE
    assert body["counts_by_type"]["PROCESSED_NOT_SETTLED"] == PROCESSED_NOT_SETTLED
    assert body["counts_by_type"]["STUCK_IN_INITIATED"] == STUCK_IN_INITIATED
    assert body["pagination"]["total"] == (
        SETTLED_AFTER_FAILURE + PROCESSED_NOT_SETTLED + STUCK_IN_INITIATED)


def test_grand_total_reconciles_across_every_grouping(seeded_client):
    """Whatever dimension you slice by, the parts must sum to the whole."""
    reference = seeded_client.get(
        "/reconciliation/summary?group_by=").json()["totals"]

    for group_by in ("merchant", "status", "currency", "merchant,status"):
        body = seeded_client.get(
            f"/reconciliation/summary?group_by={group_by}&limit=500").json()
        assert sum(g["transaction_count"] for g in body["groups"]) == \
            reference["transaction_count"], group_by
        assert sum(g["total_amount"] for g in body["groups"]) == \
            pytest.approx(reference["total_amount"]), group_by


def test_every_transaction_is_reachable_by_paging(seeded_client):
    seen: set[str] = set()
    offset = 0
    while True:
        page = seeded_client.get(
            f"/transactions?limit=500&offset={offset}&sort_by=status").json()
        seen.update(item["transaction_id"] for item in page["items"])
        if not page["pagination"]["has_more"]:
            break
        offset += 500

    assert len(seen) == TRANSACTIONS


def test_settled_after_failure_transactions_look_right(seeded_client):
    body = seeded_client.get(
        "/reconciliation/discrepancies?type=SETTLED_AFTER_FAILURE&limit=5").json()

    for item in body["items"]:
        assert item["failed_at"] is not None
        assert item["settled_at"] is not None
        assert item["settlement_status"] == "settled"
        assert item["payment_status"] == "failed"
        assert item["severity"] == "high"

        detail = seeded_client.get(f"/transactions/{item['transaction_id']}").json()
        types = [event["event_type"] for event in detail["events"]]
        assert "payment_failed" in types and "settled" in types


def test_date_filtering_narrows_the_dataset(seeded_client):
    everything = seeded_client.get(
        "/reconciliation/summary?group_by=").json()["totals"]["transaction_count"]
    january = seeded_client.get(
        "/reconciliation/summary?group_by=&date_from=2026-01-01&date_to=2026-01-31"
    ).json()["totals"]["transaction_count"]

    assert 0 < january < everything


def test_merchant_filter_partitions_the_dataset(seeded_client):
    merchants = seeded_client.get("/merchants").json()["items"]
    total = sum(
        seeded_client.get(
            f"/reconciliation/summary?group_by=&merchant_id={merchant['merchant_id']}"
        ).json()["totals"]["transaction_count"]
        for merchant in merchants
    )
    assert total == TRANSACTIONS


def test_reingesting_the_whole_file_changes_nothing(seeded_db_file, sample_events):
    """The strongest idempotency assertion available: replay all 10,355
    records and fingerprint the entire projection before and after."""
    connection = connect(seeded_db_file)
    fields = (
        "transaction_id, status, payment_status, settlement_status, "
        "initiated_at, processed_at, failed_at, settled_at, amount_minor, event_count"
    )
    query = f"SELECT {fields} FROM transactions ORDER BY transaction_id"

    before = [tuple(row) for row in connection.execute(query)]
    event_count_before = connection.execute(
        "SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    parsed = [EventIn.model_validate(record) for record in sample_events]
    for start in range(0, len(parsed), 2000):
        results, _ = ingest_events(connection, parsed[start:start + 2000])
        assert all(result.status == "duplicate" for result in results)

    after = [tuple(row) for row in connection.execute(query)]
    event_count_after = connection.execute(
        "SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    connection.close()

    assert after == before
    assert event_count_after == event_count_before == UNIQUE_EVENTS


@pytest.mark.parametrize("path", [
    "/transactions?limit=100",
    "/transactions?status=settled&limit=100",
    "/transactions?merchant_id=merchant_1&date_from=2026-02-01&limit=100",
    "/reconciliation/summary?group_by=merchant",
    "/reconciliation/summary?group_by=merchant,date&limit=500",
    "/reconciliation/discrepancies?limit=100",
])
def test_reporting_queries_are_fast_on_the_full_dataset(seeded_client, path):
    """A practical guard rail, not a benchmark: if any of these regresses
    into a table scan or a Python-side fold, it will blow past 2 seconds
    long before it gets near production volumes."""
    started = time.perf_counter()
    response = seeded_client.get(path)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 2.0, f"{path} took {elapsed:.2f}s"
