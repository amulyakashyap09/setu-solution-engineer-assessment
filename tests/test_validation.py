"""Validation and error handling at the API edge."""

from __future__ import annotations

import pytest

from app.config import settings

from .conftest import hours_ago, ingest, make_event, post


def _fields(response) -> set[str]:
    return {detail["field"] for detail in response.json()["error"]["details"]}


def test_error_envelope_shape(client):
    response = post(client, {"event_type": "payment_initiated"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"]
    assert isinstance(error["details"], list)
    assert {"field", "message", "type"} <= set(error["details"][0])


@pytest.mark.parametrize(
    "missing",
    ["event_id", "event_type", "transaction_id", "merchant_id", "amount",
     "currency", "timestamp"],
)
def test_missing_required_field_is_rejected(client, missing):
    event = make_event()
    event.pop(missing)
    response = post(client, event)
    assert response.status_code == 422
    assert any(missing in field for field in _fields(response))


def test_unknown_event_type_is_rejected(client):
    response = post(client, make_event("payment_reversed"))
    assert response.status_code == 422
    assert any("event_type" in field for field in _fields(response))


@pytest.mark.parametrize("amount", [0, -1, -0.01])
def test_non_positive_amount_is_rejected(client, amount):
    response = post(client, make_event(amount=amount))
    assert response.status_code == 422


def test_absurdly_large_amount_is_rejected(client):
    response = post(client, make_event(amount=1e15))
    assert response.status_code == 422


@pytest.mark.parametrize("amount", ["not-a-number", None, [1, 2]])
def test_non_numeric_amount_is_rejected(client, amount):
    response = post(client, make_event(amount=amount))
    assert response.status_code == 422


@pytest.mark.parametrize("timestamp", ["", "yesterday", "2026-13-45T00:00:00Z",
                                       "1749300000", "2026/01/08 12:00"])
def test_invalid_timestamp_is_rejected(client, timestamp):
    response = post(client, make_event(timestamp=timestamp))
    assert response.status_code == 422
    assert any("timestamp" in field for field in _fields(response))


@pytest.mark.parametrize("currency", ["IN", "INRR", "1NR", ""])
def test_invalid_currency_is_rejected(client, currency):
    response = post(client, make_event(currency=currency))
    assert response.status_code == 422


def test_currency_is_normalised_to_uppercase(client):
    ingest(client, make_event(currency="inr"))
    assert client.get("/transactions/txn-1").json()["currency"] == "INR"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_identifiers_are_rejected(client, blank):
    assert post(client, make_event(event_id=blank)).status_code == 422
    assert post(client, make_event(transaction_id=blank)).status_code == 422
    assert post(client, make_event(merchant_id=blank)).status_code == 422


def test_identifiers_are_trimmed(client):
    ingest(client, make_event(transaction_id="  txn-pad  "))
    assert client.get("/transactions/txn-pad").status_code == 200


def test_empty_batch_is_rejected(client):
    assert post(client, []).status_code == 422
    assert post(client, {"events": []}).status_code == 422


def test_oversized_batch_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings.__class__, "max_batch_size", 2, raising=False)
    object.__setattr__(settings, "max_batch_size", 2)
    try:
        response = post(client, [make_event() for _ in range(3)])
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
    finally:
        object.__setattr__(settings, "max_batch_size", 5000)


def test_malformed_json_body_is_rejected(client):
    response = client.post(
        "/events",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_unknown_extra_fields_are_preserved_not_rejected(client):
    """Upstream systems add fields. Dropping a real payment event because
    it carried an unrecognised key would be worse than storing it."""
    ingest(client, make_event(event_id="evt-extra", gateway="razorpay", retry_count=2))

    stored = client.get("/events?transaction_id=txn-1").json()["items"]
    assert len(stored) == 1

    raw = client.get("/transactions/txn-1")
    assert raw.status_code == 200


def test_missing_merchant_name_falls_back_to_merchant_id(client):
    ingest(client, make_event(merchant_name=None, merchant_id="merchant_9"))
    detail = client.get("/transactions/txn-1").json()
    assert detail["merchant"]["merchant_name"] == "merchant_9"


def test_timestamp_forms_are_normalised_to_one_canonical_shape(client):
    """Z suffix, explicit offset, and naive input must all compare equal
    once stored - string range filters depend on it."""
    ingest(client, [
        make_event(transaction_id="t-z", timestamp="2026-03-01T10:00:00Z"),
        make_event(transaction_id="t-off", timestamp="2026-03-01T15:30:00+05:30"),
        make_event(transaction_id="t-naive", timestamp="2026-03-01T10:00:00"),
    ])
    stamps = {
        client.get(f"/transactions/{tid}").json()["initiated_at"]
        for tid in ("t-z", "t-off", "t-naive")
    }
    assert stamps == {"2026-03-01T10:00:00.000000+00:00"}


def test_non_utc_offsets_are_converted_not_truncated(client):
    ingest(client, make_event(transaction_id="t-ist", timestamp="2026-03-01T05:30:00+05:30"))
    assert client.get("/transactions/t-ist").json()["initiated_at"].startswith(
        "2026-03-01T00:00:00"
    )


# ---------------------------------------------------------------------
# Query parameter validation
# ---------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "limit=0", "limit=-1", "limit=100000", "offset=-1",
    "sort_by=drop_table", "sort_order=sideways", "status=unknown",
    "date_field=nonsense", "min_amount=-5",
])
def test_bad_transaction_query_params_are_rejected(client, query):
    response = client.get(f"/transactions?{query}")
    assert response.status_code == 422, query
    assert response.json()["error"]["code"] == "validation_error"


def test_inverted_date_range_is_rejected(client):
    response = client.get("/transactions?date_from=2026-05-01&date_to=2026-01-01")
    assert response.status_code == 422
    assert "date_from" in response.json()["error"]["message"]


def test_inverted_amount_range_is_rejected(client):
    response = client.get("/transactions?min_amount=500&max_amount=100")
    assert response.status_code == 422


def test_invalid_date_string_is_rejected(client):
    response = client.get("/transactions?date_from=not-a-date")
    assert response.status_code == 422


def test_unknown_group_by_dimension_is_rejected(client):
    response = client.get("/reconciliation/summary?group_by=merchant,planet")
    assert response.status_code == 422
    assert "planet" in response.json()["error"]["message"]


def test_duplicate_group_by_dimension_is_rejected(client):
    response = client.get("/reconciliation/summary?group_by=merchant,merchant")
    assert response.status_code == 422


def test_unknown_discrepancy_type_is_rejected(client):
    response = client.get("/reconciliation/discrepancies?type=NOT_A_TYPE")
    assert response.status_code == 422


def test_sort_by_is_whitelisted_against_injection(client):
    """The sort column is spliced into SQL, so it must come from a
    whitelist. Anything else is a 422, never a query."""
    payload = "first_event_at; DROP TABLE transactions;--"
    assert client.get("/transactions", params={"sort_by": payload}).status_code == 422
    # The table is still there.
    assert client.get("/health").json()["status"] == "ok"


def test_filter_values_are_bound_parameters_not_string_interpolation(client):
    ingest(client, make_event(merchant_id="merchant_1"))
    response = client.get("/transactions", params={"merchant_id": "' OR 1=1 --"})
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 0


def test_404_for_unknown_route(client):
    response = client.get("/no-such-endpoint")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_transaction_returns_404_envelope(client):
    response = client.get("/transactions/missing-id")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert "missing-id" in body["error"]["message"]
