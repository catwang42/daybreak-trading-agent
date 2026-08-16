"""Email delivery of the daily brief over SMTP.

The report is written to disk (and GCS) before this module runs, so delivery is
the last thing that happens and never gates the research. What it must not do is
fail *quietly*: a run that produced a brief nobody received is indistinguishable
from a run that never happened, so a send failure raises and the caller exits
non-zero, which is what turns a missing email into a visible Cloud Run failure.

Provider-agnostic in the same sense as the LLM layer: nothing here names Gmail.
``SMTP_HOST``/``SMTP_PORT``/``SMTP_USER``/``SMTP_PASSWORD`` describe any server;
Gmail happens to be the documented default and needs an app password rather than
an account password (see README).

Format. The body is the decision sheet — one screen of computed levels, gates
and verdicts, built by :mod:`tradingagent.presentation`. It is not the brief.
Inlining the brief put ~75 KB of styled markdown in the body, and Gmail clips
anything over ~102 KB behind a "view entire message" link, so the verdicts the
email exists to show sat below a fold that a phone never reached.

The research still ships, as PDF attachments: the same markdown that goes to
GCS, rendered by :mod:`tradingagent.presentation.pdf`. A ``.md`` attachment is
undeliverable on a phone in the practical sense — the mail client offers to open
it in a text editor that is not installed.

``text/plain`` carries the same sheet without markup, including the levels, so a
client that refuses HTML can still answer "what do I do today".
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from ..config import env, env_int

# Imported rather than redefined: the subject line ranks verdicts on the same
# 5-tier scale as the portfolio manager and the journal, and a second copy here
# would be one more place for that scale to drift.
from ..pipeline.schemas import RATING_ORDER
from ..presentation import pdf
from ..presentation.charts import Chart, render_charts
from ..presentation.html import render_sheet, render_text
from ..presentation.sheet import DecisionSheet
from ..storage import console_url

log = logging.getLogger(__name__)

#: Total attachment budget. Gmail refuses messages over 25 MB, so anything past
#: this is not "slightly large", it is a send that fails for everything. Over
#: the cap the research is linked in the bucket instead — the reader loses the
#: offline copy, not the report.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

#: Charts are inline and small; a runaway one should not eat the budget the
#: research needs. Four charts at ~40 KB each is the normal case.
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024


class DeliveryError(RuntimeError):
    """Delivery was configured but did not succeed."""


@dataclass(frozen=True)
class Verdict:
    """One deep-stage outcome, reduced to what a subject line needs."""

    symbol: str
    rating: str
    confidence: str = ""

    @property
    def ok(self) -> bool:
        return self.rating in RATING_ORDER


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender and self.recipients)

    @property
    def use_ssl(self) -> bool:
        """465 is implicit TLS; everything else gets STARTTLS after connect."""
        return self.port == 465


def load_email_config() -> EmailConfig:
    """Read SMTP settings from the environment.

    ``SMTP_FROM`` defaults to ``SMTP_USER`` because for Gmail they are the same
    address and asking for it twice invites them to disagree.

    The password is read from ``SMTP_APP_PASSWORD`` first and ``SMTP_PASSWORD``
    second. The longer name is canonical because Gmail does not accept an
    account password here at all — naming the variable after the thing that
    actually works is worth the extra characters.
    """
    user = env("SMTP_USER")
    recipients = tuple(
        addr.strip() for addr in env("REPORT_EMAIL_TO").replace(";", ",").split(",") if addr.strip()
    )
    return EmailConfig(
        host=env("SMTP_HOST", "smtp.gmail.com"),
        port=env_int("SMTP_PORT", 587),
        user=user,
        password=env("SMTP_APP_PASSWORD") or env("SMTP_PASSWORD"),
        sender=env("SMTP_FROM", user),
        recipients=recipients,
    )


# --- subject line ------------------------------------------------------------

#: How many degraded source names fit in a subject before it stops being
#: readable on a phone; the rest collapse into a count.
_SUBJECT_DEGRADED_LIMIT = 3


def pick_top(verdicts: list[Verdict]) -> Verdict | None:
    """The name the subject line leads with.

    Best rating wins; ties break on the order the deep stage produced, which is
    the sector-spanning queue order from ``deep_dive_queue`` and already encodes
    conviction. Degraded verdicts are never the headline unless they are all
    there is.
    """
    usable = [v for v in verdicts if v.ok]
    pool = usable or verdicts
    if not pool:
        return None
    ranked = sorted(
        enumerate(pool), key=lambda pair: (-RATING_ORDER.get(pair[1].rating, 0), pair[0])
    )
    return ranked[0][1]


def build_subject(
    run_date: date, verdicts: list[Verdict], degraded_sources: list[str] | None = None
) -> str:
    """``Daybreak <date> — <n> verdicts, top: <ticker> <rating>``.

    A degraded run still sends, so the degradation has to be visible without
    opening the mail — it goes in the subject, named, not just flagged.
    """
    scored = [v for v in verdicts if v.ok]
    top = pick_top(verdicts)
    headline = f"{top.symbol} {top.rating}" if top else "none"
    subject = f"Daybreak {run_date.isoformat()} — {len(scored)} verdicts, top: {headline}"

    sources = list(degraded_sources or [])
    if sources:
        shown = ", ".join(sources[:_SUBJECT_DEGRADED_LIMIT])
        extra = len(sources) - _SUBJECT_DEGRADED_LIMIT
        if extra > 0:
            shown += f" +{extra} more"
        subject += f" · DEGRADED: {shown}"
    return subject


# --- message assembly ---------------------------------------------------------


@dataclass
class Attachment:
    """One file on the message. PDF when it rendered, markdown when it did not."""

    filename: str
    data: bytes
    maintype: str = "text"
    subtype: str = "markdown"
    #: Where this file lives in the bucket, for the size-cap fallback.
    source: Path | None = None

    def __post_init__(self) -> None:
        if isinstance(self.data, str):
            self.data = self.data.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class DeliveryResult:
    subject: str
    recipients: tuple[str, ...] = ()
    attachments: list[str] = field(default_factory=list)
    skipped: str = ""
    dropped: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)

    @property
    def sent(self) -> bool:
        return not self.skipped


def _as_attachment(path: Path) -> Attachment:
    """One report as a PDF, or as its markdown if WeasyPrint could not run.

    The fallback is silent by design at this level and logged inside
    :mod:`~tradingagent.presentation.pdf`: losing the format is worth strictly
    less than losing the delivery, and the caller reports the filenames it
    actually sent either way.
    """
    rendered = pdf.render_report(path)
    if rendered is not None:
        return Attachment(rendered.filename, rendered.data, "application", "pdf", source=path)
    return Attachment(path.name, path.read_bytes(), "text", "markdown", source=path)


def collect_attachments(
    brief_path: Path, deep_paths: list[Path], bucket: str = ""
) -> tuple[list[Attachment], list[str], list[tuple[str, str]]]:
    """Render the brief and each deep report to PDF, under the size cap.

    Returns the attachments, the names of anything dropped, and links to the
    dropped files in the bucket. A reader who cannot get the file must at least
    be told where it is — an attachment that vanished with no explanation is
    indistinguishable from research that was never done.
    """
    out: list[Attachment] = []
    dropped: list[str] = []
    links: list[tuple[str, str]] = []
    total = 0
    for path in [brief_path, *deep_paths]:
        if path is None:
            continue
        path = Path(path)
        if not path.exists():
            dropped.append(f"{path.name} (missing)")
            continue
        item = _as_attachment(path)
        if total + item.size > MAX_ATTACHMENT_BYTES:
            dropped.append(f"{item.filename} (size cap)")
            if bucket:
                links.append((path.name, console_url(bucket, path)))
            continue
        total += item.size
        out.append(item)
    return out, dropped, links


def build_message(
    config: EmailConfig,
    subject: str,
    sheet: DecisionSheet,
    attachments: list[Attachment],
    charts: list[Chart] | None = None,
    links: list[tuple[str, str]] | None = None,
) -> EmailMessage:
    """``mixed( alternative( plain, related( html, png... ) ), files... )``.

    That nesting is what makes ``cid:`` references resolve: the images have to
    be siblings of the HTML inside a ``related`` part, not siblings of the
    ``alternative``, or clients show them as unnamed attachments instead of
    drawing them in place.
    """
    charts = list(charts or [])
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)

    message.set_content(render_text(sheet))
    message.add_alternative(
        render_sheet(sheet, charts, [a.filename for a in attachments], links or []),
        subtype="html",
    )

    html_part = message.get_payload()[-1]
    for chart in charts:
        html_part.add_related(
            chart.png,
            maintype="image",
            subtype="png",
            cid=f"<{chart.cid}>",
            filename=chart.filename,
            disposition="inline",
        )

    for item in attachments:
        message.add_attachment(
            item.data,
            maintype=item.maintype,
            subtype=item.subtype,
            filename=item.filename,
        )
    return message


def send_message(config: EmailConfig, message: EmailMessage) -> None:
    """Open the SMTP connection and send. Raises :class:`DeliveryError`."""
    try:
        if config.use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(config.host, config.port, timeout=60)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=60)
        with server:
            server.ehlo()
            if not config.use_ssl:
                server.starttls()
                server.ehlo()
            if config.user and config.password:
                server.login(config.user, config.password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # The overwhelmingly common cause, and the error Gmail gives for it is
        # not self-explanatory.
        raise DeliveryError(
            f"SMTP auth rejected by {config.host} for {config.user}. Gmail requires a "
            f"16-character app password (not the account password) and 2FA enabled: {exc}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"SMTP send to {config.host}:{config.port} failed: {exc}") from exc


def _drawn_charts(sheet: DecisionSheet) -> list[Chart]:
    """The sheet's charts, trimmed to the inline budget.

    Never raises. A picture is the most decorative thing in the message and the
    least worth failing a send over.
    """
    if sheet.context is None:
        return []
    try:
        charts = render_charts(sheet.context)
    except Exception as exc:  # noqa: BLE001 - the tables carry the same numbers
        log.warning("Charts not drawn (%s); sending the sheet without them.", exc)
        return []
    kept: list[Chart] = []
    total = 0
    for chart in charts:
        if total + chart.size > MAX_INLINE_IMAGE_BYTES:
            log.warning("Chart %s dropped: inline image budget reached.", chart.cid)
            continue
        total += chart.size
        kept.append(chart)
    return kept


def send_daily_brief(
    run_date: date,
    brief_path: Path,
    deep_paths: list[Path] | None = None,
    verdicts: list[Verdict] | None = None,
    degraded_sources: list[str] | None = None,
    config: EmailConfig | None = None,
    evidence: str = "",
    sheet: DecisionSheet | None = None,
    bucket: str = "",
) -> DeliveryResult:
    """Send the decision sheet. Raises only when a send was attempted and failed.

    Missing configuration is a skip, not an error: a local run with no SMTP
    settings should finish normally rather than fail at the last step.

    ``evidence`` is the weekly "Evidence so far" line. It goes in the *body* and
    deliberately not into the attached brief: the attachment is the day's
    research and is archived as such, while the evidence is a rolling statement
    about every day before it. Folding it in would leave a file on disk whose
    "Evidence so far" section is wrong the moment another decision resolves.

    ``sheet`` is normally built by the report stage. Without one the email still
    goes out, saying which sections it could not build — a session that predates
    the presentation context, or one whose run did not finish, should still
    deliver its research rather than nothing.
    """
    config = config or load_email_config()
    subject = build_subject(run_date, verdicts or [], degraded_sources)

    if not config.configured:
        log.info("Email delivery skipped: SMTP_HOST/SMTP_FROM/REPORT_EMAIL_TO not all set.")
        return DeliveryResult(subject=subject, skipped="not configured")

    brief_path = Path(brief_path)
    if not brief_path.exists():
        raise DeliveryError(f"No brief to send at {brief_path}")

    if sheet is None:
        sheet = DecisionSheet(
            run_date=run_date,
            unavailable=["no decision-sheet data for this session; the research is attached"],
        )
    if evidence.strip():
        sheet.evidence = evidence.strip()

    attachments, dropped, links = collect_attachments(
        brief_path, [Path(p) for p in (deep_paths or [])], bucket
    )
    charts = _drawn_charts(sheet)
    message = build_message(config, subject, sheet, attachments, charts, links)
    send_message(config, message)

    log.info(
        "Emailed %r to %s (%d attachments, %d charts)",
        subject,
        ", ".join(config.recipients),
        len(attachments),
        len(charts),
    )
    return DeliveryResult(
        subject=subject,
        recipients=config.recipients,
        attachments=[a.filename for a in attachments],
        dropped=dropped,
        links=links,
        charts=[c.cid for c in charts],
    )
