"""Shared crawling logic. Each site is a small subclass that only declares URLs."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..browser import BrowserService
from ..cities import CITIES, City, detect_city
from ..extract import (
    EventWalker, embedded_json, event_from_jsonld, jsonld_events, labeled_facts, listing_cards,
    locality_of_jsonld, meta_tags, microdata_events, page_price, pipe_facts, register_by_from_text, soup_of,
    text_lines, is_platform_name,
)
from ..fetcher import Fetcher
from ..models import Event
from ..normalize import absolute_url, canonical_url, clean_text, clean_title, host_of, titles_match

log = logging.getLogger(__name__)

# domain -> platform display name, used to recognise where an aggregator's ticket link points
PLATFORM_DOMAINS = {
    "bookmyshow.com": "BookMyShow", "district.in": "Zomato District", "zomato.com": "Zomato District",
    "insider.in": "Zomato District", "skillboxes.com": "Skillbox", "sortmyscene.com": "SortMyScene",
    "eventbrite.com": "Eventbrite", "eventbrite.co.in": "Eventbrite", "feverup.com": "Fever",
    "meetup.com": "Meetup", "explara.com": "Explara", "dreamsetgo.com": "DreamSetGo",
    "platinumlist.net": "Platinumlist", "allevents.in": "AllEvents", "ncpamumbai.com": "NCPA",
    "nmacc.com": "NMACC", "jioworldcentre.com": "Jio World Centre", "prithvitheatre.org": "Prithvi Theatre",
    "rangashankara.org": "Ranga Shankara", "indiahabitat.org": "India Habitat Centre",
    "iicdelhi.in": "India International Centre", "bangaloreinternationalcentre.org": "Bangalore International Centre",
    "lbb.in": "LBB",
}


def platform_for_url(url: str) -> str:
    host = host_of(url)
    for domain, name in PLATFORM_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def slugify(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", clean_text(text).lower())).strip("-")


@dataclass
class SourceStats:
    listing_pages: int = 0
    listing_pages_via_browser: int = 0
    discovered: int = 0
    details_fetched: int = 0
    details_failed: int = 0
    kept: int = 0
    listing_cut: int = 0          # cities whose further listing pages were skipped for the time limit
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        if msg not in self.errors and len(self.errors) < 25:
            self.errors.append(msg)


@dataclass
class RunContext:
    fetcher: Fetcher
    browser: BrowserService
    window_start: date
    window_end: date
    ref: date
    log: Callable[[str], None] = lambda msg: None
    include_activities: bool = False
    use_browser: bool = True
    max_details: int = 1500
    debug_dir: Any = None     # pathlib.Path: save raw pages for diagnostics when set
    listing_scale: int = 1    # longer windows read more listing pages (1 month = 1, 6 months = 3)
    listing_deadline: Optional[float] = None   # time.monotonic(): after this only first pages are read

    def listing_time_up(self) -> bool:
        return self.listing_deadline is not None and time.monotonic() > self.listing_deadline


@dataclass
class PageData:
    url: str
    final_url: str
    status: int
    html: str
    links: List[Tuple[str, str]] = field(default_factory=list)
    json_payloads: List[Tuple[str, Any]] = field(default_factory=list)
    via_browser: bool = False
    error: str = ""
    blocked: bool = False


class Source:
    """A site to scrape. Subclasses set the class attributes below."""

    key = "generic"
    name = "Generic"
    platform = "Generic"
    group = "more"                  # core / more / venues / optional (GUI grouping)
    enabled_by_default = True
    description = ""

    # --- discovery
    city_slugs: Dict[str, Sequence[str]] = {}
    listing_templates: Sequence[str] = ()      # may contain {slug}
    follow_patterns: Sequence[str] = ()        # regexes (may contain {slug}) for more listing pages
    event_pattern = r"$^"                      # regex matching event page URLs
    exclude_pattern = ""                       # regex of URLs never treated as events
    id_pattern = ""                            # regex with one group = platform id, run on the URL
    json_id_pattern = ""                       # full-match regex for ids found in JSON
    slug_url_template = ""                     # e.g. "https://site/events/{slug}" for JSON that has only slugs
    fixed_city = ""                            # venue sites: every event is in this city
    national = False                           # one listing for all cities; city from the venue
    url_city_pattern = ""                      # regex, group 1 = city slug in an event URL
    max_listing_pages = 12                     # per city, for a window of about a month
    stop_after_empty_pages = 3                 # stop paging once this many pages in a row add nothing new
    isolate_city_cookies = False               # forget cookies before each city (site remembers the last city)
    listing_browser = "auto"                   # auto / always / never
    listing_scrolls: Optional[int] = None      # cap on scrolls per listing page in the browser (None = default)
    browser_for_seeds = False                  # the city's main lists only fill in fully in a browser
    detail_browser = "auto"
    min_listing_events = 6                     # fewer than this from plain HTML -> try the browser
    fetch_details = True
    max_browser_details = 60                   # event pages opened in the browser per run (it is slow)
    min_gap: Optional[Tuple[float, float]] = None   # wider per-request gap for this site (seconds)
    organizer_published = True                 # False: the site never names organisers (no point waiting for one)
    discover_unknown_links = False             # venue sites: guess event pages from link text/paths

    def __init__(self) -> None:
        self.stats = SourceStats()
        self._event_rx = re.compile(self.event_pattern, re.I)
        self._exclude_rx = re.compile(self.exclude_pattern, re.I) if self.exclude_pattern else None
        self._id_rx = re.compile(self.id_pattern, re.I) if self.id_pattern else None
        self._json_id_rx = re.compile(self.json_id_pattern) if self.json_id_pattern else None
        self._working_slug: Dict[Tuple[str, str], str] = {}
        self._slug_choice: Dict[str, int] = {}     # city -> which spelling worked (tried first on its other lists)
        # Set once plain downloads are refused but the browser gets through,
        # so later pages go straight to the browser instead of failing first.
        self._browser_first = False
        self._browser_details = 0
        self.include_activities = False

    # --------------------------------------------------------------- URLs
    def is_event_url(self, url: str) -> bool:
        if not url or not self._event_rx.search(url):
            return False
        if self._exclude_rx and self._exclude_rx.search(url):
            return False
        return True

    def event_id(self, url: str) -> str:
        if self._id_rx:
            m = self._id_rx.search(url)
            if m:
                return m.group(1)
        return ""

    _SLUG_KEYS = ("slug", "eventSlug", "event_slug", "seoSlug", "seo_slug", "urlSlug", "url_slug", "permalink",
                  "handle", "path")

    def url_from_obj(self, obj: Dict[str, Any]) -> str:
        """Build an event URL from a JSON object that only carries a slug."""
        if not self.slug_url_template:
            return ""
        for key in self._SLUG_KEYS:
            v = obj.get(key)
            if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]*", v.strip("/")):
                url = self.slug_url_template.format(slug=v.strip("/"))
                if self.is_event_url(url):
                    return url
        return ""

    def slugs_for(self, city: City) -> Sequence[str]:
        return self.city_slugs.get(city.key, ())

    def seeds_for(self, city: Optional[City]) -> List[List[str]]:
        """Seed listing URLs: one group per template, alternatives = slug spellings."""
        groups: List[List[str]] = []
        for template in self.listing_templates:
            if "{slug}" in template:
                if city is None:
                    continue
                groups.append([template.format(slug=s) for s in self.slugs_for(city)])
            else:
                groups.append([template])
        return [g for g in groups if g]

    def follow_regexes(self, city: Optional[City]) -> List["re.Pattern[str]"]:
        out = []
        slugs = self.slugs_for(city) if city else ()
        for pattern in self.follow_patterns:
            if "{slug}" in pattern:
                if not slugs:
                    continue
                alts = "|".join(re.escape(s) for s in slugs)
                pattern = pattern.replace("{slug}", f"(?:{alts})")
            out.append(re.compile(pattern, re.I))
        return out

    def covers(self, city: City) -> bool:
        if self.fixed_city:
            return city.key == self.fixed_city
        if self.national:
            return True
        return bool(self.slugs_for(city))

    # --------------------------------------------------------- discovery
    def discover(self, ctx: RunContext, city: Optional[City]) -> List[Event]:
        """Partial events from the listing pages of ``city`` (None = national listing)."""
        found: Dict[str, Event] = {}
        follow_rx = self.follow_regexes(city)
        queue: deque = deque()
        seen: set = set()
        label = city.name if city else "all cities"
        if self.isolate_city_cookies:
            for template in self.listing_templates:
                ctx.fetcher.reset_cookies(host_of(template))

        loaded_before = self.stats.listing_pages
        for group in self.seeds_for(city):
            choice = self._slug_choice.get(city.key) if city else None
            if choice is not None and 0 < choice < len(group):
                group = [group[choice]] + group[:choice] + group[choice + 1:]
            page, events = self._first_working(ctx, group, city)
            if page is None:
                continue
            seen.add(canonical_url(page.url))
            self._collect(found, events)
            self._queue_follow(page, follow_rx, queue, seen)

        budget = self.max_listing_pages * max(1, ctx.listing_scale)
        empty_in_a_row = 0
        while queue and self.stats.listing_pages - loaded_before < budget:
            ctx.fetcher.check_stop()
            if ctx.listing_time_up():
                self.stats.listing_cut += 1
                break
            url = queue.popleft()
            page = self.load_listing(ctx, url)
            if page.html:
                before = len(found)
                self._collect(found, self.extract_listing(ctx, page, city))
                self._queue_follow(page, follow_rx, queue, seen)
                empty_in_a_row = 0 if len(found) > before else empty_in_a_row + 1
                if empty_in_a_row >= self.stop_after_empty_pages:
                    break
        pages = self.stats.listing_pages - loaded_before

        events = list(found.values())
        for ev in events:
            self.stamp(ev, city)
        ctx.log(f"{self.name} · {label}: {len(events)} listings from {pages} page(s)")
        self.stats.discovered += len(events)
        return events

    def _first_working(self, ctx: RunContext, alternatives: Sequence[str], city: Optional[City]):
        key = (alternatives[0], city.key if city else "")
        if key in self._working_slug:
            alternatives = [self._working_slug[key]]
        best = None
        for url in alternatives:
            page = self.load_listing(ctx, url, seed=True)
            if not page.html or page.status == 404:
                continue
            events = self.extract_listing(ctx, page, city)
            if events:
                self._working_slug[key] = url
                if city is not None:
                    spellings = [s for s in self.slugs_for(city) if s in url]
                    if spellings:
                        self._slug_choice[city.key] = list(self.slugs_for(city)).index(max(spellings, key=len))
                return page, events
            best = best or (page, events)
        return best if best else (None, [])

    def _queue_follow(self, page: PageData, follow_rx, queue: deque, seen: set) -> None:
        if not follow_rx:
            return
        hrefs = [h for h, _ in page.links]
        for href in hrefs:
            url = canonical_url(absolute_url(page.final_url or page.url, href))
            if url and url not in seen and any(rx.search(url) for rx in follow_rx) and not self.is_event_url(url):
                seen.add(url)
                queue.append(url)

    @staticmethod
    def _collect(found: Dict[str, Event], events: Iterable[Event]) -> None:
        for ev in events:
            key = ev.url or ev.source_id or ev.title
            if key in found:
                found[key].absorb(ev)
            else:
                found[key] = ev

    def stamp(self, ev: Event, city: Optional[City]) -> None:
        ev.source = self.key
        if self.key not in ev.sources:
            ev.sources.append(self.key)
        ev.platform = ev.platform or self.platform
        if ev.platform not in ev.platforms:
            ev.platforms.insert(0, ev.platform)
        if ev.url:
            ev.links.setdefault(ev.platform, ev.url)
            ev.source_id = ev.source_id or self.event_id(ev.url)
        if self.fixed_city:
            ev.city = next(c.name for c in CITIES if c.key == self.fixed_city)
        elif city is not None and not ev.city:
            ev.city = city.name

    # ------------------------------------------------------------ listing
    def load_listing(self, ctx: RunContext, url: str, seed: bool = False) -> PageData:
        if self._browser_first and ctx.use_browser and ctx.browser.available():
            r = ctx.browser.render(url, scroll=True, max_scrolls=self._scrolls(ctx))
            if r.ok:
                self.stats.listing_pages += 1
                self.stats.listing_pages_via_browser += 1
                self._save_debug(ctx, "listing-browser", url, r.html)
                return PageData(url=url, final_url=r.final_url or url, status=r.status or 200, html=r.html,
                                links=list(r.links), json_payloads=r.json_payloads, via_browser=True)
        res = ctx.fetcher.get(url, kind="listing")
        self.stats.listing_pages += 1
        page = PageData(url=url, final_url=res.final_url or url, status=res.status,
                        html=res.text if res.ok else "", error=res.error, blocked=res.blocked)
        if res.error and not res.ok:
            self.stats.error(f"{url}: {res.error}")
        if page.html:
            soup = soup_of(page.html)
            page.links = [(a.get("href"), "") for a in soup.find_all("a", href=True)]
            n_events = len({canonical_url(absolute_url(page.final_url, h)) for h, _ in page.links
                            if self.is_event_url(canonical_url(absolute_url(page.final_url, h)))})
        else:
            n_events = 0
        self._save_debug(ctx, "listing", url, page.html)
        want_browser = ctx.use_browser and self.listing_browser != "never" and res.status != 404 and (
            self.listing_browser == "always" or n_events < self.min_listing_events
            or (seed and self.browser_for_seeds))
        if want_browser and not ctx.fetcher.is_disabled(host_of(url)) and ctx.browser.available():
            r = ctx.browser.render(url, scroll=True, max_scrolls=self._scrolls(ctx))
            if r.ok:
                self.stats.listing_pages_via_browser += 1
                if res.blocked:
                    self._browser_first = True
                page.html, page.final_url, page.status = r.html, r.final_url or url, r.status or 200
                page.links = list(r.links) + page.links
                page.json_payloads = r.json_payloads
                page.via_browser = True
                page.error = ""
                self._save_debug(ctx, "listing-browser", url, r.html)
            elif r.error and r.error != "cancelled":
                self.stats.error(f"browser {url}: {r.error}")
        log.info("%s listing %s: HTTP %s, %d event links in the plain page, %s", self.name, url, page.status,
                 n_events, "filled in by the browser" if page.via_browser else "plain download")
        return page

    def _scrolls(self, ctx: RunContext) -> Optional[int]:
        return self.listing_scrolls * max(1, ctx.listing_scale) if self.listing_scrolls else None

    def extract_listing(self, ctx: RunContext, page: PageData, city: Optional[City]) -> List[Event]:
        soup = soup_of(page.html)
        base = page.final_url or page.url
        out: Dict[str, Event] = {}

        def add(ev: Event) -> None:
            if not ev.url or not self.is_event_url(ev.url) or not ev.title:
                return
            if ev.url in out:
                out[ev.url].absorb(ev)
            else:
                out[ev.url] = ev

        for d in jsonld_events(soup):
            add(event_from_jsonld(d, base, ctx.ref))
        walker = EventWalker(base, self.is_event_url, self.url_from_obj, self._json_id_rx, ctx.ref)
        for data in embedded_json(soup) + [p for _, p in page.json_payloads]:
            for ev in walker.walk(data):
                add(ev)
        for ev in listing_cards(soup, base, self.is_event_url, ctx.ref):
            add(ev)
        for href, text in page.links:
            url = canonical_url(absolute_url(base, href))
            if url and url not in out and self.is_event_url(url):
                first = next((ln for ln in (clean_text(x) for x in (text or "").split("\n")) if len(ln) > 2), "")
                add(Event(url=url, title=clean_title(first) or self.title_from_url(url)))
        if not out and self.discover_unknown_links:
            for ev in self.guess_event_links(soup, base):
                out.setdefault(ev.url, ev)
        return list(out.values())

    def title_from_url(self, url: str) -> str:
        path = url.rstrip("/").split("?")[0].split("/")
        slug = next((p for p in reversed(path) if p and not re.fullmatch(r"(?:ET)?\d+", p, re.I)), "")
        slug = re.sub(r"-buy-tickets$|-tickets-\d+$|-\d{6,}$", "", slug)
        return clean_title(slug.replace("-", " ").title())

    _NAV_WORDS = re.compile(
        r"about|contact|career|membership|member|donat|login|log-in|sign|register|privacy|terms|faq|gallery|"
        r"press|media|news|blog|shop|store|visit|hire|rental|venue|team|policy|support|subscribe|newsletter|"
        r"facebook|instagram|twitter|youtube|linkedin|whatsapp|cart|account|search|archive|past|history", re.I)
    _EVENTISH = re.compile(r"event|programme|program|show|play|performance|concert|whats-on|calendar|"
                           r"festival|exhibition|screening|recital|talk|workshop", re.I)

    def guess_event_links(self, soup, base: str) -> List[Event]:
        """Venue sites without a known URL scheme: same-site links that look like
        individual programme pages. Pages without a date are dropped later."""
        host = host_of(base)
        out: Dict[str, Event] = {}
        for a in soup.find_all("a", href=True):
            url = canonical_url(absolute_url(base, a.get("href")))
            text = clean_text(a.get_text(" "))
            if not url or host_of(url) != host or url == canonical_url(base):
                continue
            path = url.split(host, 1)[-1].split("?")[0]
            segments = [x for x in path.split("/") if x]
            if len(segments) < 2 or self._NAV_WORDS.search(path) or not (4 <= len(text) <= 120):
                continue
            if not self._EVENTISH.search(path):
                continue
            out.setdefault(url, Event(url=url, title=clean_title(text)))
            if len(out) >= 40:
                break
        return list(out.values())

    # ------------------------------------------------------------- detail
    def needs_detail(self, ev: Event) -> bool:
        """False when the listing already gave everything the sheet shows (saves a request)."""
        if not self.fetch_details or not ev.url:
            return False
        complete = (ev.start is not None and ev.has_time and bool(ev.venue)
                    and (ev.price_min is not None or ev.is_free)
                    and len(ev.description or "") >= 40
                    and (bool(ev.organizer) or not self.organizer_published))
        return not complete

    def alternate_urls(self, url: str) -> List[str]:
        """Other addresses the same event may live at, tried when its page is missing (override)."""
        return []

    def _browser_allowed(self, ctx: RunContext) -> bool:
        return (ctx.use_browser and self.detail_browser != "never"
                and self._browser_details < self.max_browser_details and ctx.browser.available())

    def _good(self, detail: Optional[Event]) -> bool:
        return detail is not None and bool(detail.start) and bool(detail.title)

    def enrich(self, ctx: RunContext, ev: Event) -> bool:
        """Fetch the event page and merge what it says into ``ev``. True when data was found."""
        if not ev.url:
            return False
        if self._browser_first and self._browser_allowed(ctx):
            self._browser_details += 1
            r = ctx.browser.render(ev.url, scroll=False)
            if r.ok:
                self._save_debug(ctx, "detail-browser", ev.url, r.html)
                detail = self.parse_detail(ctx, r.html, ev, r.final_url or ev.url, r.json_payloads)
                if detail is not None:
                    self._accept(ev, detail, r.final_url, verified=self._good(detail))
                    return True
        res = ctx.fetcher.get(ev.url, kind="detail")
        detail = None
        if res.ok and res.text:
            self._save_debug(ctx, "detail", ev.url, res.text)
            detail = self.parse_detail(ctx, res.text, ev, res.final_url or ev.url)
        final_url = res.final_url
        if not self._good(detail) and not res.blocked:
            # a guessed address (wrong section, renamed event): try the other places it can live
            for alt in self.alternate_urls(ev.url):
                r2 = ctx.fetcher.get(alt, kind="detail")
                if r2.ok and r2.text:
                    d2 = self.parse_detail(ctx, r2.text, ev, r2.final_url or alt)
                    if self._good(d2):
                        detail, final_url = d2, r2.final_url or alt
                        ev.url = canonical_url(alt)
                        ev.links[ev.platform] = ev.url
                        break
        if (not self._good(detail) and res.status not in (404, 410)
                and not ctx.fetcher.is_disabled(host_of(ev.url)) and self._browser_allowed(ctx)):
            self._browser_details += 1
            r = ctx.browser.render(ev.url, scroll=False)
            if r.ok:
                self._save_debug(ctx, "detail-browser", ev.url, r.html)
                detail = self.parse_detail(ctx, r.html, ev, r.final_url or ev.url, r.json_payloads)
                final_url = r.final_url or final_url
                if res.blocked and detail is not None:
                    self._browser_first = True
            elif r.error and r.error != "cancelled":
                self.stats.error(f"browser {ev.url}: {r.error}")
        if detail is None:
            self.stats.details_failed += 1
            if res.error:
                self.stats.error(f"{ev.url}: {res.error}")
            return False
        self._accept(ev, detail, final_url, verified=self._good(detail))
        return True

    def _accept(self, ev: Event, detail: Event, final_url: str, verified: bool) -> None:
        ev.absorb(detail, prefer_other=True)
        ev.detail_fetched = True
        self.stats.details_fetched += 1
        # keep the address the site itself settled on (after redirects / its canonical link)
        for cand in (detail.url, final_url):
            url = canonical_url(cand or "")
            if url and url != ev.url and self.is_event_url(url) and (self.event_id(url) or "") == (
                    self.event_id(ev.url) or ""):
                ev.url = url
                ev.links[ev.platform or self.platform] = url
                break
        if verified:
            ev.link_verified = True

    def _matches(self, cand: Event, ev: Event, page_url: str) -> bool:
        if cand.url and (cand.url == ev.url or cand.url == canonical_url(page_url)):
            return True
        if cand.source_id and cand.source_id == ev.source_id:
            return True
        return bool(ev.title and cand.title and titles_match(cand.title, ev.title))

    def parse_detail(self, ctx: RunContext, html: str, ev: Event, page_url: str,
                     payloads: Sequence[Tuple[str, Any]] = ()) -> Optional[Event]:
        soup = soup_of(html)
        detail = Event(url=ev.url)
        target_city = ev.city

        lds = jsonld_events(soup)
        cands: List[Tuple[Event, str]] = [(event_from_jsonld(d, page_url, ctx.ref), locality_of_jsonld(d)) for d in lds]
        cands += [(e, "") for e in microdata_events(soup, page_url, ctx.ref)]
        matching = [(c, loc) for c, loc in cands if self._matches(c, ev, page_url)]
        if not matching and len(cands) == 1:
            matching = cands
        if matching:
            def city_of(c: Event, loc: str) -> str:
                found = detect_city(loc, c.address, c.venue)
                return found.name if found else ""

            in_city = [(c, loc) for c, loc in matching if target_city and city_of(c, loc) == target_city]
            chosen = in_city or matching
            primary = min(chosen, key=lambda t: (t[0].start is None, t[0].start or 0))[0]
            detail.absorb(primary, prefer_other=True)
            starts = [c.start for c, _ in chosen if c.start]
            ends = [c.end or c.start for c, _ in chosen if (c.end or c.start)]
            if len(chosen) > 1 and starts:
                detail.start = min(starts)
                detail.end = max(ends) if max(ends) > detail.start else None
                for c, _ in chosen:
                    if c.start and c.start.date() not in detail.session_dates:
                        detail.session_dates.append(c.start.date())
                    for name in (c.organizer,):
                        if name and not detail.organizer:
                            detail.organizer = name
                    if c.price_min is not None:
                        detail.price_min = min(x for x in (detail.price_min, c.price_min) if x is not None)
                    if c.price_max is not None:
                        detail.price_max = max(x for x in (detail.price_max, c.price_max) if x is not None)

        walker = EventWalker(page_url, None, self.url_from_obj, self._json_id_rx, ctx.ref)
        for data in embedded_json(soup) + [p for _, p in payloads]:
            for cand in walker.walk(data):
                if self._matches(cand, ev, page_url) or (detail.title and titles_match(cand.title, detail.title)):
                    detail.absorb(cand)

        meta = meta_tags(soup)
        # the page's own address for itself (canonical link / og:url / its JSON-LD), when it is an event page
        for own in [c.url for c, _ in matching[:1]] + [meta.get("canonical", ""), meta.get("og:url", "")]:
            own = canonical_url(absolute_url(page_url, own)) if own else ""
            if own and self.is_event_url(own) and own != detail.url:
                detail.url = own
                break
        if not detail.title:
            detail.title = clean_title(meta.get("og:title") or meta.get("h1") or meta.get("title") or "")
        if not detail.description:
            detail.description = meta.get("og:description") or meta.get("description") or ""
        if not detail.image:
            detail.image = meta.get("og:image", "")

        lines = text_lines(soup.body or soup)
        quick = pipe_facts(lines)
        for key in ("language", "duration", "age_limit"):
            if quick.get(key) and not getattr(detail, key):
                setattr(detail, key, quick[key])
        for cat in quick.get("categories", []):
            if cat not in detail.categories:
                detail.categories.append(cat)
        facts = labeled_facts(lines)
        if not detail.organizer and facts.get("organizer") and not is_platform_name(facts["organizer"]):
            detail.organizer = facts["organizer"]
        if not detail.register_by:
            detail.register_by = register_by_from_text(lines, ctx.ref)
        for key in ("language", "duration", "age_limit"):
            if facts.get(key) and not getattr(detail, key):
                setattr(detail, key, facts[key])
        if facts.get("genre"):
            detail.categories.extend(g for g in re.split(r"\s*[,|/]\s*", facts["genre"]) if g and g not in detail.categories)
        if not detail.venue and facts.get("venue"):
            detail.venue = facts["venue"]
        if detail.price_min is None and not detail.is_free:
            lo, hi, free = page_price(lines)
            if lo is not None or free:
                detail.price_min, detail.price_max, detail.is_free = lo, hi, free
        self.refine_detail(detail, soup, lines, meta)
        if not (detail.title or detail.start or detail.venue):
            return None
        return detail

    def refine_detail(self, detail: Event, soup, lines: List[str], meta: Dict[str, str]) -> None:
        """Site-specific touch-ups (override)."""

    # -------------------------------------------------------------- debug
    def _save_debug(self, ctx: RunContext, kind: str, url: str, html: str) -> None:
        if ctx.debug_dir is None or not html:
            return
        try:
            folder = ctx.debug_dir / self.key
            folder.mkdir(parents=True, exist_ok=True)
            existing = len(list(folder.glob(f"{kind}-*.html")))
            if existing >= (60 if kind.startswith("listing") else 12):   # every list page, a sample of events
                return
            name = f"{kind}-{existing + 1:02d}-{slugify(url)[-80:]}.html"
            (folder / name).write_text(f"<!-- {url} -->\n" + html, encoding="utf-8")
        except OSError:
            pass
