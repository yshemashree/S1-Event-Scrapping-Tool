from datetime import date, datetime

import pytest

from s1scraper.normalize import (
    canonical_url, clean_title, first_sentences, format_inr, format_price_range, iso_duration_to_text, one_line,
    parse_date_range, parse_datetime, parse_price_text, parse_price_values, title_tokens, titles_match,
)

REF = date(2026, 10, 7)


@pytest.mark.parametrize("value, expected, has_time", [
    ("2026-10-11T20:00:00+05:30", datetime(2026, 10, 11, 20, 0), True),
    ("2026-10-11T14:30:00Z", datetime(2026, 10, 11, 20, 0), True),            # UTC -> IST
    ("2026-10-11T20:00:00.123456789+0530", datetime(2026, 10, 11, 20, 0, 0, 123456), True),
    ("2026-10-11", datetime(2026, 10, 11), False),
    (1791999000, datetime(2026, 10, 14, 23, 0), True),                        # epoch seconds
    ("1791999000000", datetime(2026, 10, 14, 23, 0), True),                   # epoch ms as text
    ("Sun, 11 Oct", datetime(2026, 10, 11), False),
    ("Sat, 15 Feb onwards", datetime(2027, 2, 15), False),                    # next occurrence
    ("Friday, October 9 at 7:00 PM", datetime(2026, 10, 9, 19, 0), True),
    ("15th Jan", datetime(2027, 1, 15), False),
    ("Today, 8:00 PM", datetime(2026, 10, 7, 20, 0), True),
    ("Tomorrow", datetime(2026, 10, 8), False),
    ("10/11/2026", datetime(2026, 11, 10), False),                            # day first
])
def test_parse_datetime(value, expected, has_time):
    assert parse_datetime(value, REF) == (expected, has_time)


@pytest.mark.parametrize("value", ["nonsense", "", None, "Book now", 12, "Coming soon"])
def test_parse_datetime_rejects_non_dates(value):
    assert parse_datetime(value, REF)[0] is None


@pytest.mark.parametrize("text, start, end", [
    ("10 - 12 Oct", datetime(2026, 10, 10), datetime(2026, 10, 12)),
    ("10-12 Oct 2026", datetime(2026, 10, 10), datetime(2026, 10, 12)),
    ("Sat, 10 Oct - Sun, 11 Oct", datetime(2026, 10, 10), datetime(2026, 10, 11)),
    ("Oct 30 - Nov 2", datetime(2026, 10, 30), datetime(2026, 11, 2)),
    ("28 Dec - 3 Jan 2027", datetime(2026, 12, 28), datetime(2027, 1, 3)),
    ("Dec 30 – Jan 2", datetime(2026, 12, 30), datetime(2027, 1, 2)),
    ("15 Feb onwards", datetime(2027, 2, 15), None),
    ("2026-10-10", datetime(2026, 10, 10), None),
])
def test_parse_date_range(text, start, end):
    s, e, _ = parse_date_range(text, REF)
    assert (s, e) == (start, end)


def test_parse_date_range_time_only_right_side():
    s, e, has_time = parse_date_range("Oct 10 | 7:00 PM - 10:00 PM", REF)
    assert s == datetime(2026, 10, 10, 19, 0) and e is None and has_time


@pytest.mark.parametrize("text, expected", [
    ("₹ 499 onwards", (499.0, None, False)),
    ("From ₹999", (999.0, None, False)),
    ("Starts at Rs 299", (299.0, None, False)),
    ("₹800 – ₹2,500", (800.0, 2500.0, False)),
    ("₹1,500", (1500.0, 1500.0, False)),
    ("Rs. 1,50,000", (150000.0, 150000.0, False)),
    ("INR 1499.00", (1499.0, 1499.0, False)),
    ("Free", (0.0, None, True)),
    ("Free entry", (0.0, None, True)),
    ("₹0", (0.0, None, True)),
    ("Free – ₹500", (0.0, 500.0, True)),
    ("Free drinks ₹999", (999.0, 999.0, False)),       # "free drinks" is not a free ticket
    ("499", (None, None, False)),                      # bare numbers need context
    ("₹99,99,999", (None, None, False)),               # absurd values are ignored
])
def test_parse_price_text(text, expected):
    assert parse_price_text(text) == expected


def test_parse_price_text_bare_numbers_when_allowed():
    assert parse_price_text("Price: 299", allow_bare_numbers=True) == (299.0, 299.0, False)


def test_parse_price_values():
    assert parse_price_values([499, "₹1,999", None, ""]) == (499.0, 1999.0, False)
    assert parse_price_values([0, 500]) == (0.0, 500.0, True)
    assert parse_price_values([]) == (None, None, False)


def test_format_inr_uses_indian_grouping():
    assert format_inr(2500) == "₹2,500"
    assert format_inr(150000) == "₹1,50,000"
    assert format_inr(12345678) == "₹1,23,45,678"
    assert format_inr(999) == "₹999"


