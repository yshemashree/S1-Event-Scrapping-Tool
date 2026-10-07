import pytest

from s1scraper.cities import get_city
from s1scraper.sources import ALL_SOURCES, SOURCE_BY_KEY, platform_for_url
from s1scraper.sources.bookmyshow import BookMyShow
from s1scraper.sources.district import District
from s1scraper.sources.sites import AllEvents, Eventbrite, NCPA, Skillbox


@pytest.mark.parametrize("url, ok", [
    ("https://in.bookmyshow.com/events/zakir-khan-live/ET00412345", True),
    ("https://in.bookmyshow.com/plays/aadhe-adhure/ET00412346", True),
    ("https://in.bookmyshow.com/sports/ind-vs-sa/ET00412347", True),
    ("https://in.bookmyshow.com/mumbai/events/old-style/ET00412348", True),
    ("https://in.bookmyshow.com/activities/snow-world/ET00300001", False),   # attractions excluded by default
    ("https://in.bookmyshow.com/explore/events-mumbai", False),
    ("https://in.bookmyshow.com/movies/mumbai/some-film/ET00399999", False),
])
def test_bookmyshow_event_urls(url, ok):
    assert BookMyShow().is_event_url(url) is ok


def test_bookmyshow_ids_and_urls_from_json():
    bms = BookMyShow()
    assert bms.event_id("https://in.bookmyshow.com/events/x/ET00412345") == "ET00412345"
    assert bms.url_from_obj({"eventCode": "ET00412345", "slug": "zakir"}) == \
        "https://in.bookmyshow.com/events/zakir/ET00412345"
    assert bms.url_from_obj({"eventCode": "ET00412345", "title": "Zakir Khan: Live!"}) == \
        "https://in.bookmyshow.com/events/zakir-khan-live/ET00412345"
    bms.include_activities = True
    assert bms.is_event_url("https://in.bookmyshow.com/activities/snow-world/ET00300001")


def test_bookmyshow_seeds_and_follow_pages():
    bms = BookMyShow()
    delhi = get_city("Delhi NCR")
    assert bms.seeds_for(delhi)[0] == ["https://in.bookmyshow.com/explore/events-national-capital-region-ncr"]
    follow = bms.follow_regexes(get_city("Mumbai"))
    assert any(rx.search("https://in.bookmyshow.com/explore/comedy-shows-mumbai") for rx in follow)
    assert not any(rx.search("https://in.bookmyshow.com/explore/movies-mumbai") for rx in follow)
    assert not any(rx.search("https://in.bookmyshow.com/explore/comedy-shows-pune") for rx in follow)


@pytest.mark.parametrize("url, ok", [
    ("https://www.district.in/events/sunburn-arena-alan-walker-oct18-2026-buy-tickets", True),
    ("https://www.district.in/events/saturday-comedy-night-jul18-2026-buy-tickets", True),
    ("https://www.district.in/events/sunburn-union-local-artists-collective-feb15-2025", True),
    ("https://www.district.in/events/upcoming-events-in-pune", False),
    ("https://www.district.in/events/nightlife-this-weekend-in-mumbai", False),
    ("https://www.district.in/events/comedy-shows-in-mumbai", False),
    ("https://www.district.in/events/artist/some-artist", False),
    ("https://www.district.in/activities/mumbai-activity-tickets", False),
])
def test_district_event_urls(url, ok):
    assert District().is_event_url(url) is ok


def test_district_city_spellings_fall_back():
    seeds = District().seeds_for(get_city("Delhi NCR"))
    assert seeds[0][:2] == ["https://www.district.in/events/upcoming-events-in-delhi-ncr",
                            "https://www.district.in/events/upcoming-events-in-delhi"]
    assert District().url_from_obj({"slug": "x-oct18-2026-buy-tickets"}) == \
        "https://www.district.in/events/x-oct18-2026-buy-tickets"


def test_other_profiles():
    assert AllEvents().is_event_url("https://allevents.in/mumbai/sunburn-arena/80002712345678")
    assert not AllEvents().is_event_url("https://allevents.in/mumbai/music")
    assert AllEvents().event_id("https://allevents.in/mumbai/sunburn-arena/80002712345678") == "80002712345678"
    assert Eventbrite().is_event_url("https://www.eventbrite.com/e/pottery-workshop-tickets-123456789012")
    assert Eventbrite().event_id("https://www.eventbrite.com/e/pottery-workshop-tickets-123456789012") == "123456789012"
    assert Skillbox().national and Skillbox().covers(get_city("Kolkata"))
    assert NCPA().covers(get_city("Mumbai")) and not NCPA().covers(get_city("Pune"))


def test_registry_is_consistent():
    keys = [cls.key for cls in ALL_SOURCES]
    assert len(keys) == len(set(keys)) and set(keys) == set(SOURCE_BY_KEY)
    for cls in ALL_SOURCES:
        src = cls()
        assert src.platform and src.name and src.group in ("core", "more", "venues", "optional")
        assert src.listing_templates, cls.key
        if not (src.national or src.fixed_city):
            assert set(src.city_slugs) - {"ahmedabad"}, cls.key
    assert platform_for_url("https://in.bookmyshow.com/events/x/ET1") == "BookMyShow"
    assert platform_for_url("https://www.district.in/events/x") == "Zomato District"
    assert platform_for_url("https://insider.in/x/event") == "Zomato District"
    assert platform_for_url("https://example.com") == ""
