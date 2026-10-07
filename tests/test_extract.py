import json
import re
from datetime import date, datetime

from s1scraper.extract import (
    EventWalker, embedded_json, event_from_jsonld, jsonld_events, labeled_facts, listing_cards, loads_lenient,
    meta_tags, microdata_events, page_price, parse_rsc_payload, pipe_facts, soup_of, text_lines,
)

REF = date(2026, 10, 8)
BMS_EVENT = re.compile(r"^https://in\.bookmyshow\.com/events/[^/]+/ET\d+")


def html_with_ld(obj, body=""):
    return f"<html><head><script type='application/ld+json'>{json.dumps(obj)}</script></head><body>{body}</body></html>"


def test_loads_lenient_handles_real_world_json_ld():
    raw = '<!--\n{"@type": "Event", "name": "Line\nbreak", "tags": ["a", "b",],}\n-->'
    assert loads_lenient(raw) == {"@type": "Event", "name": "Line\nbreak", "tags": ["a", "b"]}
    assert loads_lenient("not json") is None


def test_jsonld_graph_itemlist_and_lists_are_flattened():
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "x"},
        {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "item": {"@type": "MusicEvent", "name": "A", "startDate": "2026-10-10"}},
            {"@type": "ComedyEvent", "name": "B", "startDate": "2026-10-11"}]}]}
    names = [e["name"] for e in jsonld_events(soup_of(html_with_ld(graph)))]
    assert names == ["A", "B"]
    as_list = [{"@type": "Event", "name": "C", "startDate": "2026-10-12"}, {"@type": "Organization", "name": "Org"}]
    assert [e["name"] for e in jsonld_events(soup_of(html_with_ld(as_list)))] == ["C"]


def test_event_from_jsonld_maps_every_field():
    d = {"@type": "ComedyEvent", "name": "Zakir Khan Live | BookMyShow", "url": "/events/zakir/ET00000001",
         "startDate": "2026-10-10T20:00:00+05:30", "endDate": "2026-10-10T22:00:00+05:30",
         "eventStatus": "https://schema.org/EventPostponed",
         "location": {"@type": "Place", "name": "NSCI Dome",
                      "address": {"@type": "PostalAddress", "streetAddress": "Worli", "addressLocality": "Mumbai",
                                  "postalCode": "400018"}},
         "offers": {"@type": "AggregateOffer", "lowPrice": "999", "highPrice": "4,999", "priceCurrency": "INR",
                    "availability": "https://schema.org/InStock"},
         "organizer": [{"@type": "Organization", "name": "BookMyShow"}, {"@type": "Organization", "name": "OML"}],
         "performer": {"@type": "Person", "name": "Zakir Khan"}, "inLanguage": "Hindi", "duration": "PT2H",
         "description": "<p>Sakht <b>launda</b> returns.</p>"}
    ev = event_from_jsonld(d, "https://in.bookmyshow.com/explore/x", REF)
    assert ev.title == "Zakir Khan Live"
    assert ev.url == "https://in.bookmyshow.com/events/zakir/ET00000001"
    assert ev.start == datetime(2026, 10, 10, 20, 0) and ev.has_time and ev.end == datetime(2026, 10, 10, 22, 0)
    assert ev.venue == "NSCI Dome" and ev.address == "Worli, Mumbai, 400018"
    assert (ev.price_min, ev.price_max) == (999.0, 4999.0)
    assert ev.organizer == "OML"                      # the ticketing platform is not the organiser
    assert ev.performers == ["Zakir Khan"] and ev.language == "Hindi" and ev.duration == "2 hrs"
    assert ev.status == "Postponed" and ev.categories[0] == "Comedy"
    assert ev.description == "Sakht launda returns."


