"""Write results into the StepOne workbook.

* **Master** (maintained by StepOne) is append-only: existing rows and cells
  are never changed. New events are added under a dated band after the last
  row, and a hidden index remembers every event ever added, so an event is
  never added twice - not even after its row was deleted by hand.
* **Rolling Calendar** is rebuilt from scratch on every run for the chosen
  window, in the Master's visual style, city by city, each city in date order.
* A hidden append-only **Run Log**, and optional per-city tabs and a
  **Summary** sheet with live formulas (off by default: the client works
  from the Master and the Rolling Calendar only).

The file is backed up before saving and saved atomically. If Excel has it
open (locked), results go to a new file next to it instead.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .cities import CITIES, detect_city, is_city_name
from .classify import ACTIVITY_TYPES, TIER_UNKNOWN, TIERS
from .models import Event
from .normalize import format_price_range, title_tokens, titles_match

# ----------------------------------------------------------------- styling
# Colours are taken from the reference workbook (Guide + Master Calander).
FONT = "Arial"
INK = "3A3A3A"
CREAM = "F9F6F0"
TITLE_FILL = "1A1A1A"
SUBTITLE_FILL = "FDF3DC"
SUBTITLE_INK = "7A7A7A"
HEADER_FILL = "3A3A3A"
RUN_BAND_FILL = "0E4D45"
LINK_INK = "0066CC"
GRID = "D9D9D9"

CATEGORY_FILLS = {
    "Culture": "F3E6F9", "Art": "E8F0FA", "Music": "FDEAEA", "F&B": "E6F7EC",
    "Sports to Watch": "E6F3E6", "Recreational Sports": "F7EFDF", "Theatre": "EDE6FA",
    "Comedy": "FAF0CC", "Workshops": "EAF1F4", "Other": "F2F2F2",
}
TIER_STYLES = {  # fill, font colour
    "Ultra Premium": ("FFE0E0", "8B0000"), "Luxury": ("FFF4CC", "5C4A00"),
    "Premium": ("E8F5E9", "1A5C2E"), "Standard": ("EFEFEF", "4A4A4A"), TIER_UNKNOWN: ("FFFFFF", "9A9A9A"),
}
REGION_STYLES = {"India": ("E8F0FA", "1A3A5C"), "Intl": ("FFE8CC", "7D4000")}
CITY_TAB_COLORS = ["C0392B", "2471A3", "7D3C98", "138D75", "B9770E", "A04000", "2E4053"]
CITY_BAND_FILLS = ["7D1A1A", "1B4F72", "4A235A", "145A32", "784212", "6E2C00", "2C3E50", "1A1A1A"]

DATE_FORMAT = "d mmm yyyy"

# (header, width, horizontal alignment, wraps)
COLUMNS: List[Tuple[str, float, str, bool]] = [
    ("S.No.", 6, "center", False),
    ("Event Name", 44, "left", True),
    ("Activity Type", 15, "center", False),
    ("Tier", 13, "center", False),
    ("Start Date", 12, "center", False),
    ("End Date", 12, "center", False),
    ("City / Cities", 12, "left", False),
    ("Venue", 34, "left", True),
    ("Organizer", 26, "left", True),
    ("Price Range", 17, "center", False),
    ("Ticket Platform(s)", 20, "left", True),
    ("Notes", 58, "left", True),
    ("Region", 8, "center", False),
    ("Link", 38, "left", False),
    ("Added On", 11, "center", False),
]
HEADERS = [c[0] for c in COLUMNS]
N_COLS = len(COLUMNS)
LAST_COL = get_column_letter(N_COLS)

INDEX_SHEET = "_scraper_index"
INDEX_HEADERS = ["Key", "Match Key", "Event Name", "City", "Start Date", "Link", "Platform", "Added On"]
META_SHEET = "_scraper_meta"
RUN_LOG_SHEET = "Run Log"
SUMMARY_SHEET = "Summary"


def _side(color: Optional[str] = None) -> Side:
    return Side(style="thin", color=color) if color else Side(style="thin")


def _border(color: Optional[str] = None) -> Border:
    s = _side(color)
    return Border(left=s, right=s, top=s, bottom=s)


def _font(size: float = 9, bold: bool = False, color: str = INK, underline: Optional[str] = None,
          italic: bool = False) -> Font:
    return Font(name=FONT, size=size, bold=bold, color=color, underline=underline, italic=italic)


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", start_color=color, end_color=color)


# ------------------------------------------------------------ row content

def event_row(ev: Event, serial: int, added_on: Optional[date]) -> List[object]:
    price = format_price_range(ev.price_min, ev.price_max, ev.is_free) or "TBC"
    venue = ev.venue or ("Online" if ev.online else "TBC")
    if ev.address and ev.venue and ev.city and ev.city.lower() not in venue.lower():
        locality = _locality(ev.address, ev.venue)
        venue = f"{venue}, {locality}" if locality else venue
    return [
        serial,
        ev.title,
        ev.activity_type or "Other",
        ev.tier or TIER_UNKNOWN,
        ev.start.date() if ev.start else (ev.date_text or "TBC"),
        (ev.end or ev.start).date() if ev.start else (ev.date_text or "TBC"),
        ev.city,
        venue,
        ev.organizer,
        price,
        " · ".join(ev.platforms or [ev.platform]),
        ev.notes,
        "India",
        ev.buy_link,
        added_on,
    ]


_STATES = {"india", "maharashtra", "karnataka", "tamil nadu", "west bengal", "gujarat", "delhi", "haryana",
           "uttar pradesh", "telangana", "mh", "ka", "tn", "wb", "gj", "dl", "hr", "up"}


def _locality(address: str, venue: str) -> str:
    """The neighbourhood from an address ("Worli" from "Dr A B Road, Worli, Mumbai, 400018")."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    city_idx = next((i for i, p in enumerate(parts) if is_city_name(p)), None)
    candidates = parts[:city_idx] if city_idx is not None else parts
    venue_low = venue.lower()
    for p in reversed(candidates):
        low = p.lower()
        if low in venue_low or re.search(r"\d{4,}", p) or len(p) > 30 or low in _STATES:
            continue
        return p
    return ""


