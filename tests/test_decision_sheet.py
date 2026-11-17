"""The decision sheet: what it says, what it refuses to say, and how it degrades."""

from __future__ import annotations

from datetime import date

import pytest

from tradingagent.presentation.context import (
    Avoid,
    Consensus,
    Gate,
    Overlay,
    OverlaySkip,
    PresentationContext,
    ReadingRow,
    Regime,
    SectorBar,
    SeriesPoint,
    Setup,
)
from tradingagent.presentation import charts, html
from tradingagent.presentation.sheet import (
    MAX_SETUPS,
    Change,
    DecisionSheet,
    badge_colours,
    build_sheet,
    diff_verdicts,
    previous_context,
)


def _context(run_date: str = "2026-08-16", **kwargs) -> PresentationContext:
    base = dict(
        market_as_of="2026-08-14",
        snapshot_id="deadbeef",
        session_note="market CLOSED",
        regime=Regime(
            composite=74.5,
            zone="Healthy",
            posture=ReadingRow(
                "BREADTH_POSTURE",
                "Healthy",
                "Healthy — 75-90% exposure [UNVALIDATED]",
                "UNVALIDATED",
            ),
            rotation=ReadingRow("SECTOR_ROTATION", "Early cycle", "Early-cycle rotation", "VALIDATED"),
            risk_regime="Neutral",
            risk_score=0.44,
            pct_above_50dma=70.5,
            universe_size=501,
            vix=14.25,
            leaders=["Energy"],
            laggards=["Utilities"],
            sectors=[SectorBar("Energy", 2.1), SectorBar("Utilities", -1.4)],
            spy=[SeriesPoint(f"2026-05-{d:02d}", 600.0 + d, 595.0, 580.0) for d in range(1, 29)],
        ),
        gates=[Gate("2026-08-20", "Initial Jobless Claims", "Medium", "FRED", "VERIFIED")],
        setups=[
            Setup(
                symbol="WMB",
                name="Williams Companies",
                rating="Buy",
                confidence="Medium",
                spot=75.2,
                price_target=82.0,
                entry=73.2,
                stop=71.5,
                target=82.0,
                risk_pct=2.32,
                reward_risk=5.2,
                size_pct=3.0,
                wait_condition="Wait for a pullback to $73.20 (50-day SMA)",
                consensus=Consensus("buy", 22, 78.5, 79.0),
                series=[SeriesPoint(f"2026-05-{d:02d}", 70.0 + d * 0.1) for d in range(1, 29)],
            )
        ],
        avoids=[Avoid("PYPL", "PayPal", "Underweight", "High", "Decelerating branded volume")],
        overlays=[
            Overlay(
                "DIS", "covered call", 110.0, "2026-09-18", 33, 0.36, 1.68, 17.4,
                108.32, 100.0, "clears invalidation by $8.32", "clear", [],
            )
        ],
        overlay_skips=[OverlaySkip("WMB", "no strike passed the hard filters")],
    )
    base.update(kwargs)
    return PresentationContext(run_date=run_date, **base)


def _sheet(**kwargs) -> DecisionSheet:
    context = kwargs.pop("context", _context())
    return DecisionSheet(run_date=date(2026, 8, 16), context=context, **kwargs)


# --- section 1: the regime line ---------------------------------------------


def test_the_unvalidated_marker_reaches_the_regime_line():
    # The posture band is a heuristic nobody has graded. If the sheet can print
    # it without the marker, the sheet has promoted it.
    assert "[UNVALIDATED]" in _sheet().regime_line


def test_the_regime_line_carries_both_readings():
    line = _sheet().regime_line
    assert "Healthy" in line and "Early-cycle rotation" in line


def test_posture_extras_omit_what_did_not_compute():
    regime = _context().regime
    regime.vix = None
    regime.leaders = []
    labels = [label for label, _ in _sheet(context=_context(regime=regime)).posture_extras]
    assert "VIX" not in labels and "Leading" not in labels
    assert "Risk regime" in labels


# --- section 3: how many setups ---------------------------------------------


def test_the_sheet_caps_the_setups_and_says_how_many_it_hid():
    context = _context(setups=[Setup(symbol=f"T{i}") for i in range(MAX_SETUPS + 3)])
    sheet = _sheet(context=context)
    assert len(sheet.setups) == MAX_SETUPS
    assert sheet.setups_hidden == 3


# --- section 6: the diff ----------------------------------------------------


def test_a_move_up_the_conviction_scale_reads_as_an_upgrade():
    today = _context(setups=[Setup(symbol="WMB", rating="Buy")], avoids=[])
    prior = _context("2026-08-14", setups=[Setup(symbol="WMB", rating="Overweight")], avoids=[])
    (change,) = diff_verdicts(today, prior)
    assert change.kind == "upgraded"
    assert change.detail == "Overweight → Buy"


