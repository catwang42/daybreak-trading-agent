"""Company fundamentals and positioning, from the free yfinance tier.

Feeds the fundamentals and sentiment analysts in the deep pipeline.

Ported in spirit from `reference/TradingAgents/tradingagents/dataflows/`
(`yfin_utils.py`, `finnhub_utils.py`, Apache-2.0, commit a33fd4c): the field
menu — profitability, growth, balance sheet, valuation, sell-side posture,
insider and short positioning — is upstream's. Upstream sources several of
these from paid SimFin and Finnhub premium endpoints; we take what the free
tier gives and mark the rest DEGRADED rather than adding a paid dependency.

Everything here is best-effort by design: yfinance's `info` blob is an
undocumented scrape whose keys come and go, so each field is fetched through
:func:`_pick` and a missing key becomes ``None``, never an exception.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .validate import DegradedTracker, clean_float

log = logging.getLogger(__name__)


# Plausibility ranges, in each field's NATIVE yfinance unit (see the formatter
# it is rendered through). These are not opinions about what a good company
# looks like — they are the bounds outside which the number is more likely a
# vendor error or a unit change than a fact. yfinance's `info` is an
# undocumented scrape: `dividendYield` silently switched from a ratio to
# percentage points in 0.2.5x, and for a while the fundamentals analyst was
# reading a 0.91% payer as a 91% payer and building a thesis on it. A value
# that is merely extreme is still shown, but marked SUSPECT so the analyst
# discounts it instead of citing it.
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "market_cap": (1e6, 1e13),
    "trailing_pe": (0.0, 500.0),
    "forward_pe": (-100.0, 300.0),
    "peg": (-20.0, 20.0),
    "price_to_sales": (0.0, 100.0),
    "price_to_book": (-50.0, 100.0),
    "profit_margin": (-5.0, 1.0),        # ratio; margin above 100% is arithmetic nonsense
    "operating_margin": (-5.0, 1.0),     # ratio
    "return_on_equity": (-10.0, 10.0),   # ratio
    "revenue_growth": (-1.0, 10.0),      # ratio; revenue cannot fall more than 100%
    "earnings_growth": (-50.0, 50.0),    # ratio; swings off a near-zero base are real
    "debt_to_equity": (0.0, 2000.0),     # percentage points, i.e. up to 20x equity
    "current_ratio": (0.0, 50.0),
    "free_cash_flow": (-1e12, 1e12),
    "dividend_yield": (0.0, 25.0),       # percentage points — the field that caught the unit bug
    "beta": (-5.0, 5.0),
    # positioning
    "short_percent_of_float": (0.0, 1.0),    # ratio
    "held_by_institutions": (0.0, 1.5),      # ratio; can exceed 1 when shares are lent out
    "held_by_insiders": (0.0, 1.0),          # ratio
    "short_ratio": (0.0, 100.0),             # days to cover
}

SUSPECT_MARK = " ⚠ SUSPECT"


def is_suspect(name: str, value: float | None) -> bool:
    """True when ``value`` falls outside what this field can plausibly hold."""
    if value is None or name not in PLAUSIBLE:
        return False
    low, high = PLAUSIBLE[name]
    return not (low <= value <= high)


def _suspect_note(names: list[str]) -> list[str]:
    if not names:
        return []
    return [
        "",
        f"**SUSPECT** ({', '.join(names)}): these values fall outside the plausible "
        "range for their field, so they are most likely a vendor error or a units "
        "change at the source rather than a fact about the company. Treat them as "
        "unavailable — do not build an argument on them, and say so in your "
        "evidence gaps if they mattered.",
    ]


@dataclass
class Fundamentals:
    """One company snapshot. Any field may be ``None`` on the free tier."""

    symbol: str
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    peg: float | None = None
    price_to_sales: float | None = None
    price_to_book: float | None = None
    profit_margin: float | None = None
    operating_margin: float | None = None
    return_on_equity: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    free_cash_flow: float | None = None
    dividend_yield: float | None = None
    beta: float | None = None
    quarters: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    ROWS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Market cap", "market_cap"),
        ("Trailing P/E", "trailing_pe"),
        ("Forward P/E", "forward_pe"),
        ("PEG ratio", "peg"),
        ("Price / sales", "price_to_sales"),
        ("Price / book", "price_to_book"),
        ("Profit margin", "profit_margin"),
        ("Operating margin", "operating_margin"),
        ("Return on equity", "return_on_equity"),
        ("Revenue growth (yoy)", "revenue_growth"),
        ("Earnings growth (yoy)", "earnings_growth"),
        ("Debt / equity", "debt_to_equity"),
        ("Current ratio", "current_ratio"),
        ("Free cash flow (ttm)", "free_cash_flow"),
        ("Dividend yield", "dividend_yield"),
        ("Beta", "beta"),
    )

    def suspect_fields(self) -> list[str]:
        """Labels of the metrics whose values are outside their plausible range."""
        return [label for label, name in self.ROWS if is_suspect(name, getattr(self, name))]

    def markdown(self) -> str:
        formatters = {
            "market_cap": _money, "free_cash_flow": _money,
            "profit_margin": _pct, "operating_margin": _pct, "return_on_equity": _pct,
            "revenue_growth": _pct, "earnings_growth": _pct,
            "debt_to_equity": _multiple, "dividend_yield": _pct_points,
        }
        rows = [
            (label, formatters.get(name, _num)(getattr(self, name))
             + (SUSPECT_MARK if is_suspect(name, getattr(self, name)) else ""))
            for label, name in self.ROWS
        ]
        out = ["| Metric | Value |", "|---|---:|"]
        out += [f"| {label} | {value} |" for label, value in rows]

        if self.quarters:
            out += [
                "",
                "Recent quarters (most recent first):",
                "",
                "| Quarter | Revenue | Net income | Net margin |",
                "|---|---:|---:|---:|",
            ]
            for q in self.quarters:
                revenue, income = q.get("revenue"), q.get("net_income")
                margin = (income / revenue) if revenue and income is not None and revenue != 0 else None
                out.append(
                    f"| {q.get('period', '?')} | {_money(revenue)} | {_money(income)} | {_pct(margin)} |"
                )
        else:
            out += ["", "Quarterly statements: unavailable on this tier."]

        if self.missing:
            out += ["", f"Fields the free tier did not return: {', '.join(self.missing)}."]
        out += _suspect_note(self.suspect_fields())
        return "\n".join(out)


@dataclass
class Positioning:
    """Sell-side posture and float positioning — the sentiment analyst's evidence."""

    symbol: str
    recommendation_key: str | None = None
    analyst_count: int | None = None
    target_mean: float | None = None
    #: The median matters more than the mean on a thinly covered name: one
    #: outlier target on eight analysts drags the mean somewhere no analyst
    #: actually is, and that is the number our own target gets compared against.
    target_median: float | None = None
    target_high: float | None = None
    target_low: float | None = None
    short_percent_of_float: float | None = None
    short_ratio: float | None = None
    held_by_institutions: float | None = None
    held_by_insiders: float | None = None
    recommendation_spread: str | None = None
    missing: list[str] = field(default_factory=list)

    # Only the fields with a plausible range; the rest are rendered inline below.
    ROWS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("Short interest (% of float)", "short_percent_of_float"),
        ("Short ratio (days to cover)", "short_ratio"),
        ("Held by institutions", "held_by_institutions"),
        ("Held by insiders", "held_by_insiders"),
    )

    def suspect_fields(self) -> list[str]:
        return [label for label, name in self.ROWS if is_suspect(name, getattr(self, name))]

    def markdown(self, price: float | None = None) -> str:
        gap = ""
        if price and self.target_mean:
            gap = f" — the last close is {(price / self.target_mean - 1) * 100:+.1f}% versus that mean target"

        def _flagged(name: str, text: str) -> str:
            return text + (SUSPECT_MARK if is_suspect(name, getattr(self, name)) else "")

        rows = [
            ("Consensus recommendation", self.recommendation_key or "unavailable"),
            ("Analysts covering", _num(self.analyst_count, digits=0)),
            ("Mean price target", _money_price(self.target_mean) + gap),
            ("Median price target", _money_price(self.target_median)),
            ("Target range", f"{_money_price(self.target_low)} – {_money_price(self.target_high)}"),
            ("Recommendation spread", self.recommendation_spread or "unavailable"),
            ("Short interest (% of float)", _flagged("short_percent_of_float", _pct(self.short_percent_of_float))),
            ("Short ratio (days to cover)", _flagged("short_ratio", _num(self.short_ratio))),
            ("Held by institutions", _flagged("held_by_institutions", _pct(self.held_by_institutions))),
            ("Held by insiders", _flagged("held_by_insiders", _pct(self.held_by_insiders))),
        ]
        out = ["| Positioning metric | Value |", "|---|---|"]
        out += [f"| {label} | {value} |" for label, value in rows]
        if self.missing:
            out += ["", f"Fields the free tier did not return: {', '.join(self.missing)}."]
        out += _suspect_note(self.suspect_fields())
        return "\n".join(out)


