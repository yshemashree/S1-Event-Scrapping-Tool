from datetime import datetime

from s1scraper.dedupe import dedupe
from s1scraper.models import Event


def ev(title, platform, start, city="Mumbai", venue="", url=None, **kw):
    url = url or f"https://{platform.lower().replace(' ', '')}.test/{title.lower().replace(' ', '-')}"
    e = Event(title=title, platform=platform, platforms=[platform], url=url, links={platform: url}, start=start,
              city=city, venue=venue, source=platform.lower(), **kw)
    return e


def test_same_show_on_three_platforms_merges_into_the_primary():
    d = datetime(2026, 10, 18, 16)
    merged = dedupe([
        ev("Sunburn Arena ft. Alan Walker - Mumbai", "AllEvents", d, venue="Jio World Garden", organizer="Percept Live"),
        ev("Sunburn Arena ft Alan Walker", "Zomato District", d, venue="Jio World Garden, BKC"),
        ev("Sunburn Arena ft. Alan Walker", "BookMyShow", d, venue="Jio World Garden: Mumbai"),
    ])
    assert len(merged) == 1
    m = merged[0]
    assert m.platforms == ["BookMyShow", "Zomato District", "AllEvents"]
    assert m.buy_link.startswith("https://bookmyshow.test/")
    assert m.organizer == "Percept Live"                  # gap filled from another platform
    assert set(m.links) == {"BookMyShow", "Zomato District", "AllEvents"}


def test_different_dates_cities_or_venues_stay_separate():
    d1, d2 = datetime(2026, 10, 10, 20), datetime(2026, 10, 11, 20)
    out = dedupe([
        ev("Comedy Night", "BookMyShow", d1, venue="Canvas Laugh Club"),
        ev("Comedy Night", "BookMyShow", d2, venue="Canvas Laugh Club", url="https://b.test/2"),
        ev("Comedy Night", "Zomato District", d1, city="Pune", venue="Canvas Laugh Club"),
        ev("Comedy Night", "Zomato District", d1, venue="The Habitat", url="https://d.test/3"),
    ])
    assert len(out) == 4


def test_a_run_and_one_of_its_shows_merge():
    run = ev("Mughal-E-Azam: The Musical", "BookMyShow", datetime(2026, 11, 2), end=datetime(2026, 11, 8))
    show = ev("Mughal-e-Azam The Musical", "Zomato District", datetime(2026, 11, 5, 19, 30))
    assert len(dedupe([run, show])) == 1


def test_same_listing_seen_twice_is_one_row():
    a = ev("Jazz Night", "BookMyShow", datetime(2026, 10, 3, 21), source_id="ET1")
    b = ev("Jazz Night", "BookMyShow", datetime(2026, 10, 3, 21), source_id="ET1", venue="Blue Frog")
    out = dedupe([a, b])
    assert len(out) == 1 and out[0].venue == "Blue Frog"
