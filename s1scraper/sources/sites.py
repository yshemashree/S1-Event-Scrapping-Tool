"""The other platforms and venue websites from the Guide's ticket platform list.

These are declared as URL profiles over the shared crawler. Venue sites set
``fixed_city`` and fall back to recognising programme pages by their links
when their URL scheme is not the one listed here.
"""

from __future__ import annotations

from .base import Source

_STD_SLUGS = {
    "mumbai": ("mumbai",),
    "pune": ("pune",),
    "delhi": ("delhi", "new-delhi"),
    "bengaluru": ("bangalore", "bengaluru"),
    "kolkata": ("kolkata",),
    "ahmedabad": ("ahmedabad",),
    "chennai": ("chennai",),
}


# ------------------------------------------------------------------ platforms

class AllEvents(Source):
    key = "allevents"
    name = "AllEvents"
    platform = "AllEvents"
    group = "core"
    description = "Aggregator listing events from many organisers and platforms; good for organiser names."
    city_slugs = _STD_SLUGS
    listing_templates = ("https://allevents.in/{slug}/all",)
    follow_patterns = (
        r"^https://allevents\.in/{slug}/(?:all|music|concerts|comedy|theatre|theater|art|arts|food-drinks|"
        r"sports|workshops|festivals|parties|performances|exhibitions|dance)(?:\?page=\d+)?$",
    )
    event_pattern = r"^https?://allevents\.in/[a-z0-9-]+/[^/?#]+/\d{6,}"
    id_pattern = r"/(\d{6,})(?:[/?#]|$)"
    url_city_pattern = r"^https?://allevents\.in/([a-z0-9-]+)/"   # its Mumbai list also shows Mehsana etc.
    max_listing_pages = 10


class Skillbox(Source):
    key = "skillbox"
    name = "Skillbox"
    platform = "Skillbox"
    description = "Indie music, comedy, nightlife and curated live experiences."
    national = True
    listing_templates = ("https://www.skillboxes.com/events",)
    follow_patterns = (r"^https://www\.skillboxes\.com/events\?(?:page|p)=\d+$",)
    event_pattern = r"^https?://(?:www\.)?skillboxes\.com/events?/[^/?#]+$"
    exclude_pattern = r"/events?/(?:page|category|categories|city|search|tag)(?:/|$)"
    max_listing_pages = 8


class SortMyScene(Source):
    key = "sortmyscene"
    name = "SortMyScene"
    platform = "SortMyScene"
    description = "Local and niche events: open mics, poetry, small concerts, nightlife."
    national = True
    listing_templates = ("https://sortmyscene.com/", "https://sortmyscene.com/events")
    event_pattern = r"^https?://(?:www\.)?sortmyscene\.com/(?:events?|e)/[^/?#]+$"
    max_listing_pages = 6


class Eventbrite(Source):
    key = "eventbrite"
    name = "Eventbrite"
    platform = "Eventbrite"
    description = "Workshops, meetups, conferences and community events."
    city_slugs = {
        "mumbai": ("india--mumbai",),
        "pune": ("india--pune",),
        "delhi": ("india--new-delhi", "india--delhi"),
        "bengaluru": ("india--bengaluru", "india--bangalore"),
        "kolkata": ("india--kolkata",),
        "ahmedabad": ("india--ahmedabad",),
        "chennai": ("india--chennai",),
    }
    listing_templates = ("https://www.eventbrite.com/d/{slug}/all-events/",)
    follow_patterns = (r"^https://www\.eventbrite\.(?:com|co\.in)/d/{slug}/all-events/?\?page=\d+$",)
    event_pattern = r"^https?://www\.eventbrite\.(?:com|co\.in|co\.uk|com\.au|ca|ie)/e/[^/?#]+"
    id_pattern = r"-(\d{9,})(?:[/?#]|$)"
    max_listing_pages = 6


class Fever(Source):
    key = "fever"
    name = "Fever"
    platform = "Fever"
    description = "Candlelight concerts and immersive experiences."
    city_slugs = _STD_SLUGS
    listing_templates = ("https://feverup.com/en/{slug}",)
    event_pattern = r"^https?://feverup\.com/(?:[a-z]{2}/[a-z-]+/)?m/\d+"
    id_pattern = r"/m/(\d+)"
    max_listing_pages = 4


class Meetup(Source):
    key = "meetup"
    name = "Meetup"
    platform = "Meetup"
    description = "Community meetups (mostly free)."
    city_slugs = {
        "mumbai": ("in--Mumbai",),
        "pune": ("in--Pune",),
        "delhi": ("in--New%20Delhi", "in--Delhi"),
        "bengaluru": ("in--Bangalore", "in--Bengaluru"),
        "kolkata": ("in--Kolkata",),
        "ahmedabad": ("in--Ahmedabad",),
        "chennai": ("in--Chennai",),
    }
    listing_templates = ("https://www.meetup.com/find/?location={slug}&source=EVENTS",)
    event_pattern = r"^https?://www\.meetup\.com/[^/]+/events/\d+"
    id_pattern = r"/events/(\d+)"
    max_listing_pages = 2
    listing_browser = "always"
    listing_scrolls = 10             # its feed scrolls without end; ten screens cover the next weeks


