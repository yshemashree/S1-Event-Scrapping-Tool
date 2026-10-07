"""Turn event pages and listing pages into (partial) Event records.

Sites change their markup often, so nothing here depends on one site's CSS.
Data is taken, in order of trust, from:

1. schema.org JSON-LD / microdata - what ticketing sites publish for Google's
   event search (name, dates, venue, offers, organizer, ...);
2. JSON embedded in the page or captured from its API calls (Next.js data,
   Redux state, React server payloads, XHR responses), walked generically for
   objects that look like events;
3. visible text - labelled facts ("Organised by ...", "Language") on detail
   pages and the cards on listing pages.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from bs4 import BeautifulSoup, FeatureNotFound, Tag

from .classify import schema_type_category
from .models import Event
from .normalize import (
    absolute_url, canonical_url, clean_text, clean_title, iso_duration_to_text, parse_date_range,
    parse_datetime, parse_price_text, parse_price_values, parse_time, strip_html,
)

UrlTest = Callable[[str], bool]


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html or "", "lxml")
    except FeatureNotFound:  # pragma: no cover - lxml missing
        return BeautifulSoup(html or "", "html.parser")


# ------------------------------------------------------------------ JSON utils

_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def loads_lenient(raw: str) -> Any:
    """json.loads that tolerates raw newlines in strings, comments and trailing commas."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = re.sub(r"^\s*(?:<!--|//\s*<!\[CDATA\[|<!\[CDATA\[)", "", s)
    s = re.sub(r"(?:-->|//\s*\]\]>|\]\]>)\s*$", "", s).strip()
    for candidate in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        try:
            return json.loads(candidate, strict=False)
        except (ValueError, TypeError):
            continue
    return None


def balanced_json_at(text: str, start: int) -> Optional[str]:
    """The JSON object/array beginning at ``text[start]`` (``{`` or ``[``), if balanced."""
    if start >= len(text) or text[start] not in "{[":
        return None
    depth, in_str, esc, quote = 0, False, False, ""
    for i in range(start, min(len(text), start + 20_000_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str, quote = True, ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# ------------------------------------------------------------------- JSON-LD

EVENT_TYPES = {
    "event", "musicevent", "comedyevent", "theaterevent", "sportsevent", "foodevent",
    "exhibitionevent", "visualartsevent", "danceevent", "literaryevent", "festival",
    "educationevent", "childrensevent", "businessevent", "screeningevent", "socialevent",
    "saleevent", "courseinstance", "eventseries", "publicationevent", "hackathon",
}


def ld_types(d: Dict[str, Any]) -> Set[str]:
    t = d.get("@type")
    values = t if isinstance(t, list) else [t]
    return {str(x).lower().rsplit("/", 1)[-1] for x in values if x}


def _flatten_ld(data: Any, depth: int = 0) -> Iterator[Dict[str, Any]]:
    if depth > 8:
        return
    if isinstance(data, list):
        for x in data:
            yield from _flatten_ld(x, depth + 1)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _flatten_ld(data["@graph"], depth + 1)
        yield data
        if "itemlist" in ld_types(data):
            for el in data.get("itemListElement") or []:
                if isinstance(el, dict) and isinstance(el.get("item"), dict):
                    yield from _flatten_ld(el["item"], depth + 1)
                else:
                    yield from _flatten_ld(el, depth + 1)
        for key in ("subEvent", "subEvents", "event", "events", "mainEntity"):
            if key in data and isinstance(data[key], (dict, list)):
                yield from _flatten_ld(data[key], depth + 1)


def jsonld_objects(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    out = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        data = loads_lenient(tag.string or tag.get_text() or "")
        out.extend(_flatten_ld(data))
    return out


def jsonld_events(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for d in jsonld_objects(soup):
        if ld_types(d) & EVENT_TYPES and (d.get("name") or d.get("headline")):
            key = json.dumps(d, sort_keys=True, default=str)[:2000]
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def _first_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        for v in value:
            s = _first_str(v)
            if s:
                return s
        return ""
    if isinstance(value, dict):
        for k in ("url", "contentUrl", "@id", "name", "value", "text"):
            if isinstance(value.get(k), str):
                return value[k]
    return ""


def _names(value: Any, split: bool = False) -> List[str]:
    """Display names from a string / object / list value.

    ``split`` breaks "Comedy, Stand-up" into parts - right for tags, wrong for
    an organiser called "Live Nation, India".
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"\s*[,|]\s*", value) if split else [value]
        return [clean_text(v) for v in parts if clean_text(v)]
    if isinstance(value, list):
        out: List[str] = []
        for v in value:
            out.extend(n for n in _names(v, split) if n not in out)
        return out
    if isinstance(value, dict):
        for k in ("name", "title", "displayName", "label", "value"):
            if isinstance(value.get(k), str) and value[k].strip():
                return [clean_text(value[k])]
    return []


def _address_text(addr: Any) -> Tuple[str, str]:
    """(full address, locality) from a schema.org address value."""
    if isinstance(addr, str):
        return clean_text(addr), ""
    if isinstance(addr, list):
        for a in addr:
            text, loc = _address_text(a)
            if text:
                return text, loc
        return "", ""
    if isinstance(addr, dict):
        locality = clean_text(_first_str(addr.get("addressLocality")))
        parts = []
        for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode"):
            v = clean_text(_first_str(addr.get(k)))
            if v and v.lower() not in (p.lower() for p in parts):
                parts.append(v)
        return ", ".join(parts), locality
    return "", ""


def _location(loc: Any) -> Tuple[str, str, bool]:
    """(venue name, address, online) from a schema.org location value."""
    if loc is None:
        return "", "", False
    if isinstance(loc, str):
        return clean_text(loc), "", False
    if isinstance(loc, list):
        online = False
        for item in loc:
            v, a, o = _location(item)
            online = online or o
            if v or a:
                return v, a, online
        return "", "", online
    if isinstance(loc, dict):
        if "virtuallocation" in ld_types(loc):
            return "", "", True
        name = clean_text(_first_str(loc.get("name")))
        addr, _ = _address_text(loc.get("address"))
        return name, addr, False
    return "", "", False


_PLATFORM_ORGANIZERS = {
    "bookmyshow", "bms", "book my show", "bigtree entertainment", "district", "district by zomato",
    "zomato", "zomato district", "insider", "paytm insider", "allevents", "allevents.in", "eventbrite",
    "skillbox", "skillboxes", "sortmyscene", "fever", "meetup", "explara",
}


def is_platform_name(name: str) -> bool:
    return clean_text(name).lower().rstrip(".") in _PLATFORM_ORGANIZERS


def _clean_organizer(names: Iterable[str]) -> str:
    good = [n for n in (clean_text(x) for x in names) if n and not is_platform_name(n) and len(n) <= 120]
    return " / ".join(dict.fromkeys(good))


_STATUS_MAP = {
    "eventcancelled": "Cancelled", "eventpostponed": "Postponed", "eventrescheduled": "Rescheduled",
    "eventmovedonline": "Moved online",
}


def _offers(offers: Any) -> Tuple[List[float], bool, bool, str, List[Tuple[str, float]]]:
    """(prices, free, sold_out, ticket_url, ticket_types) from schema.org offers."""
    prices: List[float] = []
    free = False
    counts = {"offers": 0, "sold_out": 0}
    url = ""
    types: List[Tuple[str, float]] = []

    def visit(o: Any) -> None:
        nonlocal free, url
        if isinstance(o, list):
            for x in o:
                visit(x)
            return
        if not isinstance(o, dict):
            return
        vals = [o.get(k) for k in ("price", "lowPrice", "highPrice", "minPrice", "maxPrice")]
        spec = o.get("priceSpecification")
        if isinstance(spec, dict):
            vals += [spec.get(k) for k in ("price", "minPrice", "maxPrice")]
        elif isinstance(spec, list):
            for sp in spec:
                if isinstance(sp, dict):
                    vals += [sp.get(k) for k in ("price", "minPrice", "maxPrice")]
        lo, hi, fr = parse_price_values(v for v in vals if v not in (None, ""))
        free = free or fr
        if lo:
            prices.append(lo)
        if hi and hi != lo:
            prices.append(hi)
        if o.get("isAccessibleForFree") in (True, "true", "True"):
            free = True
        if any(k in o for k in ("price", "lowPrice", "highPrice", "availability", "priceSpecification")):
            counts["offers"] += 1
            if "soldout" in _first_str(o.get("availability")).lower().replace(" ", ""):
                counts["sold_out"] += 1
        if not url and isinstance(o.get("url"), str):
            url = o["url"]
        name = clean_text(_first_str(o.get("name")) or _first_str(o.get("category")))
        if name and lo:
            types.append((name, lo))
        if "offers" in o:
            visit(o["offers"])

    visit(offers)
    # every ticket tier sold out, not just one of them
    sold_out = counts["offers"] > 0 and counts["sold_out"] == counts["offers"]
    return prices, free, sold_out, url, types


def event_from_jsonld(d: Dict[str, Any], page_url: str, ref: Optional[date] = None) -> Event:
    ev = Event()
    ev.title = clean_title(_first_str(d.get("name")) or _first_str(d.get("headline")))
    own_url = _first_str(d.get("url"))
    ev.url = canonical_url(absolute_url(page_url, own_url) if own_url else page_url)
    ev.start, ev.has_time = parse_datetime(_first_str(d.get("startDate")) or d.get("startDate"), ref)
    end, _ = parse_datetime(_first_str(d.get("endDate")) or d.get("endDate"), ref)
    ev.end = end if (end and (not ev.start or end >= ev.start)) else None
    ev.venue, ev.address, ev.online = _location(d.get("location"))
    if not ev.online and "online" in _first_str(d.get("eventAttendanceMode")).lower():
        ev.online = True
    prices, free, sold_out, ticket_url, types = _offers(d.get("offers"))
    if prices:
        ev.price_min, ev.price_max = min(prices), max(prices)
        if len(set(prices)) == 1 and not types:
            ev.price_max = None if len(prices) == 1 else ev.price_max
    if free:
        ev.is_free = True
        ev.price_min = 0.0
    ev.ticket_types = types
    if ticket_url:
        ev.ticket_url = canonical_url(absolute_url(page_url, ticket_url))
    ev.organizer = _clean_organizer(_names(d.get("organizer")))
    ev.performers = _names(d.get("performer"))
    ev.description = strip_html(_first_str(d.get("description")))
    ev.image = _first_str(d.get("image"))
    status = _first_str(d.get("eventStatus")).lower().rsplit("/", 1)[-1]
    ev.status = _STATUS_MAP.get(status, "")
    if sold_out and not ev.status:
        ev.status = "Sold out"
    cats = []
    cat = schema_type_category(ld_types(d))
    if cat:
        cats.append(cat)
    for key in ("genre", "keywords", "about", "category"):
        cats.extend(n for n in _names(d.get(key), split=True) if len(n) <= 40)
    ev.categories = list(dict.fromkeys(cats))
    ev.language = ", ".join(_names(d.get("inLanguage")))[:60]
    age = d.get("typicalAgeRange")
    if age:
        ev.age_limit = clean_text(_first_str(age))
    if d.get("duration"):
        ev.duration = iso_duration_to_text(_first_str(d.get("duration")))
    sub = d.get("subEvent") or d.get("subEvents")
    if isinstance(sub, list):
        for s in sub:
            if isinstance(s, dict):
                dt, _ = parse_datetime(_first_str(s.get("startDate")), ref)
                if dt and dt.date() not in ev.session_dates:
                    ev.session_dates.append(dt.date())
    return ev


def locality_of_jsonld(d: Dict[str, Any]) -> str:
    loc = d.get("location")
    locs = loc if isinstance(loc, list) else [loc]
    for item in locs:
        if isinstance(item, dict):
            text, locality = _address_text(item.get("address"))
            if locality or text:
                return " ".join(x for x in (locality, text, _first_str(item.get("name"))) if x)
    return ""


# ---------------------------------------------------------------- microdata

def microdata_events(soup: BeautifulSoup, page_url: str, ref: Optional[date] = None) -> List[Event]:
    out = []
    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/\w*Event", re.I)}):
        props: Dict[str, Any] = {}
        for el in scope.find_all(attrs={"itemprop": True}):
            name = el.get("itemprop")
            if name in props:
                continue
            props[name] = el.get("content") or el.get("datetime") or el.get("href") or clean_text(el.get_text(" "))
        if props.get("name") and props.get("startDate"):
            d = {"@type": "Event", "name": props.get("name"), "startDate": props.get("startDate"),
                 "endDate": props.get("endDate"), "url": props.get("url"),
                 "location": props.get("location"), "description": props.get("description"),
                 "offers": {"price": props.get("price"), "lowPrice": props.get("lowPrice"),
                            "highPrice": props.get("highPrice")}}
            out.append(event_from_jsonld(d, page_url, ref))
    return out


# ------------------------------------------------------------- embedded JSON

_ASSIGN_RE = re.compile(
    r"(?:window\.|self\.|globalThis\.)?(__[A-Z0-9_]+__|__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__|"
    r"__NEXT_DATA__|__NUXT_DATA__|initialState|INITIAL_DATA|__data|pageData)\s*=\s*",
)
_NEXT_F_RE = re.compile(r"self\.__next_f\.push\(\[\s*1\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)", re.S)


def embedded_json(soup: BeautifulSoup) -> List[Any]:
    """Every JSON value a page embeds for its own JavaScript."""
    out: List[Any] = []
    rsc_chunks: List[str] = []
    for tag in soup.find_all("script"):
        stype = (tag.get("type") or "").lower()
        if "ld+json" in stype:
            continue
        text = tag.string or tag.get_text() or ""
        if not text.strip():
            continue
        if "json" in stype:
            data = loads_lenient(text)
            if data is not None:
                out.append(data)
            continue
        if stype and "javascript" not in stype and "module" not in stype:
            continue
        for m in _ASSIGN_RE.finditer(text):
            pos = m.end()
            while pos < len(text) and text[pos] in " \t\r\n":
                pos += 1
            if pos < len(text) and text[pos] in "\"'":
                # state shipped as a JSON string: window.__STATE__ = "{...}"
                lit = balanced_string_at(text, pos)
                inner = loads_lenient(lit) if lit else None
                data = loads_lenient(inner) if isinstance(inner, str) else None
            else:
                raw = balanced_json_at(text, pos)
                data = loads_lenient(raw) if raw else None
            if data is not None:
                out.append(data)
        for m in _NEXT_F_RE.finditer(text):
            try:
                rsc_chunks.append(json.loads(m.group(1)))
            except ValueError:
                continue
    if rsc_chunks:
        out.extend(parse_rsc_payload("".join(rsc_chunks)))
    return out


def balanced_string_at(text: str, start: int) -> Optional[str]:
    quote = text[start]
    esc = False
    for i in range(start + 1, min(len(text), start + 20_000_000)):
        ch = text[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == quote:
            lit = text[start:i + 1]
            return lit if quote == '"' else '"' + lit[1:-1].replace('"', '\\"') + '"'
    return None


def parse_rsc_payload(payload: str) -> List[Any]:
    """Pull JSON values out of a Next.js React Server Components stream."""
    out: List[Any] = []
    for line in payload.split("\n"):
        m = re.match(r"^[0-9a-zA-Z]+:(.*)$", line)
        if not m:
            continue
        body = m.group(1)
        if body[:1] in "[{":
            data = loads_lenient(body)
            if data is not None:
                out.append(data)
        else:
            for pos in [i for i, ch in enumerate(body) if ch == "{"][:200]:
                raw = balanced_json_at(body, pos)
                if raw and len(raw) > 40:
                    data = loads_lenient(raw)
                    if isinstance(data, dict):
                        out.append(data)
                        break
    return out


# ---------------------------------------------------- generic event-like walk

TITLE_KEYS = ("name", "title", "eventName", "event_name", "eventTitle", "event_title", "displayName",
              "display_name", "heading")
START_KEYS = ("startDate", "start_date", "startTime", "start_time", "startDateTime", "start_datetime",
              "startsAt", "starts_at", "eventDate", "event_date", "eventStartDate", "event_start_date",
              "showDate", "show_date", "sessionDate", "minShowDate", "min_show_date", "firstShowDate",
              "nextShowDate", "dateTime", "datetime", "date_time", "event_start_time", "date", "start",
              "from", "startUtc", "start_utc", "startTimestamp", "start_timestamp")
END_KEYS = ("endDate", "end_date", "endTime", "end_time", "endDateTime", "end_datetime", "endsAt",
            "ends_at", "eventEndDate", "event_end_date", "lastShowDate", "maxShowDate", "max_show_date",
            "event_end_time", "end", "to", "endUtc", "endTimestamp", "end_timestamp")
DATE_TEXT_KEYS = ("dateText", "date_text", "displayDate", "display_date", "dateString", "date_string",
                  "dateDisplay", "formattedDate", "formatted_date", "eventDateText", "showDateText",
                  "date_display_string", "dateDisplayString", "subtitle", "sub_title")
URL_KEYS = ("url", "eventUrl", "event_url", "webUrl", "web_url", "shareUrl", "share_url", "ctaUrl",
            "cta_url", "canonicalUrl", "canonical_url", "permalink", "link", "href", "deeplink",
            "deepLink", "seoUrl", "seo_url", "redirectUrl", "path", "slug")
VENUE_KEYS = ("venue", "venueName", "venue_name", "venueDetails", "venue_details", "venueInfo",
              "venue_info", "location", "place", "locationName", "location_name", "address")
CITY_KEYS = ("city", "cityName", "city_name", "regionName", "region_name", "addressLocality",
             "locality", "cityDisplayName")
PRICE_KEYS = ("price", "minPrice", "min_price", "lowPrice", "low_price", "startingPrice",
              "starting_price", "priceDisplayString", "price_display_string", "displayPrice",
              "display_price", "priceText", "price_text", "fromPrice", "from_price", "ticketPrice",
              "ticket_price", "minimumPrice", "minimum_price", "basePrice", "base_price", "priceRange",
              "price_range", "prices", "amount", "pricing", "minTicketPrice")
MAX_PRICE_KEYS = ("maxPrice", "max_price", "highPrice", "high_price", "maximumPrice", "maxTicketPrice")
ORG_KEYS = ("organizer", "organiser", "organizerName", "organiserName", "organizer_name",
            "organiser_name", "organizers", "organisers", "presentedBy", "presented_by", "hostedBy",
            "hosted_by", "hostName", "host_name", "host", "promoter", "eventOrganiser", "eventOrganizer",
            "producer", "producerName")
CAT_KEYS = ("category", "categories", "genre", "genres", "tags", "eventType", "event_type",
            "subCategory", "sub_category", "categoryName", "category_name", "primaryCategory",
            "eventCategory", "event_category", "subcategory", "eventGenre", "group", "groupName",
            "vertical", "type")
DESC_KEYS = ("description", "synopsis", "about", "summary", "shortDescription", "short_description",
             "descriptionText", "eventDescription", "event_description", "details", "aboutEvent")
IMG_KEYS = ("image", "imageUrl", "image_url", "banner", "bannerUrl", "banner_url", "poster",
            "posterUrl", "thumbnail", "cover", "coverImage", "cover_image", "horizontal_cover_image",
            "vertical_cover_image", "imageURL")
LANG_KEYS = ("language", "languages", "lang", "eventLanguage")
AGE_KEYS = ("ageLimit", "age_limit", "minAge", "min_age", "ageRestriction", "age_restriction",
            "agePolicy", "age_policy", "ageGroup", "age_group", "age")
DURATION_KEYS = ("duration", "eventDuration", "event_duration", "runTime", "run_time")
TIME_KEYS = ("startTime", "start_time", "showTime", "show_time", "time", "eventTime", "event_time", "timeText")
TICKET_KEYS = ("tickets", "ticketTypes", "ticket_types", "ticketCategories", "ticket_categories", "seatCategories",
               "seat_categories", "priceList", "price_list", "inventory", "ticketTiers", "ticket_tiers", "passes")
ID_KEYS = ("eventCode", "event_code", "eventId", "event_id", "code", "id", "_id", "uuid")

_NOT_EVENT_TYPES = {"breadcrumblist", "listitem", "organization", "website", "webpage", "person",
                    "place", "postaladdress", "imageobject", "offer", "aggregateoffer", "product"}


def _get(d: Dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    lower = {k.lower(): k for k in d.keys() if isinstance(k, str)}
    for k in keys:
        real = lower.get(k.lower())
        if real is not None and d[real] not in (None, "", [], {}):
            return d[real]
    return None


def _date_value(v: Any, ref: Optional[date]):
    if isinstance(v, dict):
        for k in ("iso", "value", "dateTime", "datetime", "date", "utc", "start", "timestamp", "seconds"):
            if k in v:
                return _date_value(v[k], ref)
        return None, False
    if isinstance(v, list):
        best = None
        for x in v:
            dt, t = _date_value(x, ref)
            if dt and (best is None or dt < best[0]):
                best = (dt, t)
        return best if best else (None, False)
    if isinstance(v, str):
        start, end, t = parse_date_range(v, ref)
        return start, t
    return parse_datetime(v, ref)


def _venue_value(v: Any) -> Tuple[str, str]:
    """(venue name, address/city text)."""
    if isinstance(v, str):
        return clean_text(v), ""
    if isinstance(v, list):
        for x in v:
            name, addr = _venue_value(x)
            if name or addr:
                return name, addr
        return "", ""
    if isinstance(v, dict):
        name = clean_text(_first_str(_get(v, ("name", "venueName", "venue_name", "title", "displayName",
                                                "label"))))
        addr = _get(v, ("address", "fullAddress", "full_address", "addressLine", "address_line",
                        "locality", "area", "city", "cityName", "city_name"))
        if isinstance(addr, dict):
            addr, _loc = _address_text(addr)
        city = _get(v, ("city", "cityName", "city_name", "addressLocality"))
        parts = [clean_text(_first_str(addr)), clean_text(_first_str(city))]
        text = ", ".join(dict.fromkeys(p for p in parts if p))
        return name, text
    return "", ""


def _price_value(v: Any) -> Tuple[List[float], bool, bool]:
    """(prices, free, open_ended)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v == 0:
            return [], True, False
        return ([float(v)] if 0 < v <= 500000 else []), False, False
    if isinstance(v, str):
        lo, hi, free = parse_price_text(v, allow_bare_numbers=True)
        return [p for p in (lo, hi) if p], free, (lo is not None and hi is None)
    if isinstance(v, dict):
        prices: List[float] = []
        free = False
        for k in ("min", "max", "minPrice", "maxPrice", "amount", "value", "price", "low", "high"):
            if k in v:
                p, f, _ = _price_value(v[k])
                prices += p
                free = free or f
        return prices, free, False
    if isinstance(v, list):
        prices, free = [], False
        for x in v[:50]:
            p, f, _ = _price_value(x)
            prices += p
            free = free or f
        return prices, free, False
    return [], False, False


def _category_values(v: Any) -> List[str]:
    names = _names(v, split=True)
    return [n for n in names if 1 < len(n) <= 40 and not n.isdigit()]


class EventWalker:
    """Find event-shaped objects in arbitrary JSON.

    ``is_event_url`` recognises the source's event pages; ``url_from_slug``
    turns a bare slug/id into a URL when the JSON has no full link.
    """

    def __init__(self, page_url: str, is_event_url: Optional[UrlTest] = None,
                 url_from_obj: Optional[Callable[[Dict[str, Any]], str]] = None,
                 id_pattern: Optional["re.Pattern[str]"] = None, ref: Optional[date] = None):
        self.page_url = page_url
        self.is_event_url = is_event_url
        self.url_from_obj = url_from_obj
        self.id_pattern = id_pattern
        self.ref = ref
        self.nodes = 0

    def walk(self, data: Any, limit: int = 300_000) -> List[Event]:
        out: Dict[str, Event] = {}
        self.nodes = 0
        stack = [(data, 0)]
        while stack:
            node, depth = stack.pop()
            self.nodes += 1
            if self.nodes > limit or depth > 40:
                break
            if isinstance(node, dict):
                ev = self.event_from_obj(node)
                if ev is not None:
                    key = ev.url or ev.source_id or (ev.title + str(ev.start))
                    if key in out:
                        out[key].absorb(ev)
                    else:
                        out[key] = ev
                    continue
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append((v, depth + 1))
            elif isinstance(node, list):
                for v in reversed(node):
                    if isinstance(v, (dict, list)):
                        stack.append((v, depth + 1))
        return list(out.values())

    def _url_of(self, d: Dict[str, Any]) -> str:
        for k in URL_KEYS:
            v = d.get(k)
            if isinstance(v, dict):
                v = _first_str(v)
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
            if v.startswith(("http://", "https://", "/")):
                candidates = [v]
            elif "/" in v and " " not in v:
                candidates = ["/" + v]          # "events/some-show" (path without leading slash)
            else:
                continue                        # bare slug: left to url_from_obj
            for c in candidates:
                url = canonical_url(absolute_url(self.page_url, c))
                if self.is_event_url is None or self.is_event_url(url):
                    return url
        if self.url_from_obj:
            url = self.url_from_obj(d)
            if url:
                return canonical_url(url)
        return ""

    def _id_of(self, d: Dict[str, Any]) -> str:
        if not self.id_pattern:
            return ""
        for k in ID_KEYS:
            v = d.get(k)
            if isinstance(v, (str, int)) and self.id_pattern.fullmatch(str(v)):
                return str(v)
        return ""

    def _start_of(self, d: Dict[str, Any]):
        """First parsable start among the date keys, joined with a separate time field if any."""
        start, has_time = None, False
        for key in START_KEYS:
            for k in (key, key.lower()):
                if k in d and d[k] not in (None, "", [], {}):
                    start, has_time = _date_value(d[k], self.ref)
                    break
            if start is not None:
                break
        if start is not None and not has_time:
            for key in TIME_KEYS:
                v = d.get(key)
                if isinstance(v, str):
                    t = parse_time(v)
                    if t is not None:
                        return start.replace(hour=t.hour, minute=t.minute), True
        return start, has_time

    @staticmethod
    def _ticket_types(d: Dict[str, Any]) -> List[Tuple[str, float]]:
        raw = _get(d, TICKET_KEYS)
        out: List[Tuple[str, float]] = []
        if isinstance(raw, list):
            for t in raw[:40]:
                if not isinstance(t, dict):
                    continue
                name = clean_text(_first_str(_get(t, ("name", "title", "label", "category", "type", "ticketName",
                                                       "displayName"))))
                prices, free, _ = _price_value(_get(t, ("price", "amount", "cost", "minPrice", "basePrice",
                                                        "displayPrice", "priceText", "value")))
                if prices and name:
                    out.append((name[:40], min(prices)))
                elif free and name:
                    out.append((name[:40], 0.0))
        return out

    def event_from_obj(self, d: Dict[str, Any]) -> Optional[Event]:
        if ld_types(d) & _NOT_EVENT_TYPES:
            return None
        title_v = _get(d, TITLE_KEYS)
        if isinstance(title_v, dict):
            title_v = _first_str(title_v)
        if not isinstance(title_v, str):
            return None
        title = clean_title(strip_html(title_v))
        if not (2 <= len(title) <= 200):
            return None
        url = self._url_of(d)
        source_id = self._id_of(d)
        start, has_time = self._start_of(d)
        date_text = ""
        if start is None:
            dt_text = _get(d, DATE_TEXT_KEYS)
            if isinstance(dt_text, str):
                start, end_guess, has_time = parse_date_range(dt_text, self.ref)
                date_text = clean_text(dt_text)
        if not (url or source_id) and start is None:
            return None
        if not (url or source_id) and self.is_event_url is not None:
            return None   # a dated object with no link to an event page is not usable
        if self.is_event_url is None and not (start or source_id):
            return None   # without a URL filter a bare link is no evidence of an event
        ev = Event(title=title, url=url, source_id=source_id, start=start, has_time=has_time,
                   date_text=date_text if start is None else "")
        end, _ = _date_value(_get(d, END_KEYS), self.ref)
        if end and start and end >= start:
            ev.end = end
        venue_v = _get(d, VENUE_KEYS)
        ev.venue, ev.address = _venue_value(venue_v)
        city_v = _get(d, CITY_KEYS)
        city = clean_text(_first_str(city_v))
        if city and city.lower() not in ev.address.lower():
            ev.address = ", ".join(x for x in (ev.address, city) if x)
        prices, free, open_ended = _price_value(_get(d, PRICE_KEYS))
        max_prices, _, _ = _price_value(_get(d, MAX_PRICE_KEYS))
        tiers = self._ticket_types(d)
        if tiers:
            ev.ticket_types = tiers
            prices = prices + [p for _, p in tiers]
            open_ended = False
        if prices or max_prices:
            ev.price_min = min(prices or max_prices)
            ev.price_max = max(prices + max_prices) if (max_prices or len(prices) > 1) else None
            if open_ended:
                ev.price_max = None
        if free:
            ev.is_free, ev.price_min = True, 0.0
        ev.organizer = _clean_organizer(_names(_get(d, ORG_KEYS)))
        cats: List[str] = []
        for k in CAT_KEYS:
            if k in d:
                cats += _category_values(d[k])
        ev.categories = list(dict.fromkeys(c for c in cats if not c.startswith("http")))[:8]
        desc = _get(d, DESC_KEYS)
        if isinstance(desc, (str, list, dict)):
            ev.description = strip_html(_first_str(desc))[:2000]
        ev.image = _first_str(_get(d, IMG_KEYS))
        ev.language = ", ".join(_names(_get(d, LANG_KEYS)))[:60]
        age = _get(d, AGE_KEYS)
        if isinstance(age, (int, float)) and not isinstance(age, bool) and 0 < age < 100:
            ev.age_limit = f"{int(age)}+"
        elif isinstance(age, str):
            ev.age_limit = clean_text(age)[:40]
        dur = _get(d, DURATION_KEYS)
        if isinstance(dur, (str, int, float)) and not isinstance(dur, bool):
            ev.duration = iso_duration_to_text(dur) if isinstance(dur, str) else f"{int(dur)} mins"
        return ev


# ------------------------------------------------------------- page metadata

def meta_tags(soup: BeautifulSoup) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        val = m.get("content")
        if key and val and key not in out:
            out[key] = clean_text(val)
    link = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    if link and link.get("href"):
        out["canonical"] = link["href"].strip()
    if soup.title and soup.title.string:
        out.setdefault("title", clean_text(soup.title.string))
    h1 = soup.find("h1")
    if h1:
        out.setdefault("h1", clean_text(h1.get_text(" ")))
    return out


# ------------------------------------------------------- labelled page facts

_FACT_LABELS: List[Tuple[str, str]] = [
    ("organizer", r"organi[sz]ed\s+by|organi[sz]ers?|event\s+organi[sz]er|presented\s+by|hosted\s+by|"
                  r"promoted\s+by|produced\s+by|curated\s+by|brought\s+to\s+you\s+by|host(?:ed)?\s+by"),
    ("language", r"languages?"),
    ("duration", r"duration|run\s*time|show\s+duration"),
    ("age_limit", r"age\s+limit|age\s+restriction|age\s+group|ages|suitable\s+for|entry\s+age|age"),
    ("genre", r"genres?|category|event\s+type"),
    ("venue", r"venue"),
]
_FACT_RES = [(k, re.compile(r"^\s*(?:" + rx + r")\s*(?:[:\-–|]\s*(.*))?$", re.I)) for k, rx in _FACT_LABELS]
_BY_RE = re.compile(r"^\s*(?:organi[sz]ed|presented|hosted|promoted|produced|curated)\s+by\s+(.{2,80})$", re.I)
_FACT_JUNK = re.compile(r"^(?:view|know|read|see|show|follow|more|click|book|share|less)\b|₹|https?://", re.I)


_NOISE_LINE = re.compile(r"enable javascript|javascript is (?:disabled|required)|your browser", re.I)


def text_lines(node: Any) -> List[str]:
    """Visible text lines (get_text already skips script/style/template)."""
    if node is None:
        return []
    return [ln for ln in (clean_text(x) for x in node.get_text("\n").split("\n"))
            if ln and not _NOISE_LINE.search(ln)]


def labeled_facts(lines: Sequence[str]) -> Dict[str, str]:
    facts: Dict[str, str] = {}
    n = len(lines)
    for i, line in enumerate(lines[:4000]):
        if len(line) > 120:
            continue
        m = _BY_RE.match(line)
        if m and "organizer" not in facts and not _FACT_JUNK.search(m.group(1)):
            facts["organizer"] = m.group(1).strip(" :-–|")
            continue
        for key, rx in _FACT_RES:
            if key in facts:
                continue
            m = rx.match(line)
            if not m:
                continue
            value = (m.group(1) or "").strip()
            if not value and i + 1 < n:
                value = lines[i + 1]
            value = value.strip(" :-–|")
            if 1 < len(value) <= 80 and not _FACT_JUNK.search(value) and not any(r.match(value) for _, r in _FACT_RES):
                facts[key] = value
            break
    return facts


_PAGE_PRICE_RE = re.compile(r"(?:₹|rs\.?\s|inr\s)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:-|–|to)\s*(?:₹|rs\.?\s|inr\s)?\s*\d[\d,]*)?(?:\s*(?:onwards|\+))?", re.I)


def page_price(lines: Sequence[str]) -> Tuple[Optional[float], Optional[float], bool]:
    """Price from the top of a detail page (the booking box), ignoring recommendations lower down."""
    head = lines[:150]
    for line in head:
        if re.search(r"\bonwards\b|\bstarting\b|\bfrom\b|\bprice\b|\btickets?\b", line, re.I):
            m = _PAGE_PRICE_RE.search(line)
            if m:
                return parse_price_text(m.group(0))
    for line in head:
        if re.fullmatch(r"(?:price\s*:?\s*)?free(?:\s+entry)?", line, re.I):
            return 0.0, None, True
    return None, None, False


# ------------------------------------------------------------ listing cards

_BADGES = re.compile(
    r"^(?:book(?:\s+now)?|buy(?:\s+now)?|selling fast|fast filling|filling fast|new|featured|sponsored|promoted|"
    r"sold out|trending|popular|bestseller|best seller|limited seats|few seats left|exclusive|premiere|"
    r"view details|know more|interested|going|online|\d+% off|offer|save|share|\+\d+ more)$",
    re.I,
)
KNOWN_CATEGORY_LABELS = {
    "comedy", "comedy shows", "stand up", "stand-up", "standup", "music", "music shows", "concerts",
    "concert", "gigs", "workshops", "workshop", "theatre", "theater", "plays", "kids", "food & drinks",
    "food and drinks", "food", "nightlife", "parties", "party", "exhibitions", "exhibition", "art",
    "arts", "sports", "performances", "spirituality", "talks", "meetups", "screenings", "screening",
    "adventure", "fitness", "festivals", "festival", "dance", "open mic", "poetry", "storytelling",
    "fests & fairs", "social mixers", "conferences", "awards", "holi", "new year", "garba",
    "experiences", "tours", "classical", "bollywood", "edm", "techno", "hip hop", "jazz", "rock",
}


def _looks_like_date(line: str) -> bool:
    low = line.lower()
    return bool(re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\btoday\b|"
                          r"\btomorrow\b|\btonight\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", low))


def _looks_like_price(line: str) -> bool:
    return bool(re.search(r"₹|\brs\.?\s*\d|\binr\b|\bonwards\b|^free$", line, re.I))


_PAGE_LEVEL = {"body", "html", "[document]", "main"}


def _card_for(a: Tag, event_url_of: Callable[[str], str]) -> Tag:
    """The smallest ancestor of ``a`` holding one card's text.

    Climbing stops before a node that holds another event's link, the page
    heading (h1/h2 outside the link), or clearly more text than a card has.
    """
    node: Tag = a
    best = a
    for _ in range(5):
        parent = node.parent
        if parent is None or parent.name in _PAGE_LEVEL:
            break
        links = {event_url_of(x.get("href")) for x in parent.find_all("a", href=True)}
        links.discard("")
        if len(links) > 1:
            break
        if any(h is not a and a not in h.parents and h not in a.descendants
               for h in parent.find_all(["h1", "h2"])):
            break
        lines = _card_lines(parent, a)
        if len(lines) > 12 or sum(len(x) for x in lines) > 700:
            break
        node = parent
        if len(lines) >= 2:
            best = node
        if len(lines) >= 4:
            break
    return best


def _card_lines(card: Tag, a: Tag) -> List[str]:
    """Card text without the text of other links (category chips, "view all", nav)."""
    other = set()
    for link in card.find_all("a", href=True):
        if link is not a and a not in link.parents and link not in a.descendants:
            other.update(text_lines(link))
    own = set(text_lines(a))
    return [ln for ln in text_lines(card) if ln in own or ln not in other]


def listing_cards(soup: BeautifulSoup, page_url: str, is_event_url: UrlTest,
                  ref: Optional[date] = None) -> List[Event]:
    """One partial Event per distinct event link on a listing page."""
    memo: Dict[str, str] = {}

    def event_url_of(href: Optional[str]) -> str:
        if href not in memo:
            url = canonical_url(absolute_url(page_url, href))
            memo[href] = url if url and is_event_url(url) else ""
        return memo[href]

    found: Dict[str, Event] = {}
    for a in soup.find_all("a", href=True):
        url = event_url_of(a.get("href"))
        if not url:
            continue
        card = _card_for(a, event_url_of)
        ev = event_from_card(card, a, ref)
        ev.url = url
        if url in found:
            found[url].absorb(ev)
        else:
            found[url] = ev
    return [e for e in found.values() if e.title]


def _plausible_title(line: str) -> bool:
    return (2 < len(line) <= 200 and not _BADGES.match(line) and not _looks_like_price(line)
            and not _looks_like_date(line) and line.lower() not in KNOWN_CATEGORY_LABELS)


def event_from_card(card: Tag, a: Tag, ref: Optional[date] = None) -> Event:
    lines = _card_lines(card, a) if card is not a else text_lines(a)
    ev = Event()
    candidates: List[str] = [clean_text(a.get(attr)) for attr in ("title", "aria-label")]
    heading = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    candidates.append(clean_text(heading.get_text(" ")) if heading else "")
    img = a.find("img", alt=True)
    candidates.append(clean_text(img["alt"]) if img else "")
    candidates.extend(ln for ln in text_lines(a) if _plausible_title(ln))
    if card is not a:
        heading = card.find(["h3", "h4", "h5", "h6"])
        candidates.append(clean_text(heading.get_text(" ")) if heading else "")
        img = card.find("img", alt=True)
        candidates.append(clean_text(img["alt"]) if img else "")
    title = next((c for c in candidates if c and _plausible_title(c)), "")
    rest: List[str] = []
    for line in lines:
        if _BADGES.match(line):
            continue
        if not ev.start and _looks_like_date(line):
            start, end, has_time = parse_date_range(line, ref)
            if start:
                ev.start, ev.end, ev.has_time = start, end, has_time
                continue
        if ev.price_min is None and not ev.is_free and _looks_like_price(line):
            lo, hi, free = parse_price_text(line, allow_bare_numbers=False)
            if lo is not None or free:
                ev.price_min, ev.price_max, ev.is_free = lo, hi, free
                ev.price_text = line
                continue
        if line.lower() in KNOWN_CATEGORY_LABELS:
            ev.categories.append(line)
            continue
        rest.append(line)
    if not title and rest:
        title = max(rest[:3], key=len)
    title = clean_title(title)
    ev.title = title
    for line in rest:
        if clean_title(line) == title or len(line) < 3 or len(line) > 140:
            continue
        ev.venue = line
        break
    return ev


# ------------------------------------------------- "Genre | Language | Age" line

LANGUAGES = {
    "hindi", "english", "marathi", "gujarati", "bengali", "bangla", "tamil", "telugu", "kannada",
    "malayalam", "punjabi", "urdu", "hinglish", "sanskrit", "konkani", "odia", "assamese", "multi-language",
    "multilingual", "french", "spanish", "japanese", "korean",
}
_AGE_TOKEN = re.compile(r"^(?:\d{1,2}\s*(?:yrs?|years?)?\s*\+|\d{1,2}\s*\+\s*(?:yrs?|years?)?|all ages?(?: groups?)?|"
                        r"(?:u|ua|a)\s*\d*\+?|\d{1,2}\s*-\s*\d{1,2}\s*(?:yrs?|years?))$", re.I)
_DURATION_TOKEN = re.compile(r"^\d+\s*(?:hrs?|hours?|h)(?:\s*\d+\s*(?:mins?|minutes?|m))?$|^\d+\s*(?:mins?|minutes?)$", re.I)


def pipe_facts(lines: Sequence[str], max_lines: int = 120) -> Dict[str, Any]:
    """Parse the compact fact line event pages show under the title,
    e.g. "Comedy Shows | Hindi, English | 16yrs + | 1hr 30mins"."""
    for line in lines[:max_lines]:
        if line.count("|") < 1 or len(line) > 160 or "₹" in line:
            continue
        tokens = [t.strip() for t in line.split("|") if t.strip()]
        if not 2 <= len(tokens) <= 6:
            continue
        facts: Dict[str, Any] = {"categories": []}
        hits = 0
        for tok in tokens:
            words = {w.strip().lower() for w in re.split(r"[,/&]", tok) if w.strip()}
            if words and words <= LANGUAGES:
                facts["language"] = tok
                hits += 1
            elif _AGE_TOKEN.match(tok):
                facts["age_limit"] = tok
                hits += 1
            elif _DURATION_TOKEN.match(tok):
                facts["duration"] = tok
                hits += 1
            elif len(tok) <= 40 and not re.search(r"\d{4}|₹|:", tok):
                facts["categories"].append(tok)
        if hits >= 1 and (facts["categories"] or hits >= 2):
            return facts
    return {}
