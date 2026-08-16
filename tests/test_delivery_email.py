"""Email delivery: subject construction, HTML conversion, message assembly."""

from __future__ import annotations

import smtplib
from datetime import date
from email import message_from_bytes

import pytest

from tradingagent.delivery import email as E

RUN_DATE = date(2026, 8, 14)


def cfg(**over):
    base = dict(
        host="smtp.example.com",
        port=587,
        user="bot@example.com",
        password="app-password",
        sender="bot@example.com",
        recipients=("human@example.com",),
    )
    base.update(over)
    return E.EmailConfig(**base)


def V(symbol, rating, confidence="M"):
    return E.Verdict(symbol=symbol, rating=rating, confidence=confidence)


# --- subject -----------------------------------------------------------------


def test_subject_states_count_and_best_rated_ticker():
    subject = E.build_subject(RUN_DATE, [V("FDX", "Hold"), V("NVDA", "Buy"), V("V", "Overweight")])
    assert subject == "Daybreak 2026-08-14 — 3 verdicts, top: NVDA Buy"


def test_ties_break_on_queue_order_not_alphabetically():
    """The deep queue is already ranked, so the earlier name wins a rating tie."""
    subject = E.build_subject(RUN_DATE, [V("ZTS", "Buy"), V("AMD", "Buy")])
    assert "top: ZTS Buy" in subject


def test_degraded_verdicts_never_headline_when_a_real_one_exists():
    top = E.pick_top([V("AAA", "DEGRADED"), V("BBB", "Underweight")])
    assert top.symbol == "BBB"


def test_a_run_where_everything_degraded_still_names_something():
    subject = E.build_subject(RUN_DATE, [V("AAA", "DEGRADED"), V("BBB", "DEGRADED")])
    # Zero *scored* verdicts, but the subject still points at a ticker rather
    # than reading as an empty run.
    assert subject.startswith("Daybreak 2026-08-14 — 0 verdicts, top: AAA DEGRADED")


def test_no_verdicts_at_all_says_none():
    assert E.build_subject(RUN_DATE, []) == "Daybreak 2026-08-14 — 0 verdicts, top: none"


def test_degradation_is_named_in_the_subject():
    subject = E.build_subject(RUN_DATE, [V("NVDA", "Buy")], ["Finnhub", "SPY benchmark"])
    assert subject.endswith("· DEGRADED: Finnhub, SPY benchmark")


def test_long_degradation_lists_collapse_so_the_subject_stays_readable():
    subject = E.build_subject(
        RUN_DATE, [V("NVDA", "Buy")], ["a", "b", "c", "d", "e"]
    )
    assert subject.endswith("· DEGRADED: a, b, c +2 more")


def test_a_degraded_run_still_reports_its_verdicts():
    """The degradation annotates the subject; it does not replace the content."""
    subject = E.build_subject(RUN_DATE, [V("NVDA", "Buy")], ["Finnhub"])
    assert "1 verdicts, top: NVDA Buy" in subject


# --- markdown -> html ---------------------------------------------------------

BRIEF = """# Daily Brief — 2026-08-14

> Research only.

## 1. Market Overview

| Index | Last | 1d |
|---|---:|---:|
| SPY | 777.88 | +0.70% |

- bullet one
- bullet two
"""


def test_tables_survive_conversion_with_alignment_intact():
    html = E.markdown_to_html(BRIEF)
    assert "<table" in html and "</table>" in html
    # Column alignment from the markdown must not be lost to the injected style.
    assert "text-align:right" in html


def test_every_table_is_horizontally_scrollable_on_a_phone():
    html = E.markdown_to_html(BRIEF)
    assert html.count("overflow-x:auto") >= html.count("<table")


def test_styles_are_inline_because_gmail_strips_style_blocks():
    html = E.markdown_to_html(BRIEF)
    assert "<style" not in html
    assert "<td style=" in html and "<th style=" in html


def test_conversion_covers_the_rest_of_the_brief_vocabulary():
    html = E.markdown_to_html(BRIEF)
    for fragment in ("<h1 style=", "<h2 style=", "<blockquote style=", "<li style="):
        assert fragment in html


