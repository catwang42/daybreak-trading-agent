"""The experiment ledger and the provenance stamped on every row.

Three properties are load-bearing and each has a test that would fail loudly if
it regressed:

1. **The config hash never contains a credential.** The hash is written to a
   file we mirror into a GCS bucket. It is built by allow-list, and the test
   asserts the allow-list actually holds by putting a fake secret on a settings
   object and checking it is absent from the hashed payload.
2. **One run id per daily run, not per process.** ``--stage deep`` run at noon
   must file under the same experiment as the 06:00 discovery that produced the
   snapshot it reads, or every day's evidence is split in two.
3. **Append-only, last-write-wins.** Re-running a stage supersedes rather than
   duplicates, because a duplicated decision is a doubled sample.
"""

from datetime import date

import pytest

from tradingagent.config import Preferences, Settings
from tradingagent.evaluation.ledger import (
    CANDIDATES,
    DECISIONS,
    OUTCOMES,
    RUNS,
    CandidateRecord,
    DecisionRecord,
    ExperimentLedger,
    OutcomeRecord,
    RunRecord,
)
from tradingagent.evaluation.provenance import (
    Provenance,
    build_provenance,
    config_hash,
    config_payload,
    digest,
    prompt_versions,
    run_id_for,
)

RUN_DATE = date(2026, 8, 16)


def make_settings(**overrides) -> Settings:
    base = dict(
        fast_model="vendor/fast-1",
        smart_model="vendor/smart-1",
        deep_model="vendor/deep-1",
        vertex_project="proj",
        vertex_location="global",
        llm_max_retries=4,
        llm_timeout=120,
        alpaca_key="AK-not-a-real-key",
        alpaca_secret="SECRET-not-a-real-key",
        alpaca_paper=True,
        finnhub_key="FH-not-a-real-key",
        fred_key="",
        sec_user_agent="",
        reports_bucket="",
        deep_ticker_cap=5,
        debate_rounds=1,
        preferences=Preferences(),
        run_date=RUN_DATE,
    )
    base.update(overrides)
    return Settings(**base)


def prov(**overrides) -> Provenance:
    base = dict(run_id="run-2026-08-16-025713Z", run_date="2026-08-16")
    base.update(overrides)
    return Provenance(**base)


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def test_config_hash_carries_no_credential():
    """The allow-list is the guardrail, so test the guardrail and not the list.

    CLAUDE.md: secrets never reach a log, a report or the bucket. The hashed
    payload goes into a mirrored file, so anything that can be reversed or
    grepped out of it is a leak.
    """
    settings = make_settings()
    payload = repr(config_payload(settings))
    for secret in (settings.alpaca_key, settings.alpaca_secret, settings.finnhub_key):
        assert secret not in payload
    assert "alpaca" not in payload.lower()
    assert "finnhub" not in payload.lower()


def test_config_hash_moves_when_behaviour_moves_and_not_otherwise():
    baseline = config_hash(make_settings())
    assert config_hash(make_settings(alpaca_key="rotated")) == baseline, (
        "rotating a key does not change what the run decides"
    )
    assert config_hash(make_settings(debate_rounds=2)) != baseline
    assert config_hash(make_settings(pm_tier="smart")) != baseline, (
        "the A/B arm must be visible in the fingerprint"
    )


def test_preferences_are_inside_the_fingerprint():
    tighter = Preferences(min_market_cap=5e9)
    assert config_hash(make_settings(preferences=tighter)) != config_hash(make_settings())


def test_digest_is_stable_and_short():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert len(digest("anything")) == 12


def test_prompt_versions_hash_every_role_prompt(tmp_path):
    (tmp_path / "trader.md").write_text("buy low")
    (tmp_path / "notes.txt").write_text("ignored")
    versions = prompt_versions(tmp_path)
    assert set(versions) == {"trader"}
    (tmp_path / "trader.md").write_text("buy lower")
    assert prompt_versions(tmp_path)["trader"] != versions["trader"]


def test_prompt_versions_covers_the_shipped_prompts():
    versions = prompt_versions()
    assert {"trader", "portfolio_manager", "research_manager"} <= set(versions)


def test_run_id_comes_from_the_snapshot_not_the_clock():
    """A later stage reading this morning's snapshot is the same experiment."""
    snap = "snap-2026-08-16-market-025713Z"
    assert run_id_for(RUN_DATE, snap) == "run-2026-08-16-025713Z"
    assert run_id_for(RUN_DATE, snap) == run_id_for(RUN_DATE, snap)