def _pick(info: dict[str, Any], *keys: str) -> float | None:
    """First finite value among ``keys``; yfinance renames fields between releases."""
    for key in keys:
        value = clean_float(info.get(key))
        if value is not None:
            return value
    return None


def _money(value: float | None) -> str:
    if value is None:
        return "unavailable"
    sign = "-" if value < 0 else ""
    v = abs(value)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= cutoff:
            return f"{sign}${v / cutoff:,.2f}{suffix}"
    return f"{sign}${v:,.0f}"


def _money_price(value: float | None) -> str:
    return "unavailable" if value is None else f"${value:,.2f}"


def _num(value: float | None, digits: int = 2) -> str:
    return "unavailable" if value is None else f"{value:,.{digits}f}"


def _pct(value: float | None) -> str:
    """For the fields yfinance returns as ratios (0.23 meaning 23%)."""
    return "unavailable" if value is None else f"{value * 100:+.1f}%"


def _pct_points(value: float | None) -> str:
    """For the fields yfinance already returns in percentage points (0.91 meaning 0.91%).

    `dividendYield` switched units in yfinance 0.2.5x; feeding it through
    :func:`_pct` printed a 0.9% payer as a 91% payer and the analyst believed it.
    """
    return "unavailable" if value is None else f"{value:.2f}%"