def test_html_is_a_standalone_document():
    html = E.markdown_to_html(BRIEF, title="subject here")
    assert html.startswith("<!DOCTYPE html>")
    assert "<title>subject here</title>" in html


# --- attachments ---------------------------------------------------------------


def test_brief_and_deep_reports_are_all_attached(tmp_path):
    brief = tmp_path / "daily-brief.md"
    brief.write_text("# brief")
    deep = tmp_path / "NVDA.md"
    deep.write_text("# nvda")

    items, dropped = E.collect_attachments(brief, [deep])
    assert [i.filename for i in items] == ["daily-brief.md", "NVDA.md"]
    assert dropped == []


def test_a_missing_deep_report_is_reported_not_silently_skipped(tmp_path):
    brief = tmp_path / "daily-brief.md"
    brief.write_text("# brief")

    items, dropped = E.collect_attachments(brief, [tmp_path / "GONE.md"])
    assert [i.filename for i in items] == ["daily-brief.md"]
    assert dropped == ["GONE.md (missing)"]


def test_oversized_attachments_are_dropped_rather_than_failing_the_send(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "MAX_ATTACHMENT_BYTES", 32)
    brief = tmp_path / "daily-brief.md"
    brief.write_text("x" * 16)
    big = tmp_path / "BIG.md"
    big.write_text("y" * 64)

    items, dropped = E.collect_attachments(brief, [big])
    assert [i.filename for i in items] == ["daily-brief.md"]
    assert dropped == ["BIG.md (size cap)"]


# --- message assembly -----------------------------------------------------------


def build(brief_md=BRIEF, attachments=None):
    return E.build_message(cfg(), "subject", brief_md, attachments or [])


def test_message_carries_both_plain_text_and_html():
    message = build()
    types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_plain_text_part_is_the_markdown_verbatim():
    message = build()
    plain = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    assert plain.get_content().strip() == BRIEF.strip()


def test_attachments_ride_along_as_markdown_files():
    message = build(attachments=[E.Attachment("NVDA.md", "# nvda")])
    names = [p.get_filename() for p in message.walk() if p.get_filename()]
    assert names == ["NVDA.md"]


def test_message_round_trips_through_the_wire_format():
    """Guards against a header or MIME-structure error that only shows on send."""
    message = build(attachments=[E.Attachment("NVDA.md", "# nvda")])
    parsed = message_from_bytes(bytes(message))
    assert parsed["Subject"] == "subject"
    assert parsed["To"] == "human@example.com"
    assert parsed.is_multipart()


def test_multiple_recipients_are_comma_joined():
    message = E.build_message(
        cfg(recipients=("a@example.com", "b@example.com")), "s", "# x", []
    )
    assert message["To"] == "a@example.com, b@example.com"


# --- config ---------------------------------------------------------------------


