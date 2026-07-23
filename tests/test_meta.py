"""Health, merchants, the raw event log, and cross-cutting API behaviour."""

from __future__ import annotations

from .conftest import hours_ago, ingest, lifecycle, make_event


def test_health_reports_row_counts(client):
    assert client.get("/health").json()["events"] == 0

    ingest(client, lifecycle("t1", ["payment_initiated", "payment_processed"]))
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["events"] == 2
    assert body["transactions"] == 1
    assert body["merchants"] == 1
    assert body["version"]


def test_root_lists_the_endpoints(client):
    body = client.get("/").json()
    assert "POST /events" in body["endpoints"]
    assert body["docs"] == "/docs"


def test_openapi_schema_is_served(client):
    schema = client.get("/openapi.json").json()
    assert "/events" in schema["paths"]
    assert "/transactions/{transaction_id}" in schema["paths"]
    assert "/reconciliation/summary" in schema["paths"]
    assert "/reconciliation/discrepancies" in schema["paths"]


def test_docs_page_renders(client):
    assert client.get("/docs").status_code == 200


def test_response_time_header_is_present(client):
    response = client.get("/health")
    assert float(response.headers["X-Response-Time-Ms"]) >= 0


def test_merchants_are_created_from_events(client):
    ingest(client, [
        make_event(transaction_id="a", merchant_id="merchant_1", merchant_name="QuickMart"),
        make_event(transaction_id="b", merchant_id="merchant_2", merchant_name="FreshBasket"),
        make_event(transaction_id="c", merchant_id="merchant_1", merchant_name="QuickMart"),
    ])
    body = client.get("/merchants").json()
    assert body["total"] == 2

    by_id = {item["merchant_id"]: item for item in body["items"]}
    assert by_id["merchant_1"]["merchant_name"] == "QuickMart"
    assert by_id["merchant_1"]["transaction_count"] == 2
    assert by_id["merchant_2"]["transaction_count"] == 1


def test_merchant_name_is_updated_by_later_events(client):
    ingest(client, make_event(transaction_id="a", merchant_id="m1", merchant_name="Old Name"))
    ingest(client, make_event(transaction_id="b", merchant_id="m1", merchant_name="New Name"))

    items = client.get("/merchants").json()["items"]
    assert items[0]["merchant_name"] == "New Name"


def test_merchant_totals_are_exact(client):
    ingest(client, [
        make_event(transaction_id="a", merchant_id="m1", amount=100.10),
        make_event(transaction_id="b", merchant_id="m1", amount=200.20),
    ])
    assert client.get("/merchants").json()["items"][0]["total_amount"] == 300.30


# ---------------------------------------------------------------------
# Raw event log
# ---------------------------------------------------------------------

def test_event_log_preserves_full_history(client):
    ingest(client, lifecycle(
        "t1", ["payment_initiated", "payment_processed", "settled"]))
    body = client.get("/events?transaction_id=t1").json()
    assert body["pagination"]["total"] == 3
    assert {item["event_type"] for item in body["items"]} == {
        "payment_initiated", "payment_processed", "settled"}


def test_event_log_filters(client):
    ingest(client, [
        *lifecycle("t1", ["payment_initiated", "payment_processed"],
                   merchant_id="m1"),
        *lifecycle("t2", ["payment_initiated", "payment_failed"],
                   merchant_id="m2"),
    ])
    assert client.get("/events?merchant_id=m1").json()["pagination"]["total"] == 2
    assert client.get(
        "/events?event_type=payment_failed").json()["pagination"]["total"] == 1
    assert client.get("/events?transaction_id=t2").json()["pagination"]["total"] == 2


def test_event_log_date_filter(client):
    ingest(client, [
        make_event(transaction_id="a", timestamp="2026-01-10T10:00:00Z"),
        make_event(transaction_id="b", timestamp="2026-03-10T10:00:00Z"),
    ])
    body = client.get("/events?date_from=2026-03-01&date_to=2026-03-31").json()
    assert body["pagination"]["total"] == 1


def test_event_log_pagination(client):
    ingest(client, [make_event(transaction_id=f"t{i}") for i in range(10)])
    body = client.get("/events?limit=4&offset=0").json()
    assert len(body["items"]) == 4
    assert body["pagination"]["total"] == 10
    assert body["pagination"]["has_more"] is True


def test_event_log_records_received_at_separately_from_occurred_at(client):
    """The event carries when it happened; we record when we saw it. Both
    matter when reconstructing a late or replayed delivery."""
    ingest(client, make_event(transaction_id="t1", timestamp=hours_ago(500)))
    event = client.get("/events?transaction_id=t1").json()["items"][0]
    assert event["occurred_at"] < event["received_at"]
