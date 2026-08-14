"""Report rendering per config/report-schema.md."""

from .render import DISCLAIMER, ReportContext, render_daily_brief
from .writer import write_report

__all__ = ["DISCLAIMER", "ReportContext", "render_daily_brief", "write_report"]
