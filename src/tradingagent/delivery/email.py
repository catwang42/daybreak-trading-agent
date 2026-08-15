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

Format. The brief is markdown, so the email carries both: ``text/plain`` is the
markdown verbatim, ``text/html`` is that markdown converted with tables that
survive a phone screen. Clients that reject HTML still get a readable report.
The markdown files are attached as well, because the HTML is a convenience and
the file is the artefact.

Only the brief is inlined. The deep reports go in as attachments because Gmail
clips a message body over ~102 KB behind a "view entire message" link, and a
typical brief already renders to ~75 KB of styled HTML — inlining five deep
dives would reliably push the verdicts below the fold, which is the one thing
the email exists to show.
"""

from __future__ import annotations

import logging
import re
import smtplib
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from markdown_it import MarkdownIt

from ..config import env, env_int

# Imported rather than redefined: the subject line ranks verdicts on the same
# 5-tier scale as the portfolio manager and the journal, and a second copy here
# would be one more place for that scale to drift.
from ..pipeline.schemas import RATING_ORDER

log = logging.getLogger(__name__)

#: Gmail refuses messages over 25 MB; stay well clear and report what was cut
#: rather than having the whole send rejected for one oversized attachment.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


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


# --- markdown -> mobile-readable HTML ----------------------------------------

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])

# Gmail strips <style> and <head>, so every rule has to ride on the element.
# markdown-it's output tags are predictable, which is what makes this reliable.
_TAG_STYLES: dict[str, str] = {
    "table": "border-collapse:collapse;width:100%;margin:12px 0;font-size:14px",
    "th": "border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;"
    "text-align:left;font-weight:600",
    "td": "border:1px solid #d0d7de;padding:6px 10px",
    "h1": "font-size:22px;margin:18px 0 8px;line-height:1.3",
    "h2": "font-size:18px;margin:22px 0 8px;padding-bottom:4px;"
    "border-bottom:1px solid #d0d7de;line-height:1.3",
    "h3": "font-size:16px;margin:16px 0 6px;line-height:1.3",
    "p": "margin:10px 0",
    "ul": "margin:10px 0;padding-left:22px",
    "ol": "margin:10px 0;padding-left:22px",
    "li": "margin:4px 0",
    "blockquote": "margin:12px 0;padding:8px 12px;border-left:3px solid #d0d7de;"
    "background:#f6f8fa;color:#57606a",
    "code": "background:#f6f8fa;padding:1px 4px;border-radius:3px;font-size:13px",
    "pre": "background:#f6f8fa;padding:10px;border-radius:4px;overflow-x:auto;font-size:13px",
    "hr": "border:0;border-top:1px solid #d0d7de;margin:20px 0",
}

_BODY_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
    "font-size:15px;line-height:1.55;color:#1f2328;max-width:900px;margin:0 auto;padding:12px"
)


def _style_tags(html: str) -> str:
    """Inline the style rules above onto every generated tag.

    Merges with any style markdown-it already emitted — table cells carry
    ``text-align`` from the column alignment row, and dropping that would
    left-align every number in the brief.
    """

    def repl(match: re.Match[str]) -> str:
        tag, attrs = match.group(1), match.group(2)
        style = _TAG_STYLES[tag]
        existing = re.search(r'\sstyle="([^"]*)"', attrs)
        if existing:
            merged = f"{style};{existing.group(1)}"
            return f"<{tag}{attrs.replace(existing.group(0), '')} style=\"{merged}\">"
        return f"<{tag}{attrs} style=\"{style}\">"

    pattern = re.compile(rf"<({'|'.join(_TAG_STYLES)})((?:\s[^>]*)?)>")
    return pattern.sub(repl, html)


def markdown_to_html(markdown: str, title: str = "Daybreak") -> str:
    """Convert the brief to standalone HTML that reads on a phone.

    Tables are the hard part: the brief is mostly tables and a phone is ~360px
    wide, so each one is wrapped in a horizontally scrollable box instead of
    being squeezed until the numbers wrap.
    """
    body = _style_tags(_MD.render(markdown))
    body = body.replace("<table", '<div style="overflow-x:auto;-webkit-overflow-scrolling:touch"><table')
    body = body.replace("</table>", "</table></div>")
    return (
        "<!DOCTYPE html>"
        f'<html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title></head>"
        f'<body style="margin:0;padding:0;background:#ffffff">'
        f'<div style="{_BODY_STYLE}">{body}</div></body></html>'
    )


# --- message assembly ---------------------------------------------------------


@dataclass
class Attachment:
    filename: str
    content: str

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8"))


@dataclass
class DeliveryResult:
    subject: str
    recipients: tuple[str, ...] = ()
    attachments: list[str] = field(default_factory=list)
    skipped: str = ""
    dropped: list[str] = field(default_factory=list)

    @property
    def sent(self) -> bool:
        return not self.skipped


def collect_attachments(brief_path: Path, deep_paths: list[Path]) -> tuple[list[Attachment], list[str]]:
    """Read the brief and each deep report, newest-first, under the size cap.

    Returns the attachments plus the names of anything dropped, so the caller
    can say what was left out instead of the recipient wondering.
    """
    out: list[Attachment] = []
    dropped: list[str] = []
    total = 0
    for path in [brief_path, *deep_paths]:
        if path is None or not Path(path).exists():
            if path is not None:
                dropped.append(f"{Path(path).name} (missing)")
            continue
        item = Attachment(Path(path).name, Path(path).read_text(encoding="utf-8"))
        if total + item.size > MAX_ATTACHMENT_BYTES:
            dropped.append(f"{item.filename} (size cap)")
            continue
        total += item.size
        out.append(item)
    return out, dropped


def build_message(
    config: EmailConfig,
    subject: str,
    brief_markdown: str,
    attachments: list[Attachment],
) -> EmailMessage:
    """multipart/mixed → multipart/alternative(text, html) + markdown files."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)

    message.set_content(brief_markdown)
    message.add_alternative(markdown_to_html(brief_markdown, subject), subtype="html")

    for item in attachments:
        message.add_attachment(
            item.content.encode("utf-8"),
            maintype="text",
            subtype="markdown",
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


def send_daily_brief(
    run_date: date,
    brief_path: Path,
    deep_paths: list[Path] | None = None,
    verdicts: list[Verdict] | None = None,
    degraded_sources: list[str] | None = None,
    config: EmailConfig | None = None,
) -> DeliveryResult:
    """Send the brief. Returns a result; raises only when a send was attempted and failed.

    Missing configuration is a skip, not an error: a local run with no SMTP
    settings should finish normally rather than fail at the last step.
    """
    config = config or load_email_config()
    subject = build_subject(run_date, verdicts or [], degraded_sources)

    if not config.configured:
        log.info("Email delivery skipped: SMTP_HOST/SMTP_FROM/REPORT_EMAIL_TO not all set.")
        return DeliveryResult(subject=subject, skipped="not configured")

    brief_path = Path(brief_path)
    if not brief_path.exists():
        raise DeliveryError(f"No brief to send at {brief_path}")

    attachments, dropped = collect_attachments(brief_path, [Path(p) for p in (deep_paths or [])])
    message = build_message(config, subject, brief_path.read_text(encoding="utf-8"), attachments)
    send_message(config, message)

    log.info("Emailed %r to %s", subject, ", ".join(config.recipients))
    return DeliveryResult(
        subject=subject,
        recipients=config.recipients,
        attachments=[a.filename for a in attachments],
        dropped=dropped,
    )
