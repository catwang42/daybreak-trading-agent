"""Insider transactions from SEC EDGAR Form 4 filings.

The one source in this milestone that reports what people who know the company
actually did with their own money, rather than what someone said about it.
Cluster buying by officers and directors is the signal worth having; routine
10b5-1 sales are noise and are labelled as such rather than dropped, because
"the CFO sold, but on a scheduled plan" is a fact the debate should see.

SEC fair access (https://www.sec.gov/os/webmaster-faq#developers):
- Every request declares a descriptive User-Agent with a contact address. We
  require ``SEC_USER_AGENT`` to be set rather than shipping a fake contact —
  an unset value skips the source with a visible reason. Sending a made-up
  address to satisfy a rate-limit rule is exactly what the rule exists to stop.
- The published ceiling is 10 requests/second. We throttle to 5, and cap the
  filings fetched per ticker, because nothing here is latency-sensitive.
- No scraping: these are the documented JSON and XML endpoints.

Nothing in this module is imported from `reference/`; EDGAR is not covered by
any of the three cookbooks.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree

from ..config import env
from .base import Signal, SignalSource

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Form 4 transaction codes. Only open-market trades carry information about
# what the insider thinks the stock is worth; the rest are compensation
# mechanics that happen on a schedule regardless of the price.
OPEN_MARKET = {"P": "open-market purchase", "S": "open-market sale"}
MECHANICAL = {
    "A": "grant or award", "M": "option exercise", "F": "shares withheld for tax",
    "G": "gift", "C": "conversion", "D": "disposition to the issuer",
}

MAX_FILINGS_PER_TICKER = 8
REQUESTS_PER_SECOND = 5.0


class _Throttle:
    """Process-wide minimum spacing between SEC requests."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._interval:
                time.sleep(self._interval - gap)
            self._last = time.monotonic()


_THROTTLE = _Throttle(REQUESTS_PER_SECOND)