def test_a_move_down_reads_as_a_downgrade_even_across_the_setup_avoid_split():
    # Buy -> Hold also moves the name from the setups list to the avoids list.
    # Comparing the merged verdict maps is what keeps that a downgrade rather
    # than a drop plus an unrelated new name.
    today = _context(setups=[], avoids=[Avoid("WMB", rating="Hold")])
    prior = _context("2026-08-14", setups=[Setup(symbol="WMB", rating="Buy")], avoids=[])
    (change,) = diff_verdicts(today, prior)
    assert change.kind == "downgraded" and change.symbol == "WMB"


def test_a_name_that_left_the_queue_is_reported_not_silently_dropped():
    today = _context(setups=[], avoids=[])
    prior = _context("2026-08-14", setups=[Setup(symbol="WMB", rating="Buy")], avoids=[])
    (change,) = diff_verdicts(today, prior)
    assert change.kind == "dropped" and "was Buy" in change.detail


def test_an_unchanged_verdict_produces_no_line():
    today = _context(setups=[Setup(symbol="WMB", rating="Buy")], avoids=[])
    prior = _context("2026-08-14", setups=[Setup(symbol="WMB", rating="Buy")], avoids=[])
    assert diff_verdicts(today, prior) == []


# --- picking yesterday ------------------------------------------------------


def test_the_comparison_skips_days_the_job_never_ran(tmp_path):
    # A Monday comparing itself against a Sunday that produced nothing would
    # report every name as new.
    (tmp_path / "2026-08-14").mkdir()
    _context("2026-08-14").write(tmp_path / "2026-08-14")
    (tmp_path / "2026-08-15").mkdir()  # weekend: no context written
    prior, name = previous_context(tmp_path, date(2026, 8, 17))
    assert name == "2026-08-14" and prior is not None


def test_a_session_with_no_verdicts_is_not_a_comparison(tmp_path):
    (tmp_path / "2026-08-14").mkdir()
    _context("2026-08-14", setups=[], avoids=[]).write(tmp_path / "2026-08-14")
    prior, name = previous_context(tmp_path, date(2026, 8, 17))
    assert prior is None and name == ""


def test_no_history_at_all_is_not_an_error(tmp_path):
    assert previous_context(tmp_path / "missing", date(2026, 8, 17)) == (None, "")


# --- section 7: confidence --------------------------------------------------


def test_a_clean_run_is_high_confidence():
    assert _sheet().confidence == "HIGH"


def test_degraded_sources_are_named_not_just_counted():
    sheet = _sheet(context=_context(degraded=["reddit", "finnhub"]))
    assert sheet.confidence == "MODERATE"
    assert sheet.confidence_reasons == ["degraded source: reddit", "degraded source: finnhub"]


def test_enough_degradation_drops_confidence_to_low():
    sheet = _sheet(context=_context(degraded=["a", "b", "c"]))
    assert sheet.confidence == "LOW"


def test_a_missing_context_is_low_confidence_with_a_reason(tmp_path):
    sheet = build_sheet(date(2026, 8, 16), tmp_path)
    assert sheet.context is None
    assert sheet.confidence == "LOW"
    assert sheet.confidence_reasons and "missing" in sheet.confidence_reasons[0]


def test_build_sheet_reads_the_session_and_diffs_against_the_last_one(tmp_path):
    today = tmp_path / "2026-08-16"
    today.mkdir()
    _context().write(today)
    yesterday = tmp_path / "2026-08-14"
    yesterday.mkdir()
    _context("2026-08-14", setups=[Setup(symbol="WMB", rating="Overweight")], avoids=[]).write(
        yesterday
    )
    sheet = build_sheet(date(2026, 8, 16), today, reports_dir=tmp_path, evidence="7 of 12 correct.")
    assert sheet.compared_with == "2026-08-14"
    assert [c.kind for c in sheet.changes if c.symbol == "WMB"] == ["upgraded"]
    assert sheet.evidence == "7 of 12 correct."


def test_a_session_with_no_spy_history_says_the_chart_is_missing(tmp_path):
    regime = _context().regime
    regime.spy = []
    directory = tmp_path / "2026-08-16"
    directory.mkdir()
    _context(regime=regime).write(directory)
    sheet = build_sheet(date(2026, 8, 16), directory)
    assert any("SPY" in reason for reason in sheet.unavailable)


# --- badges -----------------------------------------------------------------


def test_buy_and_overweight_are_told_apart_by_colour():
    assert badge_colours("Buy") != badge_colours("Overweight")


def test_an_unknown_rating_still_gets_a_readable_badge():
    ink, tint = badge_colours("Wildly Bullish")
    assert ink.startswith("#") and tint.startswith("#")


# --- the HTML ---------------------------------------------------------------


