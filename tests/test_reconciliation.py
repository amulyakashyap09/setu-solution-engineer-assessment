"""GET /reconciliation/summary and GET /reconciliation/discrepancies."""

from __future__ import annotations

import pytest

from .conftest import hours_ago, ingest, lifecycle, make_event


@pytest.fixture
def scenario(client):
    """One transaction of every interesting shape, all old enough to be
    considered stale under the default 48-hour window."""
    ingest(client, [
        # clean settled, merchant_1
        *lifecycle("clean-1", ["payment_initiated", "payment_processed", "settled"],
                   start_hours_ago=200, merchant_id="merchant_1",
                   merchant_name="QuickMart", amount=1000.00),
        # clean settled, merchant_2
        *lifecycle("clean-2", ["payment_initiated", "payment_processed", "settled"],
                   start_hours_ago=190, merchant_id="merchant_2",
                   merchant_name="FreshBasket", amount=2000.00),
        # clean failure, merchant_1
        *lifecycle("failed-1", ["payment_initiated", "payment_failed"],
                   start_hours_ago=180, merchant_id="merchant_1",
                   merchant_name="QuickMart", amount=300.00),
        # settled after a failure - the money problem
        *lifecycle("settled-after-fail", ["payment_initiated", "payment_failed", "settled"],
                   start_hours_ago=170, merchant_id="merchant_1",
                   merchant_name="QuickMart", amount=500.00),
        # processed but never settled, stale
        *lifecycle("processed-stale", ["payment_initiated", "payment_processed"],
                   start_hours_ago=160, merchant_id="merchant_2",
                   merchant_name="FreshBasket", amount=700.00),
        # initiated and abandoned, stale
        *lifecycle("stuck", ["payment_initiated"],
                   start_hours_ago=150, merchant_id="merchant_2",
                   merchant_name="FreshBasket", amount=50.00),
        # settled with no processed event ever seen
        *lifecycle("settled-no-process", ["payment_initiated", "settled"],
                   start_hours_ago=140, merchant_id="merchant_3",
                   merchant_name="UrbanEats", amount=900.00),
    ])
    return client


# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------

def test_totals_are_exact(scenario):
    totals = scenario.get("/reconciliation/summary").json()["totals"]
    assert totals["transaction_count"] == 7
    assert totals["total_amount"] == pytest.approx(5450.00)
    assert totals["settled_count"] == 3      # clean-1, clean-2, settled-no-process
    assert totals["failed_count"] == 2       # failed-1, settled-after-fail
    assert totals["processed_count"] == 1
    assert totals["initiated_count"] == 1


def test_status_counts_sum_to_the_transaction_count(scenario):
    totals = scenario.get("/reconciliation/summary").json()["totals"]
    parts = sum(totals[key] for key in
                ("initiated_count", "processed_count", "failed_count", "settled_count"))
    assert parts == totals["transaction_count"]


def test_group_by_merchant(scenario):
    groups = scenario.get("/reconciliation/summary?group_by=merchant").json()["groups"]
    by_id = {group["merchant_id"]: group for group in groups}

    assert set(by_id) == {"merchant_1", "merchant_2", "merchant_3"}
    assert by_id["merchant_1"]["transaction_count"] == 3
    assert by_id["merchant_1"]["merchant_name"] == "QuickMart"
    assert by_id["merchant_2"]["transaction_count"] == 3
    assert by_id["merchant_3"]["transaction_count"] == 1


def test_group_by_status(scenario):
    groups = scenario.get("/reconciliation/summary?group_by=status").json()["groups"]
    by_status = {group["status"]: group["transaction_count"] for group in groups}
    assert by_status == {"settled": 3, "failed": 2, "processed": 1, "initiated": 1}


def test_group_by_date(client):
    ingest(client, [
        make_event(transaction_id="a", timestamp="2026-02-01T10:00:00Z"),
        make_event(transaction_id="b", timestamp="2026-02-01T18:00:00Z"),
        make_event(transaction_id="c", timestamp="2026-02-02T10:00:00Z"),
    ])
    groups = client.get("/reconciliation/summary?group_by=date").json()["groups"]
    assert {g["date"]: g["transaction_count"] for g in groups} == {
        "2026-02-01": 2, "2026-02-02": 1}


