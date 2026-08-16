"""The sheet's charts, drawn headless into PNGs.

Four pictures, each answering one question the tables answer slowly:

- ``spy_chart`` — is the index above or below its own trend?
- ``sector_chart`` — which way is money rotating?
- ``breadth_gauge`` — how much of the market is participating?
- ``setup_chart`` — where do this plan's entry, stop and target sit against
  six months of price?

Every level on the setup chart is a :class:`~..pipeline.trade_plan.TradePlan`
field copied through :mod:`.context`. None of them is read from a report, and
none is recomputed here: a chart that disagrees with the table beneath it is
worse than no chart, because it is a second opinion nobody asked for and the
reader has no way to tell which one is the plan.

Matplotlib is imported lazily and behind Agg. The import is the expensive part
of the module and a ``--stage discovery`` run has no use for it, and Agg is what
makes it work in a container with no display.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Two-up on a desktop, full width on a phone. 2x for retina.
FIGSIZE = (6.4, 3.0)
DPI = 150

INK = "#1a1d21"
MUTED = "#6b7280"
GRID = "#e5e7eb"
LINE = "#2563eb"
UP = "#059669"
DOWN = "#dc2626"
WARN = "#d97706"
ENTRY_COLOR = "#2563eb"
STOP_COLOR = "#dc2626"
TARGET_COLOR = "#059669"


@dataclass(frozen=True)
class Chart:
    """A PNG plus the id the HTML references it by (``cid:`` inline attachment)."""

    cid: str
    filename: str
    png: bytes
    alt: str

    @property
    def size(self) -> int:
        return len(self.png)


def _pyplot():
    """Agg first, then pyplot. The order matters in a container with no display."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _finish(fig, plt, cid: str, filename: str, alt: str) -> Chart:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return Chart(cid=cid, filename=filename, png=buffer.getvalue(), alt=alt)


def _style(ax, plt) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)


def _legend(ax) -> None:
    """Opaque, so a horizontal level drawn across it does not strike the text out."""
    ax.legend(
        fontsize=8,
        loc="upper left",
        labelcolor=MUTED,
        frameon=True,
        facecolor="white",
        edgecolor="none",
        framealpha=0.9,
    ).set_zorder(10)


def _dates(points):
    from datetime import date as _date

    out = []
    for point in points:
        try:
            out.append(_date.fromisoformat(point.d))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _date_axis(ax) -> None:
    """Readable date ticks at email width.

    Matplotlib's default puts a full ISO date under every other week and they
    overlap into an unreadable smear at 640 pixels wide.
    """
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter

    locator = AutoDateLocator(minticks=3, maxticks=6)
    ax.xaxis.set_major_locator(locator)
    # show_offset=False drops the floating "2026-Jun" that otherwise lands
    # outside the axes at the bottom right. The email states the session date
    # three lines above the chart; the year does not need repeating there.
    ax.xaxis.set_major_formatter(ConciseDateFormatter(locator, show_offset=False))


def spy_chart(regime) -> Chart | None:
    """Three months of SPY with its 50- and 200-day averages."""
    points = list(regime.spy or [])
    if len(points) < 5:
        return None
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=FIGSIZE)
    xs = _dates(points)
    ax.plot(xs, [p.close for p in points], color=LINE, linewidth=1.8, label="SPY")
    if any(p.sma50 is not None for p in points):
        ax.plot(
            xs,
            [p.sma50 for p in points],
            color=WARN,
            linewidth=1.1,
            linestyle="--",
            label="50-day",
        )
    if any(p.sma200 is not None for p in points):
        ax.plot(
            xs,
            [p.sma200 for p in points],
            color=MUTED,
            linewidth=1.1,
            linestyle=":",
            label="200-day",
        )
    last = points[-1]
    ax.annotate(
        f"${last.close:,.2f}",
        xy=(xs[-1], last.close),
        xytext=(4, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color=INK,
        fontweight="bold",
    )
    ax.set_title("S&P 500 (SPY), 3 months", fontsize=10, color=INK, loc="left")
    _legend(ax)
    _style(ax, plt)
    _date_axis(ax)
    return _finish(fig, plt, "spy", "spy-3mo.png", "SPY over three months with 50- and 200-day moving averages")


def sector_chart(regime) -> Chart | None:
    """Blended sector momentum, ranked. Green leads, red lags."""
    rows = list(regime.sectors or [])
    if not rows:
        return None
    plt = _pyplot()
    rows = rows[::-1]  # barh draws bottom-up; we want the leader on top
    height = max(2.4, 0.28 * len(rows) + 0.8)
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], height))
    values = [r.momentum for r in rows]
    ax.barh(
        [r.sector for r in rows],
        values,
        color=[UP if v >= 0 else DOWN for v in values],
        height=0.62,
    )
    for index, value in enumerate(values):
        ax.annotate(
            f"{value:+.1f}",
            xy=(value, index),
            xytext=(4 if value >= 0 else -4, 0),
            textcoords="offset points",
            va="center",
            ha="left" if value >= 0 else "right",
            fontsize=8,
            color=MUTED,
        )
    ax.axvline(0, color=MUTED, linewidth=0.8)
    ax.set_title("Sector momentum (blended 5d/1mo/3mo)", fontsize=10, color=INK, loc="left")
    _style(ax, plt)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.7)
    ax.margins(x=0.12)
    return _finish(fig, plt, "sectors", "sectors.png", "Sector momentum ranked from leader to laggard")