@dataclass
class InsiderTrade:
    symbol: str
    filed: date
    insider: str
    role: str
    code: str
    shares: float | None
    price: float | None
    planned: bool  # 10b5-1 scheduled trade

    @property
    def value(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price

    @property
    def is_buy(self) -> bool:
        return self.code == "P"

    @property
    def is_sale(self) -> bool:
        return self.code == "S"

    def line(self) -> str:
        amount = f"{self.shares:,.0f} sh" if self.shares else "size undisclosed"
        worth = f" ≈ ${self.value:,.0f}" if self.value else ""
        plan = " (10b5-1 plan)" if self.planned else ""
        label = OPEN_MARKET.get(self.code) or MECHANICAL.get(self.code, f"code {self.code}")
        return f"  - {self.filed.isoformat()} {self.insider} ({self.role}): {label}, {amount}{worth}{plan}"


class InsiderSource(SignalSource):
    """Form 4 open-market buys and sales over a trailing window."""

    name = "insider_form4"
    scope = "ticker"
    describes = "SEC EDGAR Form 4, open-market insider transactions"

    def __init__(self, degraded=None, days: int = 90, session: Any = None):
        super().__init__(degraded)
        self.days = days
        self._session = session
        self._cik: dict[str, int] | None = None

    # -- transport --------------------------------------------------------
    def user_agent(self) -> str:
        return env("SEC_USER_AGENT")

    def available(self) -> tuple[bool, str]:
        if not self.user_agent():
            return False, (
                "SEC_USER_AGENT not set — EDGAR fair-access rules require a descriptive "
                "User-Agent with a contact address, e.g. 'daybreak-research you@example.com'"
            )
        return True, ""

    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update(
                {"User-Agent": self.user_agent(), "Accept-Encoding": "gzip, deflate"}
            )
        return self._session

    def _get(self, url: str) -> Any:
        _THROTTLE.wait()
        response = self.session().get(url, timeout=20)
        response.raise_for_status()
        return response

    # -- collection -------------------------------------------------------
    def cik_for(self, symbol: str) -> int | None:
        if self._cik is None:
            payload = self._get(TICKER_MAP_URL).json()
            self._cik = {
                str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values()
            }
        return self._cik.get(symbol.upper())

    def collect(self, symbols: list[str], run_date: date) -> list[Signal]:
        cutoff = date.fromordinal(run_date.toordinal() - self.days)
        out: list[Signal] = []
        for symbol in symbols:
            try:
                trades = self.trades_for(symbol, cutoff)
            except Exception as exc:  # noqa: BLE001 - one bad ticker is not a dead source
                log.info("EDGAR Form 4 lookup failed for %s: %s", symbol, exc)
                continue
            signal = summarize(symbol, trades, run_date)
            if signal:
                out.append(signal)
        return out

    def trades_for(self, symbol: str, cutoff: date) -> list[InsiderTrade]:
        cik = self.cik_for(symbol)
        if cik is None:
            log.info("No EDGAR CIK for %s", symbol)
            return []
        recent = self._get(SUBMISSIONS_URL.format(cik=cik)).json().get("filings", {}).get("recent", {})
        accessions = _recent_form4(recent, cutoff)[:MAX_FILINGS_PER_TICKER]
        trades: list[InsiderTrade] = []
        for accession, filed in accessions:
            try:
                trades += self._parse_filing(symbol, cik, accession, filed)
            except Exception as exc:  # noqa: BLE001 - Form 4 XML shape varies by filer agent
                log.debug("Form 4 %s for %s unparsed: %s", accession, symbol, exc)
        return trades

    def _parse_filing(self, symbol: str, cik: int, accession: str, filed: date) -> list[InsiderTrade]:
        nodash = accession.replace("-", "")
        index = self._get(FILING_INDEX_URL.format(cik=cik, accession=nodash)).json()
        names = [item.get("name", "") for item in index.get("directory", {}).get("item", [])]
        document = next(
            (n for n in names if n.endswith(".xml") and not n.endswith("-index.xml")), None
        )
        if document is None:
            return []
        xml = self._get(ARCHIVE_URL.format(cik=cik, accession=nodash, document=document)).text
        return parse_form4(xml, symbol=symbol, filed=filed)


def _recent_form4(recent: dict[str, Any], cutoff: date) -> list[tuple[str, date]]:
    forms = recent.get("form", []) or []
    accessions = recent.get("accessionNumber", []) or []
    dates = recent.get("filingDate", []) or []
    out: list[tuple[str, date]] = []
    for form, accession, filed in zip(forms, accessions, dates):
        if str(form).strip() != "4":
            continue
        try:
            when = date.fromisoformat(str(filed))
        except ValueError:
            continue
        if when >= cutoff:
            out.append((str(accession), when))
    return out


def parse_form4(xml: str, symbol: str, filed: date) -> list[InsiderTrade]:
    """Extract non-derivative transactions from one Form 4 document."""
    root = ElementTree.fromstring(xml)
    owner = root.find(".//reportingOwner")
    name = _text(owner, "reportingOwnerId/rptOwnerName") if owner is not None else ""
    role = _role(owner)
    planned = bool(_text(root, ".//aff10b5One")) and _text(root, ".//aff10b5One") in {"1", "true"}
    if not planned:
        # Filer agents often state the plan in the footnote rather than the flag.
        planned = bool(re.search(r"10b5-1", "".join(root.itertext()), re.I))

    trades: list[InsiderTrade] = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        code = _text(txn, "transactionCoding/transactionCode")
        if not code:
            continue
        trades.append(
            InsiderTrade(
                symbol=symbol,
                filed=_date(_text(txn, "transactionDate/value")) or filed,
                insider=name or "undisclosed",
                role=role,
                code=code,
                shares=_float(_text(txn, "transactionAmounts/transactionShares/value")),
                price=_float(_text(txn, "transactionAmounts/transactionPricePerShare/value")),
                planned=planned,
            )
        )
    return trades


def summarize(symbol: str, trades: list[InsiderTrade], run_date: date) -> Signal | None:
    """Net open-market insider flow as one signal, or None when there is nothing to say."""
    open_market = [t for t in trades if t.code in OPEN_MARKET]
    if not open_market:
        return None
    buys = [t for t in open_market if t.is_buy]
    sales = [t for t in open_market if t.is_sale]
    discretionary_sales = [t for t in sales if not t.planned]
    bought = sum(t.value or 0.0 for t in buys)
    sold = sum(t.value or 0.0 for t in sales)
    net = bought - sold

    if bought and not sales:
        direction, strength = 1, min(1.0, 0.5 + 0.1 * len({t.insider for t in buys}))
    elif discretionary_sales and not buys:
        direction, strength = -1, min(1.0, 0.4 + 0.1 * len({t.insider for t in discretionary_sales}))
    elif net > 0:
        direction, strength = 1, min(1.0, abs(net) / max(bought + sold, 1.0))
    elif net < 0 and discretionary_sales:
        direction, strength = -1, min(1.0, abs(net) / max(bought + sold, 1.0))
    else:
        # Sales, but all on scheduled plans: reportable, not directional.
        direction, strength = 0, 0.2

    planned_note = ""
    if sales and len(discretionary_sales) < len(sales):
        # The label travels with the number. Reports read planned sales as
        # conviction draining away; they are scheduled months ahead on a plan
        # the seller cannot time. See tradingagent.semantics.
        planned_note = (
            f" {len(sales) - len(discretionary_sales)} of the sales were on 10b5-1 plans "
            "— scheduled in advance and NON-DIRECTIONAL, not a loss of conviction."
        )
    headline = (
        f"{len(buys)} open-market buy(s) worth ${bought:,.0f} against {len(sales)} sale(s) "
        f"worth ${sold:,.0f} over the window; net ${net:+,.0f}.{planned_note}"
    )
    return Signal(
        source=InsiderSource.name,
        kind="insider_flow",
        symbol=symbol,
        direction=direction,
        strength=strength,
        headline=headline,
        detail="\n".join(t.line() for t in sorted(open_market, key=lambda t: t.filed, reverse=True)[:6]),
        as_of=run_date,
    )


def _text(node: Any, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _role(owner: Any) -> str:
    if owner is None:
        return "insider"
    flags = [
        ("isDirector", "director"),
        ("isOfficer", "officer"),
        ("isTenPercentOwner", "10% owner"),
    ]
    roles = [label for tag, label in flags if _text(owner, f"reportingOwnerRelationship/{tag}") in {"1", "true"}]
    title = _text(owner, "reportingOwnerRelationship/officerTitle")
    if title:
        roles.append(title)
    return ", ".join(roles) or "insider"


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: str) -> date | None:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