def test_offers_variants():
    single = event_from_jsonld({"@type": "Event", "name": "S", "offers": {"price": "600"}}, "https://x/e", REF)
    assert (single.price_min, single.price_max) == (600.0, None)          # shown as "₹600 onwards"
    tiers = event_from_jsonld({"@type": "Event", "name": "T", "offers": [
        {"name": "GA", "price": "999"}, {"name": "VIP", "price": "4999", "availability": "SoldOut"}]}, "https://x/e", REF)
    assert (tiers.price_min, tiers.price_max) == (999.0, 4999.0)
    assert tiers.ticket_types == [("GA", 999.0), ("VIP", 4999.0)] and tiers.status == ""
    free = event_from_jsonld({"@type": "Event", "name": "F", "offers": {"price": "0"}}, "https://x/e", REF)
    assert free.is_free and free.price_min == 0.0
    sold = event_from_jsonld({"@type": "Event", "name": "X", "offers": {"price": 5, "availability": "https://schema.org/SoldOut"}},
                             "https://x/e", REF)
    assert sold.status == "Sold out"


def test_online_events_are_flagged():
    ev = event_from_jsonld({"@type": "Event", "name": "Webinar", "location": {"@type": "VirtualLocation"}},
                           "https://x/e", REF)
    assert ev.online and not ev.venue


def test_microdata():
    html = ("<div itemscope itemtype='https://schema.org/MusicEvent'><span itemprop='name'>Jazz</span>"
            "<meta itemprop='startDate' content='2026-10-20T19:00'><span itemprop='price'>800</span></div>")
    evs = microdata_events(soup_of(html), "https://x/e", REF)
    assert evs[0].title == "Jazz" and evs[0].start == datetime(2026, 10, 20, 19, 0)


def test_embedded_json_sources():
    html = ("<script id='__NEXT_DATA__' type='application/json'>{\"props\": {\"a\": 1}}</script>"
            "<script>window.__INITIAL_STATE__ = {\"b\": [1, 2]}; var x = 1;</script>"
            "<script>window.__PRELOADED_STATE__ = \"{\\\"c\\\": 3}\";</script>"
            "<script>self.__next_f.push([1,\"5:{\\\"d\\\":4}\\n\"])</script>")
    found = embedded_json(soup_of(html))
    assert {"props": {"a": 1}} in found and {"b": [1, 2]} in found and {"c": 3} in found and {"d": 4} in found


def test_rsc_payload_with_prefix_text():
    out = parse_rsc_payload('1:I["x.js",[],""]\n2:["$","div",null,{"children":"hi there friends ok"}]\n'
                            '3:T12,{"event": {"name": "Show", "date": "2026-10-10"}}\n')
    assert ["$", "div", None, {"children": "hi there friends ok"}] in out
    assert {"event": {"name": "Show", "date": "2026-10-10"}} in out


def test_walker_finds_events_in_arbitrary_json():
    data = {"page": {"sections": [{"title": "Trending", "items": [
        {"eventCode": "ET00410002", "eventName": "The Comedy Factory", "startTime": "21:00",
         "eventDate": "2026-10-14", "venue": {"venueName": "The Habitat", "address": "Khar West, Mumbai"},
         "minPrice": 499, "maxPrice": 999, "genre": ["Stand-up"], "language": "English",
         "organiserName": "Comedy Factory Productions",
         "tickets": [{"name": "Silver", "price": 499}, {"name": "Gold", "price": "₹999"}]},
        {"title": "Mumbai", "url": "/explore/events-mumbai"},          # a city chip, not an event
        {"name": "No date, no link"},
    ]}]}}
    walker = EventWalker("https://in.bookmyshow.com/x", None, None, re.compile(r"ET\d{5,}"), REF)
    evs = walker.walk(data)
    assert len(evs) == 1
    ev = evs[0]
    assert ev.title == "The Comedy Factory" and ev.source_id == "ET00410002"
    assert ev.start == datetime(2026, 10, 14, 21, 0) and ev.has_time
    assert ev.venue == "The Habitat" and "Khar West" in ev.address
    assert (ev.price_min, ev.price_max) == (499.0, 999.0)
    assert ev.ticket_types == [("Silver", 499.0), ("Gold", 999.0)]
    assert ev.organizer == "Comedy Factory Productions" and ev.categories == ["Stand-up"] and ev.language == "English"