def breadth_gauge(regime) -> Chart | None:
    """The breadth composite on its 0-100 scale, with the zone bands behind it."""
    if regime.composite is None:
        return None
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(FIGSIZE[0], 1.5))
    bands = [(0, 30, DOWN), (30, 50, WARN), (50, 70, "#a3a3a3"), (70, 100, UP)]
    for low, high, colour in bands:
        ax.barh([0], [high - low], left=[low], height=0.42, color=colour, alpha=0.22)
    value = float(regime.composite)
    ax.barh([0], [value], height=0.16, color=INK)
    ax.plot([value], [0], marker="o", markersize=9, color=INK)
    ax.annotate(
        f"{value:.1f}",
        xy=(value, 0),
        xytext=(0, 14),
        textcoords="offset points",
        ha="center",
        fontsize=11,
        fontweight="bold",
        color=INK,
    )
    pct = (
        f" · {regime.pct_above_50dma:.0f}% of {regime.universe_size} names above their 50-day MA"
        if regime.pct_above_50dma is not None
        else ""
    )
    ax.set_title(f"Breadth composite — {regime.zone or 'unclassified'}{pct}", fontsize=10, color=INK, loc="left")
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.5, 0.6)
    ax.set_yticks([])
    ax.set_xticks([0, 30, 50, 70, 100])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    return _finish(fig, plt, "breadth", "breadth.png", f"Breadth composite {value:.1f} out of 100")


def setup_chart(setup) -> Chart | None:
    """Six months of price with the plan's entry, stop and target drawn on it.

    The three lines are ``setup.entry``, ``setup.stop`` and ``setup.target``,
    which are :class:`TradePlan` fields. If the plan has no levels — a Hold, or
    a plan the arithmetic rejected — the chart is still drawn, without them,
    rather than being invented.
    """
    points = list(setup.series or [])
    if len(points) < 5:
        return None
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=FIGSIZE)
    xs = _dates(points)
    closes = [p.close for p in points]
    ax.plot(xs, closes, color=INK, linewidth=1.6)
    if any(p.sma50 is not None for p in points):
        ax.plot(xs, [p.sma50 for p in points], color=WARN, linewidth=1.0, linestyle="--", label="50-day")
    if any(p.sma200 is not None for p in points):
        ax.plot(xs, [p.sma200 for p in points], color=MUTED, linewidth=1.0, linestyle=":", label="200-day")

    drawn = False
    for value, colour, label in (
        (setup.target, TARGET_COLOR, "target"),
        (setup.entry, ENTRY_COLOR, "entry"),
        (setup.stop, STOP_COLOR, "stop"),
    ):
        if not value:
            continue
        drawn = True
        ax.axhline(float(value), color=colour, linewidth=1.2, alpha=0.9)
        ax.annotate(
            f"{label} ${float(value):,.2f}",
            xy=(1.0, float(value)),
            xycoords=("axes fraction", "data"),
            xytext=(4, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=colour,
            fontweight="bold",
        )

    # Keep every drawn level inside the frame. Without this a target 20% above
    # the range is clipped off the top and the chart quietly implies the plan
    # has no upside.
    levels = [float(v) for v in (setup.entry, setup.stop, setup.target) if v]
    if levels:
        low = min(min(closes), *levels)
        high = max(max(closes), *levels)
        pad = (high - low) * 0.08 or 1.0
        ax.set_ylim(low - pad, high + pad)

    title = f"{setup.symbol} — 6 months"
    if not drawn:
        title += " (no priced plan)"
    ax.set_title(title, fontsize=10, color=INK, loc="left")
    if any(p.sma50 is not None for p in points):
        _legend(ax)
    _style(ax, plt)
    _date_axis(ax)
    return _finish(
        fig,
        plt,
        f"setup-{setup.symbol.lower()}",
        f"{setup.symbol}-6mo.png",
        f"{setup.symbol} over six months with the planned entry, stop and target",
    )


def render_charts(context) -> list[Chart]:
    """Every chart the sheet can draw, in the order it shows them.

    Best effort per chart. A matplotlib failure on one ticker costs that
    picture, not the email — the tables carry the same numbers, which is why
    the charts are allowed to be optional in the first place.
    """
    charts: list[Chart] = []
    makers = [
        ("SPY", lambda: spy_chart(context.regime)),
        ("breadth", lambda: breadth_gauge(context.regime)),
        ("sectors", lambda: sector_chart(context.regime)),
    ]
    makers += [
        (setup.symbol, (lambda s=setup: setup_chart(s)))
        for setup in context.setups
    ]
    for what, make in makers:
        try:
            chart = make()
        except Exception as exc:  # noqa: BLE001 - one missing picture, not a failed send
            log.warning("Chart %s not drawn: %s", what, exc)
            continue
        if chart is not None:
            charts.append(chart)
    return charts
