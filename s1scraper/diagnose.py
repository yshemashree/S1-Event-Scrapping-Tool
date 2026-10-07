"""``--diagnose``: a quick health check of every enabled source.

For each source it reads one listing page for one city, opens a few event
pages, and reports what it could and could not extract. Raw pages are saved
under ``diagnostics/<timestamp>/`` so a failing source can be fixed from the
real markup.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, List

from .browser import BrowserService, BrowserSettings
from .cities import CITIES, resolve_cities
from .config import Settings
from .fetcher import Cancelled, Fetcher, HttpCache
from .normalize import today_ist
from .sources import RunContext


def run_diagnostics(settings: Settings, out: Callable[[str], None] = print, events_per_source: int = 3,
                    rewrite=None, unrewrite=None) -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = settings.data_path / "diagnostics" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    ref = today_ist()
    w_start, w_end = settings.window(ref)
    cities = resolve_cities(settings.cities) or list(CITIES)
    stop = threading.Event()
    politeness = settings.politeness()
    politeness.detail_cache_hours = 0      # always fetch fresh pages for a health check
    politeness.listing_cache_hours = 0
    fetcher = Fetcher(politeness, HttpCache(None), stop, rewrite, unrewrite)
    browser = BrowserService(fetcher, BrowserSettings(
        mode="auto" if settings.use_browser else "off", headless=not settings.show_browser,
        channel=settings.browser_channel, executable_path=settings.browser_path))
    ctx = RunContext(fetcher=fetcher, browser=browser, window_start=w_start, window_end=w_end, ref=ref,
                     log=lambda m: None, include_activities=settings.include_activities,
                     use_browser=settings.use_browser, max_details=events_per_source, debug_dir=folder)
    report: List[str] = [f"S1 Event Scraper diagnostics {stamp}", f"Window {w_start} → {w_end}", ""]

    def say(line: str = "") -> None:
        report.append(line)
        out(line)

    try:
        for cls in settings.enabled_sources():
            src = cls()
            src.include_activities = settings.include_activities
            src.max_listing_pages = 1
            city = None if src.national else next((c for c in cities if src.covers(c)), None)
            if city is None and not src.national:
                say(f"■ {src.name}: none of the chosen cities is covered - skipped")
                continue
            label = city.name if city else "national listing"
            say(f"■ {src.name} ({label})")
            seeds = src.seeds_for(city)
            first = seeds[0][0] if seeds and seeds[0] else ""
            if first:
                allowed = fetcher.allowed_by_robots(first)
                say(f"   robots.txt allows the listing page: {'yes' if allowed else 'NO'}")
            try:
                events = src.discover(ctx, city)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                say(f"   ERROR while reading listings: {type(exc).__name__}: {exc}")
                continue
            st = src.stats
            say(f"   listing pages read: {st.listing_pages} (in browser: {st.listing_pages_via_browser})")
            say(f"   events found on listing: {len(events)}")
            for err in st.errors[:4]:
                say(f"   problem: {err}")
            if fetcher.is_disabled(first):
                say(f"   BLOCKED: {fetcher.is_disabled(first)}")
            sample = [e for e in events if e.url][:events_per_source]
            filled = {"date": 0, "venue": 0, "price": 0, "organizer": 0, "category": 0}
            for ev in sample:
                try:
                    ok = src.enrich(ctx, ev)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    say(f"   ERROR on {ev.url}: {type(exc).__name__}: {exc}")
                    continue
                filled["date"] += ev.start is not None
                filled["venue"] += bool(ev.venue)
                filled["price"] += ev.price_min is not None or ev.is_free
                filled["organizer"] += bool(ev.organizer)
                filled["category"] += bool(ev.categories)
                when = ev.start.strftime("%d %b %Y %H:%M") if ev.start else "no date"
                say(f"   · {ev.title[:60]!r} | {when} | {ev.venue[:40] or 'no venue'} | "
                    f"price {ev.price_min if ev.price_min is not None else '-'}–{ev.price_max or '-'} | "
                    f"organiser {ev.organizer[:30] or '-'} | page read: {'yes' if ok else 'no'}")
            if sample:
                n = len(sample)
                say("   fields found on event pages: " + ", ".join(f"{k} {v}/{n}" for k, v in filled.items()))
            say()
    except Cancelled:
        say("Stopped.")
    finally:
        browser.close()
        fetcher.close()
    if browser.description:
        say(f"Browser used: {browser.description}")
    elif browser.start_error and settings.use_browser:
        say(f"Browser not available: {browser.start_error}")
    say(f"Raw pages saved in: {folder}")
    (folder / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    return 0
