"""BookMyShow (in.bookmyshow.com) - events, plays and sports for each city."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..cities import City
from ..models import Event
from .base import PageData, RunContext, Source, slugify

_KINDS = ("events", "plays", "sports", "activities")
_EVENT_URL = re.compile(r"^https?://in\.bookmyshow\.com/(?:[a-z0-9-]+/)?(events|plays|sports|activities)/([^/?#]+)/(ET\d{5,})")


class BookMyShow(Source):
    key = "bookmyshow"
    name = "BookMyShow"
    platform = "BookMyShow"
    group = "core"
    description = "Largest ticketing platform: concerts, comedy, theatre, sports, workshops."

    city_slugs = {
        "mumbai": ("mumbai",),
        "pune": ("pune",),
        "delhi": ("national-capital-region-ncr",),
        "bengaluru": ("bengaluru",),
        "kolkata": ("kolkata",),
        "ahmedabad": ("ahmedabad",),
        "chennai": ("chennai",),
    }
    listing_templates = (
        "https://in.bookmyshow.com/explore/events-{slug}",
        "https://in.bookmyshow.com/explore/plays-{slug}",
        "https://in.bookmyshow.com/explore/sports-{slug}",
    )
    # category pages of the same city, e.g. /explore/comedy-shows-mumbai (not its endless filter
    # combinations such as ?daygroups=...&languages=..., which only repeat the same events)
    follow_patterns = (
        r"^https://in\.bookmyshow\.com/explore/(?!movies|activities|home|cinemas|buzz|offers)[a-z0-9-]+-{slug}(?:\?page=\d+)?$",
    )
    event_pattern = r"^https?://in\.bookmyshow\.com/(?:[a-z0-9-]+/)?(?:events|plays|sports|activities)/[^/?#]+/ET\d{5,}"
    id_pattern = r"/(ET\d{5,})"
    json_id_pattern = r"ET\d{5,}"
    max_listing_pages = 14
    # BookMyShow turns away automated browsers and is quick to refuse busy visitors: its event pages
    # are read with normal downloads only, at a wider gap than other sites, and it never names
    # organisers on its pages.
    detail_browser = "never"
    # Its city lists send only the first events in the page itself and load the rest as you scroll
    # (a run on 9 Oct found 179 events for Mumbai but only about 20 for each other city).
    browser_for_seeds = True
    isolate_city_cookies = True      # it remembers the last city in a cookie and shows that city's events
    organizer_published = False
    min_gap = (2.5, 5.0)
    _kind = "events"                 # section of the listing being read: events / plays / sports

    def extract_listing(self, ctx: RunContext, page: PageData, city: Optional[City]) -> List[Event]:
        m = re.search(r"/explore/(events|plays|sports|activities)-", page.final_url or page.url)
        self._kind = m.group(1) if m else "events"
        return super().extract_listing(ctx, page, city)

    def alternate_urls(self, url: str) -> List[str]:
        """A play listed under /events/ (or the other way round): the same code in the other sections."""
        m = _EVENT_URL.match(url)
        if not m:
            return []
        kind, slug, code = m.groups()
        return [f"https://in.bookmyshow.com/{k}/{slug}/{code}" for k in _KINDS if k != kind
                and (k != "activities" or self.include_activities)]

    def is_event_url(self, url: str) -> bool:
        if not super().is_event_url(url):
            return False
        # Permanent attractions (water parks, gaming zones...) live under /activities/
        return self.include_activities or "/activities/" not in url

    def url_from_obj(self, obj: Dict[str, Any]) -> str:
        code = ""
        for k in ("eventCode", "event_code", "EventCode", "code", "id", "eventId"):
            v = obj.get(k)
            if isinstance(v, str) and re.fullmatch(r"ET\d{5,}", v):
                code = v
                break
        if not code:
            return super().url_from_obj(obj)
        slug = obj.get("slug") or obj.get("eventSlug") or obj.get("seoSlug") or ""
        if not isinstance(slug, str) or not slug.strip():
            title = obj.get("title") or obj.get("name") or obj.get("eventName") or ""
            slug = slugify(title if isinstance(title, str) else "") or "event"
        # the section matters: a play's page lives under /plays/, a match under /sports/
        return f"https://in.bookmyshow.com/{self._kind}/{slug.strip('/')}/{code}"

    def refine_detail(self, detail: Event, soup, lines: List[str], meta: Dict[str, str]) -> None:
        if "/plays/" in detail.url and "Plays" not in detail.categories:
            detail.categories.append("Plays")
        elif "/sports/" in detail.url and "Sports" not in detail.categories:
            detail.categories.append("Sports")