def test_group_by_multiple_dimensions(scenario):
    body = scenario.get("/reconciliation/summary?group_by=merchant,status").json()
    assert body["group_by"] == ["merchant", "status"]
    for group in body["groups"]:
        assert group["merchant_id"] is not None
        assert group["status"] is not None
    # merchant_1 has settled, failed, failed -> two distinct status buckets
    merchant_1 = [g for g in body["groups"] if g["merchant_id"] == "merchant_1"]
    assert {g["status"] for g in merchant_1} == {"settled", "failed"}


def test_group_totals_reconcile_with_the_grand_total(scenario):
    body = scenario.get("/reconciliation/summary?group_by=merchant,date").json()
    assert sum(g["transaction_count"] for g in body["groups"]) == \
        body["totals"]["transaction_count"]
    assert sum(g["total_amount"] for g in body["groups"]) == \
        pytest.approx(body["totals"]["total_amount"])


def test_empty_group_by_returns_totals_only(scenario):
    body = scenario.get("/reconciliation/summary?group_by=").json()
    assert body["groups"] == []
    assert body["totals"]["transaction_count"] == 7


def test_summary_respects_merchant_filter(scenario):
    body = scenario.get("/reconciliation/summary?merchant_id=merchant_1").json()
    assert body["totals"]["transaction_count"] == 3
    assert body["totals"]["total_amount"] == pytest.approx(1800.00)


def test_summary_respects_date_filter(client):
    ingest(client, [
        make_event(transaction_id="jan", timestamp="2026-01-10T10:00:00Z"),
        make_event(transaction_id="mar", timestamp="2026-03-10T10:00:00Z"),
    ])
    body = client.get(
        "/reconciliation/summary?date_from=2026-03-01&date_to=2026-03-31").json()
    assert body["totals"]["transaction_count"] == 1


def test_settlement_rate(scenario):
    totals = scenario.get("/reconciliation/summary").json()["totals"]
    assert totals["settlement_rate"] == pytest.approx(3 / 7, abs=1e-4)


def test_settlement_rate_of_empty_set_is_zero_not_an_error(client):
    totals = client.get("/reconciliation/summary").json()["totals"]
    assert totals["settlement_rate"] == 0.0
    assert totals["transaction_count"] == 0


def test_amount_buckets_split_correctly(scenario):
    totals = scenario.get("/reconciliation/summary").json()["totals"]
    assert totals["settled_amount"] == pytest.approx(1000 + 2000 + 900)
    assert totals["failed_amount"] == pytest.approx(300 + 500)
    # unsettled = everything with no settlement and no failure
    assert totals["unsettled_amount"] == pytest.approx(700 + 50)


def test_summary_discrepancy_count(scenario):
    totals = scenario.get("/reconciliation/summary").json()["totals"]
    # settled-after-fail, processed-stale, stuck, settled-no-process
    assert totals["discrepancy_count"] == 4


def test_summary_group_pagination(scenario):
    body = scenario.get("/reconciliation/summary?group_by=merchant&limit=2").json()
    assert len(body["groups"]) == 2
    assert body["filters"]["group_count"] == 3
    # Totals are always over the full filtered set, never just the page.
    assert body["totals"]["transaction_count"] == 7


# ---------------------------------------------------------------------
# Discrepancies
# ---------------------------------------------------------------------

def _by_id(response) -> dict[str, dict]:
    return {item["transaction_id"]: item for item in response.json()["items"]}


def test_detects_settlement_against_a_failed_payment(scenario):
    items = _by_id(scenario.get("/reconciliation/discrepancies?limit=100"))
    assert "SETTLED_AFTER_FAILURE" in items["settled-after-fail"]["discrepancy_types"]
    assert items["settled-after-fail"]["severity"] == "high"


def test_detects_processed_but_never_settled(scenario):
    items = _by_id(scenario.get("/reconciliation/discrepancies?limit=100"))
    assert "PROCESSED_NOT_SETTLED" in items["processed-stale"]["discrepancy_types"]
    assert items["processed-stale"]["severity"] == "medium"


