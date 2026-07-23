"""Unit tests for the primitives everything else rests on."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.timeutils import (
    TimestampError,
    format_ts,
    normalise_ts,
    parse_ts,
    range_lower_bound,
    range_upper_bound,
    to_major_units,
    to_minor_units,
)

from .conftest import ingest, make_event


# ---------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------

@pytest.mark.parametrize("amount,expected", [
    (0.01, 1),
    (1, 100),
    (15248.29, 1524829),
    ("15248.29", 1524829),
    (0.1 + 0.2, 30),        # 0.30000000000000004 in binary float
    (8.115, 812),           # round(8.115 * 100) gives 811 - the float trap
    (1.005, 101),
    (2.675, 268),
    (99999999.99, 9999999999),
])
def test_minor_unit_conversion_is_exact(amount, expected):
    assert to_minor_units(amount) == expected


@pytest.mark.parametrize("value", [0.01, 1.99, 15248.29, 99.95, 123456.78])
def test_round_trip_through_minor_units(value):
    assert to_major_units(to_minor_units(value)) == value


@pytest.mark.parametrize("bad", ["abc", float("nan"), float("inf"), None])
def test_invalid_amounts_raise(bad):
    with pytest.raises((ValueError, TypeError)):
        to_minor_units(bad)


def test_summing_many_amounts_stays_exact(client):
    """A thousand transactions of 0.01 must total exactly 10.00.
    Summing floats would drift; summing integer paise does not."""
    ingest(client, [
        make_event(transaction_id=f"t-{index}", amount=0.01)
        for index in range(1000)
    ])
    totals = client.get("/reconciliation/summary").json()["totals"]
    assert totals["total_amount"] == 10.0


def test_amount_with_more_than_two_decimals_is_rounded_half_up(client):
    ingest(client, make_event(transaction_id="t-round", amount=10.005))
    assert client.get("/transactions/t-round").json()["amount"] == 10.01


# ---------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------

def test_stored_format_is_fixed_width():
    """Lexicographic comparison only equals chronological comparison if
    every stored timestamp has the same width."""
    with_micros = format_ts(datetime(2026, 1, 8, 12, 0, 0, 123456, tzinfo=timezone.utc))
    without = format_ts(datetime(2026, 1, 8, 12, 0, 0, 0, tzinfo=timezone.utc))
    assert len(with_micros) == len(without)
    assert without.endswith(".000000+00:00")


def test_lexicographic_order_matches_chronological_order():
    stamps = [
        format_ts(datetime(2026, 1, 8, 12, 0, 0, 0, tzinfo=timezone.utc)),
        format_ts(datetime(2026, 1, 8, 12, 0, 0, 1, tzinfo=timezone.utc)),
        format_ts(datetime(2026, 1, 8, 12, 0, 1, 0, tzinfo=timezone.utc)),
        format_ts(datetime(2026, 1, 9, 0, 0, 0, 0, tzinfo=timezone.utc)),
    ]
    assert stamps == sorted(stamps)


@pytest.mark.parametrize("value", [
    "2026-01-08T12:00:00Z",
    "2026-01-08T12:00:00+00:00",
    "2026-01-08T17:30:00+05:30",
    "2026-01-08T12:00:00",
])
def test_equivalent_instants_normalise_identically(value):
    assert normalise_ts(value) == "2026-01-08T12:00:00.000000+00:00"


def test_naive_input_is_treated_as_utc():
    assert parse_ts("2026-01-08T12:00:00").tzinfo == timezone.utc


@pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "2026-13-01T00:00:00Z",
                                 "2026-01-32T00:00:00Z", "08/01/2026"])
def test_unparseable_timestamps_raise(bad):
    with pytest.raises(TimestampError):
        normalise_ts(bad)


def test_bare_date_bounds_cover_the_whole_day():
    assert range_lower_bound("2026-01-08") == "2026-01-08T00:00:00.000000+00:00"
    assert range_upper_bound("2026-01-08") == "2026-01-08T23:59:59.999999+00:00"


def test_full_timestamp_bounds_are_used_verbatim():
    assert range_lower_bound("2026-01-08T09:30:00Z") == "2026-01-08T09:30:00.000000+00:00"


def test_invalid_date_bound_raises():
    with pytest.raises(TimestampError):
        range_lower_bound("2026-02-30")
