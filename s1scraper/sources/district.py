"""District by Zomato (district.in, which also absorbed Paytm Insider)."""

from __future__ import annotations

import re

from .base import Source

# Listing/landing pages share the /events/ prefix with event pages:
#   /events/upcoming-events-in-pune, /events/nightlife-this-weekend-in-mumbai
_LISTING_SLUG = re.compile(
    r"(?:^|-)in-[a-z-]+$|^upcoming-|^events-|this-weekend|^things-to-do|^best-|^top-|-near-me$|^all-",
)
_DATED_SLUG = re.compile(r"-(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{1,2}-20\d\d(?:-|$)")


class District(Source):
    key = "district"
    name = "District (Zomato)"
    platform = "Zomato District"
    group = "core"
    description = "Zomato's going-out app (includes former Paytm Insider): concerts, comedy, nightlife."

    city_slugs = {
        "mumbai": ("mumbai",),
        "pune": ("pune",),
        "delhi": ("delhi-ncr", "delhi", "new-delhi", "ncr", "gurugram"),
        "bengaluru": ("bengaluru", "bangalore"),
        "kolkata": ("kolkata",),
        "ahmedabad": ("ahmedabad",),
        "chennai": ("chennai",),
    }
    listing_templates = ("https://www.district.in/events/upcoming-events-in-{slug}",)
    follow_patterns = (
        r"^https://www\.district\.in/events/[a-z0-9-]+-in-{slug}(?:\?[^#]*)?$",
    )
    event_pattern = r"^https?://(?:www\.)?district\.in/events/[a-z0-9][a-z0-9-]*[a-z0-9]$"
    id_pattern = r"/events/([a-z0-9-]+)$"
    slug_url_template = "https://www.district.in/events/{slug}"
    max_listing_pages = 14

    def is_event_url(self, url: str) -> bool:
        if not super().is_event_url(url):
            return False
        slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
        if slug.endswith("-buy-tickets") or _DATED_SLUG.search(slug):
            return True
        return not _LISTING_SLUG.search(slug)
