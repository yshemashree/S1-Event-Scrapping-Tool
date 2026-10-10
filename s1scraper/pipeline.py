"""One scraper run, start to finish.

discover listings (all sources in parallel, each site paced on its own)
  -> drop listings clearly outside the date window, online-only, abroad or in other towns
  -> merge duplicates across sites, so each event's page is fetched once
  -> fetch event pages for what the listing lacked (all sites at the same time,
     nearest dates first, within the run's time limit)
  -> settle dates and register-by, keep events in the chosen cities and window,
     classify, write Excel
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time as clock
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .browser import BrowserService, BrowserSettings
from .cities import CITIES, City, detect_city, detect_other_city, resolve_cities
from .classify import classify_activity, classify_tier
from .config import Settings
from .dedupe import dedupe
from .fetcher import BROWSER_SUFFIX, Cancelled, Fetcher, HttpCache
from .models import Event
from .normalize import host_of, now_ist, one_line, today_ist
from .sources import SOURCE_BY_KEY, RunContext, Source, platform_for_url

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
LogFn = Callable[[str], None]

CITY_ORDER = {c.name: i for i, c in enumerate(CITIES)}
# Platform section names that only repeat the Activity Type; Notes keep specific genres (EDM, Cricket ...)
_GENERIC_CATEGORIES = {
    "events", "event", "experiences", "shows", "show", "entertainment", "activities", "comedy shows",
    "comedy", "music shows", "music", "concerts", "plays", "theatre", "theater", "sports", "food & drinks",
    "food and drinks", "food", "workshops", "workshop", "talks", "exhibitions", "exhibition", "performances",
    "kids", "nightlife", "art", "arts", "culture", "festivals", "spirituality", "meetups", "screenings",
    "adventure", "fitness", "parties", "party", "other", "f&b",
}


@dataclass
class RunReport:
    started: datetime
    finished: Optional[datetime] = None
    window_start: Optional[date] = None
    window_end: Optional[date] = None
    cities: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    per_source: Dict[str, dict] = field(default_factory=dict)
    dropped: Dict[str, int] = field(default_factory=dict)
    new_in_master: int = 0
    saved_to: str = ""
    warnings: List[str] = field(default_factory=list)
    cancelled: bool = False
    error: str = ""
    browser: str = ""
    failed_sources: List[str] = field(default_factory=list)
    detail_pages_skipped: int = 0          # complete in the listing already: no request needed
    detail_pages_over_time: int = 0        # left out when the time limit was reached
    listing_pages_cut: int = 0             # city lists where only the first pages were read (time limit)
    time_limit_minutes: float = 0.0

    @property
    def minutes(self) -> float:
        if not self.finished:
            return 0.0
        return max(0.0, (self.finished - self.started).total_seconds() / 60)

    def detail_line(self) -> str:
        read = sum(st.get("details_fetched", 0) for st in self.per_source.values())
        verified = sum(1 for e in self.events if e.link_verified)
        line = (f"Event pages: {read} read, {self.detail_pages_skipped} not needed (the listing had everything). "
                f"Links checked on the event's own page: {verified} of {len(self.events)}.")
        if self.detail_pages_over_time or self.listing_pages_cut:
            line += (f" Time limit ({self.time_limit_minutes:g} min): {self.detail_pages_over_time} later events "
                     "kept their listing details only.")
        return line

    def summary_lines(self) -> List[str]:
        lines = []
        if self.cancelled:
            return ["Run stopped - the workbook was not changed."]
        if self.error:
            return [f"Run failed: {self.error}"]
        span = f"{self.window_start:%d %b %Y} – {self.window_end:%d %b %Y}" if self.window_start else ""
        lines.append(f"{len(self.events)} events in {span} across {len(self.cities)} cities.")
        by_city = Counter(e.city for e in self.events)
        lines.append("By city: " + ", ".join(f"{c} {by_city.get(c, 0)}" for c in self.cities))
        for name, st in self.per_source.items():
            line = (f"{name}: {st.get('discovered', 0)} listings, {st.get('details_fetched', 0)} event pages read, "
                    f"{st.get('kept', 0)} events kept")
            if st.get("errors"):
                line += f" ({len(st['errors'])} problems - see log)"
            lines.append(line)
        lines.append(self.detail_line())
        if self.saved_to:
            lines.append(f"Master: {self.new_in_master} new events appended. Saved to {self.saved_to}")
        if self.finished:
            lines.append(f"Run time: {self.minutes:.0f} min.")
        lines.extend("Warning: " + w for w in self.warnings)
        return lines


def in_window(ev: Event, start: date, end: date) -> bool:
    if ev.start is None:
        return False
    s, e = ev.start.date(), (ev.end or ev.start).date()
    if s <= end and e >= start:
        return True
    return any(start <= d <= end for d in ev.session_dates)


_KIND_PHRASES = {
    "Comedy": "comedy show", "Music": "live music", "Theatre": "theatre play", "F&B": "food and drinks event",
    "Art": "art event", "Culture": "cultural event", "Sports to Watch": "sports match",
    "Recreational Sports": "sports activity", "Workshops": "workshop",
}


def _tag_words(tag: str) -> str:
    """Platform tags as words: "music-shows" -> "music shows"; age buckets and the like dropped."""
    t = tag.strip()
    if re.match(r"age-bucket|outdoor-events|indoor-events|physical$|all$", t, re.I):
        return ""
    return t.replace("-", " ").replace("_", " ")


def _languages(text: str) -> str:
    """"english|hindi|marathi" -> "English, Hindi, Marathi"."""
    parts = [p.strip() for p in re.split(r"[|,/]", text or "") if p.strip()]
    return ", ".join(p[:1].upper() + p[1:] for p in parts)


def build_notes(ev: Event) -> str:
    """One short line on what the event is, like StepOne's own notes ("Stand-up comedy.",
    "Bollywood composer-singer live concert."). Dates, times and prices have their own columns."""
    line = one_line(ev.description) if ev.description else ""
    if not line:
        kinds = [w for w in (_tag_words(c) for c in ev.categories)
                 if w and w.lower() not in _GENERIC_CATEGORIES][:2]
        line = ", ".join(kinds) or _KIND_PHRASES.get(ev.activity_type, "")
        language = _languages(ev.language)
        if language:
            line = f"{line} in {language}" if line else language
        performers = [p for p in ev.performers if p and p.lower() not in ev.title.lower()][:2]
        if performers:
            line = f"{line} featuring {' and '.join(performers)}" if line else "Featuring " + " and ".join(performers)
        if line:
            line = line[0].upper() + line[1:] + "."
    if ev.status:
        line = f"{ev.status.upper()}. {line}".strip()
    return line


_ONLINE_RE = re.compile(r"\b(?:online|virtual(?! reality)|webinars?|zoom|google meet|livestream|live stream)\b", re.I)


def is_online(ev: Event) -> bool:
    """Online-only: flagged by the page, or "Online event" as the venue, or online/virtual/webinar in the title."""
    venue = (ev.venue or "").strip()
    if ev.online and (not venue or _ONLINE_RE.search(venue)):
        return True
    return bool(re.fullmatch(r"(?:online|online event|virtual|virtual event)", venue, re.I)
                or _ONLINE_RE.search(ev.title or ""))


_ABROAD_RE = re.compile(
    r"\b(?:australia|united kingdom|england|scotland|united states|usa|u\.s\.a|canada|singapore|dubai|"
    r"abu dhabi|united arab emirates|uae|qatar|doha|saudi arabia|riyadh|malaysia|kuala lumpur|thailand|bangkok|"
    r"nepal|kathmandu|sri lanka|colombo|london|new york|toronto|sydney|melbourne|worcester)\b", re.I)


def is_abroad(ev: Event) -> bool:
    """Held outside India: the venue or address names another country or a city abroad, or the
    title reads like "... in Worcester, MA". (Indian events also appear on eventbrite.co.uk, so the
    site's country is no evidence either way.)"""
    if _ABROAD_RE.search(" ".join((ev.venue or "", ev.address or ""))):
        return True
    return bool(re.search(r"\bin [A-Z][a-z]+, [A-Z]{2}\b", ev.title or ""))


_PLACEHOLDER_TITLES = {"post", "test", "event", "events", "other org", "tbd", "tba", "untitled"}


def is_placeholder_title(title: str) -> bool:
    t = (title or "").strip()
    return (len(t) <= 4 and not t.isupper()) or t.lower() in _PLACEHOLDER_TITLES or bool(
        re.fullmatch(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}", t, re.I))


_OVERNIGHT_END = time(6, 0)
_LONG_RUNNING_RE = re.compile(r"\b(?:exhibitions?|exhibit|museum|gallery|installation|retrospective|art show|"
                              r"immersive|experience centre|showcase)\b", re.I)


def settle_dates(ev: Event, w_start: date, w_end: date) -> bool:
    """Make Start and End dates trustworthy. Returns False to leave the event out.

    * A night that ends after midnight is one evening, not a two-day event.
    * Shows on several dates start on the first date inside the window.
    * Spans over a month are series (weekly runs, "every Saturday") whose last date sites
      report unreliably: the End Date then reads "Multiple dates". A series that began
      long before the window, with none of its dates known, is left out. Exhibitions keep
      the closing date their own page gives.
    """
    if ev.start is None:
        return True
    end = ev.end
    if end is not None and end < ev.start:
        end = None
    if end is not None and end.date() > ev.start.date() and end - ev.start < timedelta(hours=24) \
            and end.time() <= _OVERNIGHT_END:
        end = None
    sessions = sorted({d for d in ev.session_dates if w_start <= d <= w_end})
    if ev.start.date() < w_start and sessions:
        ev.start = datetime.combine(sessions[0], ev.start.time() if ev.has_time else time())
    if end is not None and (end.date() - ev.start.date()).days > 31:
        if ev.detail_fetched and _LONG_RUNNING_RE.search(" ".join([ev.title] + ev.categories)):
            ev.end, ev.end_known = end, True
            return True
        if ev.start.date() < w_start and not sessions:
            return False
        ev.end, ev.end_known = None, False
        return True
    ev.end, ev.end_known = end, True
    return True


def settle_register_by(ev: Event, from_date: Optional[date] = None) -> None:
    """Register By: the site's own deadline, never after the event's last day. Without one, the last day
    you can still go (a run's end date, or the date of a one-day event), marked as inferred; for an event
    running on many dates, its start, though never before ``from_date`` (the first day of the window)."""
    runs = ev.end_known and ev.end is not None and ev.start is not None and ev.end.date() > ev.start.date()
    last = ev.end if runs else ev.start
    rb = ev.register_by
    if rb is not None and ev.start is not None:
        if rb > last:
            rb = last
        if rb < ev.start - timedelta(days=365):
            rb = None
    if rb is not None:
        ev.register_by, ev.register_by_inferred = rb, False
        return
    if last is not None and from_date is not None and last.date() < from_date:
        last = datetime.combine(from_date, time())
    ev.register_by, ev.register_by_inferred = last, True


_JUNK_VENUE_RE = re.compile(r"^(?:sales end soon|sales ended|sold out|almost full|going fast|selling fast|"
                            r"few tickets left|free|book now|register|tickets?|.*\bonwards\b|₹.*|\$.*|"
                            r"venue (?:to be|will be) (?:announced|updated).*|tba|tbd|to be announced)$", re.I)


# Share of the time limit that finding events may use; after that only the first page of each list
# is read. The rest goes to event pages (nearest dates first), less a moment for writing Excel.
LISTING_SHARE = 0.5


def time_budget(minutes: float, started: float) -> Tuple[Optional[float], Optional[float]]:
    """(listing deadline, event-page deadline) as time.monotonic() values; (None, None) = no limit."""
    seconds = max(0.0, float(minutes or 0)) * 60
    if not seconds:
        return None, None
    return started + seconds * LISTING_SHARE, started + seconds - min(120.0, seconds * 0.05)


def listing_scale(w_start: date, w_end: date) -> int:
    """How many more listing pages a long window needs (1 month = 1x, 3 months = 2x, 6 months = 3x)."""
    days = (w_end - w_start).days
    return 1 if days <= 45 else 2 if days <= 100 else 3


class Runner:
    def __init__(self, settings: Settings, log_fn: Optional[LogFn] = None, progress_fn: Optional[ProgressFn] = None,
                 stop_event: Optional[threading.Event] = None, rewrite: Optional[Callable[[str], str]] = None,
                 debug_dir: Optional[Path] = None, today: Optional[date] = None, write_excel: bool = True,
                 unrewrite: Optional[Callable[[str], str]] = None, find: Sequence[str] = ()):
        self.settings = settings
        self.log_fn = log_fn
        self.progress_fn = progress_fn
        self.stop_event = stop_event or threading.Event()
        self.rewrite = rewrite
        self.unrewrite = unrewrite
        self.debug_dir = debug_dir
        self.today = today
        self.write_excel = write_excel
        self._lock = threading.Lock()
        self._detail_deadline: Optional[float] = None
        self.find = [f.strip() for f in find if f and f.strip()]     # --find: report where these events went
        self._traces: Dict[str, List[str]] = {}

    # ----------------------------------------------------------- feedback
    def log(self, msg: str) -> None:
        log.info(msg)
        if self.log_fn:
            self.log_fn(msg)

    def progress(self, frac: float, text: str) -> None:
        if self.progress_fn:
            self.progress_fn(max(0.0, min(1.0, frac)), text)

    # ---------------------------------------------------------------- run
    def run(self) -> RunReport:
        s = self.settings
        s.validate()
        ref = self.today or today_ist()
        w_start, w_end = s.window(ref)
        cities = resolve_cities(s.cities)
        sources = [cls() for cls in s.enabled_sources()]
        for src in sources:
            src.include_activities = s.include_activities
        report = RunReport(started=now_ist(), window_start=w_start, window_end=w_end,
                           cities=[c.name for c in cities], sources=[src.name for src in sources],
                           time_limit_minutes=max(0.0, float(s.time_limit_minutes or 0)))
        listing_deadline, self._detail_deadline = time_budget(report.time_limit_minutes, clock.monotonic())

        cache = HttpCache(s.cache_path / "http_cache.sqlite3")
        cache.prune(older_than_days=max(30.0, s.detail_cache_days * 2))
        fetcher = Fetcher(s.politeness(), cache, self.stop_event, self.rewrite, self.unrewrite)
        browser = BrowserService(fetcher, BrowserSettings(
            mode="auto" if s.use_browser else "off", headless=not s.show_browser,
            channel=s.browser_channel, executable_path=s.browser_path))
        ctx = RunContext(fetcher=fetcher, browser=browser, window_start=w_start, window_end=w_end, ref=ref,
                         log=self.log, include_activities=s.include_activities, use_browser=s.use_browser,
                         max_details=s.max_details_per_source, debug_dir=self.debug_dir,
                         listing_scale=listing_scale(w_start, w_end), listing_deadline=listing_deadline)
        for src in sources:
            if src.min_gap:
                for template in src.listing_templates:
                    fetcher.set_host_pacing(host_of(template), *src.min_gap)
        self._early_dropped: Counter = Counter()
        self.log(f"Window {w_start:%d %b %Y} → {w_end:%d %b %Y} · cities: {', '.join(report.cities)}")
        self.log(f"Sources: {', '.join(report.sources)}")
        final: List[Event] = []
        try:
            partials = self._discover(ctx, sources, cities)
            self._report_found_on_listings(partials)
            report.listing_pages_cut = sum(src.stats.listing_cut for src in sources)
            if report.listing_pages_cut:
                self.log(f"Time limit: only the first pages of {report.listing_pages_cut} long lists were read, "
                         "to leave time for event pages")
            candidates = dedupe(self._prefilter(partials, cities, w_start, w_end))
            self.log(f"{len(partials)} listings found; {len(candidates)} unique candidates in or near the window")
            self._enrich(ctx, sources, candidates, report)
            final = self._finalize(candidates, cities, w_start, w_end, report)
            report.events = final
            self._report_found_in_sheet(final)
        except Cancelled:
            report.cancelled = True
            self.log("Stopped by user - the workbook was not changed.")
        finally:
            browser.close()
            fetcher.close()
            cache.close()
            report.browser = browser.description or (browser.start_error or "")
            if cache.problem:
                self.log(f"Note: {cache.problem}.")
            report.per_source = self._source_stats(sources, final, fetcher)
        if report.cancelled:
            return report
        failed = []
        for name, st in report.per_source.items():
            report.warnings.extend(st.get("browser_blocked") or [])    # a warning, not a failed source
            if st.get("blocked_hosts"):
                report.warnings.extend(st["blocked_hosts"])
            if st.get("blocked_hosts") or (st.get("errors") and not st.get("discovered")):
                failed.append(name)
        report.failed_sources = failed

        if self.write_excel and not final and failed:
            # Nothing found and sources failed (offline, VPN, blocked): keep last run's sheets.
            report.warnings.append("No events were found and some sources could not be read, so the workbook "
                                   "was left unchanged. Check the internet connection and run again.")
            self.log(report.warnings[-1])
        elif self.write_excel:
            self.progress(0.95, "Writing Excel")
            from .excel import write_workbook

            result = write_workbook(s, final, report)
            report.new_in_master = result.new_in_master
            report.saved_to = str(result.saved_to)
            report.warnings.extend(result.warnings)
            for note in result.notes:
                self.log(note)
            self.log(f"Excel updated: {report.saved_to} (+{report.new_in_master} new in Master)")
        report.finished = now_ist()
        self._save_report(report)
        self.progress(1.0, "Done")
        return report

    # ---------------------------------------------------------- discovery
    def _discover(self, ctx: RunContext, sources: List[Source], cities: List[City]) -> List[Event]:
        plan: Dict[str, List[Optional[City]]] = {}
        for src in sources:
            plan[src.key] = [None] if src.national else [c for c in cities if src.covers(c)]
        total = max(1, sum(len(v) for v in plan.values()))
        done = [0]

        def work(src: Source) -> List[Event]:
            out: List[Event] = []
            for city in plan[src.key]:
                ctx.fetcher.check_stop()
                label = city.name if city else "all cities"
                try:
                    out.extend(src.discover(ctx, city))
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - one broken site must not stop the run
                    log.exception("discover failed")
                    src.stats.error(f"{label}: {type(exc).__name__}: {exc}")
                    self.log(f"Problem reading {src.name} · {label}: {exc}")
                with self._lock:
                    done[0] += 1
                    self.progress(0.02 + 0.33 * done[0] / total, f"Finding events · {src.name} · {label}")
            return out

        results: List[Event] = []
        # every site at the same time: each one is still paced on its own
        with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
            futures = [pool.submit(work, src) for src in sources]
            for fut in as_completed(futures):
                results.extend(fut.result())
        return results

    # ---------------------------------------------------------- --find
    def _watched(self, ev: Event) -> List[str]:
        """The --find names this event's title matches (all the longer words of the name, any order)."""
        if not self.find:
            return []
        words = set(re.sub(r"[^a-z0-9]+", " ", (ev.title or "").lower()).split())
        out = []
        for term in self.find:
            parts = re.sub(r"[^a-z0-9]+", " ", term.lower()).split()
            key = [w for w in parts if len(w) >= 3] or parts
            if key and all(w in words for w in key):
                out.append(term)
        return out

    @staticmethod
    def _describe(ev: Event) -> str:
        when = f"{ev.start:%d %b %Y}" if ev.start else "no date"
        return f"\"{ev.title}\" · {ev.platform or ev.source} · {ev.city or 'city unknown'} · {when}"

    def _trace(self, ev: Event, reason: str) -> None:
        for term in self._watched(ev):
            self._traces.setdefault(term, []).append(f"left out ({reason}): {self._describe(ev)}")

    def _left_out(self, counter: Counter, reason: str, ev: Event) -> None:
        counter[reason] += 1
        self._trace(ev, reason)

    def _report_found_on_listings(self, partials: List[Event]) -> None:
        for term in self.find:
            hits = {ev.url or ev.title: ev for ev in partials if term in self._watched(ev)}
            if not hits:
                self.log(f"Find \"{term}\": not on any listing page this run read.")
                continue
            self.log(f"Find \"{term}\": on {len(hits)} listing(s):")
            for ev in list(hits.values())[:8]:
                self.log(f"    {self._describe(ev)}  {ev.url}")

    def _report_found_in_sheet(self, final: List[Event]) -> None:
        for term in self.find:
            kept = [ev for ev in final if term in self._watched(ev)]
            if kept:
                for ev in kept[:8]:
                    self.log(f"Find \"{term}\": in the sheet: {self._describe(ev)}")
            else:
                self.log(f"Find \"{term}\": not in the sheet.")
            for line in dict.fromkeys(self._traces.get(term, [])):
                self.log(f"    {line}")

    def _prefilter(self, events: List[Event], cities: List[City], w_start: date, w_end: date) -> List[Event]:
        """Drop what can already be ruled out from the listing, before any event page is requested."""
        keep = []
        early: Counter = getattr(self, "_early_dropped", Counter())
        for ev in events:
            if ev.start is not None:
                if ev.start.date() > w_end or (ev.end is not None and ev.end.date() < w_start):
                    self._trace(ev, "outside the chosen dates")
                    continue
            if not self.settings.include_online and is_online(ev):
                self._left_out(early, "online-only", ev)
                continue
            if is_abroad(ev):
                self._left_out(early, "outside India", ev)
                continue
            if is_placeholder_title(ev.title):
                self._left_out(early, "no real title", ev)
                continue
            if self._other_town(ev, cities):
                self._left_out(early, "other city", ev)
                continue
            keep.append(ev)
        self._early_dropped = early
        return keep

    @staticmethod
    def _other_town(ev: Event, cities: List[City]) -> bool:
        """The site's own URL names a town outside our cities (AllEvents lists Mehsana under Mumbai)."""
        src_cls = SOURCE_BY_KEY.get(ev.source)
        if src_cls is None or not src_cls.url_city_pattern:
            return False
        m = re.match(src_cls.url_city_pattern, ev.url or "")
        if not m:
            return False
        ours = {slug for c in cities for slug in src_cls.city_slugs.get(c.key, ())}
        ours |= {re.sub(r"[^a-z0-9]+", "-", a) for c in cities for a in c.aliases}
        return m.group(1) not in ours

    # ------------------------------------------------------------ details
    def _enrich(self, ctx: RunContext, sources: List[Source], events: List[Event],
                report: Optional[RunReport] = None) -> None:
        by_key = {src.key: src for src in sources}
        queues: Dict[str, List[Event]] = {}
        skipped = 0
        for ev in events:
            src = by_key.get(ev.source)
            if src is None or not ev.url:
                continue
            if not src.needs_detail(ev):
                skipped += 1
                continue
            queues.setdefault(src.key, []).append(ev)
        if report is not None:
            report.detail_pages_skipped = skipped
        deadline = self._detail_deadline
        over_time = [0]
        for key, evs in queues.items():
            evs.sort(key=lambda e: (e.start is None, e.start or datetime.max))
            if len(evs) > ctx.max_details:
                self.log(f"{by_key[key].name}: reading the first {ctx.max_details} of {len(evs)} event pages "
                         "(raise 'max_details_per_source' in settings.json for more)")
                del evs[ctx.max_details:]
        total = max(1, sum(len(v) for v in queues.values()))
        done = [0]

        def work(src: Source, evs: List[Event]) -> None:
            for i, ev in enumerate(evs):
                ctx.fetcher.check_stop()
                if deadline is not None and clock.monotonic() > deadline:
                    with self._lock:
                        over_time[0] += len(evs) - i
                    return
                try:
                    ok = src.enrich(ctx, ev)
                    if not ok:
                        self._enrich_from_other_platform(ctx, by_key, ev)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("enrich failed")
                    src.stats.error(f"{ev.url}: {type(exc).__name__}: {exc}")
                with self._lock:
                    done[0] += 1
                    left = ""
                    if deadline is not None:
                        left = f" · at most {max(1, round((deadline - clock.monotonic()) / 60))} min to go"
                    self.progress(0.35 + 0.58 * done[0] / total,
                                  f"Reading event pages · {src.name} ({done[0]}/{total}){left}")

        with ThreadPoolExecutor(max_workers=max(1, len(queues))) as pool:
            futures = [pool.submit(work, by_key[k], v) for k, v in queues.items()]
            for fut in as_completed(futures):
                fut.result()
        if report is not None:
            report.detail_pages_over_time = over_time[0]
        if over_time[0]:
            self.log(f"Time limit of {self.settings.time_limit_minutes:g} min reached: {over_time[0]} later events "
                     "keep what their listing showed (a longer time limit reads them too).")

    @staticmethod
    def _enrich_from_other_platform(ctx: RunContext, by_key: Dict[str, Source], ev: Event) -> None:
        """If the primary site would not serve the page, read the same event on another site."""
        for platform in ev.platforms[1:]:
            link = ev.links.get(platform)
            src = next((s for s in by_key.values() if s.platform == platform), None)
            if not link or src is None or ctx.fetcher.is_disabled(host_of(link)):
                continue
            alt = Event(url=link, title=ev.title, city=ev.city, source=src.key, platform=platform)
            if src.enrich(ctx, alt):
                # the other site's event page beats our own listing card; keep our title and id
                title = ev.title
                ev.absorb(alt, prefer_other=True)
                ev.title = title or ev.title
                return

    # ------------------------------------------------------------ finalize
    def _finalize(self, events: List[Event], cities: List[City], w_start: date, w_end: date,
                  report: RunReport) -> List[Event]:
        s = self.settings
        wanted = {c.name for c in cities}
        dropped: Counter = Counter(getattr(self, "_early_dropped", {}))
        out: List[Event] = []
        for ev in events:
            src_cls = SOURCE_BY_KEY.get(ev.source)
            if not (src_cls and src_cls.fixed_city):
                found = detect_city(ev.address, ev.venue)
                if found is not None:
                    ev.city = found.name
                elif (src_cls is not None and src_cls.national) or detect_other_city(ev.address):
                    self._left_out(dropped, "city not one of ours", ev)
                    continue
            if src_cls is not None and src_cls.url_city_pattern:
                m = re.match(src_cls.url_city_pattern, ev.url or "")
                ours = {slug for c in cities for slug in src_cls.city_slugs.get(c.key, ())}
                ours |= {re.sub(r"[^a-z0-9]+", "-", a) for c in cities for a in c.aliases}
                if m and m.group(1) not in ours:
                    self._left_out(dropped, "other city", ev)
                    continue
            if ev.city not in wanted:
                self._left_out(dropped, "other city", ev)
                continue
            if not s.include_online and is_online(ev):
                self._left_out(dropped, "online-only", ev)
                continue
            if is_abroad(ev):
                self._left_out(dropped, "outside India", ev)
                continue
            if is_placeholder_title(ev.title):
                self._left_out(dropped, "no real title", ev)
                continue
            if ev.start is None:
                if not s.include_undated:
                    self._left_out(dropped, "no date found", ev)
                    continue
            elif not settle_dates(ev, w_start, w_end):
                self._left_out(dropped, "repeats on dates not listed", ev)
                continue
            elif not in_window(ev, w_start, w_end):
                self._left_out(dropped, "outside the window", ev)
                continue
            if ev.venue and _JUNK_VENUE_RE.match(ev.venue.strip()):
                ev.venue = ""
            if not ev.title:
                self._left_out(dropped, "no title", ev)
                continue
            ticket_platform = platform_for_url(ev.ticket_url) if ev.ticket_url else ""
            if ticket_platform and ticket_platform not in ev.platforms:
                ev.platforms.insert(0, ticket_platform)
                ev.links[ticket_platform] = ev.ticket_url
            out.append(ev)

        out = dedupe(out)
        for ev in out:
            ev.activity_type = classify_activity(ev.categories, ev.title, ev.description, ev.venue)
            ev.tier = classify_tier(ev.price_min, ev.price_max, ev.is_free, s.tier_basis)
            if not ev.organizer:
                # Requested fallback: no organiser published -> name the source
                ev.organizer = "Source: " + " / ".join(ev.platforms or [ev.platform])
            settle_register_by(ev, w_start)
            ev.notes = build_notes(ev)
        out.sort(key=lambda e: (max(e.start.date(), w_start) if e.start else w_end,
                                CITY_ORDER.get(e.city, 99), e.title.lower()))
        report.dropped = dict(dropped)
        if dropped:
            self.log("Left out: " + ", ".join(f"{n} {why}" for why, n in dropped.most_common()))
        self.log(f"{len(out)} events in the window after merging duplicates")
        return out

    # ------------------------------------------------------------- report
    @staticmethod
    def _source_stats(sources: List[Source], final: List[Event], fetcher: Fetcher) -> Dict[str, dict]:
        kept = Counter()
        for ev in final:
            for key in ev.sources or [ev.source]:
                kept[key] += 1
        host_stats = fetcher.stats()
        out = {}
        for src in sources:
            st = src.stats
            blocked, browser_blocked = [], []
            for host, hs in host_stats.items():
                reason = fetcher.is_disabled(host)
                site = host[:-len(BROWSER_SUFFIX)] if host.endswith(BROWSER_SUFFIX) else host
                if reason and any(site.endswith(d) for d in _source_domains(src)):
                    (browser_blocked if site != host else blocked).append(reason)
            out[src.name] = {
                "discovered": st.discovered, "details_fetched": st.details_fetched,
                "details_failed": st.details_failed, "kept": kept.get(src.key, 0),
                "listing_pages": st.listing_pages, "listing_pages_via_browser": st.listing_pages_via_browser,
                "errors": list(st.errors), "blocked_hosts": blocked, "browser_blocked": browser_blocked,
            }
        return out

    def _save_report(self, report: RunReport) -> None:
        try:
            folder = self.settings.data_path / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = report.started.strftime("%Y%m%d_%H%M%S")
            data = {
                "started": report.started.isoformat(), "finished": report.finished.isoformat() if report.finished else None,
                "window": [str(report.window_start), str(report.window_end)], "cities": report.cities,
                "sources": report.sources, "events": len(report.events), "new_in_master": report.new_in_master,
                "saved_to": report.saved_to, "dropped": report.dropped, "per_source": report.per_source,
                "warnings": report.warnings, "browser": report.browser,
            }
            (folder / f"run_{stamp}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except OSError:
            log.warning("could not save run report", exc_info=True)


def _source_domains(src: Source) -> List[str]:
    domains = set()
    for template in list(src.listing_templates):
        host = host_of(template.replace("{slug}", "x"))
        if host:
            domains.add(host.split(":")[0].removeprefix("www."))
    return sorted(domains)
