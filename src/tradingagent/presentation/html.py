"""The decision sheet as email HTML: inline CSS, one column, phone first.

Constraints that shaped every choice here:

- **Gmail deletes ``<head>`` and ``<style>``.** Every rule is therefore an
  inline ``style`` attribute. There is no stylesheet to factor into, so the
  repetition below is the format, not an oversight — :func:`_css` exists to keep
  it declarative rather than to remove it.
- **Outlook renders with Word**, which ignores ``max-width`` on a ``div`` and
  most of flexbox. The layout is a single centred table, one column, because
  that is what every client has agreed on since 2003.
- **Images may not load.** Every chart has alt text that states the number it
  draws, and no chart is the only place a figure appears — the tables carry all
  of them. A reader with images off loses the picture and none of the content.

Nothing here formats a number it was not given. Prices arrive from
:mod:`.context` already computed by the trade plan.
"""

from __future__ import annotations

from html import escape

from .sheet import DecisionSheet, badge_colours

MAX_WIDTH = 640

INK = "#111827"
BODY = "#374151"
MUTED = "#6b7280"
HAIR = "#e5e7eb"
CARD_BG = "#ffffff"
PAGE_BG = "#f3f4f6"
ACCENT = "#1d4ed8"
WARN_BG = "#fffbeb"
WARN_INK = "#92400e"
BAD_BG = "#fef2f2"
BAD_INK = "#991b1b"
GOOD_INK = "#065f46"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"
)


def _css(**rules: str) -> str:
    """``font-size: 13px; color: #111`` from keyword arguments."""
    return ";".join(f"{k.replace('_', '-')}:{v}" for k, v in rules.items())


_P = _css(margin="0 0 10px", font_size="14px", line_height="1.5", color=BODY)
_H2 = _css(
    margin="0 0 10px",
    font_size="12px",
    letter_spacing="0.08em",
    text_transform="uppercase",
    color=MUTED,
    font_weight="700",
)
_TD = _css(padding="6px 8px", font_size="13px", color=BODY, border_bottom=f"1px solid {HAIR}")
_TH = _css(
    padding="6px 8px",
    font_size="11px",
    color=MUTED,
    text_align="left",
    text_transform="uppercase",
    letter_spacing="0.04em",
    border_bottom=f"2px solid {HAIR}",
)


def _card(body: str, tint: str = CARD_BG, edge: str = HAIR) -> str:
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="{_css(width="100%", background=tint, border=f"1px solid {edge}", border_radius="10px", margin="0 0 14px")}">'
        f'<tr><td style="{_css(padding="14px 16px")}">{body}</td></tr></table>'
    )


def _section(title: str, body: str, tint: str = CARD_BG, edge: str = HAIR) -> str:
    return _card(f'<div style="{_H2}">{escape(title)}</div>{body}', tint, edge)


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value else "—"


def _pct(value: float | None, digits: int = 1) -> str:
    return f"{value:.{digits}f}%" if value is not None else "—"


def _verdict_badge(rating: str, confidence: str) -> str:
    ink, tint = badge_colours(rating)
    label = f"{rating} · {confidence}" if confidence else rating
    return (
        f'<span style="display:inline-block;padding:3px 10px;border-radius:999px;white-space:nowrap;'
        f'background:{tint};color:{ink};font-size:12px;font-weight:700;">{escape(label)}</span>'
    )


def _chart_img(chart, width: int = MAX_WIDTH - 32) -> str:
    if chart is None:
        return ""
    return (
        f'<img src="cid:{chart.cid}" alt="{escape(chart.alt)}" width="{width}" '
        f'style="{_css(display="block", width="100%", max_width=f"{width}px", height="auto", border="0", margin="8px 0 4px")}">'
    )


def _table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] * len(headers)
    head = "".join(
        f'<th style="{_TH};text-align:{a}">{escape(h)}</th>' for h, a in zip(headers, aligns)
    )
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f'<td style="{_TD};text-align:{a}">{cell}</td>' for cell, a in zip(row, aligns)
        ) + "</tr>"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="{_css(border_collapse="collapse", width="100%")}">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


# --- sections ---------------------------------------------------------------


