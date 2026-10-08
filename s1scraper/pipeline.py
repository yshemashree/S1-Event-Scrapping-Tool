"""One scraper run, start to finish.

discover listings (all sources in parallel, each site paced on its own)
  -> drop listings clearly outside the date window
  -> merge duplicates across sites, so each event's page is fetched once
  -> fetch event pages for the details (organiser, end date, prices, ...)
  -> keep events in the chosen cities and window, classify, write Excel
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .browser import BrowserService, BrowserSettings
from .cities import CITIES, City, detect_city, detect_other_city, resolve_cities
from .classify import classify_activity, classify_tier
from .config import Settings
from .dedupe import dedupe
from .fetcher import Cancelled, Fetcher, HttpCache
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
        if self.saved_to:
            lines.append(f"Master: {self.new_in_master} new events appended. Saved to {self.saved_to}")
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


def build_notes(ev: Event) -> str:
    """One short line on what the event is, like StepOne's own notes ("Stand-up comedy.",
    "Bollywood composer-singer live concert."). Dates, times and prices have their own columns."""
    line = one_line(ev.description) if ev.description else ""
    if not line:
        kinds = [c for c in ev.categories if c.lower() not in _GENERIC_CATEGORIES][:2]
        line = ", ".join(kinds) or _KIND_PHRASES.get(ev.activity_type, "")
        if ev.language:
            line = f"{line} in {ev.language}" if line else ev.language
        performers = [p for p in ev.performers if p and p.lower() not in ev.title.lower()][:2]
        if performers:
            line = f"{line} featuring {' and '.join(performers)}" if line else "Featuring " + " and ".join(performers)
        if line:
            line = line[0].upper() + line[1:] + "."
    if ev.status:
        line = f"{ev.status.upper()}. {line}".strip()
    return line


class Runner:
    def __init__(self, settings: Settings, log_fn: Optional[LogFn] = None, progress_fn: Optional[ProgressFn] = None,
                 stop_event: Optional[threading.Event] = None, rewrite: Optional[Callable[[str], str]] = None,
                 debug_dir: Optional[Path] = None, today: Optional[date] = None, write_excel: bool = True,
                 unrewrite: Optional[Callable[[str], str]] = None):
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
                           cities=[c.name for c in cities], sources=[src.name for src in sources])

        data = s.data_path
        (data / "cache").mkdir(parents=True, exist_ok=True)
        cache = HttpCache(data / "cache" / "http_cache.sqlite3")
        cache.prune(older_than_days=max(30.0, s.detail_cache_days * 2))
        fetcher = Fetcher(s.politeness(), cache, self.stop_event, self.rewrite, self.unrewrite)
        browser = BrowserService(fetcher, BrowserSettings(
            mode="auto" if s.use_browser else "off", headless=not s.show_browser,
            channel=s.browser_channel, executable_path=s.browser_path))
        ctx = RunContext(fetcher=fetcher, browser=browser, window_start=w_start, window_end=w_end, ref=ref,
                         log=self.log, include_activities=s.include_activities, use_browser=s.use_browser,
                         max_details=s.max_details_per_source, debug_dir=self.debug_dir)
        self.log(f"Window {w_start:%d %b %Y} → {w_end:%d %b %Y} · cities: {', '.join(report.cities)}")
        self.log(f"Sources: {', '.join(report.sources)}")
        final: List[Event] = []
        try:
            partials = self._discover(ctx, sources, cities)
            candidates = dedupe(self._prefilter(partials, w_start, w_end))
            self.log(f"{len(partials)} listings found; {len(candidates)} unique candidates in or near the window")
            self._enrich(ctx, sources, candidates)
            final = self._finalize(candidates, cities, w_start, w_end, report)
            report.events = final
        except Cancelled:
            report.cancelled = True
            self.log("Stopped by user - the workbook was not changed.")
        finally:
            browser.close()
            fetcher.close()
            cache.close()
            report.browser = browser.description or (browser.start_error or "")
            report.per_source = self._source_stats(sources, final, fetcher)
        if report.cancelled:
            return report
        failed = []
        for name, st in report.per_source.items():
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
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(sources)))) as pool:
            futures = [pool.submit(work, src) for src in sources]
            for fut in as_completed(futures):
                results.extend(fut.result())
        return results

    @staticmethod
    def _prefilter(events: List[Event], w_start: date, w_end: date) -> List[Event]:
        keep = []
        for ev in events:
            if ev.start is not None:
                if ev.start.date() > w_end:
                    continue
                if ev.end is not None and ev.end.date() < w_start:
                    continue
            keep.append(ev)
        return keep

    # ------------------------------------------------------------ details
    def _enrich(self, ctx: RunContext, sources: List[Source], events: List[Event]) -> None:
        by_key = {src.key: src for src in sources}
        queues: Dict[str, List[Event]] = {}
        for ev in events:
            src = by_key.get(ev.source)
            if src is None or not src.fetch_details or not ev.url:
                continue
            queues.setdefault(src.key, []).append(ev)
        for key, evs in queues.items():
            evs.sort(key=lambda e: (e.start is None, e.start or datetime.max))
            if len(evs) > ctx.max_details:
                self.log(f"{by_key[key].name}: reading the first {ctx.max_details} of {len(evs)} event pages "
                         "(raise 'max_details_per_source' in settings.json for more)")
                del evs[ctx.max_details:]
        total = max(1, sum(len(v) for v in queues.values()))
        done = [0]

        def work(src: Source, evs: List[Event]) -> None:
            for ev in evs:
                ctx.fetcher.check_stop()
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
                    self.progress(0.35 + 0.58 * done[0] / total,
                                  f"Reading event pages · {src.name} ({done[0]}/{total})")

        with ThreadPoolExecutor(max_workers=max(1, min(6, len(queues)))) as pool:
            futures = [pool.submit(work, by_key[k], v) for k, v in queues.items()]
            for fut in as_completed(futures):
                fut.result()

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
        dropped: Counter = Counter()
        out: List[Event] = []
        for ev in events:
            src_cls = SOURCE_BY_KEY.get(ev.source)
            if not (src_cls and src_cls.fixed_city):
                found = detect_city(ev.address, ev.venue)
                if found is not None:
                    ev.city = found.name
                elif (src_cls is not None and src_cls.national) or detect_other_city(ev.address):
                    dropped["city not one of ours"] += 1
                    continue
            if ev.city not in wanted:
                dropped["other city"] += 1
                continue
            if ev.online and not ev.venue and not s.include_online:
                dropped["online-only"] += 1
                continue
            if ev.start is None:
                if not s.include_undated:
                    dropped["no date found"] += 1
                    continue
            elif not in_window(ev, w_start, w_end):
                dropped["outside the window"] += 1
                continue
            if not ev.title:
                dropped["no title"] += 1
                continue
            ticket_platform = platform_for_url(ev.ticket_url) if ev.ticket_url else ""
            if ticket_platform and ticket_platform not in ev.platforms:
                ev.platforms.insert(0, ticket_platform)
                ev.links[ticket_platform] = ev.ticket_url
            out.append(ev)

        out = dedupe(out)
        for ev in out:
            ev.activity_type = classify_activity(ev.categories, ev.title, ev.description)
            ev.tier = classify_tier(ev.price_min, ev.price_max, ev.is_free, s.tier_basis)
            if not ev.organizer:
                # Requested fallback: no organiser published -> name the source
                ev.organizer = "Source: " + " / ".join(ev.platforms or [ev.platform])
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
            blocked = []
            for host, hs in host_stats.items():
                reason = fetcher.is_disabled(host)
                if reason and any(host.endswith(d) for d in _source_domains(src)):
                    blocked.append(reason)
            out[src.name] = {
                "discovered": st.discovered, "details_fetched": st.details_fetched,
                "details_failed": st.details_failed, "kept": kept.get(src.key, 0),
                "listing_pages": st.listing_pages, "listing_pages_via_browser": st.listing_pages_via_browser,
                "errors": list(st.errors), "blocked_hosts": blocked,
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