def test_the_body_has_no_style_block_because_gmail_deletes_them():
    body = html.render_sheet(_sheet())
    assert "<style" not in body and "<head" not in body
    assert 'style="' in body


def test_the_body_states_the_computed_levels_not_a_pointer_to_the_attachment():
    body = html.render_sheet(_sheet())
    assert "$73.20" in body and "$71.50" in body and "$82.00" in body


def test_the_unvalidated_marker_survives_escaping_into_the_html():
    assert "[UNVALIDATED]" in html.render_sheet(_sheet())


def test_an_indicative_gate_never_appears_because_it_never_reaches_the_context():
    # The filtering happens in build.build_gates; this asserts the renderer does
    # not reintroduce anything, and that an empty list still explains itself.
    body = html.render_sheet(_sheet(context=_context(gates=[])))
    assert "VERIFIED" in body and "Do not act before" in body


def test_a_ticker_name_with_markup_in_it_is_escaped():
    context = _context(avoids=[Avoid("EVIL", reason="<script>alert(1)</script>")])
    body = html.render_sheet(_sheet(context=context))
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_charts_are_referenced_by_content_id_so_they_embed():
    context = _context()
    drawn = charts.render_charts(context)
    body = html.render_sheet(_sheet(context=context), drawn)
    assert drawn, "the sample context should draw at least one chart"
    for chart in drawn:
        assert f"cid:{chart.cid}" in body


def test_every_chart_has_alt_text_for_a_client_with_images_off():
    for chart in charts.render_charts(_context()):
        assert len(chart.alt) > 10


def test_a_missing_context_renders_a_body_that_says_so_rather_than_crashing():
    sheet = DecisionSheet(
        run_date=date(2026, 8, 16), context=None, unavailable=["no data file for this session"]
    )
    body = html.render_sheet(sheet)
    assert "Daybreak" in body and "no data file" in body


def test_the_disclaimer_is_always_in_the_body():
    for sheet in (_sheet(), DecisionSheet(run_date=date(2026, 8, 16))):
        assert "not investment advice" in html.render_sheet(sheet)


def test_an_unpriced_plan_shows_its_reason_instead_of_a_row_of_dashes():
    context = _context(
        setups=[Setup(symbol="DIS", rating="Overweight", status="rejected: reward:risk 1.4x")]
    )
    body = html.render_sheet(_sheet(context=context))
    assert "reward:risk 1.4x" in body
    assert "Entry" not in body


def test_the_plain_text_alternative_carries_the_same_levels():
    text = html.render_text(_sheet())
    assert "$73.20" in text and "[UNVALIDATED]" in text
    assert "not investment advice" in text


def test_the_plain_text_alternative_survives_a_missing_context():
    text = html.render_text(DecisionSheet(run_date=date(2026, 8, 16), unavailable=["no data"]))
    assert "no data" in text


def test_the_overlay_row_states_where_the_breakeven_sits():
    body = html.render_sheet(_sheet())
    assert "clears invalidation by $8.32" in body
    assert "no strike passed the hard filters" in body


def test_changes_with_no_prior_session_say_so_rather_than_showing_nothing():
    body = html.render_sheet(_sheet(compared_with="", changes=[]))
    assert "No earlier session" in body


def test_changes_that_are_empty_against_a_real_prior_say_nothing_moved():
    body = html.render_sheet(_sheet(compared_with="2026-08-14", changes=[]))
    assert "Nothing changed against 2026-08-14" in body


def test_the_change_lines_render():
    body = html.render_sheet(_sheet(compared_with="2026-08-14", changes=[Change("WMB", "upgraded", "Overweight → Buy")]))
    assert "Overweight → Buy" in body


# --- the PDFs ---------------------------------------------------------------


def test_a_markdown_report_becomes_a_pdf():
    from tradingagent.presentation import pdf

    rendered = pdf.render_markdown("# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", "x.pdf")
    if rendered is None:
        pytest.skip("WeasyPrint's system libraries are not installed here")
    assert rendered.data.startswith(b"%PDF")
    assert rendered.filename == "x.pdf"


def test_a_missing_report_file_is_a_none_not_a_crash(tmp_path):
    from tradingagent.presentation import pdf

    assert pdf.render_report(tmp_path / "nope.md") is None


def test_the_pdf_is_named_after_the_report_it_renders(tmp_path):
    from tradingagent.presentation import pdf

    source = tmp_path / "WMB.md"
    source.write_text("# WMB\n\nText.\n")
    rendered = pdf.render_report(source)
    if rendered is None:
        pytest.skip("WeasyPrint's system libraries are not installed here")
    assert rendered.filename == "WMB.pdf"


def test_markdown_tables_survive_the_conversion():
    from tradingagent.presentation import pdf

    body = pdf.markdown_body("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<table>" in body and "<td>" in body
