import pytest

from tradingagent.config import ConfigError, Preferences, _clean, load_settings, parse_preferences

PREFS = """# Preferences
## Target sectors (priority order)
1. Technology
2. Materials
3. Financials
## Universe
- US-listed equities, market cap > $2B, avg daily volume > 1M shares, options available
## Risk profile
- Style: swing / position
- Shortlist size: 5-10; deep-analysis cap: 3-5/day
"""


def test_parse_preferences_reads_every_knob():
    prefs = parse_preferences(PREFS)
    assert prefs.target_sectors == ["Technology", "Materials", "Financials"]
    assert prefs.min_market_cap == 2e9
    assert prefs.min_avg_volume == 1e6
    assert (prefs.shortlist_min, prefs.shortlist_max) == (5, 10)
    assert prefs.deep_cap == 3


def test_parse_preferences_falls_back_on_garbage():
    prefs = parse_preferences("nothing parseable here")
    assert prefs == Preferences(raw_markdown="nothing parseable here")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true    # guardrail: asserted at startup", "true"),
        ("# GCS bucket (empty = write locally)", ""),
        ("  vertex_ai/claude-haiku-4-5  ", "vertex_ai/claude-haiku-4-5"),
        (None, ""),
        ("", ""),
    ],
)
def test_clean_strips_inline_comments(raw, expected):
    assert _clean(raw) == expected


def test_live_trading_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(ConfigError, match="ALPACA_PAPER must be true"):
        load_settings(env_file=tmp_path / "absent.env")


def test_debate_rounds_capped_at_two(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("DEBATE_ROUNDS", "9")
    assert load_settings(env_file=tmp_path / "absent.env").debate_rounds == 2


def test_pm_tier_defaults_to_deep(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.delenv("PM_TIER", raising=False)
    assert load_settings(env_file=tmp_path / "absent.env").pm_tier == "deep"


def test_pm_tier_accepts_a_cheaper_arm(monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("PM_TIER", "SMART")
    assert load_settings(env_file=tmp_path / "absent.env").pm_tier == "smart"


def test_an_unknown_pm_tier_is_refused_rather_than_silently_defaulted(monkeypatch, tmp_path):
    """A typo'd arm that quietly falls back makes the two arms indistinguishable."""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("PM_TIER", "opus")
    with pytest.raises(ConfigError, match="PM_TIER must be one of"):
        load_settings(env_file=tmp_path / "absent.env")
