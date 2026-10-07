"""Parsing and formatting helpers shared by every source: dates, prices, text, URLs."""

from __future__ import annotations

import html
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from dateutil import parser as du_parser

# India has no daylight saving, so a fixed offset is exact and avoids needing
# the tz database (absent on stock Windows Python).
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()


# --------------------------------------------------------------------- text

_WS_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = html.unescape(value).replace("\xa0", " ").replace("​", "")
    return _WS_RE.sub(" ", value).strip()


def strip_html(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if "<" in text and ">" in text:
        from bs4 import BeautifulSoup

        text = BeautifulSoup(text, "html.parser").get_text(" ")
    return clean_text(text)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(])")


def first_sentences(text: str, max_chars: int = 200) -> str:
    """Up to ``max_chars`` of whole sentences, ellipsised if one sentence is too long."""
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    out = ""
    for sentence in _SENTENCE_RE.split(text):
        candidate = (out + " " + sentence).strip()
        if len(candidate) > max_chars:
            break
        out = candidate
    if not out:
        cut = text[: max_chars - 1]
        out = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(",;:-– ") + "…"
    return out


_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:[|\-–—:]\s*)?(?:buy\s+)?(?:event\s+)?tickets?(?:\s+online)?(?:\s+(?:at|on)\s+[\w .]+)?$"
    r"|\s*[|\-–—]\s*(?:bookmyshow|district(?: by zomato)?|zomato district|allevents(?:\.in)?|"
    r"eventbrite|meetup|fever|skillbox(?:es)?|sortmyscene|insider|explara)\b.*$",
    re.I,
)
_TITLE_PREFIX_RE = re.compile(r"^(?:book\s+tickets?\s+(?:for|to)\s+|buy\s+tickets?\s+(?:for|to)\s+)", re.I)


def clean_title(title: Any) -> str:
    t = clean_text(title)
    for _ in range(2):
        t = _TITLE_SUFFIX_RE.sub("", t).strip()
        t = _TITLE_PREFIX_RE.sub("", t).strip()
    t = t.strip(" \"'“”|-–—")
    return t


_TITLE_STOPWORDS = {
    "live", "tour", "india", "show", "shows", "concert", "the", "a", "an", "ft", "feat",
    "featuring", "presents", "present", "presented", "by", "and", "with", "in", "at", "of",
    "x", "edition", "season", "official", "tickets", "ticket", "event", "on", "for", "to",
    "mumbai", "pune", "delhi", "new", "ncr", "gurugram", "gurgaon", "noida", "bengaluru",
    "bangalore", "kolkata", "ahmedabad", "chennai", "night", "nights", "experience",
}


def title_tokens(title: str) -> frozenset:
    words = re.findall(r"[a-z0-9]+", clean_text(title).lower())
    return frozenset(w for w in words if w not in _TITLE_STOPWORDS and not re.fullmatch(r"20\d\d", w))