def test_walker_with_url_filter_and_slug_builder():
    data = {"events": [{"title": "Show A", "slug": "a-show", "startTime": 1792319400000},
                       {"title": "Show B", "url": "/events/b-show/ET00000002", "date": "2026-10-20"},
                       {"title": "Show C", "url": "/elsewhere/c", "date": "2026-10-21"}]}
    walker = EventWalker("https://in.bookmyshow.com/explore/x", BMS_EVENT.match,
                         lambda o: f"https://in.bookmyshow.com/events/{o['slug']}/ET00000001" if "slug" in o else "",
                         None, REF)
    urls = sorted(e.url for e in walker.walk(data))
    assert urls == ["https://in.bookmyshow.com/events/a-show/ET00000001",
                    "https://in.bookmyshow.com/events/b-show/ET00000002"]


def test_listing_cards_isolate_each_card():
    html = """<main><h1>Events in Mumbai</h1>
      <div class='chips'><a href='/explore/comedy-mumbai'>Comedy</a><a href='/explore/music-mumbai'>Music</a></div>
      <div class='grid'>
        <div class='card'><a href='/events/aakash/ET00000001'><img alt='Aakash Gupta Live' src='a.jpg'></a>
          <div>Sat, 10 Oct</div><div>St. Andrew's Auditorium: Mumbai</div><div>₹ 999 onwards</div><span>Selling fast</span></div>
        <div class='card'><a href='/events/jazz/ET00000002'><h3>Jazz Night</h3><p>Sun, 11 Oct</p><p>Blue Frog</p><p>Free</p></a></div>
      </div></main>"""
    evs = {e.title: e for e in listing_cards(soup_of(html), "https://in.bookmyshow.com/explore/events-mumbai",
                                              BMS_EVENT.match, REF)}
    assert set(evs) == {"Aakash Gupta Live", "Jazz Night"}
    a = evs["Aakash Gupta Live"]
    assert a.start == datetime(2026, 10, 10) and a.venue == "St. Andrew's Auditorium: Mumbai"
    assert (a.price_min, a.price_max) == (999.0, None) and a.categories == []
    j = evs["Jazz Night"]
    assert j.is_free and j.venue == "Blue Frog"


def test_single_card_does_not_swallow_the_page_heading():
    html = ("<main><h1>Artists in your District</h1><a href='/events/comedy-in-mumbai'>Comedy</a>"
            "<div><a href='/events/sunburn-oct18-2026-buy-tickets'><div><h3>Sunburn Arena</h3></div></a></div></main>")
    evs = listing_cards(soup_of(html), "https://www.district.in/events/upcoming-events-in-mumbai",
                        lambda u: u.endswith("-buy-tickets"), REF)
    assert [(e.title, e.categories) for e in evs] == [("Sunburn Arena", [])]


def test_labeled_facts():
    lines = text_lines(soup_of(
        "<div><p>Organised By</p><p>Comedy Munch</p><p>View profile</p><p>Language: Hindi, English</p>"
        "<p>Duration</p><p>2 hrs</p><p>Age Limit - 16+</p><p>Genres | Stand-up</p></div>"))
    facts = labeled_facts(lines)
    assert facts == {"organizer": "Comedy Munch", "language": "Hindi, English", "duration": "2 hrs",
                     "age_limit": "16+", "genre": "Stand-up"}
    assert labeled_facts(["Presented by Live Nation India"])["organizer"] == "Live Nation India"
    assert "organizer" not in labeled_facts(["Organised by", "View profile"])


def test_pipe_facts_and_page_price():
    assert pipe_facts(["Title", "Comedy Shows | Hindi | 16yrs + | 1hr 30mins"]) == {
        "categories": ["Comedy Shows"], "language": "Hindi", "age_limit": "16yrs +", "duration": "1hr 30mins"}
    assert pipe_facts(["Home | Events | Mumbai"]) == {}
    lines = ["Show", "₹1,499 onwards", "Book", "You may also like", "Other", "₹199 onwards"]
    assert page_price(lines) == (1499.0, None, False)


def test_meta_tags():
    meta = meta_tags(soup_of("<head><title>T</title><meta property='og:title' content='OG'>"
                             "<link rel='canonical' href='https://x/e'></head><body><h1>H</h1></body>"))
    assert meta["og:title"] == "OG" and meta["canonical"] == "https://x/e" and meta["title"] == "T" and meta["h1"] == "H"
