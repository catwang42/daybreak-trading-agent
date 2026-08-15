"""Alpaca PAPER option chains — the M4 data source.

Guardrail (CLAUDE.md): read-only, paper endpoints only. Two Alpaca calls per
underlying, both reference/market-data reads:

- ``TradingClient(paper=True).get_option_contracts`` — the contract reference
  list: strike, expiry, open interest, previous close. Same read-only paper
  client :mod:`tradingagent.data.alpaca_client` already uses for the clock.
- ``OptionHistoricalDataClient.get_option_chain`` — the quote snapshots.

There is no order path here and none can be added: ``alpaca.trading.client``
is imported for one getter whose name says what it does.

What the free tier actually gives you, verified against Alpaca on 2026-08-14:

- The ``opra`` feed 403s with "OPRA agreement is not signed". The free feed is
  ``indicative``, and its snapshots carry ``implied_volatility=None`` and
  ``greeks=None`` — every delta and IV in this stage is computed by
  :mod:`tradingagent.options.black_scholes` from the quote.
- Snapshots carry no open interest and no option volume. OI comes from the
  contracts endpoint instead (dated, typically T-1); per-contract volume is
  simply not available free, so the liquidity screen is OI-only and says so.
- Far-OTM books are one-sided: bid 0.00 with a real ask, and a last trade that
  can be days old. Both are handled explicitly rather than filtered out, because
  the 0.20-delta strikes we want are exactly the thin ones.

Adapted from staskh's IBKR helpers (`broker/options.py`, `broker/roll.py`) and
their yfinance chain reader (`options.py`): the quote shape — bid/ask/mid with a
last-price fallback, staleness carried alongside the number — is theirs; the
transport and the OI join are ours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..config import Settings
from .validate import DegradedTracker, clean_float

log = logging.getLogger(__name__)

#: Free-tier options feed. OPRA needs a signed exchange agreement (see module docstring).
FREE_FEED = "indicative"
#: A quote older than this during a session is stale enough to say so out loud.
STALE_QUOTE_MINUTES = 20
#: Contracts-endpoint page size; one underlying's monthly chain fits in one page.
_PAGE_LIMIT = 1000
_MAX_PAGES = 5


@dataclass
class OptionQuote:
    """One contract: what it is, what it is worth, and how much to trust that."""

    symbol: str  # OCC, e.g. V260918P00340000
    underlying: str
    right: str  # "put" | "call"
    strike: float
    expiry: date
    dte: int
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    quote_at: datetime | None = None
    last_trade_at: datetime | None = None
    open_interest: int | None = None
    open_interest_date: date | None = None
    prev_close: float | None = None

    # -- pricing ---------------------------------------------------------
    @property
    def mid(self) -> float | None:
        """Bid/ask mid, only when both sides are real.

        A one-sided book (bid 0.00, ask 0.04) has no mid worth the name: taking
        (0 + 0.04)/2 invents a 2-cent print that no one would fill.
        """
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def price_basis(self) -> str:
        """Which number the pricing below rests on — carried into the report."""
        if self.mid is not None:
            return "mid"
        if self.bid is not None and self.bid > 0:
            return "bid"
        if self.last is not None and self.last > 0:
            return "last trade"
        if self.prev_close is not None and self.prev_close > 0:
            return "prior close"
        return "none"

    @property
    def reference_price(self) -> float | None:
        """Best available mark, for solving IV. See :attr:`price_basis` for which."""
        for value in (self.mid, self.bid if (self.bid or 0) > 0 else None, self.last, self.prev_close):
            if value is not None and value > 0:
                return float(value)
        return None

    @property
    def credit(self) -> float | None:
        """What a *seller* would realistically collect, per share.

        Deliberately conservative and deliberately not :attr:`reference_price`:
        you sell into the bid, so the bid is the number when there is one. Only
        when the book has no bid does this fall back to the last print, which
        the report then labels as such.
        """
        if self.bid is not None and self.bid > 0:
            return float(self.bid)
        if self.mid is not None:
            return float(self.mid)
        for value in (self.last, self.prev_close):
            if value is not None and value > 0:
                return float(value)
        return None

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread as a percentage of mid; ``None`` when the book is one-sided."""
        mid = self.mid
        if mid is None or mid <= 0 or self.ask is None or self.bid is None:
            return None
        return (self.ask - self.bid) / mid * 100

    # -- freshness -------------------------------------------------------
    @property
    def pricing_stamp(self) -> datetime | None:
        """When the number in :attr:`credit` was actually observed."""
        if self.price_basis in {"mid", "bid"}:
            return self.quote_at
        if self.price_basis == "last trade":
            return self.last_trade_at
        return None

    def age_minutes(self, now: datetime | None = None) -> float | None:
        stamp = self.pricing_stamp
        if stamp is None:
            return None
        now = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (now - stamp).total_seconds() / 60.0

    def freshness(self, now: datetime | None = None) -> str:
        age = self.age_minutes(now)
        if age is None:
            return f"no timestamp ({self.price_basis})"
        if age < 60:
            return f"{age:.0f}m old"
        if age < 48 * 60:
            return f"{age / 60:.1f}h old"
        return f"{age / 1440:.1f}d old"