def titles_match(a: str, b: str) -> bool:
    """Same event, judging only by title words (city/tour filler ignored)."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    smaller = min(len(ta), len(tb))
    if smaller >= 2 and inter == smaller:
        return True
    return inter / len(ta | tb) >= 0.6


# ---------------------------------------------------------------------- URLs

_TRACKING_PARAMS = re.compile(r"^(utm_.*|gclid|fbclid|ref|referrer|source|src|_branch.*|mc_.*|igshid)$", re.I)


def absolute_url(base: str, href: Optional[str]) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return ""
    return urljoin(base, href)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)])
    return urlunsplit((scheme, host, path, query, ""))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


# --------------------------------------------------------------------- dates

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTH_RE = re.compile(r"\b(" + _MONTHS + r")[a-z]*\b", re.I)
_NUMERIC_DATE_RE = re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b")
_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)
_WEEKDAY_RE = re.compile(r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day|sday|nesday|rsday|urday)?\b\.?,?", re.I)
_ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _to_ist_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def _from_epoch(v: float) -> Optional[datetime]:
    if v > 1e12:
        v = v / 1000.0
    if not (1e9 <= v <= 4.2e9):  # 2001 .. 2103
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc).astimezone(IST).replace(tzinfo=None)


def parse_datetime(value: Any, ref: Optional[date] = None) -> Tuple[Optional[datetime], bool]:
    """Parse ISO strings, epochs, or human text like "Sat, 15 Feb onwards".

    Returns ``(naive IST datetime, has_time)``. When the text carries no year,
    the next occurrence on or after ``ref`` (minus a little slack) is chosen.
    """
    ref = ref or today_ist()
    if value is None or value == "":
        return None, False
    if isinstance(value, datetime):
        dt = _to_ist_naive(value)
        return dt, dt.time() != time(0, 0)
    if isinstance(value, date):
        return datetime.combine(value, time()), False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = _from_epoch(float(value))
        return dt, dt is not None
    if not isinstance(value, str):
        return None, False

    s = clean_text(value)
    if not s:
        return None, False
    if re.fullmatch(r"\d{10,13}", s):
        dt = _from_epoch(float(s))
        return dt, dt is not None

    m = _ISO_RE.match(s)
    if m:
        y, mo, d, hh, mm, ss, frac, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0),
                          int((frac or "0")[:6].ljust(6, "0")))
        except ValueError:
            return None, False
        if tz:
            if tz == "Z":
                offset = timedelta(0)
            else:
                sign = 1 if tz[0] == "+" else -1
                digits = tz[1:].replace(":", "")
                offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt.replace(tzinfo=timezone(offset))
            dt = _to_ist_naive(dt)
        has_time = hh is not None
        return dt, has_time

    return _parse_human_date(s, ref)


def _parse_human_date(s: str, ref: date) -> Tuple[Optional[datetime], bool]:
    low = s.lower()
    has_time = bool(_TIME_RE.search(low))
    base: Optional[date] = None
    if re.search(r"\btoday\b|\btonight\b", low):
        base = ref
    elif re.search(r"\btomorrow\b", low):
        base = ref + timedelta(days=1)
    if base is not None:
        t = parse_time(low)
        return datetime.combine(base, t or time()), t is not None

    if not (_MONTH_RE.search(low) or _NUMERIC_DATE_RE.search(low)):
        return None, False
    cleaned = _WEEKDAY_RE.sub(" ", low)
    cleaned = _ORDINAL_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"\b(onwards|on wards|starting|starts|from|at|on|the|of|ist|hrs)\b", " ", cleaned)
    cleaned = re.sub(r"[|•·,]", " ", cleaned)
    cleaned = clean_text(cleaned)
    default = datetime(ref.year, ref.month, 1)
    try:
        dt = du_parser.parse(cleaned, dayfirst=True, fuzzy=True, default=default)
    except (ValueError, OverflowError, TypeError):
        return None, False
    if not _YEAR_RE.search(low) and dt.date() < ref - timedelta(days=45):
        try:
            dt = dt.replace(year=dt.year + 1)
        except ValueError:  # 29 Feb
            dt = dt + timedelta(days=365)
    if not has_time:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt, has_time


def parse_time(text: str) -> Optional[time]:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        if h == 12:
            h = 0
        if ap == "pm":
            h += 12
        if 0 <= h < 24 and 0 <= mi < 60:
            return time(h, mi)
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return time(int(m.group(1)), int(m.group(2)))
    return None


_RANGE_SPLIT_RE = re.compile(r"\s+(?:-|–|—|to|till|until)\s+|\s*(?:–|—)\s*", re.I)


def parse_date_range(text: Any, ref: Optional[date] = None) -> Tuple[Optional[datetime], Optional[datetime], bool]:
    """Parse "10 - 12 Oct", "Sat, 10 Oct - Sun, 11 Oct", "15 Feb onwards" ...

    Returns ``(start, end, has_time)``; ``end`` is None for single dates.
    """
    ref = ref or today_ist()
    if not isinstance(text, str):
        start, has_time = parse_datetime(text, ref)
        return start, None, has_time
    s = clean_text(text)
    if not s:
        return None, None, False
    if _ISO_RE.match(s):
        start, has_time = parse_datetime(s, ref)
        return start, None, has_time
    # "10-12 Oct" style: day-only left side joined by a bare hyphen
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s*[-–]\s*(\d{1,2}(?:st|nd|rd|th)?\s+.+)$", s, re.I)
    if m:
        end, end_t = parse_datetime(m.group(2), ref)
        if end:
            try:
                start = end.replace(day=int(m.group(1)), hour=0, minute=0)
                if start > end:
                    start = (start.replace(day=1) - timedelta(days=1)).replace(day=int(m.group(1)))
                return start, end, False
            except ValueError:
                pass
    parts = [p for p in _RANGE_SPLIT_RE.split(s) if p.strip()]
    if len(parts) >= 2:
        start, start_t = parse_datetime(parts[0], ref)
        end, end_t = parse_datetime(parts[-1], ref)
        if start is None and end is not None and re.fullmatch(r"\d{1,2}", parts[0].strip()):
            try:
                start = end.replace(day=int(parts[0]), hour=0, minute=0)
            except ValueError:
                start = None
        if start and not end:
            # right side was only a time ("7 PM - 10 PM")
            return start, None, start_t
        if start and end:
            if not _YEAR_RE.search(parts[0]) and _YEAR_RE.search(parts[-1]):
                try:
                    start = start.replace(year=end.year)
                    if start > end:
                        start = start.replace(year=end.year - 1)
                except ValueError:
                    pass
            if end < start:
                try:
                    end = end.replace(year=end.year + 1)
                except ValueError:
                    end = None
            return start, end, start_t
    start, has_time = parse_datetime(s, ref)
    return start, None, has_time


def format_time(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    return dt.strftime("%I:%M %p").lstrip("0")


def iso_duration_to_text(value: Any) -> str:
    """"PT1H30M" -> "1 hr 30 mins"; other strings pass through cleaned."""
    s = clean_text(value)
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", s, re.I)
    if not m or not (m.group(1) or m.group(2)):
        return s
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    parts = []
    if h:
        parts.append(f"{h} hr" + ("s" if h > 1 else ""))
    if mi:
        parts.append(f"{mi} mins")
    return " ".join(parts)


# -------------------------------------------------------------------- prices

_AMOUNT = r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d{1,2}))?"
_CURRENCY_AMOUNT_RE = re.compile(r"(?:₹|\brs\.?|\binr\b)\s*" + _AMOUNT, re.I)
_BARE_AMOUNT_RE = re.compile(r"(?<![\w.])" + _AMOUNT + r"(?![\w%])")
_FREE_RE = re.compile(r"\bfree\b", re.I)
_OPEN_ENDED_RE = re.compile(r"onwards|upwards|and above|starting|starts? (?:at|from)|\bfrom\b|\d\s*\+", re.I)
_MAX_PRICE = 5_00_000.0   # anything above is a typo or a package, not a ticket


def _amount(groups: Tuple[str, Optional[str]]) -> float:
    whole, frac = groups
    v = float(whole.replace(",", ""))
    if frac:
        v += float("0." + frac)
    return v


def parse_price_text(text: Any, allow_bare_numbers: bool = False) -> Tuple[Optional[float], Optional[float], bool]:
    """Extract ``(min, max, is_free)`` from text like "₹ 499 onwards" or "Free".

    Bare numbers are only trusted when the caller knows the text is a price field.
    """
    s = clean_text(text)
    if not s:
        return None, None, False
    amounts = [_amount(m.groups()) for m in _CURRENCY_AMOUNT_RE.finditer(s)]
    if not amounts and allow_bare_numbers:
        amounts = [_amount(m.groups()) for m in _BARE_AMOUNT_RE.finditer(s)]
    is_free = bool(_FREE_RE.search(s)) and not re.search(r"free\s+(?:drink|shot|merch|parking|gift)", s, re.I)
    amounts = [a for a in amounts if 0 <= a <= _MAX_PRICE]
    positive = [a for a in amounts if a > 0]
    if amounts and not positive:
        is_free = True
    if not positive:
        return (0.0 if is_free else None), None, is_free
    lo, hi = min(positive), max(positive)
    if lo == hi and _OPEN_ENDED_RE.search(s):
        hi = None   # "₹499 onwards": only the entry price is known
    # "Free – ₹500": free entry is the cheapest option
    return (0.0 if is_free else lo), hi, is_free


def parse_price_values(values: Iterable[Any]) -> Tuple[Optional[float], Optional[float], bool]:
    nums: List[float] = []
    is_free = False
    for v in values:
        if v is None or v == "" or isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            n = float(v)
        else:
            lo, hi, free = parse_price_text(v, allow_bare_numbers=True)
            is_free = is_free or free
            nums.extend(x for x in (lo, hi) if x)
            continue
        if n == 0:
            is_free = True
        elif 0 < n <= _MAX_PRICE:
            nums.append(n)
    if not nums:
        return (0.0 if is_free else None), None, is_free
    return (0.0 if is_free else min(nums)), max(nums), is_free


def format_inr(amount: float) -> str:
    """Indian digit grouping: 150000 -> "₹1,50,000"."""
    n = int(round(amount))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def format_price_range(price_min: Optional[float], price_max: Optional[float], is_free: bool,
                       min_only: bool = False) -> str:
    """Match the master sheet style: "₹800 – ₹2,500", "Free", "₹499 onwards"."""
    if not price_min and not price_max:
        return "Free" if is_free or price_min == 0 else ""
    lo = price_min if price_min is not None else price_max
    hi = price_max if price_max is not None else price_min
    if is_free or not lo:
        return "Free – " + format_inr(hi) if hi else "Free"
    if min_only or price_max is None:
        return format_inr(lo) + " onwards"
    if round(lo) == round(hi):
        return format_inr(lo)
    return f"{format_inr(lo)} – {format_inr(hi)}"
