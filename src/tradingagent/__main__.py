"""Entrypoint: ``python -m tradingagent [--stage discovery|deep|options|report|all]``.

Runs headless in a container (Cloud Run Jobs) or from a shell. Asserts
``ALPACA_PAPER=true`` at startup — the process refuses to start otherwise.
"""

from __future__ import annotations

import logging
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    _configure_logging(verbose)
    log = logging.getLogger("tradingagent")

    try:
        parsed_date = date.fromisoformat(run_date) if run_date else None
    except ValueError:
        typer.secho(f"Invalid --date '{run_date}'; expected YYYY-MM-DD.", fg="red", err=True)
        raise typer.Exit(2)

    try:
        settings = load_settings(run_date=parsed_date)
    except ConfigError as exc:
        typer.secho(f"Configuration refused: {exc}", fg="red", err=True)
        raise typer.Exit(2)

    if stage in (Stage.deep, Stage.options):
        typer.secho(
            f"Stage '{stage.value}' is not implemented yet (Milestone "
            f"{'2' if stage is Stage.deep else '4'}).",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(1)

    from .stages import run_discovery

    log.info("Stage=%s date=%s paper=%s", stage.value, settings.run_date, settings.alpaca_paper)
    result = run_discovery(
        settings,
        refresh_universe=refresh_universe,
        universe_limit=universe_limit,
        shortlist_size=shortlist_size,
        skip_llm=skip_llm,
    )

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


if __name__ == "__main__":
    app()