def _row_height(values: Sequence[object]) -> float:
    lines = 1
    for (header, width, _align, wraps), value in zip(COLUMNS, values):
        if not wraps or value is None:
            continue
        chars_per_line = max(8, int(width * 1.18))
        need = math.ceil(len(str(value)) / chars_per_line)
        lines = max(lines, min(need, 3))
    return {1: 15.0, 2: 26.0, 3: 37.5}[lines]


def style_event_row(ws: Worksheet, row: int, values: Sequence[object], ev_activity: str, ev_tier: str,
                    border: Border, set_height: bool = True) -> None:
    fill = _fill(CATEGORY_FILLS.get(ev_activity, CATEGORY_FILLS["Other"]))
    for idx, ((header, width, align, wraps), value) in enumerate(zip(COLUMNS, values), start=1):
        cell = ws.cell(row=row, column=idx, value=value if value != "" else None)
        cell.font = _font()
        cell.fill = fill
        cell.border = border
        cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wraps)
        if header in ("Start Date", "End Date", "Added On") and isinstance(value, (date, datetime)):
            cell.number_format = DATE_FORMAT
        elif header == "Tier":
            bg, fg = TIER_STYLES.get(ev_tier, TIER_STYLES[TIER_UNKNOWN])
            cell.fill = _fill(bg)
            cell.font = _font(bold=True, color=fg, italic=ev_tier == TIER_UNKNOWN)
        elif header == "Region":
            bg, fg = REGION_STYLES.get(str(value), REGION_STYLES["India"])
            cell.fill = _fill(bg)
            cell.font = _font(size=8, bold=True, color=fg)
        elif header == "Link" and value:
            cell.hyperlink = str(value)
            cell.font = _font(color=LINK_INK, underline="single")
        elif header == "Organizer" and isinstance(value, str) and value.startswith("Source:"):
            cell.font = _font(color="6B6B6B", italic=True)
    if set_height:
        ws.row_dimensions[row].height = _row_height(values)


def band_row(ws: Worksheet, row: int, text: str, fill: str, size: float = 11, last_col: int = N_COLS) -> None:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = _font(size=size, bold=True, color=CREAM)
    cell.fill = _fill(fill)
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 20


def header_row(ws: Worksheet, row: int) -> None:
    for idx, (header, width, _a, _w) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=row, column=idx, value=header)
        cell.font = _font(size=10, bold=True, color=CREAM)
        cell.fill = _fill(HEADER_FILL)
        cell.border = _border()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[row].height = 22


# ------------------------------------------------------------- workbook io

@dataclass
class WriteResult:
    saved_to: Path
    new_in_master: int = 0
    warnings: List[str] = field(default_factory=list)


def _norm_header(value: object) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def find_master(wb: Workbook, name: str) -> Optional[Worksheet]:
    if name in wb.sheetnames:
        return wb[name]
    wanted = name.strip().lower()
    for ws in wb.worksheets:
        if ws.title.strip().lower() == wanted:
            return ws
    for ws in wb.worksheets:
        if "master" in ws.title.lower():
            return ws
    return None


def find_header(ws: Worksheet) -> Tuple[int, Dict[str, int]]:
    """Header row number and {normalised header: column} of a calendar sheet."""
    for row in range(1, min(ws.max_row, 15) + 1):
        cols = {}
        for col in range(1, min(ws.max_column, 60) + 1):
            key = _norm_header(ws.cell(row=row, column=col).value)
            if key:
                cols.setdefault(key, col)
        if "eventname" in cols:
            return row, cols
    return 0, {}


