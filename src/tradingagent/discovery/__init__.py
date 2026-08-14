"""Discovery stage: breadth, sector rotation, screening, calendar, shortlist.

Logic mined from tradermonty/claude-trading-skills (MIT) — see PORTING_NOTES.md.
The cookbooks are read-only references; nothing here imports them.
"""

from .breadth import BreadthResult, analyze_breadth
from .calendar import CalendarView, build_calendar
from .screener import Candidate, screen_universe
from .sectors import SectorMap, build_sector_map
from .shortlist import ShortlistEntry, build_shortlist

__all__ = [
    "BreadthResult",
    "analyze_breadth",
    "CalendarView",
    "build_calendar",
    "Candidate",
    "screen_universe",
    "SectorMap",
    "build_sector_map",
    "ShortlistEntry",
    "build_shortlist",
]
