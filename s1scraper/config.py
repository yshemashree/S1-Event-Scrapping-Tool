"""User settings (``settings.json`` next to the app) and the date window."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

from dateutil.relativedelta import relativedelta

from .cities import CITIES
from .fetcher import SPEED_PRESETS, Politeness
from .sources import ALL_SOURCES, Source

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"

WINDOW_PRESETS = {
    "1 week": relativedelta(weeks=1),
    "2 weeks": relativedelta(weeks=2),
    "1 month": relativedelta(months=1),
    "2 months": relativedelta(months=2),
    "3 months": relativedelta(months=3),
    "6 months": relativedelta(months=6),
}
CUSTOM = "Custom dates"


class SettingsError(ValueError):
    pass


def parse_user_date(text: str) -> date:
    """Accept 2026-10-08, 08-10-2026, 08/10/2026 or 8 Oct 2026."""
    s = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise SettingsError(f"Could not read the date {text!r}. Use the format 2026-10-08.")


def compute_window(preset: str, start: str = "", end: str = "", today: Optional[date] = None) -> Tuple[date, date]:
    today = today or date.today()
    if preset == CUSTOM:
        if not start or not end:
            raise SettingsError("Enter both a From and a To date for a custom window.")
        d1, d2 = parse_user_date(start), parse_user_date(end)
        if d2 < d1:
            raise SettingsError("The To date is before the From date.")
        if (d2 - d1).days > 400:
            raise SettingsError("Custom windows are limited to about a year.")
        return d1, d2
    delta = WINDOW_PRESETS.get(preset)
    if delta is None:
        m = re.fullmatch(r"\s*(\d+)\s*(d|day|days|w|week|weeks|m|month|months)\s*", preset or "", re.I)
        if not m:
            raise SettingsError(f"Unknown window {preset!r}. Use one of: {', '.join(WINDOW_PRESETS)}.")
        n, unit = int(m.group(1)), m.group(2)[0].lower()
        delta = relativedelta(days=n) if unit == "d" else relativedelta(weeks=n) if unit == "w" else relativedelta(months=n)
    return today, today + delta


@dataclass
class Settings:
    workbook_path: str = "Rolling_Event_Calendar.xlsx"
    master_sheet: str = "Master Calander "
    rolling_sheet: str = "Rolling Calendar"
    window_preset: str = "1 month"
    window_start: str = ""
    window_end: str = ""
    cities: List[str] = field(default_factory=lambda: [c.name for c in CITIES])
    sources: Dict[str, bool] = field(default_factory=lambda: {s.key: s.enabled_by_default for s in ALL_SOURCES})
    speed: str = "normal"                  # gentle / normal / fast
    respect_robots_txt: bool = True
    use_browser: bool = True               # open pages in Edge/Chrome when a plain download is not enough
    show_browser: bool = False             # only for one run with --show-browser; never kept between runs
    browser_channel: str = ""              # msedge / chrome / "" (auto)
    browser_path: str = ""
    max_details_per_source: int = 1500
    time_limit_minutes: float = 60.0       # whole run (0 = none); nearest dates get their event pages first
    detail_cache_days: float = 10.0
    tier_basis: str = "min"                # min / avg / max ticket price
    include_activities: bool = False       # BookMyShow "activities" (water parks, gaming zones ...)
    include_online: bool = False
    include_undated: bool = False
    city_tabs: bool = False                # extra tabs; by default the workbook is just Master + Rolling
    summary_tab: bool = False
    backup_before_write: bool = True
    keep_backups: int = 15
    data_dir: str = ""                     # cache/, logs/, backups/ (default: the app folder)
    schedule_frequency: str = "off"        # automatic runs: off / daily / weekly
    schedule_day: str = "Friday"
    schedule_time: str = "07:00"

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = Path(path) if path else SETTINGS_FILE
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SettingsError(f"settings.json is not valid JSON: {exc}") from exc
            known = {f.name for f in fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(s, key, value)
            defaults = {src.key: src.enabled_by_default for src in ALL_SOURCES}
            defaults.update({k: bool(v) for k, v in (data.get("sources") or {}).items() if k in defaults})
            s.sources = defaults
            s.show_browser = False      # the browser always works out of sight (older versions could save True)
        return s

    def save(self, path: Optional[Path] = None) -> None:
        path = Path(path) if path else SETTINGS_FILE
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- helpers
    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else APP_DIR / p

    @property
    def data_path(self) -> Path:
        return self.resolve(self.data_dir) if self.data_dir else APP_DIR

    @property
    def cache_path(self) -> Path:
        """Folder of the page cache. It lives in the computer's own cache folder rather than next to
        the app, which often sits on a Desktop that iCloud or OneDrive syncs (a synced folder can swap
        the file out mid-run). An explicit data_dir keeps everything together."""
        if self.data_dir:
            return self.data_path / "cache"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Caches" / "S1 Event Scraper"
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "S1 Event Scraper" / "cache"
        return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "s1-event-scraper"

    def window(self, today: Optional[date] = None) -> Tuple[date, date]:
        return compute_window(self.window_preset, self.window_start, self.window_end, today)

    def politeness(self) -> Politeness:
        p = Politeness(**SPEED_PRESETS.get(self.speed, SPEED_PRESETS["normal"]))
        p.respect_robots_txt = bool(self.respect_robots_txt)
        p.detail_cache_hours = max(0.0, float(self.detail_cache_days)) * 24
        return p

    def enabled_sources(self) -> List[Type[Source]]:
        return [cls for cls in ALL_SOURCES if self.sources.get(cls.key, cls.enabled_by_default)]

    def validate(self) -> None:
        from .cities import resolve_cities

        if not resolve_cities(self.cities):
            raise SettingsError("Pick at least one city.")
        if not self.enabled_sources():
            raise SettingsError("Pick at least one source.")
        try:
            if float(self.time_limit_minutes or 0) < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise SettingsError("time_limit_minutes must be a number of minutes (0 = no limit).") from None
        if self.tier_basis not in ("min", "avg", "max"):
            raise SettingsError("tier_basis must be min, avg or max.")
        if not str(self.workbook_path).strip():
            raise SettingsError("Choose the Excel workbook to update.")
        if not str(self.workbook_path).lower().endswith((".xlsx", ".xlsm")):
            raise SettingsError("The workbook must be an .xlsx file.")
        self.window()
