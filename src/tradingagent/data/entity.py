"""Which company a headline is actually about.

A news feed's idea of "news about V" is loose. Finnhub's company endpoint
returns syndicated wire copy tagged with a symbol it may only appear next to in
a list, and our RSS leg used to attach a headline to a ticker whenever the bare
ticker token showed up in it. Both produced real misattributions in shipped
reports:

- V and NFLX were both given "SanDisk's Investor Day Puts NAND Center Stage" as
  their latest headline.
- AON's was "BRO Stock Trading at a Discount to Industry at 15.04X".
- STZ's was "Berkshire Hathaway 13F Preview".
- UNP's news tone was driven to +0.68 — worth +5.4 ranking points — by
  "Berkshire Hathaway Stock Nears Record", a headline that never mentions Union
  Pacific.

A bare ticker token is the worst offender because so many symbols are ordinary
words or letters: V, A, C, ON, IT, ALL, CAT, GAP, KEY, NOW. "A Deep Dive Into
Chevron" is not news about Agilent.

So a headline is attached to a ticker only on evidence a reader would accept:

``STRONG`` (relevance ≥ :data:`MIN_TONE_RELEVANCE`)
    A ``$V`` cashtag, an exchange parenthetical ``(V)`` / ``(NYSE: V)``, or the
    issuer's name from the S&P 500 constituent list. Only these may feed a tone
    score, a "latest headline" line, or a per-ticker mention signal.
``MEDIUM``
    The feed tagged it to the symbol and nothing in the headline confirms it.
    Kept, labelled, and never scored — it is a lead, not a fact.
``NONE``
    Nothing but a bare ticker token, or an ambiguous single-word issuer name
    with no corroboration. Excluded.

The universe file supplies the issuer names, so this costs nothing and adds no
source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

STRONG = "STRONG"
MEDIUM = "MEDIUM"
NONE = "NONE"

#: A headline must clear this to be treated as being about the ticker: to score
#: its tone, to be printed as the name's latest headline, or to count as a
#: mention. Feed-tagged-only sits deliberately below it.
MIN_TONE_RELEVANCE = 0.8

RELEVANCE = {
    "cashtag": 1.0,
    "parenthetical": 0.95,
    "issuer": 0.9,
    "feed_tag": 0.6,
    "ambiguous_issuer": 0.5,
    "ticker_token": 0.0,
}

#: Legal-form noise that no headline writer uses. Stripped to get the name a
#: reader would recognise: "Aon plc" -> "aon", "AbbVie Inc." -> "abbvie".
_SUFFIXES = (
    "incorporated", "corporation", "company", "companies", "holdings", "holding",
    "group", "inc", "corp", "co", "plc", "ltd", "limited", "lp", "nv", "sa", "ag",
    "the", "class a", "class b", "class c", "& co", "and co",
)

#: Single-word issuer names that are also ordinary English. "Raises Price
#: Target to $120" is not news about TGT. On their own these resolve MEDIUM;
#: they attach only when the headline treats the word as a company (see
#: :data:`_EQUITY_CUES`). Multi-word names are not at risk and are not listed.
_AMBIGUOUS_NAMES = frozenset({
    "target", "gap", "ally", "match", "cadence", "charter", "principal",
    "progressive", "public", "republic", "travelers", "first", "general",
    "applied", "advanced", "global", "key", "now", "all", "on", "it", "cat",
    "best", "dollar", "east", "west", "north", "south", "union", "state",
    "street", "air", "live", "sun", "well", "old", "new", "range", "centre",
})

#: What a headline says right after a company's name. "Target Q2 Earnings Beat"
#: is the company; "price target to $120" is not. Cheap, and it only ever
#: promotes a name that is already the issuer's.
_EQUITY_CUES = (
    "stock", "stocks", "shares", "share", "earnings", "revenue", "guidance",
    "dividend", "results", "ceo", "cfo", "beats", "beat", "misses", "miss",
    "reports", "reported", "announces", "announced", "said", "says", "raises",
    "cuts", "upgraded", "downgraded", "inc", "corp", "q1", "q2", "q3", "q4",
)


@dataclass(frozen=True)
class Resolution:
    """Whether a headline is about ``symbol``, and on what evidence."""

    symbol: str
    relevance: float
    confidence: str
    basis: str

    @property
    def attributable(self) -> bool:
        """May this headline be presented, or scored, as news about the name?"""
        return self.relevance >= MIN_TONE_RELEVANCE

    def note(self) -> str:
        return f"{self.confidence} ({self.basis})"


def aliases_for(name: str) -> tuple[str, ...]:
    """Ways a headline writer would name the issuer behind ``name``.

    Kept deliberately small. An alias that is a substring of ordinary prose
    costs a wrong attribution; a missing alias costs one unattached headline.
    """
    cleaned = re.sub(r"[.,]", " ", name or "").strip()
    if not cleaned:
        return ()
    out = [cleaned]
    words = cleaned.split()
    while words and words[-1].lower().strip("&") in _SUFFIXES:
        words = words[:-1]
        if words:
            out.append(" ".join(words))
    trimmed = out[-1]
    if "&" in trimmed:
        out.append(trimmed.replace("&", "and"))
    # "Alphabet (Class A)" and friends leave a trailing parenthetical.
    out.append(re.sub(r"\s*\([^)]*\)", "", trimmed).strip())
    seen: dict[str, None] = {}
    for alias in out:
        alias = alias.strip()
        if len(alias) >= 3:
            seen.setdefault(alias, None)
    return tuple(seen)


class IssuerIndex:
    """Ticker -> issuer aliases, from the S&P 500 constituent list."""

    def __init__(self, names: dict[str, str]):
        self.names = {sym.upper(): name for sym, name in names.items()}
        self.aliases = {sym: aliases_for(name) for sym, name in self.names.items()}

    def resolve(self, headline: str, symbol: str, feed_tagged: bool = False) -> Resolution:
        """What evidence ``headline`` offers that it is about ``symbol``.

        ``feed_tagged`` means the provider returned this story for this symbol.
        That is a claim by a wire aggregator, not a fact about the story, so on
        its own it stays below :data:`MIN_TONE_RELEVANCE`.
        """
        symbol = symbol.upper()
        text = headline or ""
        if _cashtag(text, symbol):
            return Resolution(symbol, RELEVANCE["cashtag"], STRONG, f"${symbol} cashtag")
        if _parenthetical(text, symbol):
            return Resolution(symbol, RELEVANCE["parenthetical"], STRONG, f"({symbol}) in the headline")

        hit = self._issuer_hit(text, symbol)
        if hit and not _is_ambiguous(hit):
            return Resolution(symbol, RELEVANCE["issuer"], STRONG, f"issuer name “{hit}”")
        if hit and _reads_as_a_company(text, hit):
            return Resolution(
                symbol, RELEVANCE["issuer"], STRONG,
                f"issuer name “{hit}” used as a company",
            )
        if hit:
            # "Target beats" or "target price raised" — same letters, and the
            # headline gives us no way to tell which. A lead, not an attribution.
            return Resolution(
                symbol, RELEVANCE["ambiguous_issuer"], MEDIUM,
                f"issuer name “{hit}” is also an ordinary word — no ticker in the headline",
            )
        if feed_tagged:
            return Resolution(
                symbol, RELEVANCE["feed_tag"], MEDIUM,
                "feed tagged it to this symbol; the headline does not name the company",
            )
        if _bare_token(text, symbol):
            return Resolution(
                symbol, RELEVANCE["ticker_token"], NONE,
                f"only a bare “{symbol}” token, which is not a reference to the company",
            )
        return Resolution(symbol, 0.0, NONE, "the headline does not mention this company")

    def _issuer_hit(self, text: str, symbol: str) -> str | None:
        for alias in self.aliases.get(symbol, ()):
            if _word_in(text, alias):
                return alias
        return None

    def other_issuers(self, headline: str, exclude: str = "") -> list[str]:
        """Universe names the headline does mention — why an attribution failed."""
        exclude = exclude.upper()
        out = []
        for symbol, aliases in self.aliases.items():
            if symbol == exclude:
                continue
            for alias in aliases:
                if not _is_ambiguous(alias) and _word_in(headline, alias):
                    out.append(symbol)
                    break
        return sorted(out)


@lru_cache(maxsize=1)
def issuer_index() -> IssuerIndex:
    """The index built from the bundled universe snapshot."""
    from .universe import load_snapshot

    try:
        return IssuerIndex({c.symbol: c.name for c in load_snapshot()})
    except Exception:  # noqa: BLE001 - a missing name file must not stop a run
        return IssuerIndex({})


def resolve(headline: str, symbol: str, feed_tagged: bool = False,
            index: IssuerIndex | None = None) -> Resolution:
    return (index or issuer_index()).resolve(headline, symbol, feed_tagged=feed_tagged)


def _cashtag(text: str, symbol: str) -> bool:
    return re.search(rf"\${re.escape(symbol)}(?![A-Za-z0-9])", text, re.I) is not None


def _parenthetical(text: str, symbol: str) -> bool:
    """``(V)``, ``(NYSE: V)``, ``(NASDAQ:V)`` — a writer naming the listing."""
    pattern = rf"[(\[]\s*(?:[A-Z]{{2,6}}\s*:\s*)?{re.escape(symbol)}\s*[)\],]"
    return re.search(pattern, text) is not None


def _bare_token(text: str, symbol: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", text) is not None


def _word_in(text: str, alias: str) -> bool:
    """Alias as whole words, case-insensitively, apostrophes tolerated."""
    if not alias:
        return False
    parts = [re.escape(word) for word in alias.split()]
    pattern = r"(?<![A-Za-z0-9])" + r"[\s\-]+".join(parts) + r"(?:['’]s)?(?![A-Za-z0-9])"
    return re.search(pattern, text, re.I) is not None


def _is_ambiguous(alias: str) -> bool:
    return " " not in alias and alias.lower() in _AMBIGUOUS_NAMES


def _reads_as_a_company(text: str, alias: str) -> bool:
    """Is the ambiguous word being used as the company's name?

    "Target Q2 Earnings Beat" and "shares of Gap" are the company. "Raises
    Price Target to $120" and "the gap between bid and ask" are not.
    """
    cues = "|".join(_EQUITY_CUES)
    after = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?:['’]s)?\s+(?:{cues})(?![A-Za-z0-9])"
    before = rf"shares\s+(?:of|in)\s+{re.escape(alias)}(?![A-Za-z0-9])"
    return bool(re.search(after, text, re.I) or re.search(before, text, re.I))
