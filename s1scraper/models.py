"""The Event record every source produces and the Excel writer consumes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

# Order of preference when the same event is listed on several platforms: the
# record from the earliest platform here supplies the primary link and fields.
PLATFORM_PRIORITY = [
    "BookMyShow", "Zomato District", "Skillbox", "SortMyScene", "Eventbrite", "Fever",
    "Meetup", "Explara", "DreamSetGo", "Platinumlist", "NCPA", "NMACC", "Jio World Centre",
    "Prithvi Theatre", "Ranga Shankara", "India Habitat Centre", "India International Centre",
    "Bangalore International Centre", "LBB", "AllEvents",
]


def platform_rank(platform: str) -> int:
    try:
        return PLATFORM_PRIORITY.index(platform)
    except ValueError:
        return len(PLATFORM_PRIORITY)


@dataclass
class Event:
    title: str = ""
    url: str = ""                 # page on the source site (canonical)
    source: str = ""              # source key that found it, e.g. "bookmyshow"
    platform: str = ""            # ticket platform display name, e.g. "BookMyShow"
    source_id: str = ""           # platform id, e.g. "ET00412345"
    city: str = ""                # display name from cities.CITIES
    start: Optional[datetime] = None   # naive, Indian Standard Time
    end: Optional[datetime] = None
    has_time: bool = False        # start carries a real show time
    date_text: str = ""           # raw date text when it could not be parsed
    session_dates: List[date] = field(default_factory=list)
    venue: str = ""
    address: str = ""
    organizer: str = ""
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    is_free: bool = False
    price_text: str = ""
    ticket_types: List[Tuple[str, float]] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    description: str = ""
    language: str = ""
    age_limit: str = ""
    duration: str = ""
    performers: List[str] = field(default_factory=list)
    image: str = ""
    status: str = ""              # Cancelled / Postponed / Sold out / ...
    online: bool = False
    # Filled in by the pipeline
    activity_type: str = ""
    tier: str = ""
    notes: str = ""
    platforms: List[str] = field(default_factory=list)
    links: Dict[str, str] = field(default_factory=dict)   # platform -> buy-tickets url
    ticket_url: str = ""          # where tickets are sold, when different from url
    detail_fetched: bool = False
    sources: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ keys
    def identity(self) -> str:
        """Stable id of this listing on its own platform, per city."""
        ident = self.source_id or self.url
        return f"{self.platform or self.source}|{ident}|{self.city}".lower()

    @property
    def start_date(self) -> Optional[date]:
        return self.start.date() if self.start else None

    @property
    def end_date(self) -> Optional[date]:
        if self.end:
            return self.end.date()
        return self.start.date() if self.start else None

    @property
    def buy_link(self) -> str:
        """Best link for buying tickets: the primary platform's page."""
        if self.platforms:
            link = self.links.get(self.platforms[0])
            if link:
                return link
        return self.ticket_url or self.url

    # ----------------------------------------------------------------- merge
    def absorb(self, other: "Event", prefer_other: bool = False) -> None:
        """Fill this record's gaps from ``other``.

        With ``prefer_other`` the other record's non-empty values win; that is
        used when a detail page refines what a listing card showed.
        """
        scalar_fields = (
            "title", "venue", "address", "organizer", "description", "language",
            "age_limit", "duration", "image", "status", "date_text", "price_text", "ticket_url",
        )
        # an id only means something on its own platform
        if other.source_id and not self.source_id and other.platform in ("", self.platform):
            self.source_id = other.source_id
        for name in scalar_fields:
            mine, theirs = getattr(self, name), getattr(other, name)
            if theirs and (prefer_other or not mine):
                setattr(self, name, theirs)
        if other.start and (prefer_other or not self.start):
            self.start, self.has_time = other.start, other.has_time
            if other.end:
                self.end = other.end
            elif self.end and self.end < self.start:
                self.end = None
        if other.end and not self.end:
            self.end = other.end
        if other.price_min is not None and (prefer_other or self.price_min is None):
            self.price_min = other.price_min
        if other.price_max is not None and (prefer_other or self.price_max is None):
            self.price_max = other.price_max
        self.is_free = self.is_free or other.is_free
        if other.online and (prefer_other or not self.venue):
            self.online = other.online
        for name in ("categories", "performers", "ticket_types", "session_dates"):
            mine = getattr(self, name)
            for item in getattr(other, name):
                if item not in mine:
                    mine.append(item)
        for platform in other.platforms:
            if platform not in self.platforms:
                self.platforms.append(platform)
        for platform, link in other.links.items():
            self.links.setdefault(platform, link)
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)
        self.detail_fetched = self.detail_fetched or other.detail_fetched
