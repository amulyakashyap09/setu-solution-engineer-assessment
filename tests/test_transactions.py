"""GET /transactions and GET /transactions/{id}."""

from __future__ import annotations

import pytest

from .conftest import hours_ago, ingest, lifecycle, make_event


@pytest.fixture
def populated(client):
    """A small fixed dataset covering every terminal and non-terminal state."""
    ingest(client, [
        # settled
        *lifecycle("t-settled-1", ["payment_initiated", "payment_processed", "settled"],
                   start_hours_ago=100, merchant_id="merchant_1",
                   merchant_name="QuickMart", amount=100.00),
        *lifecycle("t-settled-2", ["payment_initiated", "payment_processed", "settled"],
                   start_hours_ago=50, merchant_id="merchant_2",
                   merchant_name="FreshBasket", amount=250.50),
        # failed
        *lifecycle("t-failed", ["payment_initiated", "payment_failed"],
                   start_hours_ago=80, merchant_id="merchant_1",
                   merchant_name="QuickMart", amount=75.25),
        # processed, awaiting settlement
        *lifecycle("t-processed", ["payment_initiated", "payment_processed"],
                   start_hours_ago=60, merchant_id="merchant_2",
                   merchant_name="FreshBasket", amount=500.00),
        # initiated only
        *lifecycle("t-initiated", ["payment_initiated"],
                   start_hours_ago=10, merchant_id="merchant_3",
                   merchant_name="UrbanEats", amount=10.00),
    ])
    return client


def ids(response) -> list[str]:
    return [item["transaction_id"] for item in response.json()["items"]]


def test_list_returns_all_transactions(populated):
    body = populated.get("/transactions").json()
    assert body["pagination"]["total"] == 5
    assert body["pagination"]["returned"] == 5
    assert body["pagination"]["has_more"] is False


def test_filter_by_merchant(populated):
    assert set(ids(populated.get("/transactions?merchant_id=merchant_1"))) == {
        "t-settled-1", "t-failed"}


def test_filter_by_several_merchants(populated):
    response = populated.get("/transactions?merchant_id=merchant_1&merchant_id=merchant_3")
    assert set(ids(response)) == {"t-settled-1", "t-failed", "t-initiated"}


@pytest.mark.parametrize("status,expected", [
    ("settled", {"t-settled-1", "t-settled-2"}),
    ("failed", {"t-failed"}),
    ("processed", {"t-processed"}),
    ("initiated", {"t-initiated"}),
])
def test_filter_by_status(populated, status, expected):
    assert set(ids(populated.get(f"/transactions?status={status}"))) == expected


def test_filter_by_multiple_statuses(populated):
    response = populated.get("/transactions?status=failed&status=initiated")
    assert set(ids(response)) == {"t-failed", "t-initiated"}


def test_filter_by_settlement_status(populated):
    settled = populated.get("/transactions?settlement_status=settled")
    assert set(ids(settled)) == {"t-settled-1", "t-settled-2"}

    pending = populated.get("/transactions?settlement_status=pending")
    assert set(ids(pending)) == {"t-failed", "t-processed", "t-initiated"}


def test_filter_by_payment_status(populated):
    response = populated.get("/transactions?payment_status=processed")
    assert set(ids(response)) == {"t-settled-1", "t-settled-2", "t-processed"}


def test_filter_by_unknown_merchant_returns_empty_not_error(populated):
    body = populated.get("/transactions?merchant_id=merchant_999").json()
    assert body["pagination"]["total"] == 0
    assert body["items"] == []


def test_filter_by_amount_range(populated):
    response = populated.get("/transactions?min_amount=100&max_amount=300")
    assert set(ids(response)) == {"t-settled-1", "t-settled-2"}


def test_filter_by_currency(populated):
    assert populated.get("/transactions?currency=INR").json()["pagination"]["total"] == 5
    assert populated.get("/transactions?currency=USD").json()["pagination"]["total"] == 0


def test_date_range_filter(client):
    ingest(client, [
        make_event(transaction_id="jan", timestamp="2026-01-15T10:00:00Z"),
        make_event(transaction_id="feb", timestamp="2026-02-15T10:00:00Z"),
        make_event(transaction_id="mar", timestamp="2026-03-15T10:00:00Z"),
    ])
    response = client.get("/transactions?date_from=2026-02-01&date_to=2026-02-28")
    assert ids(response) == ["feb"]


def test_bare_date_upper_bound_is_inclusive_of_the_whole_day(client):
    """date_to=2026-02-15 must include an event at 23:59 on the 15th, not
    just midnight."""
    ingest(client, [
        make_event(transaction_id="early", timestamp="2026-02-15T00:00:01Z"),
        make_event(transaction_id="late", timestamp="2026-02-15T23:59:59Z"),
    ])
    response = client.get("/transactions?date_from=2026-02-15&date_to=2026-02-15")
    assert set(ids(response)) == {"early", "late"}


def test_date_range_accepts_full_iso_timestamps(client):
    ingest(client, [
        make_event(transaction_id="a", timestamp="2026-02-15T09:00:00Z"),
        make_event(transaction_id="b", timestamp="2026-02-15T11:00:00Z"),
    ])
    response = client.get(
        "/transactions?date_from=2026-02-15T10:00:00Z&date_to=2026-02-15T12:00:00Z")
    assert ids(response) == ["b"]


def test_date_field_selects_which_timestamp_is_filtered(client):
    ingest(client, [
        make_event("payment_initiated", transaction_id="t1",
                   timestamp="2026-01-10T10:00:00Z"),
        make_event("payment_processed", transaction_id="t1",
                   timestamp="2026-01-11T10:00:00Z"),
        make_event("settled", transaction_id="t1", timestamp="2026-03-20T10:00:00Z"),
    ])
    # Initiated in January, settled in March.
    assert ids(client.get(
        "/transactions?date_field=first_event_at&date_from=2026-03-01")) == []
    assert ids(client.get(
        "/transactions?date_field=settled_at&date_from=2026-03-01")) == ["t1"]