def _multiple(value: float | None) -> str:
    """`debtToEquity` arrives in percentage points (124.3 meaning 1.24x equity)."""
    return "unavailable" if value is None else f"{value / 100:,.2f}x"


class FundamentalsClient:
    """yfinance company data, validated and DEGRADED-tracked."""

    def __init__(self, degraded: DegradedTracker | None = None):
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self._info: dict[str, dict[str, Any]] = {}

    def _ticker(self, symbol: str) -> Any:
        import yfinance as yf

        return yf.Ticker(symbol)

    def info(self, symbol: str) -> dict[str, Any]:
        if symbol in self._info:
            return self._info[symbol]
        data: dict[str, Any] = {}
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                data = dict(self._ticker(symbol).info or {})
        except Exception as exc:  # noqa: BLE001 - scraped endpoint, any failure is possible
            self.degraded.add(f"yfinance fundamentals {symbol}", f"info unavailable: {exc}")
        self._info[symbol] = data
        return data

    def fundamentals(self, symbol: str) -> Fundamentals:
        info = self.info(symbol)
        snapshot = Fundamentals(
            symbol=symbol,
            market_cap=_pick(info, "marketCap"),
            trailing_pe=_pick(info, "trailingPE"),
            forward_pe=_pick(info, "forwardPE"),
            peg=_pick(info, "trailingPegRatio", "pegRatio"),
            price_to_sales=_pick(info, "priceToSalesTrailing12Months"),
            price_to_book=_pick(info, "priceToBook"),
            profit_margin=_pick(info, "profitMargins"),
            operating_margin=_pick(info, "operatingMargins"),
            return_on_equity=_pick(info, "returnOnEquity"),
            revenue_growth=_pick(info, "revenueGrowth"),
            earnings_growth=_pick(info, "earningsGrowth", "earningsQuarterlyGrowth"),
            debt_to_equity=_pick(info, "debtToEquity"),
            current_ratio=_pick(info, "currentRatio"),
            free_cash_flow=_pick(info, "freeCashflow"),
            dividend_yield=_pick(info, "dividendYield"),
            beta=_pick(info, "beta"),
            quarters=self.quarterly_trend(symbol),
        )
        snapshot.missing = [
            name
            for name, value in (
                ("market cap", snapshot.market_cap),
                ("trailing P/E", snapshot.trailing_pe),
                ("forward P/E", snapshot.forward_pe),
                ("revenue growth", snapshot.revenue_growth),
                ("free cash flow", snapshot.free_cash_flow),
            )
            if value is None
        ]
        if len(snapshot.missing) >= 4:
            self.degraded.add(
                f"yfinance fundamentals {symbol}",
                f"only sparse data returned (missing {', '.join(snapshot.missing)})",
            )
        suspect = snapshot.suspect_fields()
        if suspect:
            log.warning(
                "%s: implausible fundamentals from yfinance, marked SUSPECT in the pack: %s",
                symbol,
                ", ".join(suspect),
            )
        return snapshot

    def quarterly_trend(self, symbol: str, quarters: int = 4) -> list[dict[str, Any]]:
        """Revenue and net income for the last few quarters, newest first."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                stmt = self._ticker(symbol).quarterly_income_stmt
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(f"yfinance statements {symbol}", str(exc))
            return []
        if stmt is None or getattr(stmt, "empty", True):
            return []

        def _row(*names: str) -> Any:
            for name in names:
                if name in stmt.index:
                    return stmt.loc[name]
            return None

        revenue = _row("Total Revenue", "Operating Revenue")
        income = _row("Net Income", "Net Income Common Stockholders")
        out: list[dict[str, Any]] = []
        for column in list(stmt.columns)[:quarters]:
            out.append(
                {
                    "period": str(getattr(column, "date", lambda: column)()),
                    "revenue": clean_float(revenue[column]) if revenue is not None else None,
                    "net_income": clean_float(income[column]) if income is not None else None,
                }
            )
        return out

    def positioning(self, symbol: str) -> Positioning:
        info = self.info(symbol)
        count = _pick(info, "numberOfAnalystOpinions")
        snapshot = Positioning(
            symbol=symbol,
            recommendation_key=str(info.get("recommendationKey") or "") or None,
            analyst_count=int(count) if count is not None else None,
            target_mean=_pick(info, "targetMeanPrice"),
            target_median=_pick(info, "targetMedianPrice"),
            target_high=_pick(info, "targetHighPrice"),
            target_low=_pick(info, "targetLowPrice"),
            short_percent_of_float=_pick(info, "shortPercentOfFloat"),
            short_ratio=_pick(info, "shortRatio"),
            held_by_institutions=_pick(info, "heldPercentInstitutions"),
            held_by_insiders=_pick(info, "heldPercentInsiders"),
            recommendation_spread=self.recommendation_spread(symbol),
        )
        snapshot.missing = [
            name
            for name, value in (
                ("mean price target", snapshot.target_mean),
                ("short interest", snapshot.short_percent_of_float),
                ("recommendation spread", snapshot.recommendation_spread),
            )
            if value is None
        ]
        return snapshot

    def recommendation_spread(self, symbol: str) -> str | None:
        """Current-month analyst vote distribution, e.g. '12 strong buy / 9 buy / 4 hold'."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                table = self._ticker(symbol).recommendations
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(f"yfinance recommendations {symbol}", str(exc))
            return None
        if table is None or getattr(table, "empty", True):
            return None
        try:
            latest = table.iloc[0]
            buckets = [
                ("strong buy", "strongBuy"),
                ("buy", "buy"),
                ("hold", "hold"),
                ("sell", "sell"),
                ("strong sell", "strongSell"),
            ]
            parts = [
                f"{int(latest[key])} {label}"
                for label, key in buckets
                if key in latest.index and clean_float(latest[key]) is not None
            ]
            return " / ".join(parts) or None
        except Exception:  # noqa: BLE001 - shape varies across yfinance releases
            return None
