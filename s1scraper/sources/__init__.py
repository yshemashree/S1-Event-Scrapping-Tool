"""Registry of every site the scraper knows."""

from __future__ import annotations

from typing import Dict, List, Type

from .base import PLATFORM_DOMAINS, RunContext, Source, SourceStats, platform_for_url
from .bookmyshow import BookMyShow
from .district import District
from .sites import (
    NCPA, NMACC, AllEvents, BangaloreInternationalCentre, DreamSetGo, Eventbrite, Explara, Fever,
    IndiaHabitatCentre, IndiaInternationalCentre, JioWorldCentre, LBB, Meetup, Platinumlist,
    PrithviTheatre, RangaShankara, Skillbox, SortMyScene,
)

ALL_SOURCES: List[Type[Source]] = [
    BookMyShow, District, AllEvents,
    Skillbox, SortMyScene, Eventbrite, Fever, Meetup,
    NCPA, NMACC, JioWorldCentre, PrithviTheatre, RangaShankara, IndiaHabitatCentre,
    IndiaInternationalCentre, BangaloreInternationalCentre,
    Explara, LBB, DreamSetGo, Platinumlist,
]
SOURCE_BY_KEY: Dict[str, Type[Source]] = {cls.key: cls for cls in ALL_SOURCES}
GROUP_LABELS = {
    "core": "Main platforms",
    "more": "More platforms",
    "venues": "Venue websites",
    "optional": "Optional (off by default)",
}

__all__ = [
    "ALL_SOURCES", "SOURCE_BY_KEY", "GROUP_LABELS", "PLATFORM_DOMAINS", "RunContext", "Source",
    "SourceStats", "platform_for_url",
]
