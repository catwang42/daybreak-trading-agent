"""Unit handling in the fundamentals renderer.

yfinance is an undocumented scrape and its units are not uniform: most ratio
fields arrive as ratios (0.23 = 23%), but `dividendYield` and `debtToEquity`
arrive already in percentage points. Rendering them all the same way is a
silent data error — the analyst reads the table and believes it.
"""

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