def last_used_row(ws: Worksheet, min_row: int) -> int:
    for row in range(ws.max_row, min_row - 1, -1):
        for col in range(1, min(ws.max_column, 40) + 1):
            v = ws.cell(row=row, column=col).value
            if v is not None and str(v).strip() != "":
                return row
    return min_row


def _generated_sheets(wb: Workbook) -> List[str]:
    if META_SHEET not in wb.sheetnames:
        return []
    ws = wb[META_SHEET]
    return [str(ws.cell(row=r, column=1).value) for r in range(2, ws.max_row + 1) if ws.cell(row=r, column=1).value]


def _remember_generated(wb: Workbook, names: Iterable[str]) -> None:
    if META_SHEET in wb.sheetnames:
        ws = wb[META_SHEET]
        ws.delete_rows(1, ws.max_row)
    else:
        ws = wb.create_sheet(META_SHEET)
        ws.sheet_state = "hidden"
    ws.cell(row=1, column=1, value="Sheets generated by S1 Event Scraper (safe to replace on each run)")
    for i, name in enumerate(sorted(set(names)), start=2):
        ws.cell(row=i, column=1, value=name)


def _replace_sheet(wb: Workbook, title: str, generated: List[str], after: Optional[str] = None) -> Worksheet:
    """Recreate a generated sheet in place (same tab position). Never deletes a user's own sheet."""
    index = None
    if title in wb.sheetnames:
        if title not in generated:
            raise ValueError(title)
        index = wb.sheetnames.index(title)
        del wb[title]
    if index is None and after and after in wb.sheetnames:
        index = wb.sheetnames.index(after) + 1
    return wb.create_sheet(title, index)


def _safe_title(wb: Workbook, title: str, generated: List[str]) -> str:
    if title not in wb.sheetnames or title in generated:
        return title
    return f"{title} (Rolling)"


# ----------------------------------------------------------------- master

def _match_key(title: str, city: str, start: object) -> str:
    d = start.date() if isinstance(start, datetime) else start
    return f"{' '.join(sorted(title_tokens(title)))}|{city.lower()}|{d.isoformat() if isinstance(d, date) else ''}"


class MasterIndex:
    """Everything already in the Master: scraper keys, links and the visible rows."""

    def __init__(self, wb: Workbook, master: Worksheet, header_row_no: int, cols: Dict[str, int]):
        self.keys: set = set()
        self.match_keys: set = set()
        self.links: set = set()
        self.first_added: Dict[str, date] = {}
        self.rows_by_city_date: Dict[Tuple[str, date], List[str]] = {}
        if INDEX_SHEET in wb.sheetnames:
            ws = wb[INDEX_SHEET]
            for r in range(2, ws.max_row + 1):
                key, mkey, link, added = (ws.cell(row=r, column=c).value for c in (1, 2, 6, 8))
                if key:
                    self.keys.add(str(key))
                    if isinstance(added, datetime):
                        self.first_added[str(key)] = added.date()
                    elif isinstance(added, date):
                        self.first_added[str(key)] = added
                if mkey:
                    self.match_keys.add(str(mkey))
                if link:
                    self.links.add(str(link))
        c_title, c_city, c_start = cols.get("eventname"), cols.get("citycities") or cols.get("city"), cols.get("startdate")
        c_link = cols.get("link")
        for r in range(header_row_no + 1, master.max_row + 1):
            title = master.cell(row=r, column=c_title).value if c_title else None
            if not title:
                continue
            if c_link:
                link = master.cell(row=r, column=c_link).value
                if link:
                    self.links.add(str(link))
            start = master.cell(row=r, column=c_start).value if c_start else None
            if isinstance(start, datetime):
                start = start.date()
            if not isinstance(start, date):
                continue
            cell_city = str(master.cell(row=r, column=c_city).value or "") if c_city else ""
            city = detect_city(cell_city)
            for name in ([city.name] if city else [c.name for c in CITIES if c.name.lower() in cell_city.lower()]):
                self.rows_by_city_date.setdefault((name, start), []).append(str(title))

    def contains(self, ev: Event) -> bool:
        if ev.identity() in self.keys:
            return True
        if ev.buy_link in self.links or (ev.url and ev.url in self.links):
            return True
        if ev.start and _match_key(ev.title, ev.city, ev.start) in self.match_keys:
            return True
        if ev.start:
            for title in self.rows_by_city_date.get((ev.city, ev.start.date()), []):
                if titles_match(title, ev.title):
                    return True
        return False

    def added_on(self, ev: Event) -> Optional[date]:
        return self.first_added.get(ev.identity())