def _header(sheet: DecisionSheet) -> str:
    context = sheet.context
    as_of = context.market_as_of if context else ""
    sub = f"market data as of {escape(as_of)} close" if as_of else "market data unavailable"
    if context and context.session_note:
        sub += f" · {escape(context.session_note)}"
    return (
        f'<div style="{_css(font_size="20px", font_weight="700", color=INK, margin="0 0 4px")}">'
        f"Daybreak — {sheet.run_date.isoformat()}</div>"
        f'<div style="{_css(font_size="12px", color=MUTED, margin="0 0 16px")}">{sub}</div>'
    )


def _regime(sheet: DecisionSheet, charts: dict) -> str:
    if sheet.context is None:
        return ""
    line = sheet.regime_line
    if not line:
        return ""
    rows = "".join(
        f'<tr><td style="{_css(padding="2px 10px 2px 0", font_size="12px", color=MUTED, white_space="nowrap")}">'
        f"{escape(label)}</td>"
        f'<td style="{_css(padding="2px 0", font_size="12px", color=BODY)}">{escape(value)}</td></tr>'
        for label, value in sheet.posture_extras
    )
    body = (
        f'<div style="{_css(font_size="15px", line_height="1.5", color=INK, font_weight="600", margin="0 0 8px")}">'
        f"{escape(line)}</div>"
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
        + _chart_img(charts.get("breadth"))
        + _chart_img(charts.get("spy"))
        + _chart_img(charts.get("sectors"))
    )
    return _section("Regime", body)


def _gates(sheet: DecisionSheet) -> str:
    if sheet.context is None:
        return ""
    gates = sheet.context.gates
    if not gates:
        return _section(
            "Do not act before",
            f'<div style="{_P}">Nothing on the calendar with a confirmed date gates today. '
            "Only releases whose date is VERIFIED against an official calendar can appear "
            "here; an estimated date is background, never an instruction to wait.</div>",
        )
    rows = [
        [
            f'<strong style="color:{INK}">{escape(g.date)}</strong>',
            escape(g.name),
            escape(g.impact),
            f'<span style="color:{MUTED}">{escape(g.source)}</span>',
        ]
        for g in gates
    ]
    return _section(
        "Do not act before",
        _table(["Date", "Release", "Impact", "Source"], rows)
        + f'<div style="{_css(font_size="11px", color=MUTED, margin="8px 0 0")}">'
        "Every row is VERIFIED against an official release calendar. Estimated dates are "
        "excluded by design — a guess that reaches this list has become an instruction.</div>",
        tint=WARN_BG,
        edge="#fde68a",
    )


def _consensus_line(setup) -> str:
    c = setup.consensus
    if not c.known:
        return ""
    bits = []
    if c.recommendation:
        bits.append(escape(c.recommendation))
    if c.analysts:
        bits.append(f"{c.analysts} analysts")
    if c.mean:
        gap = (
            f" ({(setup.price_target / c.mean - 1) * 100:+.1f}% vs ours)"
            if setup.price_target
            else ""
        )
        bits.append(f"mean {_money(c.mean)}{gap}")
    if c.median:
        bits.append(f"median {_money(c.median)}")
    return (
        f'<div style="{_css(font_size="11px", color=MUTED, margin="6px 0 0")}">'
        f"Street: {' · '.join(bits)}</div>"
    )


