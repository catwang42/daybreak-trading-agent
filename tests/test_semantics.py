"""The vocabulary guards: a label may not be read as something adjacent.

Every phrase banned here appeared in a shipped report.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tradingagent.discovery.sectors import ROTATION_PATTERN, SectorMap
from tradingagent.report.render import render_daily_brief
from tradingagent.semantics import (
    GLOSSARY,
    INSIDER_PLANNED_SALE,
    UNVALIDATED,
    Reading,
    guard_block,
)
from tradingagent.signals.insiders import InsiderTrade, summarize
from tests.test_report_and_journal import context

SRC = Path(__file__).resolve().parents[1] / "src" / "tradingagent"


# --- the terms themselves --------------------------------------------------


def test_every_reading_carries_its_label_and_its_prohibitions():
    reading = Reading(GLOSSARY["breadth_cycle_position"], "mid-range", basis="score 55/100")
    assert reading.canonical_label == "Breadth cycle position"
    assert "a valuation" in reading.forbidden_interpretations
    assert reading.to_dict()["forbidden_interpretations"][0] == "a valuation"


def test_the_breadth_cycle_component_is_never_a_valuation():
    guard = GLOSSARY["breadth_cycle_position"].guard()
    assert "peak and trough" in guard
    assert "It is NOT a valuation" in guard


def test_the_exposure_band_is_marked_unvalidated_wherever_it_prints():
    posture = GLOSSARY["breadth_posture"]
    assert posture.validation == UNVALIDATED
    assert posture.render("Healthy — 75-90%").endswith("[UNVALIDATED]")
    assert "never been validated" in posture.guard()
    assert "a position-sizing instruction" in posture.forbidden_interpretations


# --- the sector cycle table ------------------------------------------------


def test_the_cycle_phase_is_described_as_a_pattern_not_an_economy():
    reading = SectorMap(cycle_phase="Early Cycle Recovery", cycle_confidence="Medium").rotation_reading()
    assert reading.describe() == "Sector rotation pattern: early-cycle-like (match confidence Medium)"
    assert "a statement about where the economy is in its business cycle" in (
        reading.forbidden_interpretations
    )


def test_every_phase_in_the_table_has_a_pattern_word():
    from tradingagent.discovery.sectors import CYCLE_PHASES

    assert set(CYCLE_PHASES) | {"Undetermined"} == set(ROTATION_PATTERN)


# --- what actually reaches a reader ----------------------------------------


def test_the_brief_no_longer_calls_a_lookup_an_economic_reading():
    brief = render_daily_brief(context())
    assert "Estimated cycle phase" not in brief
    assert "Suggested equity exposure" not in brief
    assert "Sector rotation pattern: mid-cycle-like" in brief
    assert "Breadth regime + posture" in brief and "[UNVALIDATED]" in brief
    assert "not a position size" in brief


def test_the_commentary_prompt_ships_the_prohibitions_with_the_numbers():
    block = guard_block("breadth_posture", "sector_rotation", "breadth_cycle_position")
    for phrase in ("a position-sizing instruction", "an economic forecast", "a valuation"):
        assert phrase in block


@pytest.mark.parametrize("banned", ["Estimated cycle phase", "Suggested equity exposure"])
def test_the_retired_phrasings_are_gone_from_the_prompts(banned):
    for path in (SRC / "pipeline" / "prompts").glob("*.md"):
        assert banned not in path.read_text(), path.name


# --- 10b5-1 ----------------------------------------------------------------


def test_a_planned_sale_is_reported_as_non_directional():
    trades = [
        InsiderTrade(
            symbol="TST", filed=date(2026, 8, 10), insider="An Officer", role="CFO",
            code="S", shares=1_000.0, price=100.0, planned=True,
        )
    ]
    signal = summarize("TST", trades, date(2026, 8, 14))
    assert signal.direction == 0
    assert "NON-DIRECTIONAL" in signal.headline
    assert "not a loss of conviction" in signal.headline


def test_the_sentiment_analyst_is_told_the_rule():
    prompt = (SRC / "pipeline" / "prompts" / "analyst_sentiment.md").read_text()
    assert "10b5-1 sale is not" in prompt
    for banned in INSIDER_PLANNED_SALE.forbidden_interpretations[:2]:
        assert banned in prompt, banned
