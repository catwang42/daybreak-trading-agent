"""Markdown reports rendered to PDF, because a phone renders a PDF.

The daily brief and each deep report used to ride along as ``.md``. On a desktop
that is fine. On a phone it is a file the mail client offers to open in a text
editor it does not have, so the research was effectively undelivered whenever it
was most likely to be read.

WeasyPrint needs pango and cairo, which are C libraries pip cannot install; the
Dockerfile brings them in. Everything here is therefore written so that their
absence costs the *format* and never the *delivery*: :func:`render_markdown`
returns ``None`` and the caller attaches the markdown it already had. That is
also why the import is inside the function — a ``--stage discovery`` run should
not pay for it, and a machine without the libraries should not fail at import.

The markdown is the same text that goes to GCS. Nothing is re-rendered from a
model, so the PDF and the ``.md`` cannot disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Print CSS. Separate from the email's inline styles on purpose: this one is a
#: document read on a screen at full width, not a card in a mail client, and it
#: is allowed to use a stylesheet because WeasyPrint is not Gmail.
STYLESHEET = """
@page { size: A4; margin: 18mm 16mm 20mm; @bottom-center {
  content: counter(page) " / " counter(pages);
  font: 8pt -apple-system, 'DejaVu Sans', sans-serif; color: #9ca3af; } }
body { font: 10.5pt/1.5 -apple-system, 'DejaVu Sans', Helvetica, sans-serif;
  color: #1f2937; }
h1 { font-size: 19pt; margin: 0 0 4pt; color: #111827; }
h2 { font-size: 13pt; margin: 16pt 0 5pt; color: #111827;
  border-bottom: 1px solid #e5e7eb; padding-bottom: 3pt; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; color: #374151; }
h4 { font-size: 10pt; margin: 10pt 0 3pt; color: #4b5563; }
p, li { margin: 0 0 6pt; }
ul, ol { margin: 0 0 6pt; padding-left: 16pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt;
  font-size: 9pt; }
/* Long tables split across pages and repeat their header rather than being
   pushed whole onto the next page, which left a third of every page blank. A
   single row still stays intact. */
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th { text-align: left; background: #f3f4f6; color: #374151; font-size: 8pt;
  text-transform: uppercase; letter-spacing: 0.04em; padding: 4pt 6pt;
  border-bottom: 1.5px solid #d1d5db; }
td { padding: 4pt 6pt; border-bottom: 1px solid #e5e7eb;
  vertical-align: top; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 9pt;
  background: #f3f4f6; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 4pt;
  padding: 6pt; font-size: 8.5pt; white-space: pre-wrap; }
blockquote { margin: 0 0 8pt; padding: 4pt 10pt; border-left: 3px solid #d1d5db;
  color: #4b5563; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 12pt 0; }
a { color: #1d4ed8; text-decoration: none; word-break: break-all; }
h1, h2, h3, h4 { page-break-after: avoid; }
"""


@dataclass(frozen=True)
class Rendered:
    """A PDF and the name it should be attached under."""

    filename: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def _weasyprint():
    """Import WeasyPrint, or ``None`` if its C libraries are missing.

    ``OSError`` is the specific failure when pango is absent, and it is raised
    at import rather than at call, which is why this is wrapped at all.
    """
    try:
        from weasyprint import CSS, HTML
    except (ImportError, OSError) as exc:  # pragma: no cover - environment shaped
        log.warning("WeasyPrint unavailable (%s); attaching markdown instead.", exc)
        return None
    return HTML, CSS


def markdown_body(text: str) -> str:
    """Markdown to an HTML fragment, with tables.

    Reuses the same ``markdown_it`` configuration the email body has always
    used, so a table that renders in one renders in the other.
    """
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable("table").enable("strikethrough").render(text)


def render_markdown(text: str, filename: str, *, title: str = "") -> Rendered | None:
    """One markdown document as a PDF, or ``None`` if it cannot be rendered.

    ``None`` is a normal return, not an error path: the caller falls back to the
    markdown, which is the same content in a less convenient wrapper.
    """
    tools = _weasyprint()
    if tools is None:
        return None
    HTML, CSS = tools
    head = f"<title>{title}</title>" if title else ""
    document = f"<!doctype html><html><head><meta charset='utf-8'>{head}</head><body>{markdown_body(text)}</body></html>"
    try:
        data = HTML(string=document).write_pdf(stylesheets=[CSS(string=STYLESHEET)])
    except Exception as exc:  # noqa: BLE001 - a malformed report is not a failed send
        log.warning("PDF for %s not rendered (%s); attaching markdown instead.", filename, exc)
        return None
    return Rendered(filename=filename, data=data)


def render_report(path: Path) -> Rendered | None:
    """Render a report file on disk, naming the PDF after it."""
    try:
        text = path.read_text()
    except OSError as exc:
        log.warning("Report %s unreadable (%s).", path, exc)
        return None
    return render_markdown(text, path.with_suffix(".pdf").name, title=path.stem)