def test_has_duplicates_filter(client):
    event = make_event(event_id="dup-me", transaction_id="t-dup")
    ingest(client, event)
    ingest(client, event)
    ingest(client, make_event(transaction_id="t-clean"))

    assert ids(client.get("/transactions?has_duplicates=true")) == ["t-dup"]
    assert ids(client.get("/transactions?has_duplicates=false")) == ["t-clean"]


def test_combined_filters_are_anded(populated):
    response = populated.get("/transactions?merchant_id=merchant_2&status=settled")
    assert ids(response) == ["t-settled-2"]


# ---------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------

def test_sort_by_amount_ascending(populated):
    response = populated.get("/transactions?sort_by=amount&sort_order=asc")
    amounts = [item["amount"] for item in response.json()["items"]]
    assert amounts == sorted(amounts)
    assert amounts[0] == 10.00


def test_sort_by_amount_descending(populated):
    response = populated.get("/transactions?sort_by=amount&sort_order=desc")
    amounts = [item["amount"] for item in response.json()["items"]]
    assert amounts == sorted(amounts, reverse=True)
    assert amounts[0] == 500.00


def test_default_sort_is_most_recent_first(populated):
    stamps = [item["first_event_at"] for item in populated.get("/transactions").json()["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_sort_is_stable_across_pages_on_a_non_unique_key(client):
    """Ten transactions share one status. Paging sorted by that status
    must not repeat or skip rows - the transaction_id tiebreaker is what
    guarantees it."""
    ingest(client, [
        make_event(transaction_id=f"t-{index:02d}", timestamp=hours_ago(5))
        for index in range(10)
    ])
    collected: list[str] = []
    for offset in range(0, 10, 3):
        page = client.get(f"/transactions?sort_by=status&limit=3&offset={offset}")
        collected.extend(ids(page))

    assert len(collected) == 10
    assert len(set(collected)) == 10


def test_null_sort_values_are_ordered_last(populated):
    response = populated.get("/transactions?sort_by=settled_at&sort_order=desc")
    settled_values = [item["settled_at"] for item in response.json()["items"]]
    non_null = [v for v in settled_values if v is not None]
    assert settled_values[:len(non_null)] == non_null


# ---------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------

def test_pagination_metadata(populated):
    body = populated.get("/transactions?limit=2&offset=0").json()
    assert body["pagination"] == {
        "limit": 2, "offset": 0, "total": 5, "returned": 2, "has_more": True}


def test_last_page_reports_no_more(populated):
    body = populated.get("/transactions?limit=2&offset=4").json()
    assert body["pagination"]["returned"] == 1
    assert body["pagination"]["has_more"] is False


def test_offset_beyond_end_returns_empty_page(populated):
    body = populated.get("/transactions?limit=10&offset=999").json()
    assert body["items"] == []
    assert body["pagination"]["total"] == 5
    assert body["pagination"]["has_more"] is False


def test_paging_covers_every_row_exactly_once(populated):
    seen: list[str] = []
    for offset in range(0, 5, 2):
        seen.extend(ids(populated.get(f"/transactions?limit=2&offset={offset}")))
    assert sorted(seen) == sorted(ids(populated.get("/transactions")))


def test_total_is_unaffected_by_the_page_size(populated):
    for limit in (1, 3, 5, 100):
        assert populated.get(f"/transactions?limit={limit}").json()[
            "pagination"]["total"] == 5


def test_empty_database_lists_cleanly(client):
    body = client.get("/transactions").json()
    assert body["items"] == []
    assert body["pagination"]["total"] == 0


# ---------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------

def test_detail_includes_merchant_and_event_history(populated):
    detail = populated.get("/transactions/t-settled-1").json()

    assert detail["transaction_id"] == "t-settled-1"
    assert detail["status"] == "settled"
    assert detail["merchant"] == {
        "merchant_id": "merchant_1", "merchant_name": "QuickMart"}
    assert [event["event_type"] for event in detail["events"]] == [
        "payment_initiated", "payment_processed", "settled"]


def test_detail_event_history_is_chronological(populated):
    events = populated.get("/transactions/t-processed").json()["events"]
    stamps = [event["occurred_at"] for event in events]
    assert stamps == sorted(stamps)


def test_detail_exposes_both_status_axes(populated):
    detail = populated.get("/transactions/t-processed").json()
    assert detail["payment_status"] == "processed"
    assert detail["settlement_status"] == "pending"
    assert detail["status"] == "processed"


def test_detail_reports_discrepancies(client):
    ingest(client, lifecycle(
        "t-bad", ["payment_initiated", "payment_failed", "settled"], start_hours_ago=100))
    detail = client.get("/transactions/t-bad").json()
    assert "SETTLED_AFTER_FAILURE" in detail["discrepancies"]


def test_detail_of_clean_transaction_has_no_discrepancies(client):
    ingest(client, lifecycle(
        "t-good", ["payment_initiated", "payment_processed", "settled"],
        start_hours_ago=100))
    assert client.get("/transactions/t-good").json()["discrepancies"] == []


def test_detail_timestamps_match_the_event_log(populated):
    detail = populated.get("/transactions/t-settled-1").json()
    by_type = {event["event_type"]: event["occurred_at"] for event in detail["events"]}
    assert detail["initiated_at"] == by_type["payment_initiated"]
    assert detail["processed_at"] == by_type["payment_processed"]
    assert detail["settled_at"] == by_type["settled"]
    assert detail["failed_at"] is None