@dataclass
class ChainSlice:
    """Every contract of one right, for one underlying, in one expiry window."""

    underlying: str
    right: str
    quotes: list[OptionQuote]
    feed: str = FREE_FEED
    fetched_at: datetime | None = None

    def expiries(self) -> list[date]:
        return sorted({q.expiry for q in self.quotes})

    def for_expiry(self, expiry: date) -> list[OptionQuote]:
        return sorted((q for q in self.quotes if q.expiry == expiry), key=lambda q: q.strike)

    def nearest_expiry(self, target_dte: int) -> date | None:
        """The listed expiry closest to ``target_dte``, ties going longer."""
        expiries = self.expiries()
        if not expiries:
            return None
        return min(expiries, key=lambda e: (abs((e - self._today()).days - target_dte), -e.toordinal()))

    def _today(self) -> date:
        return (self.fetched_at or datetime.now(timezone.utc)).date()

    def priced(self) -> list[OptionQuote]:
        return [q for q in self.quotes if q.reference_price is not None]


class AlpacaOptionChain:
    """Read-only option-chain reader over the Alpaca paper account."""

    def __init__(self, settings: Settings, degraded: DegradedTracker | None = None):
        if not settings.alpaca_paper:
            raise RuntimeError("Refusing to read option chains with ALPACA_PAPER != true")
        self.settings = settings
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self._enabled = bool(settings.alpaca_key and settings.alpaca_secret)
        self._feed_note: str | None = None
        if not self._enabled:
            self.degraded.add("Alpaca option chains", "ALPACA_API_KEY/SECRET not set")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def feed_note(self) -> str:
        return self._feed_note or (
            f"Alpaca `{FREE_FEED}` feed — no greeks, no implied vol, no per-contract "
            "volume; delta and IV are computed from the quote."
        )

    def chain(
        self,
        symbol: str,
        right: str,
        *,
        min_dte: int,
        max_dte: int,
        run_date: date | None = None,
    ) -> ChainSlice | None:
        """Contracts of one right expiring in ``[min_dte, max_dte]``, quotes joined on.

        Returns ``None`` only when the whole read failed. An empty
        :class:`ChainSlice` means Alpaca answered and there was nothing listed —
        a different fact, and the caller reports it differently.
        """
        if not self._enabled:
            return None
        today = run_date or datetime.now(timezone.utc).date()
        lo, hi = today + timedelta(days=min_dte), today + timedelta(days=max_dte)

        contracts = self._contracts(symbol, right, lo, hi)
        if contracts is None:
            return None
        snapshots = self._snapshots(symbol, right, lo, hi)

        quotes: list[OptionQuote] = []
        for occ, ref in contracts.items():
            snap = (snapshots or {}).get(occ)
            trade = getattr(snap, "latest_trade", None)
            quote = getattr(snap, "latest_quote", None)
            quotes.append(
                OptionQuote(
                    symbol=occ,
                    underlying=symbol,
                    right=right,
                    strike=ref["strike"],
                    expiry=ref["expiry"],
                    dte=(ref["expiry"] - today).days,
                    bid=clean_float(getattr(quote, "bid_price", None)),
                    ask=clean_float(getattr(quote, "ask_price", None)),
                    last=clean_float(getattr(trade, "price", None)),
                    quote_at=getattr(quote, "timestamp", None),
                    last_trade_at=getattr(trade, "timestamp", None),
                    open_interest=ref["open_interest"],
                    open_interest_date=ref["open_interest_date"],
                    prev_close=ref["prev_close"],
                )
            )
        if snapshots is None:
            self.degraded.add(
                f"Alpaca option quotes {symbol}",
                "contract list returned but no quote snapshots; strikes are priced off "
                "the previous close",
            )
        log.info(
            "Option chain %s %s: %d contracts, %d with a usable price, expiries %s",
            symbol,
            right,
            len(quotes),
            sum(1 for q in quotes if q.reference_price is not None),
            ", ".join(str(e) for e in sorted({q.expiry for q in quotes})) or "none",
        )
        return ChainSlice(
            underlying=symbol,
            right=right,
            quotes=quotes,
            fetched_at=datetime.now(timezone.utc),
        )

    # -- transport -------------------------------------------------------
    def _contracts(self, symbol: str, right: str, lo: date, hi: date) -> dict[str, dict] | None:
        """Contract reference data, paged. Carries the open interest."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOptionContractsRequest
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            self.degraded.add(f"Alpaca option contracts {symbol}", f"alpaca-py missing: {exc}")
            return None

        client = TradingClient(self.settings.alpaca_key, self.settings.alpaca_secret, paper=True)
        out: dict[str, dict] = {}
        token: str | None = None
        try:
            for _ in range(_MAX_PAGES):
                page = client.get_option_contracts(
                    GetOptionContractsRequest(
                        underlying_symbols=[symbol],
                        type=right,
                        status="active",
                        style="american",
                        expiration_date_gte=lo,
                        expiration_date_lte=hi,
                        limit=_PAGE_LIMIT,
                        page_token=token,
                    )
                )
                for c in page.option_contracts or []:
                    strike = clean_float(c.strike_price)
                    if strike is None or strike <= 0 or c.expiration_date is None:
                        continue
                    out[c.symbol] = {
                        "strike": strike,
                        "expiry": c.expiration_date,
                        "open_interest": _to_int(c.open_interest),
                        "open_interest_date": c.open_interest_date,
                        "prev_close": clean_float(c.close_price),
                    }
                token = page.next_page_token
                if not token:
                    break
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(f"Alpaca option contracts {symbol}", str(exc))
            return None
        if not out:
            self.degraded.add(
                f"Alpaca option contracts {symbol}",
                f"no {right} contracts listed between {lo} and {hi}",
            )
        return out

    def _snapshots(self, symbol: str, right: str, lo: date, hi: date) -> dict | None:
        """Quote snapshots on the free feed. Greeks and IV come back empty; we compute them."""
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionChainRequest
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            self.degraded.add(f"Alpaca option quotes {symbol}", f"alpaca-py missing: {exc}")
            return None

        client = OptionHistoricalDataClient(self.settings.alpaca_key, self.settings.alpaca_secret)
        try:
            return client.get_option_chain(
                OptionChainRequest(
                    underlying_symbol=symbol,
                    feed=FREE_FEED,
                    type=right,
                    expiration_date_gte=lo,
                    expiration_date_lte=hi,
                )
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "OPRA" in message:
                self._feed_note = f"OPRA refused ({message[:80]}); fell back to `{FREE_FEED}`."
            self.degraded.add(f"Alpaca option quotes {symbol}", message)
            return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
