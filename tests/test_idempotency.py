"""Idempotency: the single most important property of this service.

Replaying an event must never create a second record, never re-apply a
state transition, and never change the projected transaction state.
"""

from __future__ import annotations

import itertools

import pytest

from .conftest import hours_ago, ingest, lifecycle, make_event, post


def _projection(client, transaction_id: str) -> dict:
    detail = client.get(f"/transactions/{transaction_id}").json()
    return {
        key: detail[key]
        for key in (
            "status", "payment_status", "settlement_status",
            "initiated_at", "processed_at", "failed_at", "settled_at",
            "amount", "event_count", "merchant_id",
        )
    }


def test_same_event_twice_is_stored_once(client):
    event = make_event(event_id="evt-fixed")

    first = post(client, event)
    second = post(client, event)

    assert first.status_code == 201
    assert first.json()["created"] == 1
    assert first.json()["duplicates"] == 0

    # A pure replay is a 200, not a 201: nothing was created.
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] == 1
    assert second.json()["results"][0]["status"] == "duplicate"

    assert client.get("/events").json()["pagination"]["total"] == 1
    assert client.get("/transactions/txn-1").json()["event_count"] == 1


def test_replaying_a_full_lifecycle_does_not_change_state(client):
    events = lifecycle("txn-1", ["payment_initiated", "payment_processed", "settled"])
    ingest(client, events)
    before = _projection(client, "txn-1")

    for _ in range(3):
        ingest(client, events)

    assert _projection(client, "txn-1") == before
    assert client.get("/events").json()["pagination"]["total"] == 3


def test_duplicate_within_a_single_request_is_deduplicated(client):
    event = make_event(event_id="evt-dup")
    body = ingest(client, [event, event, event])

    assert body["received"] == 3
    assert body["created"] == 1
    assert body["duplicates"] == 2
    assert client.get("/events").json()["pagination"]["total"] == 1


def test_duplicate_bumps_the_audit_counter_only(client):
    event = make_event(event_id="evt-audit")
    ingest(client, event)
    ingest(client, event)
    ingest(client, event)

    detail = client.get("/transactions/txn-1").json()
    assert detail["event_count"] == 1          # real events
    assert detail["duplicate_event_count"] == 2  # replays observed


def test_replay_with_a_different_payload_is_rejected_and_flagged(client):
    """Same event_id, different amount. The stored event wins - we do not
    let a redelivery silently rewrite history - but the caller is told."""
    original = make_event(event_id="evt-conflict", amount=100.0)
    tampered = make_event(event_id="evt-conflict", amount=999.0)

    ingest(client, original)
    body = ingest(client, tampered)

    result = body["results"][0]
    assert result["status"] == "duplicate"
    assert result["payload_conflict"] is True
    assert "different payload" in result["message"]

    assert client.get("/transactions/txn-1").json()["amount"] == 100.0


def test_duplicate_never_corrupts_a_settled_transaction(client):
    ingest(client, lifecycle("txn-1", ["payment_initiated", "payment_processed", "settled"]))
    settled_state = _projection(client, "txn-1")
    assert settled_state["status"] == "settled"

    # Redeliver the initiated event on its own, long after settlement.
    initiated = client.get("/events?transaction_id=txn-1&event_type=payment_initiated")
    replay = initiated.json()["items"][0]
    ingest(client, make_event(
        "payment_initiated",
        event_id=replay["event_id"],
        transaction_id="txn-1",
        timestamp=replay["occurred_at"],
    ))

    assert _projection(client, "txn-1")["status"] == "settled"


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations(["payment_initiated", "payment_processed", "settled"])),
)
def test_projection_is_order_independent(client, order):
    """Events may arrive out of order. Because the projection folds each
    event type with MIN() rather than overwriting a status, every arrival
    order must converge on the same state."""
    timestamps = {
        "payment_initiated": hours_ago(72),
        "payment_processed": hours_ago(71),
        "settled": hours_ago(70),
    }
    events = [
        make_event(event_type, transaction_id="txn-order", timestamp=timestamps[event_type])
        for event_type in order
    ]
    ingest(client, events)

    detail = client.get("/transactions/txn-order").json()
    assert detail["status"] == "settled"
    assert detail["initiated_at"] == timestamps["payment_initiated"]
    assert detail["processed_at"] == timestamps["payment_processed"]
    assert detail["settled_at"] == timestamps["settled"]