def _ensure_master_columns(master: Worksheet, header_row_no: int, cols: Dict[str, int]) -> Dict[str, int]:
    """Add the Link / Added On header cells if the Master predates them (no other cell changes)."""
    last = max(cols.values()) if cols else 0
    ref = master.cell(row=header_row_no, column=cols.get("eventname", 1))
    for header in ("Link", "Added On"):
        key = _norm_header(header)
        if key in cols:
            continue
        last += 1
        cell = master.cell(row=header_row_no, column=last, value=header)
        cell.font = Font(name=ref.font.name or FONT, size=ref.font.sz, bold=True,
                         color=ref.font.color.rgb if ref.font.color is not None and isinstance(ref.font.color.rgb, str) else CREAM)
        cell.fill = _fill(ref.fill.fgColor.rgb[-6:]) if ref.fill is not None and ref.fill.fill_type and isinstance(ref.fill.fgColor.rgb, str) else _fill(HEADER_FILL)
        cell.border = _border()
        cell.alignment = Alignment(horizontal="center")
        cols[key] = last
        letter = get_column_letter(last)
        if letter not in master.column_dimensions:
            master.column_dimensions[letter].width = {"Link": 38, "Added On": 11}[header]
    return cols


def append_to_master(wb: Workbook, master: Worksheet, events: Sequence[Event], run_at: datetime,
                     window: Tuple[date, date]) -> Tuple[int, Dict[str, date]]:
    """Append events not yet in the Master. Returns (count, {identity: added-on date})."""
    header_row_no, cols = find_header(master)
    if not header_row_no:
        raise ValueError(f"Could not find the header row (with 'Event Name') in sheet '{master.title}'.")
    cols = _ensure_master_columns(master, header_row_no, cols)
    index = MasterIndex(wb, master, header_row_no, cols)

    added_on: Dict[str, date] = {}
    new_events: List[Event] = []
    for ev in events:
        known = index.added_on(ev)
        if index.contains(ev):
            if known:
                added_on[ev.identity()] = known
            continue
        new_events.append(ev)
        index.keys.add(ev.identity())
        added_on[ev.identity()] = run_at.date()
    if not new_events:
        return 0, added_on

    # Column positions follow the Master's own headers, whatever their order.
    positions: List[Optional[int]] = []
    aliases = {"citycities": ("citycities", "city"), "ticketplatforms": ("ticketplatforms", "ticketplatform", "platform")}
    for header in HEADERS:
        key = _norm_header(header)
        col = next((cols[k] for k in aliases.get(key, (key,)) if k in cols), None)
        positions.append(col)
    last_col = max(c for c in positions if c)

    serials = []
    c_serial = cols.get("sno")
    if c_serial:
        for r in range(header_row_no + 1, master.max_row + 1):
            v = master.cell(row=r, column=c_serial).value
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                serials.append(int(v))
    serial = max(serials, default=0)

    row = last_used_row(master, header_row_no) + 1
    band = (f"▌ SCRAPER UPDATE · {run_at:%d %b %Y}".upper() + f" · {len(new_events)} NEW EVENTS · WINDOW "
            f"{window[0]:%d %b} – {window[1]:%d %b %Y}".upper())
    band_row(master, row, band, RUN_BAND_FILL, size=10, last_col=last_col)
    # Same look as the rows StepOne types in: Arial 9, category fill, thin borders,
    # centred S.No./type/tier/price/region, tier and region badges.
    centred = {"S.No.", "Activity Type", "Tier", "Price Range", "Region", "Added On"}
    border = _border()
    current_city = None
    per_city = Counter(ev.city for ev in new_events)
    for ev in sorted(new_events, key=lambda e: (_city_rank(e.city), e.start or datetime.max, e.title.lower())):
        if ev.city != current_city:
            row += 1
            current_city = ev.city
            count = per_city[ev.city]
            band_row(master, row, f"{ev.city or 'Other'} · {count} event{'s' if count != 1 else ''}".upper(),
                     CITY_BAND_FILLS[_city_rank(ev.city) % len(CITY_BAND_FILLS)], size=9, last_col=last_col)
        row += 1
        serial += 1
        values = event_row(ev, serial, run_at.date())
        fill = _fill(CATEGORY_FILLS.get(ev.activity_type, CATEGORY_FILLS["Other"]))
        for (header, _w, _align, _wraps), value, col in zip(COLUMNS, values, positions):
            if not col:
                continue
            cell = master.cell(row=row, column=col, value=value if value != "" else None)
            cell.font = _font()
            cell.fill = fill
            cell.border = border
            cell.alignment = Alignment(horizontal="center" if header in centred else None)
            if header in ("Start Date", "End Date", "Added On") and isinstance(value, (date, datetime)):
                cell.number_format = DATE_FORMAT
            elif header == "Tier":
                bg, fg = TIER_STYLES.get(ev.tier, TIER_STYLES[TIER_UNKNOWN])
                cell.fill, cell.font = _fill(bg), _font(bold=True, color=fg)
            elif header == "Region":
                bg, fg = REGION_STYLES["India"]
                cell.fill, cell.font = _fill(bg), _font(size=8, bold=True, color=fg)
            elif header == "Link" and value:
                cell.hyperlink = str(value)
                cell.font = _font(color=LINK_INK, underline="single")
            elif header == "Organizer" and isinstance(value, str) and value.startswith("Source:"):
                cell.font = _font(color="6B6B6B", italic=True)

    _append_index(wb, new_events, run_at)
    if master.freeze_panes is None:
        master.freeze_panes = f"C{header_row_no + 1}"
    return len(new_events), added_on


