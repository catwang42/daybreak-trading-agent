"""Entrypoint: python -m tradingagent [--stage discovery|deep|options|report|all]
Implemented incrementally by Claude Code per BUILD_PLAN.md. Stage flags let Cloud Run
Jobs or cron run the full scan or a single stage. Asserts ALPACA_PAPER=true at startup.
"""
# TODO(M1): Typer CLI wiring discovery -> report; later stages added per milestone.