def test_settled_arriving_before_initiated_still_settles(client):
    ingest(client, make_event("settled", transaction_id="txn-x", timestamp=hours_ago(1)))
    ingest(client, make_event("payment_initiated", transaction_id="txn-x", timestamp=hours_ago(5)))
    ingest(client, make_event("payment_processed", transaction_id="txn-x", timestamp=hours_ago(3)))

    detail = client.get("/transactions/txn-x").json()
    assert detail["status"] == "settled"
    assert detail["first_event_at"] == detail["initiated_at"]
    assert detail["last_event_at"] == detail["settled_at"]


def test_earliest_timestamp_wins_for_a_repeated_event_type(client):
    """Two distinct events of the same type: the projection keeps the
    first occurrence, regardless of ingestion order."""
    late = make_event("payment_processed", transaction_id="txn-1", timestamp=hours_ago(10))
    early = make_event("payment_processed", transaction_id="txn-1", timestamp=hours_ago(40))

    ingest(client, late)
    ingest(client, early)

    detail = client.get("/transactions/txn-1").json()
    assert detail["processed_at"] == early["timestamp"]
    assert detail["event_count"] == 2  # both are real, distinct events


def test_batch_is_atomic(client, monkeypatch):
    """If any event in a batch blows up, none of the batch is persisted."""
    import app.ingest as ingest_module

    original = ingest_module._to_row
    calls = {"n": 0}

    def exploding(event, now):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated failure mid-batch")
        return original(event, now)

    monkeypatch.setattr(ingest_module, "_to_row", exploding)

    batch = [
        make_event("payment_initiated", transaction_id="txn-a"),
        make_event("payment_processed", transaction_id="txn-a"),
        make_event("settled", transaction_id="txn-a"),
    ]
    response = post(client, batch)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert client.get("/events").json()["pagination"]["total"] == 0
    assert client.get("/transactions").json()["pagination"]["total"] == 0


def test_ingest_reports_transactions_affected(client):
    body = ingest(client, [
        make_event(transaction_id="txn-a"),
        make_event(transaction_id="txn-b"),
        make_event("payment_processed", transaction_id="txn-b"),
    ])
    assert body["transactions_affected"] == 2


def test_concurrent_submission_of_the_same_event_stores_it_once(client):
    """The real-world duplicate: an upstream retry arriving while the
    original is still in flight. Ingest runs inside BEGIN IMMEDIATE, so
    SQLite serialises the writers and exactly one wins."""
    from concurrent.futures import ThreadPoolExecutor

    event = make_event(event_id="evt-race", transaction_id="txn-race")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(client, event), range(8)))

    assert all(response.status_code in (200, 201) for response in responses)
    created = sum(response.json()["created"] for response in responses)
    duplicates = sum(response.json()["duplicates"] for response in responses)

    assert created == 1
    assert duplicates == 7
    assert client.get("/events").json()["pagination"]["total"] == 1
    assert client.get("/transactions/txn-race").json()["event_count"] == 1


def test_concurrent_distinct_events_on_one_transaction_all_land(client):
    """Eight different events for the same transaction, submitted at
    once. None may be lost, and the projection must end up consistent."""
    from concurrent.futures import ThreadPoolExecutor

    events = [
        make_event("payment_initiated", event_id=f"evt-c{index}",
                   transaction_id="txn-multi", timestamp=hours_ago(100 - index))
        for index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda event: post(client, event), events))

    assert all(response.status_code == 201 for response in responses)
    detail = client.get("/transactions/txn-multi").json()
    assert detail["event_count"] == 8
    # MIN() fold: the earliest of the eight wins.
    assert detail["initiated_at"] == events[0]["timestamp"]