def _append_index(wb: Workbook, events: Sequence[Event], run_at: datetime) -> None:
    if INDEX_SHEET in wb.sheetnames:
        ws = wb[INDEX_SHEET]
    else:
        ws = wb.create_sheet(INDEX_SHEET)
        ws.sheet_state = "hidden"
        ws.append(INDEX_HEADERS)
    for ev in events:
        ws.append([ev.identity(), _match_key(ev.title, ev.city, ev.start) if ev.start else "", ev.title, ev.city,
                   ev.start.date() if ev.start else None, ev.buy_link, ev.platform, run_at.date()])


# ---------------------------------------------------------------- rolling

def write_calendar_sheet(ws: Worksheet, title: str, subtitle: str, events: Sequence[Event],
                         added_on: Dict[str, date], window: Tuple[date, date], tab_color: Optional[str] = None,
                         empty_message: str = "No events found for this window. The app's log says why.") -> int:
    """Lay out a calendar sheet (title, subtitle, header, city bands, rows). Returns last row."""
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 100
    if tab_color:
        ws.sheet_properties.tabColor = tab_color
    band_row(ws, 1, title, TITLE_FILL, size=13)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N_COLS)
    sub = ws.cell(row=2, column=1, value=subtitle)
    sub.font = _font(size=8, color=SUBTITLE_INK)
    sub.fill = _fill(SUBTITLE_FILL)
    sub.alignment = Alignment(horizontal="left", vertical="center", indent=1, wrap_text=True)
    ws.row_dimensions[2].height = 26
    header_row(ws, 3)

    row = 3
    border = _border(GRID)
    current = None
    serial = 0

    def effective(ev: Event) -> date:
        return max(ev.start.date(), window[0]) if ev.start else window[1]

    # City by city (Mumbai, Pune, ... as in the app), each in date order.
    events = sorted(events, key=lambda e: (_city_rank(e.city), effective(e), e.start or datetime.max,
                                           e.title.lower()))
    per_city = Counter(ev.city for ev in events)
    for ev in events:
        if ev.city != current:
            row += 1
            count = per_city[ev.city]
            band_row(ws, row, f"▌ {ev.city or 'Other'} · {count} event{'s' if count != 1 else ''}".upper(),
                     CITY_BAND_FILLS[_city_rank(ev.city) % len(CITY_BAND_FILLS)])
            current = ev.city
        row += 1
        serial += 1
        values = event_row(ev, serial, added_on.get(ev.identity()))
        style_event_row(ws, row, values, ev.activity_type, ev.tier, border)
    if not events:
        row += 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=N_COLS)
        cell = ws.cell(row=row, column=1, value=empty_message)
        cell.font = _font(size=10, italic=True, color=SUBTITLE_INK)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[row].height = 30

    ws.freeze_panes = "C4"
    ws.auto_filter.ref = f"A3:{LAST_COL}{max(row, 4)}"
    ws.print_title_rows = "1:3"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.3
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    return row


def _city_rank(city: str) -> int:
    return next((i for i, c in enumerate(CITIES) if c.name == city), len(CITIES))


def _source_line(events: Sequence[Event], sources: Sequence[str]) -> str:
    counts = Counter(p for ev in events for p in (ev.platforms or [ev.platform]))
    if counts:
        return " · ".join(f"{p} ({n})" for p, n in counts.most_common())
    return " · ".join(sources)


