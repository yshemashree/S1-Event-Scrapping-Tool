"""The cities the calendar covers, and how to recognise them in scraped addresses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class City:
    name: str                # label written to the sheet
    key: str                 # stable lowercase id used in settings and keys
    aliases: Tuple[str, ...]  # lowercase names/localities that identify the city in an address


CITIES: Tuple[City, ...] = (
    City("Mumbai", "mumbai", (
        "mumbai", "bombay", "navi mumbai", "thane", "andheri", "bandra", "powai", "worli",
        "juhu", "colaba", "lower parel", "goregaon", "malad", "borivali", "vashi", "kharghar",
        "bandra kurla complex", "bkc", "mahalaxmi", "nariman point", "dadar", "chembur",
    )),
    City("Pune", "pune", (
        "pune", "pimpri", "chinchwad", "hinjewadi", "kharadi", "koregaon park", "viman nagar",
        "baner", "wakad", "hadapsar", "kalyani nagar", "shivajinagar",
    )),
    City("Delhi NCR", "delhi", (
        "delhi ncr", "new delhi", "delhi", "ncr", "gurgaon", "gurugram", "noida", "greater noida",
        "faridabad", "ghaziabad", "saket", "connaught place", "aerocity", "vasant kunj",
        "hauz khas", "pragati maidan", "bharat mandapam",
    )),
    City("Bengaluru", "bengaluru", (
        "bengaluru", "bangalore", "whitefield", "koramangala", "indiranagar", "hsr layout",
        "jayanagar", "electronic city", "yelahanka", "hebbal", "marathahalli",
    )),
    City("Kolkata", "kolkata", (
        "kolkata", "calcutta", "howrah", "salt lake", "new town", "rajarhat", "bidhannagar",
        "park street",
    )),
    City("Ahmedabad", "ahmedabad", (
        "ahmedabad", "gandhinagar", "sg highway", "bodakdev", "navrangpura", "sabarmati",
    )),
    City("Chennai", "chennai", (
        "chennai", "madras", "nungambakkam", "t nagar", "t. nagar", "adyar", "velachery",
        "anna nagar", "guindy", "mylapore", "egmore",
    )),
)

CITY_BY_KEY = {c.key: c for c in CITIES}
CITY_BY_NAME = {c.name.lower(): c for c in CITIES}

def _alias_pattern(aliases: Iterable[str]) -> "re.Pattern[str]":
    alts = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(" + alts + r")(?![a-z])")


# City names (first two aliases) are tried before neighbourhood names, so
# "Andheri, Mumbai" never resolves through a weaker locality match.
_CITY_NAME_PATTERNS = [(c, _alias_pattern(c.aliases[:2])) for c in CITIES]
_ALIAS_PATTERNS = [(c, _alias_pattern(c.aliases)) for c in CITIES]


def get_city(value: str) -> Optional[City]:
    """Look a city up by key, display name or any alias (``"Bangalore"`` -> Bengaluru)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in CITY_BY_KEY:
        return CITY_BY_KEY[v]
    if v in CITY_BY_NAME:
        return CITY_BY_NAME[v]
    for c in CITIES:
        if v in c.aliases:
            return c
    return None


def detect_city(*texts: Optional[str]) -> Optional[City]:
    """Find which covered city an address/venue string belongs to, if any."""
    blob = " | ".join(t for t in texts if t).lower()
    if not blob:
        return None
    # Addresses end with the city ("Mumbai Masala, Koregaon Park, Pune"), so the
    # right-most mention wins.
    for patterns in (_CITY_NAME_PATTERNS, _ALIAS_PATTERNS):
        best, best_pos = None, -1
        for city, pattern in patterns:
            for m in pattern.finditer(blob):
                if m.start() > best_pos:
                    best, best_pos = city, m.start()
        if best is not None:
            return best
    return None


def resolve_cities(names: Iterable[str]) -> list:
    out = []
    for n in names:
        c = get_city(n)
        if c and c not in out:
            out.append(c)
    return out


_CITY_NAMES = {a for c in CITIES for a in c.aliases[:2]} | {
    "delhi", "ncr", "gurugram", "gurgaon", "noida", "greater noida", "faridabad", "ghaziabad", "navi mumbai",
    "thane", "howrah", "gandhinagar", "secunderabad", "hyderabad",
}


def is_city_name(text: str) -> bool:
    """True for a bare city name ("Mumbai", "Navi Mumbai 400614"), not a neighbourhood ("Bandra West")."""
    t = re.sub(r"[\d\s-]+$", "", (text or "").strip().lower()).strip(" .")
    return t in _CITY_NAMES


# Other Indian cities that show up in national promotions on city pages.
_OTHER_CITIES = (
    "hyderabad", "secunderabad", "goa", "panaji", "jaipur", "lucknow", "chandigarh", "kochi", "cochin",
    "thiruvananthapuram", "trivandrum", "coimbatore", "mysuru", "mysore", "mangaluru", "mangalore", "nagpur",
    "nashik", "surat", "vadodara", "baroda", "rajkot", "indore", "bhopal", "bhubaneswar", "guwahati", "patna",
    "ranchi", "visakhapatnam", "vizag", "vijayawada", "madurai", "dehradun", "amritsar", "ludhiana",
    "udaipur", "jodhpur", "agra", "varanasi", "shillong", "lonavala", "alibaug", "pondicherry", "puducherry",
    "dubai", "abu dhabi", "singapore", "london", "bangkok",
)
_OTHER_PATTERN = _alias_pattern(_OTHER_CITIES)


def detect_other_city(address: str) -> Optional[str]:
    """A non-covered city named in an address, when none of ours is."""
    if not address or detect_city(address):
        return None
    hits = list(_OTHER_PATTERN.finditer(address.lower()))
    return hits[-1].group(1).title() if hits else None
