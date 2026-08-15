"""The deep -> options handoff: what gets frozen, and what survives a round trip.

``build_options_context`` reads a finished ``DeepResult`` through ``getattr``
rather than importing the type, so these fakes are the contract. If the deep
pipeline renames a field, the assertion that breaks here is the one that would
otherwise have shipped an options stage anchored to nothing.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tradingagent.options.context import (
    OptionsContext,
    VerdictRow,
    build_options_context,
    levels_from,
)


class FakeIndicators:
    close = 100.0

    def __init__(self, values: dict):
        self._values = values

    def get(self, key):
        return self._values.get(key)


class FakeEvidence:
    def __init__(self, indicators=None, dividend_yield=None):
        self.indicators = indicators
        self.price = indicators.close if indicators else None
        self.fundamentals = type("F", (), {"dividend_yield": dividend_yield})()

    def price_context(self):
        return "- Last close: $100.00"


class FakeDecision:
    rating = "Overweight"
    confidence = "H"
    price_target = 112.0
    time_horizon = "8-12 weeks"
    executive_summary = "Enter in two tranches."
    invalidation = "A close below the 50-day."


class FakeResult:
    def __init__(self, symbol="XYZ", decision=None, evidence=None, screener=None):
        self.symbol = symbol
        self.decision = decision
        self.evidence = evidence
        self.degraded = False
        self.queued = type(
            "Q",
            (),
            {
                "name": "Example Corp",
                "screener": screener or {},
                "earnings_note": "no confirmed earnings in the next 10 days",
            },
        )()


def test_levels_are_pulled_from_the_indicators_the_deep_roles_argued_over():
    indicators = FakeIndicators(
        {"close_50_sma": 96.0, "close_200_sma": 88.0, "boll_lb": 92.0, "boll_ub": 106.0, "atr": 2.5}
    )
    result = FakeResult(
        decision=FakeDecision(),
        evidence=FakeEvidence(indicators),
        screener={"stop_ref": 90.5, "entry_ref": 101.2},
    )
    levels = levels_from(result)
    assert levels["50-day SMA"] == 96.0
    assert levels["Bollinger upper band"] == 106.0
    assert levels["2-ATR band"] == pytest.approx(95.0)
    assert levels["screener stop reference"] == 90.5
    assert levels["portfolio manager price target"] == 112.0


def test_levels_skip_indicators_that_are_absent_rather_than_writing_zero():
    result = FakeResult(evidence=FakeEvidence(FakeIndicators({"close_50_sma": None, "atr": 0})))
    levels = levels_from(result)
    assert "50-day SMA" not in levels
    assert "2-ATR band" not in levels


def test_a_screener_field_that_is_not_a_number_is_dropped_quietly():
    result = FakeResult(
        evidence=FakeEvidence(FakeIndicators({})), screener={"stop_ref": "n/a", "entry_ref": 0}
    )
    assert levels_from(result) == {}


def test_a_degraded_deep_dive_is_carried_not_dropped():
    """A missing verdict must still appear in section 6, with its reason."""
    context = build_options_context([FakeResult(symbol="ABC")], date(2026, 8, 14))
    assert [v.symbol for v in context.verdicts] == ["ABC"]
    assert context.verdicts[0].rating == "DEGRADED"
    assert context.verdicts[0].spot is None


def test_the_context_round_trips_through_json():
    context = build_options_context(
        [
            FakeResult(
                decision=FakeDecision(),
                evidence=FakeEvidence(FakeIndicators({"close_50_sma": 96.0}), dividend_yield=3.1),
            )
        ],
        date(2026, 8, 14),
        data_as_of="2026-08-13 close",
    )
    restored = OptionsContext.from_json(context.to_json())
    assert restored.date == date(2026, 8, 14)
    assert restored.data_as_of == "2026-08-13 close"
    row = restored.verdicts[0]
    assert isinstance(row, VerdictRow)
    assert (row.rating, row.price_target, row.dividend_yield_pct) == ("Overweight", 112.0, 3.1)
    assert row.levels["50-day SMA"] == 96.0


def test_a_stale_schema_version_is_refused_rather_than_read_thin():
    payload = json.dumps({**json.loads(OptionsContext(run_date="2026-08-14").to_json()), "version": 0})
    with pytest.raises(ValueError, match="re-run the deep stage"):
        OptionsContext.from_json(payload)


def test_select_filters_to_the_requested_tickers():
    context = OptionsContext(
        run_date="2026-08-14",
        verdicts=[VerdictRow(symbol="KMI"), VerdictRow(symbol="V"), VerdictRow(symbol="VZ")],
    )
    assert [v.symbol for v in context.select(["kmi", "vz"])] == ["KMI", "VZ"]
    assert len(context.select(None)) == 3