def test_run_id_falls_back_to_the_clock_without_a_snapshot():
    got = run_id_for(RUN_DATE, "", observed="120000Z")
    assert got == "run-2026-08-16-120000Z"


def test_build_provenance_records_the_models(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "abc123")
    p = build_provenance(make_settings())
    assert p.models == {
        "fast": "vendor/fast-1",
        "smart": "vendor/smart-1",
        "deep": "vendor/deep-1",
    }
    assert p.git_commit == "abc123"
    assert p.backfilled is False


def test_provenance_round_trips():
    p = prov(snapshot_id="snap-x", models={"fast": "m"}, backfilled=True)
    assert Provenance.from_dict(p.to_dict()) == p


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------


def test_ledger_appends_rather_than_overwrites(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger")
    ledger.append(RUNS, [RunRecord(provenance=prov(), stage="discovery", candidates=40)])
    ledger.append(RUNS, [RunRecord(provenance=prov(), stage="deep", queued=5)])
    rows = ledger.read(RUNS)
    assert [r["stage"] for r in rows] == ["discovery", "deep"]


def test_a_rerun_supersedes_rather_than_doubles_the_sample(tmp_path):
    """Two rows, one observation. ``latest`` is what the grader reads."""
    ledger = ExperimentLedger(tmp_path / "ledger")
    first = DecisionRecord(
        provenance=prov(run_id="run-2026-08-16-025713Z"),
        ticker="WMB", date="2026-08-16", stage="deep", rating="Neutral",
    )
    replay = DecisionRecord(
        provenance=prov(run_id="run-2026-08-16-140000Z"),
        ticker="WMB", date="2026-08-16", stage="deep", rating="Overweight",
    )
    ledger.append(DECISIONS, [first])
    ledger.append(DECISIONS, [replay])

    assert len(ledger.read(DECISIONS)) == 2, "history is kept"
    latest = ledger.latest(DECISIONS, "decision_id")
    assert len(latest) == 1
    assert latest["2026-08-16:WMB:deep"]["rating"] == "Overweight"


def test_discovery_and_deep_are_distinct_decisions_on_the_same_name(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger")
    ledger.append(DECISIONS, [
        DecisionRecord(provenance=prov(), ticker="V", date="2026-08-16", stage="discovery"),
        DecisionRecord(provenance=prov(), ticker="V", date="2026-08-16", stage="deep"),
    ])
    assert len(ledger.latest(DECISIONS, "decision_id")) == 2


def test_the_whole_pool_is_recorded_not_just_the_shortlist(tmp_path):
    """A ranking can only be graded against the names it rejected."""
    ledger = ExperimentLedger(tmp_path / "ledger")
    pool = [
        CandidateRecord(
            provenance=prov(), ticker=f"T{i}", date="2026-08-16",
            screener_score=90 - i, screen_rank=i + 1, final_rank=i + 1,
            selected=i < 3,
        )
        for i in range(40)
    ]
    ledger.append(CANDIDATES, pool)
    rows = ledger.read(CANDIDATES)
    assert len(rows) == 40
    assert sum(1 for r in rows if r["selected"]) == 3
    assert sum(1 for r in rows if not r["selected"]) == 37


def test_candidate_id_is_unique_per_run_and_ticker(tmp_path):
    a = CandidateRecord(provenance=prov(run_id="run-a"), ticker="V", date="2026-08-16")
    b = CandidateRecord(provenance=prov(run_id="run-b"), ticker="V", date="2026-08-16")
    assert a.candidate_id == "run-a:V"
    assert a.key != b.key


def test_control_and_treatment_are_recorded_side_by_side():
    """The shipped shortlist is the price-only control; the shadow list is the arm."""
    row = CandidateRecord(
        provenance=prov(), ticker="ORCL", date="2026-08-16",
        screener_score=61, shadow_adjustment=4.0,
        per_signal_shadow={"news_tone": 2.0, "insider": 2.0},
        selected=False, counterfactual_selected=True,
    ).to_dict()
    assert row["selected"] is False
    assert row["counterfactual_selected"] is True
    assert row["signal_adjustment"] == 0.0, "signals are shadowed: they moved nothing"
    assert row["per_signal_shadow"] == {"insider": 2.0, "news_tone": 2.0}


def test_decision_row_carries_the_computed_plan_and_the_seat_tiers():
    row = DecisionRecord(
        provenance=prov(), ticker="WMB", date="2026-08-16", stage="deep",
        rating="Neutral", confidence="Medium", horizon="4-8 weeks",
        entry_condition="pullback to $71.50", invalidation="close below $69",
        target=78.0,
        trade_plan={"entry": 71.5, "stop": 69.0, "target": 78.0, "verdict": "NO TRADE"},
        seat_tiers={"portfolio_manager": "deep", "analyst_technical": "fast"},
    ).to_dict()
    assert row["trade_plan"]["verdict"] == "NO TRADE"
    assert row["seat_tiers"]["portfolio_manager"] == "deep"
    assert row["target"] == 78.0


def test_outcome_reports_only_matured_horizons():
    row = OutcomeRecord(
        provenance=prov(), decision_id="2026-08-16:WMB:deep", ticker="WMB",
        date="2026-08-16", stage="deep", as_of="2026-08-25",
        horizons={
            "1": {"return_pct": 0.8, "excess_spy_pct": 0.3, "excess_sector_pct": 0.1},
            "5": {"return_pct": 2.1, "excess_spy_pct": 1.4, "excess_sector_pct": 0.9},
        },
    )
    assert row.matured == [1, 5]
    assert row.excess(5) == 1.4
    assert row.excess(5, "sector") == 0.9
    assert row.excess(20) is None, "an unmatured horizon is absent, not zero"


def test_outcome_round_trips(tmp_path):
    row = OutcomeRecord(
        provenance=prov(backfilled=True), decision_id="d1", ticker="V",
        date="2026-08-16", stage="deep", as_of="2026-08-25",
        mfe_pct=3.2, mae_pct=-1.1, excursion_window=5,
        entry_triggered=True, stop_hit=False, target_hit=True, first_hit="target",
    )
    ledger = ExperimentLedger(tmp_path / "ledger")
    ledger.append(OUTCOMES, [row])
    back = OutcomeRecord.from_dict(ledger.read(OUTCOMES)[0])
    assert back == row
    assert back.provenance.backfilled is True


def test_a_truncated_line_does_not_take_the_report_down(tmp_path):
    """The ledger round-trips through a bucket; one bad row must not be fatal."""
    ledger = ExperimentLedger(tmp_path / "ledger")
    ledger.append(RUNS, [RunRecord(provenance=prov(), stage="discovery")])
    with ledger.path(RUNS).open("a", encoding="utf-8") as fh:
        fh.write('{"v": 1, "stage": "de\n')
    ledger.append(RUNS, [RunRecord(provenance=prov(), stage="options")])
    assert [r["stage"] for r in ledger.read(RUNS)] == ["discovery", "options"]


def test_reading_a_stream_that_was_never_written_is_empty(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger")
    assert ledger.read(DECISIONS) == []
    assert ledger.counts() == {RUNS: 0, CANDIDATES: 0, DECISIONS: 0, OUTCOMES: 0}


def test_ledger_sits_beside_the_journal(tmp_path):
    journal = tmp_path / "journal" / "journal.jsonl"
    ledger = ExperimentLedger.beside_journal(journal)
    assert ledger.root == tmp_path / "journal" / "ledger"


def test_appending_nothing_creates_nothing(tmp_path):
    ledger = ExperimentLedger(tmp_path / "ledger")
    assert ledger.append(RUNS, []) == 0
    assert not ledger.path(RUNS).exists()


@pytest.mark.parametrize("stream", [RUNS, CANDIDATES, DECISIONS, OUTCOMES])
def test_every_row_carries_provenance(tmp_path, stream):
    ledger = ExperimentLedger(tmp_path / "ledger")
    records = {
        RUNS: RunRecord(provenance=prov(), stage="discovery"),
        CANDIDATES: CandidateRecord(provenance=prov(), ticker="V", date="2026-08-16"),
        DECISIONS: DecisionRecord(provenance=prov(), ticker="V", date="2026-08-16", stage="deep"),
        OUTCOMES: OutcomeRecord(
            provenance=prov(), decision_id="d", ticker="V", date="2026-08-16", stage="deep"
        ),
    }
    ledger.append(stream, [records[stream]])
    row = ledger.read(stream)[0]
    assert row["provenance"]["run_id"] == "run-2026-08-16-025713Z"
    assert row["v"] == 1