@pytest.mark.parametrize("args, text", [
    ((800, 2500, False), "₹800 – ₹2,500"),
    ((499, None, False), "₹499 onwards"),
    ((1500, 1500, False), "₹1,500"),
    ((0, None, True), "Free"),
    ((0, 500, True), "Free – ₹500"),
    ((None, None, False), ""),
])
def test_format_price_range(args, text):
    assert format_price_range(*args) == text


@pytest.mark.parametrize("raw, clean", [
    ("Zakir Khan Live | BookMyShow", "Zakir Khan Live"),
    ("Buy Tickets for Sunburn Arena ft. Alan Walker - District", "Sunburn Arena ft. Alan Walker"),
    ("Comedy Night Tickets", "Comedy Night"),
    ("Hamlet - Buy Tickets Online at Insider", "Hamlet"),
    ("The Ticket Collector", "The Ticket Collector"),
    ("  &quot;Quoted&quot;  ", "Quoted"),
])
def test_clean_title(raw, clean):
    assert clean_title(raw) == clean


def test_titles_match_ignores_city_and_tour_filler():
    assert titles_match("Aise Kaise – Amit Tandon Live – Ahmedabad", "Aise Kaise by Amit Tandon")
    assert titles_match("Sunburn Arena ft Alan Walker Mumbai", "Sunburn Arena ft. Alan Walker")
    assert not titles_match("Comedy Night", "Jazz Night")
    assert not titles_match("", "Anything")
    assert "2026" not in title_tokens("NH7 Weekender 2026")


def test_canonical_url_strips_tracking_and_fragment():
    assert canonical_url("HTTPS://In.BookMyShow.com/events/x/ET001/?utm_source=a&b=1#frag") == \
        "https://in.bookmyshow.com/events/x/ET001?b=1"


def test_first_sentences_and_duration():
    assert first_sentences("One. Two is longer. Three.", 12) == "One."
    assert first_sentences("x" * 50, 10).endswith("…")
    assert iso_duration_to_text("PT1H30M") == "1 hr 30 mins"
    assert iso_duration_to_text("PT2H") == "2 hrs"
    assert iso_duration_to_text("2 hours") == "2 hours"


def test_one_line_notes():
    assert one_line("About the event: A Hindi play on family ties. Runs 2 hours. Book now!") == "A Hindi play on family ties."
    assert one_line("Live on stage! Prateek Kuhad plays his new album in full.") == (
        "Live on stage! Prateek Kuhad plays his new album in full.")
    long = one_line("word " * 60)
    assert len(long) <= 140 and long.endswith("…") and "\n" not in one_line("Line one\nline two.")


def test_one_line_skips_listing_site_filler():
    assert one_line("Join Aabo-Hawaa in Mumbai at NCPA on October 7, 2026.") == ""
    assert one_line("Looking for things to do in Mumbai? Attend Duos at KCC on October 7, 2026.") == ""
    assert one_line("Book online tickets for Tote Bag Painting in Mumbai on BookMyShow which is a workshops event") == ""
    assert one_line("The Royal Opera House Mumbai is hosting this Candlelight: Queen vs. ABBA in Mumbai! "
                    "Get your concert tickets today!") == ""
    assert one_line("Step into Yayoi Kusama's Infinity Mirror Room. Book online tickets for it on BookMyShow") == (
        "Step into Yayoi Kusama's Infinity Mirror Room.")


def test_webview_flag_is_dropped_from_links():
    assert canonical_url("https://in.bookmyshow.com/events/pottery/ET00395324?webview=true") == (
        "https://in.bookmyshow.com/events/pottery/ET00395324")


@pytest.mark.parametrize("text, expected", [
    ("11 Oct 2026 7:30 pm +2400", datetime(2026, 10, 11, 19, 30)),      # crashed a 7-city run
    ("11 Oct 2026 +530", datetime(2026, 10, 11)),
    ("11 Oct, 2026 | 7:00 PM | Call +91 98200 12345", datetime(2026, 10, 11, 19, 0)),
])
def test_numbers_after_a_date_are_not_time_zones(text, expected):
    dt, _ = parse_datetime(text, date(2026, 10, 8))
    assert dt == expected and dt.tzinfo is None
    sorted([dt, datetime(2026, 10, 9)])                                 # comparable with plain dates


def test_text_is_safe_for_excel_and_plain():
    from s1scraper.normalize import clean_text, plain_text

    assert clean_text("Saturd\ud835 night \x07") == "Saturd night"          # half an emoji broke a workbook
    assert one_line("𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝘁𝗼 **Chennai** 𝗔𝗜 𝗕𝘂𝗶𝗹𝗱𝗲𝗿𝘀! 💡 More text") == "Welcome to Chennai AI Builders!"
    assert one_line("<p>The beauty of rain in raga Malhar.</p>") == "The beauty of rain in raga Malhar."
    assert plain_text("🎙️ Build Voice Agents \\| No Code") == "Build Voice Agents | No Code"
    assert one_line("मुंबई में भव्य गरबा उत्सव। सभी का स्वागत है।").startswith("मुंबई में भव्य गरबा")
