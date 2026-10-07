"""BookMyShow (in.bookmyshow.com) - events, plays and sports for each city."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import Event
from .base import Source, slugify


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
    # category/filter pages of the same city, e.g. /explore/comedy-shows-mumbai
    follow_patterns = (
        r"^https://in\.bookmyshow\.com/explore/(?!movies|activities|home|cinemas|buzz|offers)[a-z0-9-]+-{slug}(?:\?[^#]*)?$",
    )
    event_pattern = r"^https?://in\.bookmyshow\.com/(?:[a-z0-9-]+/)?(?:events|plays|sports|activities)/[^/?#]+/ET\d{5,}"
    id_pattern = r"/(ET\d{5,})"
    json_id_pattern = r"ET\d{5,}"
    max_listing_pages = 14

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
        return f"https://in.bookmyshow.com/events/{slug.strip('/')}/{code}"

    def refine_detail(self, detail: Event, soup, lines: List[str], meta: Dict[str, str]) -> None:
        if "/plays/" in detail.url and "Plays" not in detail.categories:
            detail.categories.append("Plays")
        elif "/sports/" in detail.url and "Sports" not in detail.categories:
            detail.categories.append("Sports")
