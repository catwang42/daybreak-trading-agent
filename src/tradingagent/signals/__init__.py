"""Signal-fusion layer (Milestone 3) — see PORTING_NOTES.md for cookbook mapping.

Four direct API clients behind one interface: company/market news tone
(Finnhub + RSS), insider Form 4 filings (SEC EDGAR), macro series (FRED) and
event odds (Polymarket). :class:`~tradingagent.signals.bundle.SignalHub` runs
them and hands out per-ticker :class:`~tradingagent.signals.bundle.SignalBundle`
objects, which re-rank the shortlist and supply context to the analyst and
debate prompts. :mod:`tradingagent.signals.accuracy` scores each source
against journal outcomes and turns that record into its weight.

Adding a source means implementing
:class:`~tradingagent.signals.base.SignalSource` and adding it to
:func:`~tradingagent.signals.bundle.build_default_hub`.
"""

from .base import Signal, SignalSource, SourceResult
from .bundle import SignalBundle, SignalHub, build_default_hub

__all__ = [
    "Signal",
    "SignalSource",
    "SourceResult",
    "SignalBundle",
    "SignalHub",
    "build_default_hub",
]
