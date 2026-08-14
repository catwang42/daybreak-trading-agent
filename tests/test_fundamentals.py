"""Unit handling in the fundamentals renderer.

yfinance is an undocumented scrape and its units are not uniform: most ratio
fields arrive as ratios (0.23 = 23%), but `dividendYield` and `debtToEquity`
arrive already in percentage points. Rendering them all the same way is a
silent data error — the analyst reads the table and believes it.
"""

import pytest

from tradingagent.data import fundamentals as F
from tradingagent.data.fundamentals import Fundamentals, Positioning


def test_dividend_yield_is_rendered_in_the_units_yfinance_actually_returns():
    """CRM 2026-08-14: `dividendYield` of 0.91 means 0.91%, not 91%.

    The fundamentals analyst read a 91% yield off this table and built a
    'suspiciously high payout' argument on it.
    """
    table = Fundamentals(symbol="CRM", dividend_yield=0.91).markdown()
    assert "| Dividend yield | 0.91% |" in table
    assert "91.0%" not in table


def test_debt_to_equity_is_rendered_as_a_multiple_not_a_bare_percentage():
    """124.28 percentage points is 1.24x equity, not a 124x leverage ratio."""
    table = Fundamentals(symbol="CRM", debt_to_equity=124.282).markdown()
    assert "| Debt / equity | 1.24x |" in table


def test_true_ratio_fields_are_still_scaled_to_percent():
    table = Fundamentals(symbol="CRM", profit_margin=0.1634, revenue_growth=0.075).markdown()
    assert "| Profit margin | +16.3% |" in table
    assert "| Revenue growth (yoy) | +7.5% |" in table


def test_missing_metrics_render_as_unavailable_rather_than_blank_or_zero():
    table = Fundamentals(symbol="CRM").markdown()
    # 16 metric rows, plus the line saying the statements themselves are missing.
    assert table.count("unavailable") == 17
    assert "Quarterly statements: unavailable" in table


def test_quarterly_rows_derive_net_margin_and_survive_a_missing_income_line():
    table = Fundamentals(
        symbol="CRM",
        quarters=[
            {"period": "2026-04-30", "revenue": 1000.0, "net_income": 200.0},
            {"period": "2026-01-31", "revenue": 900.0, "net_income": None},
        ],
    ).markdown()
    assert "+20.0%" in table
    assert "| 2026-01-31 | $900 | unavailable | unavailable |" in table


def test_positioning_states_the_gap_between_price_and_the_mean_target():
    block = Positioning(symbol="CRM", target_mean=250.0).markdown(price=200.0)
    assert "-20.0% versus that mean target" in block


def test_positioning_omits_the_gap_when_either_side_is_missing():
    assert "versus that mean target" not in Positioning(symbol="CRM").markdown(price=200.0)
    assert "versus that mean target" not in Positioning(
        symbol="CRM", target_mean=250.0
    ).markdown(price=None)


# --- plausibility ranges (Gate 2 pre-work) --------------------------------


def test_every_plausible_range_is_ordered_and_names_a_real_field():
    """A typo in the key would silently disable the check for that field."""
    owners = set(dict(F.Fundamentals.ROWS).values()) | set(dict(F.Positioning.ROWS).values())
    for name, (low, high) in F.PLAUSIBLE.items():
        assert low < high, f"{name}: range is inverted"
        assert name in owners, f"{name}: no rendered row reads this range"
    for _, name in F.Fundamentals.ROWS + F.Positioning.ROWS:
        assert name in F.PLAUSIBLE, f"{name}: rendered but never range-checked"


@pytest.mark.parametrize(
    "field_name, ok, bad",
    [
        ("dividend_yield", 0.91, 91.0),      # the yfinance units switch
        ("profit_margin", 0.18, 1.8),        # a ratio mistaken for percentage points
        ("revenue_growth", -0.12, -4.0),     # revenue cannot fall 400%
        ("debt_to_equity", 124.28, 250_000.0),  # 2500x equity is a vendor artefact
        ("beta", 1.14, 42.0),
        ("market_cap", 2.4e11, 500.0),
    ],
)
def test_is_suspect_catches_the_unit_slips_it_was_built_for(field_name, ok, bad):
    assert F.is_suspect(field_name, ok) is False
    assert F.is_suspect(field_name, bad) is True
    assert F.is_suspect(field_name, None) is False


def test_suspect_values_are_marked_in_the_pack_rather_than_passed_through():
    snapshot = F.Fundamentals(
        symbol="TST", market_cap=2.4e11, trailing_pe=31.2, dividend_yield=91.0, profit_margin=0.18
    )
    assert snapshot.suspect_fields() == ["Dividend yield"]

    text = snapshot.markdown()
    assert "91.00%" in text                     # the number is still shown, not hidden
    assert F.SUSPECT_MARK.strip() in text       # but flagged where the analyst reads it
    assert "**SUSPECT** (Dividend yield)" in text
    assert "Treat them as unavailable" in text
    # A plausible neighbour on the same row set is left alone.
    assert f"31.20{F.SUSPECT_MARK}" not in text


def test_a_clean_snapshot_carries_no_suspect_marks():
    snapshot = F.Fundamentals(
        symbol="TST", market_cap=2.4e11, trailing_pe=31.2, profit_margin=0.18,
        debt_to_equity=124.28, dividend_yield=0.91, beta=1.14,
    )
    assert snapshot.suspect_fields() == []
    assert "SUSPECT" not in snapshot.markdown()


def test_positioning_flags_an_impossible_short_interest():
    pos = F.Positioning(symbol="TST", short_percent_of_float=4.2, held_by_insiders=0.03)
    assert pos.suspect_fields() == ["Short interest (% of float)"]
    text = pos.markdown(price=100.0)
    assert F.SUSPECT_MARK.strip() in text
    assert "**SUSPECT** (Short interest (% of float))" in text
