"""Assign the Guide's Activity Type and Tier to each event."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

# The eight categories of the Guide's colour key, plus two buckets for listings
# that fit none of them (kept separate so they are easy to filter out).
ACTIVITY_TYPES = [
    "Culture", "Art", "Music", "F&B", "Sports to Watch", "Recreational Sports", "Theatre",
    "Comedy", "Workshops", "Other",
]

TIERS = ["Ultra Premium", "Luxury", "Premium", "Standard"]
TIER_UNKNOWN = "TBC"

# Tier thresholds from the Guide sheet, per ticket in INR:
#   Ultra Premium ₹5,000+ | Luxury ₹1,550–5,000 | Premium ₹500–1,500 | Standard < ₹500 / Free
# The Guide leaves ₹1,501–1,549 unassigned; it is treated as Premium.
ULTRA_PREMIUM_FROM = 5000
LUXURY_FROM = 1550
PREMIUM_FROM = 500


def classify_tier(price_min: Optional[float], price_max: Optional[float], is_free: bool,
                  basis: str = "min") -> str:
    """Tier from the ticket price.

    ``basis="min"`` (default) uses the cheapest ticket - the entry price, which
    is all most listings publish. ``"max"`` uses the top ticket (VIP), and
    ``"avg"`` the midpoint of the two.
    """
    lo = 0.0 if is_free else price_min
    hi = price_max if price_max is not None else price_min
    if lo is None and hi is None:
        return TIER_UNKNOWN
    if lo is None:
        lo = hi
    if hi is None or hi < lo:
        hi = lo
    if basis == "max":
        price = hi
    elif basis == "avg":
        price = (lo + hi) / 2
    else:
        price = lo
    if price >= ULTRA_PREMIUM_FROM:
        return "Ultra Premium"
    if price >= LUXURY_FROM:
        return "Luxury"
    if price >= PREMIUM_FROM:
        return "Premium"
    return "Standard"


def _rx(*words: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z0-9])", re.I)


# First match wins; order puts the specific before the generic (a "food
# festival" is F&B, a "comedy workshop" is a workshop, "festival" alone is Culture).
_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Workshops", _rx(
        r"workshops?", r"master\s?class(?:es)?", r"bootcamps?", r"crash course", r"certificate course",
        r"short course", r"classes",
        r"hands[\s-]on", r"diy", r"pottery", r"candle making", r"resin art", r"terrarium",
        r"calligraphy", r"crochet", r"embroidery", r"learn to \w+", r"training", r"skill session",
    )),
    ("Comedy", _rx(
        r"comedy", r"comedians?", r"comic", r"stand[\s-]?up", r"standup", r"improv", r"roast",
        r"sketch show", r"hasya", r"funny", r"laugh(?:ter)? (?:club|riot)",
    )),
    ("Theatre", _rx(
        r"theat(?:re|er|rical)", r"plays?", r"drama", r"natak", r"naatak", r"nataka",
        r"the musical", r"a musical", r"musical play", r"broadway", r"stage play", r"monologues?",
        r"immersive theat(?:re|er)", r"puppetry", r"mime", r"tamasha", r"lavani", r"yakshagana",
    )),
    ("Sports to Watch", _rx(
        r"cricket", r"ipl", r"isl", r"football match", r"football", r"kabaddi", r"hockey",
        r"badminton", r"tennis", r"formula ?1", r"f1", r"motogp", r"grand prix", r"ufc", r"wwe",
        r"boxing", r"mma", r"wrestling", r"e-?sports", r"basketball", r"volleyball",
        r"t20", r"odi", r"test match", r"premier league", r"pro league", r"league match",
        r"sports? screening", r"live screening", r"match screening", r"spectator", r"rugby",
        r"nrl", r"afl", r"grand final", r"state of origin", r"athletics", r"vs\.?", r"v/s",
        r"fight night", r"olympics?", r"(?:asian|commonwealth|olympic|national|khelo india)[\w &]{0,30} games",
    )),
    ("Recreational Sports", _rx(
        r"marathon", r"half marathon", r"\d{1,2}\s?k\s?(?:run|walk)", r"fun run", r"cyclothon",
        r"cycling", r"cycle ride", r"treks?", r"trekking", r"hikes?", r"hiking", r"yoga",
        r"fitness", r"workout", r"zumba", r"golf", r"swimming", r"triathlon", r"pickleball",
        r"kayaking", r"climbing", r"bouldering", r"skating", r"paragliding", r"camping",
        r"adventure", r"run club", r"running", r"turf", r"futsal", r"run(?=\s+20\d\d)",
        r"(?:city|fun|charity|colou?r|heritage|pink|women'?s|freedom|monsoon|midnight) run",
    )),
    ("F&B", _rx(
        r"food", r"foodie", r"drinks?", r"brunch", r"dining", r"dinner", r"lunch", r"chefs?",
        r"wine", r"beer", r"brew(?:ery|eries)?", r"cocktails?", r"whisk(?:e)?y", r"gin", r"tequila",
        r"tasting", r"culinary", r"cuisine", r"mixology", r"buffet", r"feast", r"supper club",
        r"bar takeover", r"pop-?up kitchen", r"high tea", r"coffee", r"bakery", r"food & drinks",
    )),
    ("Art", _rx(
        r"(?<!martial )arts?", r"artist", r"exhibitions?", r"gallery", r"galleries", r"museums?",
        r"paintings?", r"sculptures?", r"photography", r"installations?", r"art fair", r"biennale",
        r"visual arts", r"retrospective", r"solo show", r"group show", r"curated show",
    )),
    ("Music", _rx(
        r"music", r"musical night", r"concerts?", r"gigs?", r"dj", r"djs", r"edm", r"techno",
        r"house music", r"hip[\s-]?hop", r"rap", r"bands?", r"orchestra", r"symphony", r"jazz",
        r"blues", r"rock", r"metal", r"indie", r"qawwali", r"orchestra(?:l)?", r"symphon(?:y|ic)",
        r"tribute", r"dj sets?", r"weekender", r"residency", r"ghazal", r"sufi", r"classical music",
        r"carnatic", r"hindustani", r"bollywood night", r"karaoke", r"nightlife", r"party",
        r"parties", r"rave", r"club night", r"singers?", r"unplugged", r"acoustic", r"live music",
        r"open air", r"festival of music", r"bhajan clubbing", r"kirtan", r"recital", r"choir",
        r"music shows?", r"sunburn", r"lollapalooza", r"gig",
    )),
    ("Culture", _rx(
        r"cultur(?:e|al)", r"heritage", r"literature", r"literary", r"fairs?", r"parv", r"jayanti",
        r"lit fest", r"books?", r"poetry", r"poets?", r"storytelling", r"spoken word", r"mushaira",
        r"kavi", r"sammelan", r"spiritual(?:ity)?", r"devotional", r"satsang", r"garba",
        r"dandiya", r"navratri", r"diwali", r"holi", r"durga puja", r"ganesh", r"dance",
        r"kathak", r"bharatanatyam", r"odissi", r"kuchipudi", r"talks?", r"lectures?", r"screenings?",
        r"film", r"films", r"cinema", r"heritage walks?", r"walking tours?", r"walks?",
        r"performances?", r"mela", r"utsav",
    )),
    ("Other", _rx(
        r"kids", r"children", r"family", r"conferences?", r"summit", r"expo", r"exhibitors?",
        r"trade (?:fair|show)", r"meetups?", r"networking", r"business", r"startups?", r"career",
        r"seminars?", r"webinars?", r"pets?", r"dogs?", r"gaming", r"quiz",
        r"flea market", r"pop-?up market", r"sale", r"awards?",
    )),
]

# schema.org Event subtypes map straight onto categories
_SCHEMA_TYPES = {
    "comedyevent": "Comedy", "musicevent": "Music", "theaterevent": "Theatre",
    "sportsevent": "Sports to Watch", "foodevent": "F&B", "exhibitionevent": "Art",
    "visualartsevent": "Art", "danceevent": "Culture", "literaryevent": "Culture",
    "festival": "Culture", "educationevent": "Workshops", "childrensevent": "Other",
    "businessevent": "Other", "screeningevent": "Culture",
}


def schema_type_category(types: Iterable[str]) -> Optional[str]:
    for t in types:
        cat = _SCHEMA_TYPES.get(str(t).lower().rsplit("/", 1)[-1])
        if cat:
            return cat
    return None


# Generic platform labels ("Sports", "Performances") only decide when neither
# the categories nor the title said anything more specific.
_WEAK_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Sports to Watch", _rx(r"sports?", r"matches", r"match", r"tournaments?", r"league", r"championships?")),
    ("Music", _rx(r"tours?", r"live in", r"live at", r"live", r"world tour", r"in concert")),
    ("Culture", _rx(r"festivals?", r"fests?", r"celebrations?", r"performances?", r"shows?",
                    r"entertainment", r"events?")),
]


def _first_rule(text: str, rules: Sequence[Tuple[str, "re.Pattern[str]"]] = _RULES) -> Optional[str]:
    if not text:
        return None
    for name, rx in rules:
        if rx.search(text):
            return name
    return None


def classify_activity(categories: Sequence[str], title: str = "", description: str = "") -> str:
    """Pick the Activity Type: platform categories, then title, then weak labels, then description."""
    cats = " | ".join(c for c in categories if c)
    for text in (cats, title):
        found = _first_rule(text)
        if found:
            return found
    found = _first_rule(cats + " | " + title, _WEAK_RULES)
    if found and found != "Culture":
        return found
    desc = description[:400]
    found = _first_rule(desc) or found or _first_rule(desc, _WEAK_RULES)
    return found or "Other"