def _setup_card(setup, charts: dict) -> str:
    head = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="{_css(font_size="17px", font_weight="700", color=INK)}">'
        f"{escape(setup.symbol)} "
        f'<span style="{_css(font_size="12px", font_weight="400", color=MUTED)}">'
        f"{escape(setup.name)}</span></td>"
        f'<td align="right">{_verdict_badge(setup.rating, setup.confidence)}</td>'
        f"</tr></table>"
    )
    wait = (
        f'<div style="{_css(margin="10px 0 2px", font_size="14px", color=INK, font_weight="600")}">'
        f"{escape(setup.wait_condition)}</div>"
        if setup.wait_condition
        else ""
    )
    # A row of six dashes is not information. An unpriced plan says why it is
    # unpriced — the status line below — and shows nothing where the levels
    # would be, so the reader does not scan a table to learn it is empty.
    levels = (
        _table(
            ["Entry", "Stop", "Target", "Risk", "R:R", "Size"],
            [
                [
                    _money(setup.entry),
                    _money(setup.stop),
                    _money(setup.target),
                    _pct(setup.risk_pct, 2),
                    f"{setup.reward_risk:.1f}x" if setup.reward_risk else "—",
                    _pct(setup.size_pct),
                ]
            ],
            ["right"] * 6,
        )
        if setup.entry or setup.stop or setup.target
        else ""
    )
    status = (
        f'<div style="{_css(margin="8px 0 0", font_size="12px", color=BAD_INK, font_weight="600")}">'
        f"{escape(setup.status)}</div>"
        if setup.status and not setup.actionable
        else ""
    )
    horizon = (
        f'<div style="{_css(font_size="11px", color=MUTED, margin="6px 0 0")}">'
        f"Horizon {escape(setup.time_horizon or 'not stated')} · last close "
        f"{_money(setup.spot)}</div>"
        if setup.spot or setup.time_horizon
        else ""
    )
    return _card(
        head
        + wait
        + levels
        + status
        + horizon
        + _consensus_line(setup)
        + _chart_img(charts.get(f"setup-{setup.symbol.lower()}"))
    )


def _setups(sheet: DecisionSheet, charts: dict) -> str:
    if sheet.context is None:
        return ""
    setups = sheet.setups
    if not setups:
        return _section(
            "Best setups",
            f'<div style="{_P}">No name earned a long or short verdict today. '
            "That is a result, not a gap.</div>",
        )
    cards = "".join(_setup_card(s, charts) for s in setups)
    more = (
        f'<div style="{_css(font_size="11px", color=MUTED)}">'
        f"{sheet.setups_hidden} further verdict(s) in the attached brief.</div>"
        if sheet.setups_hidden
        else ""
    )
    return f'<div style="{_H2}">Best setups</div>{cards}{more}'


def _avoids(sheet: DecisionSheet) -> str:
    if sheet.context is None or not sheet.context.avoids:
        return ""
    rows = [
        [
            f'<strong style="color:{INK}">{escape(a.symbol)}</strong>',
            _verdict_badge(a.rating, a.confidence),
            f'<span style="color:{MUTED}">{escape(a.reason)}</span>',
        ]
        for a in sheet.context.avoids
    ]
    return _section("Avoids", _table(["Ticker", "Verdict", "Why"], rows))


def _overlays(sheet: DecisionSheet) -> str:
    if sheet.context is None:
        return ""
    context = sheet.context
    if not context.overlays and not context.overlay_skips:
        return ""
    body = ""
    if context.overlays:
        rows = []
        for o in context.overlays:
            conflict = (
                f'<div style="color:{BAD_INK};font-size:11px;margin-top:3px">⚠ '
                + escape("; ".join(o.conflicts))
                + "</div>"
                if o.conflicts
                else ""
            )
            ink = BAD_INK if "BELOW" in o.breakeven_status else GOOD_INK
            rows.append(
                [
                    f'<strong style="color:{INK}">{escape(o.symbol)}</strong>'
                    f'<div style="font-size:11px;color:{MUTED}">{escape(o.strategy)}</div>',
                    f"{_money(o.strike)}<div style='font-size:11px;color:{MUTED}'>"
                    f"{escape(o.expiry)} · {o.dte}d</div>",
                    f"{o.delta:.2f}" if o.delta is not None else "—",
                    f"{_money(o.credit)}<div style='font-size:11px;color:{MUTED}'>"
                    f"{_pct(o.annualized_yield_pct)} ann.</div>",
                    f'<span style="color:{ink};font-size:11px">{escape(o.breakeven_status)}</span>'
                    f"{conflict}",
                ]
            )
        body += _table(
            ["Ticker", "Strike", "Delta", "Credit", "Breakeven vs invalidation"],
            rows,
            ["left", "right", "right", "right", "left"],
        )
    if context.overlay_skips:
        items = "".join(
            f'<li style="{_css(margin="0 0 3px")}"><strong>{escape(s.symbol)}</strong> — '
            f"{escape(s.reason)}</li>"
            for s in context.overlay_skips
        )
        body += (
            f'<div style="{_css(font_size="11px", color=MUTED, margin="10px 0 4px")}">No overlay</div>'
            f'<ul style="{_css(margin="0", padding_left="18px", font_size="12px", color=MUTED)}">{items}</ul>'
        )
    return _section("Options go / no-go", body)