def write_summary(wb: Workbook, ws: Worksheet, rolling_title: str, last_row: int, events: Sequence[Event],
                  report, settings) -> None:
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = "0E4D45"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    widths = [24] + [14] * 12
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ref = f"'{rolling_title}'"
    rng = lambda col: f"{ref}!${col}$4:${col}${max(last_row, 4)}"  # noqa: E731
    cities = [c for c in report.cities]

    def title(row: int, text: str, span: int) -> None:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
        c = ws.cell(row=row, column=1, value=text)
        c.font = _font(size=12, bold=True, color=CREAM)
        c.fill = _fill(TITLE_FILL)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[row].height = 24

    def head(row: int, labels: Sequence[str]) -> None:
        for i, label in enumerate(labels, start=1):
            c = ws.cell(row=row, column=i, value=label)
            c.font = _font(size=9, bold=True, color=CREAM)
            c.fill = _fill(HEADER_FILL)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = _border()
        ws.row_dimensions[row].height = 28

    def matrix(start: int, labels: Sequence[str], col_letter: str, fills: Optional[Dict[str, str]] = None) -> int:
        head(start, ["City"] + list(labels) + ["Total"])
        r = start
        for city in cities:
            r += 1
            ws.cell(row=r, column=1, value=city).font = _font(bold=True)
            for j, label in enumerate(labels, start=2):
                c = ws.cell(row=r, column=j, value=f'=COUNTIFS({rng("G")},$A{r},{rng(col_letter)},{get_column_letter(j)}${start})')
                c.font, c.alignment = _font(), Alignment(horizontal="center")
                if fills and label in fills:
                    c.fill = _fill(fills[label])
            total_col = len(labels) + 2
            c = ws.cell(row=r, column=total_col, value=f"=SUM(B{r}:{get_column_letter(total_col - 1)}{r})")
            c.font, c.alignment = _font(bold=True), Alignment(horizontal="center")
        r += 1
        ws.cell(row=r, column=1, value="Total").font = _font(bold=True)
        for j in range(2, len(labels) + 3):
            letter = get_column_letter(j)
            c = ws.cell(row=r, column=j, value=f"=SUM({letter}{start + 1}:{letter}{r - 1})")
            c.font, c.alignment = _font(bold=True), Alignment(horizontal="center")
            c.fill = _fill("F2F2F2")
        for rr in range(start + 1, r + 1):
            for j in range(1, len(labels) + 3):
                ws.cell(row=rr, column=j).border = _border(GRID)
        return r

    span = len(ACTIVITY_TYPES) + 2
    title(1, f"SUMMARY · {report.window_start:%d %b %Y} – {report.window_end:%d %b %Y}".upper(), span)
    info = ws.cell(row=2, column=1, value=(
        f"Counts are live formulas over the '{rolling_title}' sheet. "
        f"Last run: {report.started:%d %b %Y, %I:%M %p} IST · {len(events)} events · "
        f"{report.new_in_master} new rows added to the Master."))
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
    info.font = _font(size=8, color=SUBTITLE_INK)
    info.fill = _fill(SUBTITLE_FILL)
    info.alignment = Alignment(wrap_text=True, vertical="center", indent=1)
    ws.row_dimensions[2].height = 26

    tier_labels = TIERS + [TIER_UNKNOWN]
    ws.cell(row=4, column=1, value="Events by city and tier").font = _font(size=10, bold=True)
    end = matrix(5, tier_labels, "D", {t: TIER_STYLES[t][0] for t in tier_labels})
    start = end + 3
    ws.cell(row=start - 1, column=1, value="Events by city and activity type").font = _font(size=10, bold=True)
    end = matrix(start, ACTIVITY_TYPES, "C", CATEGORY_FILLS)

    r = end + 3
    ws.cell(row=r - 1, column=1, value="Events by ticket platform").font = _font(size=10, bold=True)
    head(r, ["Platform", "Events"])
    platforms = Counter(p for ev in events for p in (ev.platforms or [ev.platform]))
    for name, _n in platforms.most_common():
        r += 1
        ws.cell(row=r, column=1, value=name).font = _font()
        c = ws.cell(row=r, column=2, value=f'=COUNTIF({rng("K")},"*{name}*")')
        c.font, c.alignment = _font(), Alignment(horizontal="center")
        for j in (1, 2):
            ws.cell(row=r, column=j).border = _border(GRID)

    r += 3
    ws.cell(row=r, column=1, value="How this calendar is built").font = _font(size=10, bold=True)
    notes = [
        "Tier: from the lowest (entry) ticket price, using the Guide's bands - Ultra Premium ₹5,000+, "
        "Luxury ₹1,550–5,000, Premium ₹500–1,549, Standard below ₹500 or free. TBC = price not published."
        + ("" if settings.tier_basis == "min" else f" (Currently set to use the {settings.tier_basis} price.)"),
        "Activity Type: from each platform's own category, then the event title and description.",
        "Organizer: as published on the event page; when none is published the cell names the source "
        "platform (shown in grey italics, e.g. 'Source: BookMyShow').",
        "Link: the event's ticket page. Duplicates listed on several platforms are merged into one row; all "
        "platforms are listed in Ticket Platform(s).",
        "Master Calander is never edited - new events are only appended below the last row. The Rolling "
        "Calendar and city tabs are rebuilt on every run.",
    ]
    for line in notes:
        r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=span)
        c = ws.cell(row=r, column=1, value="• " + line)
        c.font = _font(size=9)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 26


RUN_LOG_HEADERS = ["Run At", "Window", "Cities", "Events in Window", "New in Master", "By Source", "Warnings"]


