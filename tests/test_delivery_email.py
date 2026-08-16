"""Email delivery: subject construction, attachment rendering, message assembly."""

from __future__ import annotations

import smtplib
from datetime import date
from email import message_from_bytes

import pytest

from tradingagent.delivery import email as E
from tradingagent.presentation.charts import Chart
from tradingagent.presentation.sheet import DecisionSheet

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


# --- the sheet, the attachments, the message ----------------------------------

BRIEF = """# Daily Brief — 2026-08-14

> Research only.

## 1. Market Overview

| Index | Last | 1d |
|---|---:|---:|
| SPY | 777.88 | +0.70% |

- bullet one
- bullet two
"""


def sheet(**over):
    """A sheet with no presentation context — the degraded-but-deliverable case.

    Section content is exercised in test_decision_sheet.py; what matters here is
    the MIME envelope, which has to be right whether or not the sheet is full.
    """
    base = dict(run_date=RUN_DATE, unavailable=["no data file for this session"])
    base.update(over)
    return DecisionSheet(**base)


# --- attachments ---------------------------------------------------------------


def test_the_reports_go_out_as_pdfs_not_markdown(tmp_path):
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)
    deep = tmp_path / "NVDA.md"
    deep.write_text("# nvda")

    items, dropped, links = E.collect_attachments(brief, [deep])
    assert dropped == [] and links == []
    if items[0].subtype == "markdown":
        pytest.skip("WeasyPrint's system libraries are not installed here")
    assert [i.filename for i in items] == ["daily-brief.pdf", "NVDA.pdf"]
    assert all(i.data.startswith(b"%PDF") for i in items)


def test_a_report_that_will_not_render_still_ships_as_markdown(tmp_path, monkeypatch):
    """Losing the format is worth strictly less than losing the delivery."""
    monkeypatch.setattr(E.pdf, "render_report", lambda path: None)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    items, _, _ = E.collect_attachments(brief, [])
    assert [(i.filename, i.subtype) for i in items] == [("daily-brief.md", "markdown")]


def test_a_missing_deep_report_is_reported_not_silently_skipped(tmp_path):
    brief = tmp_path / "daily-brief.md"
    brief.write_text("# brief")

    items, dropped, _ = E.collect_attachments(brief, [tmp_path / "GONE.md"])
    assert len(items) == 1
    assert dropped == ["GONE.md (missing)"]


def test_over_the_size_cap_the_research_is_linked_rather_than_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "MAX_ATTACHMENT_BYTES", 32)
    monkeypatch.setattr(E.pdf, "render_report", lambda path: None)  # keep sizes predictable
    brief = tmp_path / "daily-brief.md"
    brief.write_text("x" * 16)
    big = tmp_path / "BIG.md"
    big.write_text("y" * 64)

    items, dropped, links = E.collect_attachments(brief, [big], bucket="gs://our-bucket")
    assert [i.filename for i in items] == ["daily-brief.md"]
    assert dropped == ["BIG.md (size cap)"]
    assert links and links[0][0] == "BIG.md" and "our-bucket" in links[0][1]


def test_without_a_bucket_a_dropped_file_is_still_named(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "MAX_ATTACHMENT_BYTES", 8)
    monkeypatch.setattr(E.pdf, "render_report", lambda path: None)
    brief = tmp_path / "daily-brief.md"
    brief.write_text("y" * 64)

    items, dropped, links = E.collect_attachments(brief, [])
    assert items == [] and dropped == ["daily-brief.md (size cap)"] and links == []


# --- message assembly -----------------------------------------------------------


def build(attachments=None, charts=None, links=None, s=None):
    return E.build_message(cfg(), "subject", s or sheet(), attachments or [], charts, links)


def test_message_carries_both_plain_text_and_html():
    types = {part.get_content_type() for part in build().walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_the_plain_text_part_is_the_sheet_not_the_brief():
    plain = next(p for p in build().walk() if p.get_content_type() == "text/plain")
    body = plain.get_content()
    assert "DAYBREAK — 2026-08-14" in body
    assert "| Index | Last |" not in body


def test_attachments_ride_along_under_their_own_type():
    message = build(attachments=[E.Attachment("NVDA.pdf", b"%PDF-1.7", "application", "pdf")])
    parts = [p for p in message.walk() if p.get_filename()]
    assert [p.get_filename() for p in parts] == ["NVDA.pdf"]
    assert parts[0].get_content_type() == "application/pdf"


def test_charts_are_inline_related_parts_so_the_body_can_draw_them():
    chart = Chart(cid="spy", filename="spy.png", png=b"\x89PNG-not-really", alt="SPY")
    message = build(charts=[chart])
    related = next(p for p in message.walk() if p.get_content_type() == "multipart/related")
    image = next(p for p in related.walk() if p.get_content_type() == "image/png")
    assert image["Content-ID"] == "<spy>"
    assert image.get_content_disposition() == "inline"
    # That the body actually references cid:spy is asserted where the body is
    # built, in test_decision_sheet.py; here the point is the MIME nesting,
    # which is what decides whether a client draws it or lists it as a file.


def test_the_html_lists_the_attachment_names_so_the_reader_knows_what_arrived():
    message = build(attachments=[E.Attachment("WMB.pdf", b"%PDF", "application", "pdf")])
    html = next(p for p in message.walk() if p.get_content_type() == "text/html").get_content()
    assert "WMB.pdf" in html


def test_size_capped_files_appear_in_the_body_as_links():
    message = build(links=[("BIG.md", "https://console.example/BIG.md")])
    html = next(p for p in message.walk() if p.get_content_type() == "text/html").get_content()
    assert "https://console.example/BIG.md" in html


def test_message_round_trips_through_the_wire_format():
    """Guards against a header or MIME-structure error that only shows on send."""
    chart = Chart(cid="spy", filename="spy.png", png=b"\x89PNG", alt="SPY")
    message = build(attachments=[E.Attachment("NVDA.pdf", b"%PDF", "application", "pdf")], charts=[chart])
    parsed = message_from_bytes(bytes(message))
    assert parsed["Subject"] == "subject"
    assert parsed["To"] == "human@example.com"
    assert parsed.is_multipart()


def test_multiple_recipients_are_comma_joined():
    message = E.build_message(
        cfg(recipients=("a@example.com", "b@example.com")), "s", sheet(), []
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
    assert [name.rsplit(".", 1)[0] for name in result.attachments] == ["daily-brief", "NVDA"]
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
    for subtype in ("plain", "html"):
        assert "Evidence so far" in sent.get_body(preferencelist=(subtype,)).get_content()
    assert brief.read_text() == BRIEF  # the file on disk is untouched


def test_the_evidence_survives_a_session_with_no_decision_sheet_data(tmp_path, monkeypatch):
    """It is a statement about every prior day, not about this one.

    Dropping it when today's context is missing would lose Friday's record on
    exactly the runs that went least well.
    """
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    E.send_daily_brief(RUN_DATE, brief, config=cfg(), evidence="7 of 12 correct.")
    body = FakeSMTP.instances[-1].sent.get_body(preferencelist=("plain",)).get_content()
    assert "7 of 12 correct." in body


def test_no_evidence_means_no_evidence_section(tmp_path, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    brief = tmp_path / "daily-brief.md"
    brief.write_text(BRIEF)

    E.send_daily_brief(RUN_DATE, brief, config=cfg(), evidence="   ")
    sent = FakeSMTP.instances[-1].sent
    for subtype in ("plain", "html"):
        assert "vidence so far" not in sent.get_body(preferencelist=(subtype,)).get_content()