def test_config_reads_the_documented_env_vars(monkeypatch):
    for key, value in {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "465",
        "SMTP_USER": "bot@gmail.com",
        "SMTP_APP_PASSWORD": "abcd efgh ijkl mnop",
        "REPORT_EMAIL_TO": "me@example.com, you@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("SMTP_FROM", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    config = E.load_email_config()
    assert config.recipients == ("me@example.com", "you@example.com")
    assert config.sender == "bot@gmail.com"  # defaults to SMTP_USER
    assert config.password == "abcd efgh ijkl mnop"
    assert config.use_ssl is True  # port 465 is implicit TLS
    assert config.configured


def test_smtp_password_is_accepted_as_an_alias(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "bot@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "fallback")
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    assert E.load_email_config().password == "fallback"


def test_app_password_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("SMTP_APP_PASSWORD", "canonical")
    monkeypatch.setenv("SMTP_PASSWORD", "legacy")
    assert E.load_email_config().password == "canonical"


def test_port_587_uses_starttls_not_implicit_tls():
    assert cfg(port=587).use_ssl is False


def test_missing_recipients_means_not_configured():
    assert not cfg(recipients=()).configured


# --- sending ---------------------------------------------------------------------


class FakeSMTP:
    """Records the conversation instead of opening a socket."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent = message


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeSMTP.instances = []


def test_starttls_path_upgrades_then_authenticates(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    E.send_message(cfg(port=587), build())

    server = FakeSMTP.instances[0]
    assert server.started_tls is True
    assert server.logged_in == ("bot@example.com", "app-password")
    assert server.sent is not None


def test_ssl_path_does_not_call_starttls(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    E.send_message(cfg(port=465), build())

    server = FakeSMTP.instances[0]
    assert server.started_tls is False
    assert server.sent is not None


def test_auth_failure_explains_the_gmail_app_password_requirement(monkeypatch):
    class Rejecting(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(smtplib, "SMTP", Rejecting)
    with pytest.raises(E.DeliveryError, match="app password"):
        E.send_message(cfg(), build())


def test_network_failure_is_wrapped_not_leaked(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", boom)
    with pytest.raises(E.DeliveryError, match="failed"):
        E.send_message(cfg(), build())


# --- end to end ---------------------------------------------------------------------


def test_send_daily_brief_attaches_everything_and_builds_the_subject(tmp_path, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)
    deep = tmp_path / "NVDA.md"
    deep.write_text("# NVDA deep")

    result = E.send_daily_brief(
        RUN_DATE,
        brief,
        deep_paths=[deep],
        verdicts=[V("NVDA", "Buy")],
        degraded_sources=["Finnhub"],
        config=cfg(),
    )

    assert result.sent
    assert result.subject == "Daybreak 2026-08-14 — 1 verdicts, top: NVDA Buy · DEGRADED: Finnhub"
    assert result.attachments == ["daily-brief.md", "NVDA.md"]
    assert FakeSMTP.instances[0].sent["Subject"] == result.subject


def test_unconfigured_smtp_skips_instead_of_failing_the_run(tmp_path):
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    result = E.send_daily_brief(RUN_DATE, brief, config=cfg(recipients=()))
    assert not result.sent
    assert result.skipped == "not configured"


def test_a_degraded_run_is_still_delivered(tmp_path, monkeypatch):
    """The whole point of the DEGRADED banner: send it, do not withhold it."""
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    result = E.send_daily_brief(
        RUN_DATE,
        brief,
        verdicts=[],
        degraded_sources=["Finnhub", "yfinance", "Alpaca"],
        config=cfg(),
    )
    assert result.sent
    assert "DEGRADED: Finnhub, yfinance, Alpaca" in result.subject
    assert FakeSMTP.instances[0].sent is not None


def test_a_missing_brief_is_an_error_not_a_blank_email(tmp_path):
    with pytest.raises(E.DeliveryError, match="No brief"):
        E.send_daily_brief(RUN_DATE, tmp_path / "nope.md", config=cfg())


def test_the_evidence_block_rides_in_the_body_and_not_in_the_attachment(tmp_path, monkeypatch):
    """The attachment is the day's research and is archived as such.

    The evidence block is a rolling statement about every prior day, so folding
    it into the attached file would leave a brief on disk whose "Evidence so
    far" section is wrong the moment another decision resolves.
    """
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    result = E.send_daily_brief(
        RUN_DATE,
        brief,
        config=cfg(),
        evidence="## Evidence so far\n\n4 resolved observations. INSUFFICIENT DATA.",
    )

    assert result.sent
    sent = FakeSMTP.instances[-1].sent
    body = sent.get_body(preferencelist=("plain",)).get_content()
    assert "Evidence so far" in body
    attachment = next(part for part in sent.iter_attachments())
    assert "Evidence so far" not in attachment.get_content()
    assert brief.read_text() == BRIEF  # the file on disk is untouched


def test_no_evidence_leaves_the_body_exactly_as_the_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    E.send_daily_brief(RUN_DATE, brief, config=cfg(), evidence="   ")
    body = FakeSMTP.instances[-1].sent.get_body(preferencelist=("plain",)).get_content()
    assert body.strip() == BRIEF.strip()