def append_run_log(wb: Workbook, report, total_events: int, new_rows: int) -> None:
    if RUN_LOG_SHEET in wb.sheetnames:
        ws = wb[RUN_LOG_SHEET]
    else:
        ws = wb.create_sheet(RUN_LOG_SHEET)
        ws.sheet_properties.tabColor = "7F8C8D"
        for i, (h, w) in enumerate(zip(RUN_LOG_HEADERS, [18, 26, 40, 10, 10, 70, 60]), start=1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = _font(size=9, bold=True, color=CREAM)
            c.fill = _fill(HEADER_FILL)
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
    by_source = "; ".join(f"{name}: {st.get('kept', 0)} kept / {st.get('discovered', 0)} found"
                          for name, st in report.per_source.items())
    row = [report.started.replace(microsecond=0),
           f"{report.window_start:%d %b %Y} – {report.window_end:%d %b %Y}",
           ", ".join(report.cities), total_events, new_rows, by_source, " | ".join(report.warnings)[:1000]]
    ws.append(row)
    r = ws.max_row
    for i in range(1, len(row) + 1):
        c = ws.cell(row=r, column=i)
        c.font = _font(size=9)
        c.alignment = Alignment(vertical="top", wrap_text=i in (3, 6, 7))
        c.border = _border(GRID)
    ws.cell(row=r, column=1).number_format = "d mmm yyyy h:mm AM/PM"


# --------------------------------------------------------- new workbooks

GUIDE_CATEGORIES = [
    ("Culture", "Heritage festivals, literature, cultural celebrations"),
    ("Art", "Art fairs, gallery exhibitions, museum shows"),
    ("Music", "Concerts, music festivals, live performances"),
    ("F&B", "Food festivals, luxury dining, chef collabs"),
    ("Sports to Watch", "Spectator sports – cricket, F1, UFC, AFL, NRL"),
    ("Recreational Sports", "Participatory – marathons, cycling, golf"),
    ("Theatre", "Stage plays, musicals, immersive theatre"),
    ("Comedy", "Stand-up comedy, comedy festivals"),
    ("Workshops", "Hands-on classes and masterclasses (added by the scraper)"),
    ("Other", "Kids, conferences, expos, meetups (added by the scraper)"),
]
GUIDE_TIERS = [
    ("Ultra Premium", "₹5,000+ / AED 500+ / AUD 200+ per ticket"),
    ("Luxury", "₹1,550–5,000 / AED 150–500 / AUD 79–200"),
    ("Premium", "₹500–1,500 / AED 50–150 / AUD 40–79"),
    ("Standard", "< ₹500 / Free / Trade Pass"),
]


def create_template(path: Path, master_name: str) -> Workbook:
    """A fresh workbook laid out like StepOne's (Guide + empty Master)."""
    wb = Workbook()
    guide = wb.active
    guide.title = "Guide"
    guide.column_dimensions["A"].width = 27
    guide.column_dimensions["B"].width = 12
    guide.column_dimensions["C"].width = 60
    guide.merge_cells("A1:C1")
    c = guide.cell(row=1, column=1, value="COLOUR KEY & REGION GUIDE")
    c.font, c.fill = _font(size=12, bold=True, color=CREAM), _fill(TITLE_FILL)
    r = 1
    for name, desc in GUIDE_CATEGORIES:
        r += 1
        for col, value in ((1, name), (2, "Category"), (3, desc)):
            cell = guide.cell(row=r, column=col, value=value)
            cell.fill = _fill(CATEGORY_FILLS[name])
            cell.font = _font(bold=col == 1, size=8 if col == 2 else 9)
    r += 1
    for name, desc in GUIDE_TIERS:
        r += 1
        bg, fg = TIER_STYLES[name]
        for col, value in ((1, name), (2, "Tier"), (3, desc)):
            cell = guide.cell(row=r, column=col, value=value)
            cell.fill = _fill(bg)
            cell.font = _font(bold=col == 1, size=8 if col == 2 else 9, color=fg if col == 1 else INK)
    for name, label in (("India", "India"), ("Intl", "Intl (ME/APAC)")):
        r += 1
        bg, fg = REGION_STYLES[name]
        for col, value in ((1, label), (2, "Region")):
            cell = guide.cell(row=r, column=col, value=value)
            cell.fill = _fill(bg)
            cell.font = _font(bold=col == 1, size=8 if col == 2 else 9, color=fg if col == 1 else INK)

    init_master_sheet(wb.create_sheet(master_name))
    return wb


def init_master_sheet(master: Worksheet) -> None:
    master.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N_COLS)
    t = master.cell(row=1, column=1, value="MASTER EVENTS CALENDAR · INDIA")
    t.font, t.fill = _font(size=13, bold=True, color=CREAM), _fill(TITLE_FILL)
    t.alignment = Alignment(horizontal="center")
    master.merge_cells(start_row=2, start_column=1, end_row=2, end_column=N_COLS)
    s = master.cell(row=2, column=1, value="Maintained by StepOne · the scraper only appends new events below the last row")
    s.font, s.fill = _font(size=8, color=SUBTITLE_INK), _fill(SUBTITLE_FILL)
    header_row(master, 3)
    master.freeze_panes = "C4"


# ------------------------------------------------------------------ save