def _changes(sheet: DecisionSheet) -> str:
    if sheet.context is None:
        return ""
    if not sheet.compared_with:
        return _section(
            "Changes since the last session",
            f'<div style="{_P}">No earlier session on record to compare against.</div>',
        )
    if not sheet.changes:
        return _section(
            "Changes since the last session",
            f'<div style="{_P}">Nothing changed against {escape(sheet.compared_with)} — '
            "the same names, the same verdicts.</div>",
        )
    tint = {"upgraded": GOOD_INK, "downgraded": BAD_INK, "new": ACCENT, "dropped": MUTED}
    items = "".join(
        f'<li style="{_css(margin="0 0 4px", font_size="13px", color=BODY)}">'
        f'<strong style="color:{tint.get(c.kind, BODY)}">{escape(c.symbol)}</strong> — '
        f"{escape(c.detail)}</li>"
        for c in sheet.changes
    )
    return _section(
        "Changes since the last session",
        f'<div style="{_css(font_size="11px", color=MUTED, margin="0 0 6px")}">'
        f"versus {escape(sheet.compared_with)}</div>"
        f'<ul style="{_css(margin="0", padding_left="18px")}">{items}</ul>',
    )


def _confidence(sheet: DecisionSheet) -> str:
    level = sheet.confidence
    ink, tint = {
        "HIGH": (GOOD_INK, "#ecfdf5"),
        "MODERATE": (WARN_INK, WARN_BG),
        "LOW": (BAD_INK, BAD_BG),
    }[level]
    reasons = sheet.confidence_reasons
    body = (
        f'<div style="{_css(font_size="15px", font_weight="700", margin="0 0 6px")};color:{ink}">'
        f"{level}</div>"
    )
    if reasons:
        body += (
            f'<ul style="{_css(margin="0", padding_left="18px", font_size="12px", color=BODY)}">'
            + "".join(f"<li>{escape(r)}</li>" for r in reasons)
            + "</ul>"
        )
    else:
        body += (
            f'<div style="{_css(font_size="12px", color=BODY)}">Every source answered; '
            "nothing degraded this run.</div>"
        )
    return _section("System confidence", body, tint=tint, edge=HAIR)


def _evidence(sheet: DecisionSheet) -> str:
    if not sheet.evidence:
        return ""
    text = sheet.evidence.strip()
    return _section(
        "Evidence so far",
        f'<div style="{_css(font_size="13px", color=BODY, line_height="1.5", white_space="pre-wrap")}">'
        f"{escape(text)}</div>",
    )


def _footer(sheet: DecisionSheet, attachments: list[str], links: list[tuple[str, str]]) -> str:
    parts = []
    if attachments:
        parts.append(
            f'<div style="{_css(font_size="12px", color=BODY, margin="0 0 6px")}">'
            "Full research attached: " + escape(", ".join(attachments)) + "</div>"
        )
    if links:
        rows = "".join(
            f'<li style="margin:0 0 3px"><a href="{escape(url)}" '
            f'style="color:{ACCENT};text-decoration:none">{escape(label)}</a></li>'
            for label, url in links
        )
        parts.append(
            f'<div style="{_css(font_size="12px", color=BODY, margin="0 0 4px")}">In the bucket:</div>'
            f'<ul style="{_css(margin="0 0 6px", padding_left="18px", font_size="12px")}">{rows}</ul>'
        )
    if sheet.context and sheet.context.snapshot_id:
        parts.append(
            f'<div style="{_css(font_size="11px", color=MUTED)}">Snapshot '
            f"<code>{escape(sheet.context.snapshot_id)}</code></div>"
        )
    parts.append(
        f'<div style="{_css(font_size="11px", color=MUTED, margin="10px 0 0", line_height="1.5")}">'
        "Research only — not investment advice. Paper trading only; no live orders are ever "
        "placed. Every figure is model output over free-tier data and may be wrong. "
        "The human makes every decision.</div>"
    )
    return _card("".join(parts), tint="#fafafa")


