import pytest

from tradingagent.pipeline.prompts_loader import PROMPTS_DIR, PromptError, load_prompt, render


def test_shipped_prompts_are_present():
    for name in ("quick_take", "market_commentary"):
        assert load_prompt(name).strip()


def test_render_accepts_a_placeholder_literally_called_name():
    """`render` is positional-only precisely so this cannot regress."""
    out = render("quick_take", **_quick_take_fields())
    assert "Test Co" in out and "TST" in out


def test_missing_placeholder_raises_instead_of_shipping_braces():
    with pytest.raises(PromptError, match="needs placeholder"):
        render("quick_take", symbol="TST")


def test_prompts_are_provider_agnostic_markdown():
    for path in PROMPTS_DIR.glob("*.md"):
        text = path.read_text().lower()
        for vendor in ("anthropic", "openai", "vertex_ai/", "gpt-", "gemini/", "claude-"):
            assert vendor not in text, f"{path.name} mentions {vendor}"


def _quick_take_fields():
    return dict(
        symbol="TST", name="Test Co", sector="Information Technology", industry="Software",
        price=100.0, day_gain_pct=5.0, score=85, rating="A-", state="ACTIONABLE_DAY1",
        triggers="4pct_breakout", volume_ratio_20d=3.0, close_location_pct=95.0,
        prior_base_days=12, base_width_pct=6.0, entry_ref=101.0, stop_ref=98.0, risk_pct=3.0,
        dist_52w_high_pct=-2.0, trend_note="above both", rs_note="+12", reject_reasons="none",
        breadth_composite=70.0, breadth_zone="Healthy", breadth_guidance="Normal operations.",
        risk_regime="Risk-On", cycle_phase="Mid Cycle Expansion", sector_note="ranks 1/11",
        earnings_note="none in 10 days", news_note="none retrieved",
    )
