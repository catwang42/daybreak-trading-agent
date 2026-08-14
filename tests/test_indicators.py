import numpy as np
import pandas as pd
import pytest

from tradingagent.data.indicators import (
    atr,
    bollinger,
    compute_indicators,
    money_flow_index,
    rsi,
    sma,
    vwma,
)


def frame(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs if highs is not None else [c * 1.01 for c in closes],
            "Low": lows if lows is not None else [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": volumes if volumes is not None else [1_000_000] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


def test_short_history_returns_none_rather_than_nan():
    """A thin series must degrade one line, not poison the report with NaN."""
    closes = [10.0, 11.0, 12.0]
    f = frame(closes)
    assert sma(f["Close"], 50) is None
    assert rsi(f["Close"]) is None
    assert atr(f) is None
    assert vwma(f) is None
    assert money_flow_index(f) is None
    assert bollinger(f["Close"]) == (None, None, None)


def test_rsi_saturates_at_100_on_an_unbroken_advance():
    """The divide-by-zero branch: no down-closes means RSI is 100 by definition."""
    closes = [100.0 + i for i in range(40)]
    assert rsi(pd.Series(closes)) == pytest.approx(100.0)


def test_rsi_is_neutral_on_a_flat_series():
    assert rsi(pd.Series([50.0] * 40)) == pytest.approx(50.0)


def test_rsi_lands_between_the_extremes_on_a_mixed_series():
    rng = np.random.default_rng(7)
    closes = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    value = rsi(closes)
    assert value is not None and 0 < value < 100


def test_sma_matches_a_hand_computed_mean():
    closes = pd.Series([float(i) for i in range(1, 61)])
    assert sma(closes, 50) == pytest.approx(sum(range(11, 61)) / 50)


def test_bollinger_bands_straddle_the_mean_and_widen_with_volatility():
    calm = pd.Series([100.0 + (i % 2) * 0.1 for i in range(40)])
    wild = pd.Series([100.0 + (i % 2) * 10.0 for i in range(40)])
    mid_c, up_c, low_c = bollinger(calm)
    mid_w, up_w, low_w = bollinger(wild)
    assert low_c < mid_c < up_c
    assert (up_w - low_w) > (up_c - low_c)


def test_atr_tracks_the_true_range_of_a_steady_series():
    """Constant 2% daily range, no gaps: ATR converges on that range."""
    closes = [100.0] * 60
    f = frame(closes, highs=[101.0] * 60, lows=[99.0] * 60)
    assert atr(f) == pytest.approx(2.0, abs=0.05)


def test_vwma_is_pulled_toward_the_heavily_traded_price():
    closes = [10.0] * 19 + [20.0]
    volumes = [1] * 19 + [1_000_000]
    value = vwma(frame(closes, volumes=volumes))
    assert value is not None and value > 19.9


def test_money_flow_index_is_100_when_every_bar_closes_up():
    closes = [100.0 + i for i in range(30)]
    assert money_flow_index(frame(closes)) == pytest.approx(100.0)


def test_compute_indicators_renders_every_row_even_when_some_are_unavailable():
    """120 bars is not enough for a 200-day SMA; that row must still appear."""
    closes = [100.0 + i * 0.5 for i in range(120)]
    result = compute_indicators("TST", frame(closes))

    assert result.close == pytest.approx(closes[-1])
    assert result.sessions == 120
    assert result.get("close_200_sma") is None
    assert result.get("close_50_sma") is not None

    table = result.markdown()
    assert "200-day SMA" in table and "unavailable" in table
    # One header row, one separator, one row per indicator.
    assert table.count("\n|") == len(result.indicators) + 2