def _backup(path: Path, folder: Path, keep: int) -> Optional[Path]:
    if not path.exists():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}"
    shutil.copy2(path, target)
    backups = sorted(folder.glob(f"{path.stem}_backup_*{path.suffix}"))
    for old in backups[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def is_locked(path: Path) -> bool:
    """True when another program (Excel) holds the file open for writing."""
    if not path.exists():
        return False
    lock_file = path.with_name("~$" + path.name)
    if lock_file.exists():
        return True
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def save_workbook(wb: Workbook, path: Path) -> Tuple[Path, Optional[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.saving{path.suffix}")
    wb.save(tmp)
    try:
        if is_locked(path):
            raise PermissionError(str(path))
        os.replace(tmp, path)
        return path, None
    except PermissionError:
        alt = path.with_name(f"{path.stem} (updated {datetime.now():%Y-%m-%d %H%M}){path.suffix}")
        os.replace(tmp, alt)
        return alt, (f"'{path.name}' is open in another program, so the results were saved to '{alt.name}'. "
                     "Close the file before the next run.")


# ------------------------------------------------------------------ entry

def write_workbook(settings, events: Sequence[Event], report) -> WriteResult:
    path = settings.resolve(settings.workbook_path)
    warnings: List[str] = []
    run_at = report.started
    window = (report.window_start, report.window_end)
    if path.exists():
        wb = load_workbook(path)
        if settings.backup_before_write:
            _backup(path, settings.data_path / "backups", settings.keep_backups)
    else:
        wb = create_template(path, settings.master_sheet)
        warnings.append(f"Created a new workbook at {path}")

    master = find_master(wb, settings.master_sheet)
    if master is None:
        master = wb.create_sheet(settings.master_sheet)
        init_master_sheet(master)
        warnings.append(f"No Master sheet named '{settings.master_sheet}' was found, so one was added.")
    new_count, added_on = append_to_master(wb, master, events, run_at, window)
    report.new_in_master = new_count

    generated = _generated_sheets(wb)
    made: List[str] = []
    rolling_title = _safe_title(wb, settings.rolling_sheet, generated)
    rolling = _replace_sheet(wb, rolling_title, generated, after=master.title)
    made.append(rolling_title)
    span = f"{window[0]:%d %b %Y} – {window[1]:%d %b %Y}".upper()
    title = f"ROLLING EVENTS CALENDAR · {span} · " + " · ".join(c.upper() for c in report.cities)
    subtitle = (f"Sources: {_source_line(events, report.sources)}  |  {len(events)} events  |  City by city, "
                f"each in date order  |  Last updated: {run_at:%d %b %Y, %I:%M %p} IST  |  Tier = entry ticket price "
                f"(see Guide)  |  Use the filter arrows in row 3 to pick a city, tier or category")
    failed = getattr(report, "failed_sources", None) or []
    if failed:
        subtitle = (f"⚠ Not read this run (site unreachable or refused): {', '.join(failed)} - their events are "
                    f"missing below; run again later.  |  " + subtitle)
    last_row = write_calendar_sheet(rolling, title, subtitle, events, added_on, window, tab_color="0E4D45")

    anchor = rolling_title
    if settings.city_tabs:
        for i, city in enumerate(report.cities):
            tab = _safe_title(wb, city, generated)
            ws = _replace_sheet(wb, tab, generated, after=anchor)
            anchor = tab
            made.append(tab)
            city_events = [e for e in events if e.city == city]
            write_calendar_sheet(
                ws, f"{city.upper()} · ROLLING EVENTS · {span}",
                f"{len(city_events)} events  |  Sources: {_source_line(city_events, report.sources)}  |  "
                f"Last updated: {run_at:%d %b %Y, %I:%M %p} IST",
                city_events, added_on, window, tab_color=CITY_TAB_COLORS[i % len(CITY_TAB_COLORS)],
                empty_message=f"No events found in {city} for this window.")
    if settings.summary_tab:
        tab = _safe_title(wb, SUMMARY_SHEET, generated)
        ws = _replace_sheet(wb, tab, generated, after=rolling_title)
        made.append(tab)
        write_summary(wb, ws, rolling_title, last_row, events, report, settings)
    for stale in generated:
        if stale not in made and stale in wb.sheetnames and stale not in (master.title,):
            del wb[stale]
    append_run_log(wb, report, len(events), new_count)
    _remember_generated(wb, made)
    for hidden in (RUN_LOG_SHEET, INDEX_SHEET, META_SHEET):
        if hidden in wb.sheetnames:
            wb[hidden].sheet_state = "hidden"
            wb.move_sheet(hidden, offset=len(wb.sheetnames) - 1 - wb.sheetnames.index(hidden))
    wb.active = wb.sheetnames.index(rolling_title)
    for ws in wb.worksheets:
        ws.sheet_view.tabSelected = ws.title == rolling_title

    saved, warning = save_workbook(wb, path)
    if warning:
        warnings.append(warning)
    return WriteResult(saved_to=saved, new_in_master=new_count, warnings=warnings)