def render_sheet(
    sheet: DecisionSheet,
    charts: list | None = None,
    attachments: list[str] | None = None,
    links: list[tuple[str, str]] | None = None,
) -> str:
    """The whole email body, as one string of inline-styled HTML."""
    by_cid = {c.cid: c for c in (charts or [])}
    sections = [
        _header(sheet),
        _regime(sheet, by_cid),
        _gates(sheet),
        _setups(sheet, by_cid),
        _avoids(sheet),
        _overlays(sheet),
        _changes(sheet),
        _confidence(sheet),
        _evidence(sheet),
        _footer(sheet, attachments or [], links or []),
    ]
    inner = "".join(s for s in sections if s)
    return (
        f'<div style="{_css(margin="0", padding="16px 0", background=PAGE_BG, font_family=_FONT)}">'
        f'<table role="presentation" align="center" width="{MAX_WIDTH}" cellpadding="0" '
        f'cellspacing="0" border="0" style="{_css(width="100%", max_width=f"{MAX_WIDTH}px", margin="0 auto")}">'
        f'<tr><td style="{_css(padding="0 12px")}">{inner}</td></tr></table></div>'
    )


def render_text(sheet: DecisionSheet) -> str:
    """The plain-text alternative. Same order, same numbers, no markup.

    Not a courtesy: a client that shows this instead of the HTML must still be
    able to answer "what do I do today", so it carries the levels rather than
    telling the reader to open the attachment.
    """
    out = [f"DAYBREAK — {sheet.run_date.isoformat()}", ""]
    context = sheet.context
    if context is None:
        out += ["The decision sheet's data file is missing for this session.", ""]
        out += sheet.unavailable
        # The evidence line is a rolling statement about every prior session and
        # does not depend on today's context. Dropping it here would silently
        # lose Friday's record on exactly the runs that went least well.
        if sheet.evidence:
            out += ["", "EVIDENCE SO FAR", sheet.evidence.strip()]
        return "\n".join(out)

    out += [f"Market data as of {context.market_as_of} close", ""]
    if sheet.regime_line:
        out += ["REGIME", sheet.regime_line, ""]
    out += ["DO NOT ACT BEFORE"]
    out += (
        [f"  {g.date}  {g.name} ({g.impact}, {g.source})" for g in context.gates]
        if context.gates
        else ["  nothing with a verified date gates today"]
    )
    out += ["", "BEST SETUPS"]
    if sheet.setups:
        for s in sheet.setups:
            out.append(f"  {s.symbol} — {s.rating} ({s.confidence})")
            if s.wait_condition:
                out.append(f"    {s.wait_condition}")
            out.append(
                f"    entry {_money(s.entry)} / stop {_money(s.stop)} / target {_money(s.target)}"
                f" · risk {_pct(s.risk_pct, 2)} · size {_pct(s.size_pct)}"
            )
    else:
        out.append("  none today")
    out += ["", "AVOIDS"]
    out += [f"  {a.symbol} — {a.reason}" for a in context.avoids] or ["  none"]
    out += ["", "OPTIONS"]
    if context.overlays:
        out += [
            f"  {o.symbol} {o.strategy} {_money(o.strike)} {o.expiry} — {o.breakeven_status}"
            for o in context.overlays
        ]
    else:
        out.append("  no overlay passed the filters")
    out += ["", f"CHANGES SINCE {sheet.compared_with or 'n/a'}"]
    out += [f"  {c.symbol} — {c.detail}" for c in sheet.changes] or ["  none"]
    out += ["", f"SYSTEM CONFIDENCE: {sheet.confidence}"]
    out += [f"  - {r}" for r in sheet.confidence_reasons]
    if sheet.evidence:
        out += ["", "EVIDENCE SO FAR", sheet.evidence.strip()]
    out += [
        "",
        "Research only — not investment advice. Paper trading only. "
        "The human makes every decision.",
    ]
    return "\n".join(out)