class Explara(Source):
    key = "explara"
    name = "Explara"
    platform = "Explara"
    group = "optional"
    enabled_by_default = False
    description = "Conferences, marathons and business events."
    national = True
    listing_templates = ("https://www.explara.com/events",)
    event_pattern = r"^https?://(?:www\.)?explara\.com/e/[^/?#]+"
    max_listing_pages = 4


class LBB(Source):
    key = "lbb"
    name = "LBB (Little Black Book)"
    platform = "LBB"
    group = "optional"
    enabled_by_default = False
    description = "City discovery guide with event write-ups."
    city_slugs = {k: v for k, v in _STD_SLUGS.items() if k != "ahmedabad"}
    listing_templates = ("https://lbb.in/{slug}/events/",)
    event_pattern = r"^https?://lbb\.in/[a-z]+/(?!events/?$)[^/?#]+/?$"
    max_listing_pages = 3


class DreamSetGo(Source):
    key = "dreamsetgo"
    name = "DreamSetGo"
    platform = "DreamSetGo"
    group = "optional"
    enabled_by_default = False
    description = "Luxury sports travel and experience packages."
    national = True
    listing_templates = ("https://www.dreamsetgo.com/",)
    event_pattern = r"^https?://(?:www\.)?dreamsetgo\.com/(?:experiences?|packages?|events?|tickets?)/[^/?#]+"
    max_listing_pages = 4


class Platinumlist(Source):
    key = "platinumlist"
    name = "Platinumlist"
    platform = "Platinumlist"
    group = "optional"
    enabled_by_default = False
    description = "Middle East ticketing; occasional India listings."
    national = True
    listing_templates = ("https://platinumlist.net/event-tickets",)
    event_pattern = r"^https?://(?:[a-z-]+\.)?platinumlist\.net/event-tickets/\d+/[^/?#]+"
    id_pattern = r"/event-tickets/(\d+)/"
    max_listing_pages = 4


# --------------------------------------------------------------------- venues

class _Venue(Source):
    group = "venues"
    discover_unknown_links = True
    max_listing_pages = 4


class NCPA(_Venue):
    key = "ncpa"
    name = "NCPA Mumbai"
    platform = "NCPA"
    fixed_city = "mumbai"
    listing_templates = ("https://www.ncpamumbai.com/whats-on", "https://www.ncpamumbai.com/events")
    event_pattern = r"^https?://(?:www\.)?ncpamumbai\.com/(?:events?|shows?|whats-on)/[^/?#]+$"


class NMACC(_Venue):
    key = "nmacc"
    name = "NMACC"
    platform = "NMACC"
    fixed_city = "mumbai"
    listing_templates = ("https://nmacc.com/events", "https://nmacc.com/whats-on")
    event_pattern = r"^https?://(?:www\.)?nmacc\.com/(?:events?|shows?|whats-on|performances?)/[^/?#]+$"


class JioWorldCentre(_Venue):
    key = "jioworldcentre"
    name = "Jio World Centre"
    platform = "Jio World Centre"
    fixed_city = "mumbai"
    listing_templates = ("https://www.jioworldcentre.com/events",)
    event_pattern = r"^https?://(?:www\.)?jioworldcentre\.com/events?/[^/?#]+$"


class PrithviTheatre(_Venue):
    key = "prithvi"
    name = "Prithvi Theatre"
    platform = "Prithvi Theatre"
    fixed_city = "mumbai"
    listing_templates = ("https://prithvitheatre.org/",)
    event_pattern = r"^https?://(?:www\.)?prithvitheatre\.org/(?:plays?|events?|shows?|productions?|performances?)/[^/?#]+/?$"


class RangaShankara(_Venue):
    key = "rangashankara"
    name = "Ranga Shankara"
    platform = "Ranga Shankara"
    fixed_city = "bengaluru"
    listing_templates = ("https://rangashankara.org/",)
    event_pattern = r"^https?://(?:www\.)?rangashankara\.org/(?:plays?|events?|shows?|productions?|performances?)/[^/?#]+/?$"


class IndiaHabitatCentre(_Venue):
    key = "ihc"
    name = "India Habitat Centre"
    platform = "India Habitat Centre"
    fixed_city = "delhi"
    listing_templates = ("https://www.indiahabitat.org/ihc-programmes", "https://www.indiahabitat.org/")
    event_pattern = r"^https?://(?:www\.)?indiahabitat\.org/(?:ihc-programmes?|programmes?|events?)/[^/?#]+"


class IndiaInternationalCentre(_Venue):
    key = "iic"
    name = "India International Centre"
    platform = "India International Centre"
    fixed_city = "delhi"
    listing_templates = ("https://iicdelhi.in/programmes", "https://iicdelhi.in/")
    event_pattern = r"^https?://(?:www\.)?iicdelhi\.in/programmes?/[^/?#]+"


class BangaloreInternationalCentre(_Venue):
    key = "bic"
    name = "Bangalore International Centre"
    platform = "Bangalore International Centre"
    fixed_city = "bengaluru"
    listing_templates = ("https://bangaloreinternationalcentre.org/events/",)
    event_pattern = r"^https?://(?:www\.)?bangaloreinternationalcentre\.org/events?/[^/?#]+/?$"
