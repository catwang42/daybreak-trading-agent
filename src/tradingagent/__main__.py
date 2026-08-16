"""Entrypoint: ``python -m tradingagent [--stage discovery|deep|options|report|all|outcomes|evaluate]``.

Runs headless in a container (Cloud Run Jobs) or from a shell. Asserts
``ALPACA_PAPER=true`` at startup — the process refuses to start otherwise.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from enum import Enum
from typing import Optional

import typer

from .config import ConfigError, load_settings

app = typer.Typer(add_completion=False, help="Daily trading research agent (research only).")


class Stage(str, Enum):
    discovery = "discovery"
    deep = "deep"
    options = "options"
    report = "report"
    all = "all"
    # M7. Both read records and bars; neither spends a token or writes a
    # recommendation, which is why they are safe to schedule separately and
    # to re-run over the same day as often as you like.
    outcomes = "outcomes"
    evaluate = "evaluate"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "LiteLLM", "litellm", "yfinance", "peewee", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def main(
    stage: Stage = typer.Option(Stage.discovery, "--stage", help="Pipeline stage to run."),
    run_date: Optional[str] = typer.Option(
        None, "--date", help="ISO date to label the run (defaults to today)."
    ),
    shortlist_size: Optional[int] = typer.Option(
        None, "--shortlist", help="Override shortlist size from preferences.md."
    ),
    universe_limit: Optional[int] = typer.Option(
        None, "--limit", help="Screen only the first N universe names (smoke tests)."
    ),
    refresh_universe: bool = typer.Option(
        False, "--refresh-universe", help="Re-scrape the S&P 500 constituent list."
    ),
    skip_llm: bool = typer.Option(
        False, "--skip-llm", help="Run data + screening with zero LLM spend."
    ),
    tickers: Optional[str] = typer.Option(
        None, "--tickers", help="Comma-separated deep-stage override, e.g. 'ADSK,V,FDX'."
    ),
    pm_tier: Optional[str] = typer.Option(
        None,
        "--pm-tier",
        help="Tier for the portfolio manager's verdict (fast|smart|deep). A/B arm; logged in the ledger.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    _configure_logging(verbose)
    log = logging.getLogger("tradingagent")

    try:
        parsed_date = date.fromisoformat(run_date) if run_date else None
    except ValueError:
        typer.secho(f"Invalid --date '{run_date}'; expected YYYY-MM-DD.", fg="red", err=True)
        raise typer.Exit(2)

    # Set before load_settings so the override lands inside the config hash: an
    # A/B arm chosen on the command line has to be as visible to the ledger as
    # one chosen in the environment, or the two days are indistinguishable.
    if pm_tier:
        os.environ["PM_TIER"] = pm_tier

    try:
        settings = load_settings(run_date=parsed_date)
    except ConfigError as exc:
        typer.secho(f"Configuration refused: {exc}", fg="red", err=True)
        raise typer.Exit(2)

    only = [t.strip().upper() for t in tickers.split(",") if t.strip()] if tickers else None
    log.info("Stage=%s date=%s paper=%s", stage.value, settings.run_date, settings.alpaca_paper)

    # Stateless containers start with an empty disk, so the journal has to come
    # back from GCS before anything reads it — the source-accuracy tracker
    # weights signals against weeks of history it would otherwise not have.
    from .storage import mirror_journal, mirror_ledger, restore_journal, restore_ledger

    if settings.reports_bucket:
        restored = restore_journal(settings.reports_bucket, settings.journal_path)
        log.info("Journal restored from GCS: %d entries", restored)
        # The outcomes job resolves horizons written weeks ago, so it needs the
        # whole ledger back before it can find anything to resolve.
        streams = restore_ledger(settings.reports_bucket, settings.ledger_root)
        if streams:
            log.info("Ledger restored from GCS: %s", streams)

    from .delivery.email import DeliveryError
    from .delivery.stage import run_report, verdicts_from_results
    from .stages import run_all, run_deep, run_discovery, run_options

    def dispatch() -> None:
        if stage in (Stage.outcomes, Stage.evaluate):
            from .evaluation.stage import run_evaluate, run_outcomes

            if stage is Stage.outcomes:
                _echo_outcomes(run_outcomes(settings))
            else:
                _echo_evaluate(run_evaluate(settings))
            return

        if stage is Stage.report:
            # Re-delivery only: no market data, no LLM calls, no journal writes.
            try:
                _echo_delivery(run_report(settings))
            except DeliveryError as exc:
                typer.secho(f"Delivery failed: {exc}", fg="red", err=True)
                raise typer.Exit(1)
            return

        if stage is Stage.options:
            if skip_llm:
                typer.secho(
                    "--skip-llm makes the options strategist a no-op; nothing to run.",
                    fg="red",
                    err=True,
                )
                raise typer.Exit(2)
            try:
                options = run_options(settings, only=only)
            except FileNotFoundError as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(2)
            _echo_options(settings, options)
            return

        if stage is Stage.deep:
            if skip_llm:
                typer.secho("--skip-llm makes the deep stage a no-op; nothing to run.", fg="red", err=True)
                raise typer.Exit(2)
            try:
                deep = run_deep(settings, only=only)
            except FileNotFoundError as exc:
                typer.secho(str(exc), fg="red", err=True)
                raise typer.Exit(2)
            _echo_deep(settings, deep)
            return

        if stage is Stage.all:
            if skip_llm:
                typer.secho("--skip-llm makes the deep stage a no-op; nothing to run.", fg="red", err=True)
                raise typer.Exit(2)
            discovery, deep, options = run_all(
                settings,
                refresh_universe=refresh_universe,
                universe_limit=universe_limit,
                shortlist_size=shortlist_size,
                only=only,
            )
            _echo_discovery(settings, discovery)
            _echo_deep(settings, deep)
            _echo_options(settings, options)

            # Delivery is last, after every artefact is on disk and in GCS, so a
            # send failure costs the email and not the run. It still exits
            # non-zero: in Cloud Run a silent non-delivery is indistinguishable
            # from a job that never fired, and that is worth being loud about.
            try:
                _echo_delivery(
                    run_report(
                        settings,
                        verdicts=verdicts_from_results(deep.results),
                        degraded=discovery.context.degraded,
                    )
                )
            except DeliveryError as exc:
                typer.secho(f"Delivery failed (reports are written): {exc}", fg="red", err=True)
                raise typer.Exit(1)
            return

        result = run_discovery(
            settings,
            refresh_universe=refresh_universe,
            universe_limit=universe_limit,
            shortlist_size=shortlist_size,
            skip_llm=skip_llm,
        )
        _echo_discovery(settings, result)

    try:
        dispatch()
    finally:
        # In `finally` on purpose: a run that aborted halfway may still have
        # journaled a shortlist, and losing those entries would quietly corrupt
        # the accuracy tracker's denominator.
        if settings.reports_bucket:
            if mirror_journal(settings.reports_bucket, settings.journal_path):
                log.info("Journal mirrored to GCS.")
            pushed = mirror_ledger(settings.reports_bucket, settings.ledger_root)
            if pushed:
                log.info("Ledger mirrored to GCS: %s", pushed)


def _echo_discovery(settings, result) -> None:
    ctx = result.context
    typer.echo("")
    typer.secho(f"Report:   {result.report_path}", fg="green")
    typer.echo(f"Journal:  {result.journal_written} entries -> {settings.journal_path}")
    typer.echo(
        f"Runtime:  {ctx.runtime_seconds:.1f}s · screened {ctx.screened}/{ctx.universe_size} "
        f"· {len(ctx.candidates)} candidates · shortlist {len(ctx.shortlist)}"
    )
    typer.echo(
        f"Tokens:   {ctx.ledger.total_tokens:,} across {ctx.ledger.total_calls} calls "
        f"· est. ${ctx.ledger.total_cost_usd:.4f}"
    )
    if ctx.degraded.entries:
        typer.secho(f"DEGRADED: {', '.join(ctx.degraded.sources)}", fg="yellow")


def _echo_deep(settings, deep) -> None:
    typer.echo("")
    typer.secho(f"Deep:     {len(deep.results)} ticker(s) in {deep.seconds:.1f}s", fg="green")
    for result in deep.results:
        typer.echo(
            f"  {result.symbol:<6} {result.verdict:<18} {result.total_calls:>2} calls · "
            f"{result.total_tokens:>7,} tok · ${result.total_cost_usd:.4f} · {result.seconds:.0f}s"
        )
    for path in deep.report_paths:
        typer.echo(f"  -> {path}")
    typer.echo(f"Brief:    {deep.brief_path}")
    typer.echo(f"Journal:  {deep.journal_written} deep entries -> {settings.journal_path}")
    if deep.ledger is not None:
        typer.echo("")
        typer.echo("  Tier   Calls   Prompt tok  Completion tok   Est. cost")
        for tier in ("fast", "smart", "deep"):
            usage = deep.ledger.by_tier.get(tier)
            if not usage:
                continue
            typer.echo(
                f"  {tier:<6} {usage.calls:>5}   {usage.prompt_tokens:>10,}  "
                f"{usage.completion_tokens:>14,}   ${usage.cost_usd:>8.4f}"
            )
        typer.echo(
            f"  total  {deep.ledger.total_calls:>5}   "
            f"{deep.ledger.total_tokens:>26,}   ${deep.ledger.total_cost_usd:>8.4f}"
        )
    if deep.degraded.entries:
        typer.secho(f"DEGRADED: {', '.join(deep.degraded.sources)}", fg="yellow")


def _echo_options(settings, options) -> None:
    typer.echo("")
    typer.secho(
        f"Options:  {options.proposed} overlay(s) from {len(options.plans)} verdict(s) "
        f"in {options.seconds:.1f}s",
        fg="green",
    )
    for plan in options.plans:
        chosen = plan.chosen
        if chosen is not None:
            detail = (
                f"{chosen.strike:>8,.2f} {chosen.quote.expiry} · delta "
                f"{abs(chosen.delta):.2f} · ${chosen.credit:.2f} · "
                f"{chosen.annualized_yield_pct:.1f}% ann."
            )
        elif plan.candidates:
            # Distinguishing these matters: an empty screen is a data or
            # liquidity story, a declined screen is the strategist's judgement.
            detail = plan.error or (
                f"strategist declined all {len(plan.candidates)} screened candidate(s)"
            )
        else:
            detail = plan.skipped or plan.error or "no candidate passed the screen"
        strategy = {"cash-secured put": "CSP", "covered call": "CC"}.get(plan.strategy or "", "—")
        typer.echo(f"  {plan.symbol:<6} {strategy:<4} {detail}")
    typer.echo(f"Journal:  {options.journal_written} options entries -> {settings.journal_path}")
    # The milestone question is what the overlay adds, so this is the stage's
    # own spend, not the run total.
    typer.echo(
        f"Cost:     +{options.calls} call(s) · {options.tokens:,} tok · "
        f"${options.cost_usd:.4f} on the "
        f"{', '.join(options.cost_by_tier) or 'no'} tier"
    )
    if options.degraded.entries:
        typer.secho(f"DEGRADED: {', '.join(options.degraded.sources)}", fg="yellow")


def _echo_outcomes(result) -> None:
    typer.echo("")
    typer.secho(
        f"Outcomes: {result.resolved} newly resolved · {result.updated} updated · "
        f"{result.pending} pending in {result.seconds:.1f}s",
        fg="green",
    )
    typer.echo(f"As of:    {result.as_of} (latest session in the bars, not the wall clock)")
    typer.echo(f"Complete: {result.complete} decision(s) have every horizon")
    for note in result.notes[:10]:
        typer.echo(f"  · {note}")
    if len(result.notes) > 10:
        typer.echo(f"  · ...and {len(result.notes) - 10} more")
    if result.degraded.entries:
        typer.secho(f"DEGRADED: {', '.join(result.degraded.sources)}", fg="yellow")


def _echo_evaluate(result) -> None:
    typer.echo("")
    typer.secho(f"Evaluation: week {result.week} -> {result.path}", fg="green")
    typer.echo(f"Resolved:   {result.resolved} observation(s) in {result.seconds:.1f}s")


def _echo_delivery(result) -> None:
    typer.echo("")
    if not result.sent:
        typer.secho(f"Email:    skipped ({result.skipped})", fg="yellow")
        typer.echo(f"  would have sent: {result.subject}")
        return
    typer.secho(f"Email:    sent to {', '.join(result.recipients)}", fg="green")
    typer.echo(f"  subject: {result.subject}")
    typer.echo(f"  attached: {', '.join(result.attachments) or 'nothing'}")
    if result.dropped:
        typer.secho(f"  DROPPED: {', '.join(result.dropped)}", fg="yellow")


if __name__ == "__main__":
    app()