def test_detects_stuck_in_initiated(scenario):
    items = _by_id(scenario.get("/reconciliation/discrepancies?limit=100"))
    assert "STUCK_IN_INITIATED" in items["stuck"]["discrepancy_types"]


def test_detects_settled_without_processing(scenario):
    items = _by_id(scenario.get("/reconciliation/discrepancies?limit=100"))
    assert "SETTLED_WITHOUT_PROCESSING" in items["settled-no-process"]["discrepancy_types"]


def test_clean_transactions_are_never_reported(scenario):
    items = _by_id(scenario.get("/reconciliation/discrepancies?limit=100"))
    assert "clean-1" not in items
    assert "clean-2" not in items
    assert "failed-1" not in items  # a clean failure is not a discrepancy


def test_detects_conflicting_payment_state(client):
    """Both processed and failed for the same transaction."""
    ingest(client, [
        make_event("payment_initiated", transaction_id="conflict", timestamp=hours_ago(100)),
        make_event("payment_processed", transaction_id="conflict", timestamp=hours_ago(99)),
        make_event("payment_failed", transaction_id="conflict", timestamp=hours_ago(98)),
    ])
    detail = client.get("/transactions/conflict").json()
    assert detail["payment_status"] == "conflicted"

    items = _by_id(client.get("/reconciliation/discrepancies?limit=100"))
    assert "CONFLICTING_PAYMENT_STATE" in items["conflict"]["discrepancy_types"]
    assert items["conflict"]["severity"] == "high"


def test_detects_amount_mismatch_between_events(client):
    ingest(client, [
        make_event("payment_initiated", transaction_id="mismatch",
                   amount=100.00, timestamp=hours_ago(100)),
        make_event("settled", transaction_id="mismatch",
                   amount=150.00, timestamp=hours_ago(99)),
    ])
    items = _by_id(client.get("/reconciliation/discrepancies?limit=100"))
    assert "AMOUNT_MISMATCH" in items["mismatch"]["discrepancy_types"]


def test_detects_merchant_mismatch_between_events(client):
    ingest(client, [
        make_event("payment_initiated", transaction_id="wrong-merchant",
                   merchant_id="merchant_1", timestamp=hours_ago(100)),
        make_event("settled", transaction_id="wrong-merchant",
                   merchant_id="merchant_2", timestamp=hours_ago(99)),
    ])
    items = _by_id(client.get("/reconciliation/discrepancies?limit=100"))
    assert "MERCHANT_MISMATCH" in items["wrong-merchant"]["discrepancy_types"]


def test_staleness_window_is_respected(client):
    """A transaction processed ten minutes ago is pending, not a
    discrepancy. The same transaction is a discrepancy once the window
    has passed."""
    ingest(client, [
        make_event("payment_initiated", transaction_id="fresh", timestamp=hours_ago(0.5)),
        make_event("payment_processed", transaction_id="fresh", timestamp=hours_ago(0.2)),
    ])

    default_window = _by_id(client.get("/reconciliation/discrepancies?limit=100"))
    assert "fresh" not in default_window

    tight_window = _by_id(
        client.get("/reconciliation/discrepancies?stale_after_hours=0&limit=100"))
    assert "PROCESSED_NOT_SETTLED" in tight_window["fresh"]["discrepancy_types"]


def test_as_of_lets_the_report_be_run_for_a_past_instant(client):
    ingest(client, [
        make_event("payment_initiated", transaction_id="t", timestamp="2026-03-01T00:00:00Z"),
        make_event("payment_processed", transaction_id="t", timestamp="2026-03-01T01:00:00Z"),
    ])
    # As at one hour after processing, nothing is stale yet.
    early = client.get(
        "/reconciliation/discrepancies?as_of=2026-03-01T02:00:00Z&stale_after_hours=48")
    assert early.json()["pagination"]["total"] == 0

    later = client.get(
        "/reconciliation/discrepancies?as_of=2026-03-10T00:00:00Z&stale_after_hours=48")
    assert later.json()["pagination"]["total"] == 1


def test_duplicate_events_are_excluded_by_default_and_available_on_request(client):
    event = make_event(event_id="dup", transaction_id="t-dup", timestamp=hours_ago(100))
    ingest(client, event)
    ingest(client, event)

    default_report = client.get("/reconciliation/discrepancies?limit=100").json()
    assert "DUPLICATE_EVENTS" not in default_report["counts_by_type"]

    opt_in = client.get(
        "/reconciliation/discrepancies?type=DUPLICATE_EVENTS&limit=100").json()
    assert opt_in["counts_by_type"]["DUPLICATE_EVENTS"] == 1
    assert opt_in["items"][0]["transaction_id"] == "t-dup"


def test_filter_by_discrepancy_type(scenario):
    body = scenario.get(
        "/reconciliation/discrepancies?type=SETTLED_AFTER_FAILURE&limit=100").json()
    assert [item["transaction_id"] for item in body["items"]] == ["settled-after-fail"]
    assert body["counts_by_type"] == {"SETTLED_AFTER_FAILURE": 1}


def test_filter_by_several_discrepancy_types(scenario):
    body = scenario.get(
        "/reconciliation/discrepancies"
        "?type=SETTLED_AFTER_FAILURE&type=STUCK_IN_INITIATED&limit=100").json()
    assert {item["transaction_id"] for item in body["items"]} == {
        "settled-after-fail", "stuck"}


def test_filter_by_merchant(scenario):
    body = scenario.get(
        "/reconciliation/discrepancies?merchant_id=merchant_2&limit=100").json()
    assert {item["transaction_id"] for item in body["items"]} == {
        "processed-stale", "stuck"}


def test_filter_by_severity(scenario):
    body = scenario.get("/reconciliation/discrepancies?severity=high&limit=100").json()
    assert all(item["severity"] == "high" for item in body["items"])
    assert {item["transaction_id"] for item in body["items"]} == {"settled-after-fail"}


def test_counts_by_type_cover_the_whole_population_not_the_page(scenario):
    full = scenario.get("/reconciliation/discrepancies?limit=100").json()
    paged = scenario.get("/reconciliation/discrepancies?limit=1").json()
    assert paged["counts_by_type"] == full["counts_by_type"]
    assert len(paged["items"]) == 1
    assert paged["pagination"]["total"] == full["pagination"]["total"]


def test_default_sort_is_most_severe_first(scenario):
    items = scenario.get("/reconciliation/discrepancies?limit=100").json()["items"]
    rank = {"high": 3, "medium": 2, "low": 1}
    ranks = [rank[item["severity"]] for item in items]
    assert ranks == sorted(ranks, reverse=True)


def test_discrepancy_pagination(scenario):
    first = scenario.get("/reconciliation/discrepancies?limit=2&offset=0").json()
    assert len(first["items"]) == 2
    assert first["pagination"]["has_more"] is True

    collected = []
    for offset in range(0, first["pagination"]["total"], 2):
        page = scenario.get(f"/reconciliation/discrepancies?limit=2&offset={offset}")
        collected.extend(item["transaction_id"] for item in page.json()["items"])
    assert len(set(collected)) == first["pagination"]["total"]


def test_age_hours_is_reported(scenario):
    items = scenario.get("/reconciliation/discrepancies?limit=100").json()["items"]
    assert all(item["age_hours"] > 0 for item in items)


def test_clean_database_reports_nothing(client):
    body = client.get("/reconciliation/discrepancies").json()
    assert body["items"] == []
    assert body["pagination"]["total"] == 0
    assert set(body["counts_by_type"].values()) == {0}


def test_a_transaction_can_carry_several_discrepancy_types(client):
    """Settled after failure AND with a mismatched amount."""
    ingest(client, [
        make_event("payment_initiated", transaction_id="multi",
                   amount=100.0, timestamp=hours_ago(100)),
        make_event("payment_failed", transaction_id="multi",
                   amount=100.0, timestamp=hours_ago(99)),
        make_event("settled", transaction_id="multi",
                   amount=250.0, timestamp=hours_ago(98)),
    ])
    item = _by_id(client.get("/reconciliation/discrepancies?limit=100"))["multi"]
    assert set(item["discrepancy_types"]) == {"SETTLED_AFTER_FAILURE", "AMOUNT_MISMATCH"}
    assert item["severity"] == "high"
