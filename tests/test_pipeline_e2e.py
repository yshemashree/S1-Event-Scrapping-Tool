"""Whole runs against the local fake BookMyShow / District / AllEvents."""

import hashlib
import threading
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from conftest import TODAY, browser_path, make_reference_workbook
from fakesites import FakeSite, FakeWeb
from s1scraper.config import Settings
from s1scraper.diagnose import run_diagnostics
from s1scraper.pipeline import Runner
from s1scraper.sources import SOURCE_BY_KEY

THREE = ("bookmyshow", "district", "allevents")


def settings_for(tmp_path, use_browser=False, sources=THREE, **kw):
    wb = make_reference_workbook(tmp_path / "calendar.xlsx")
    s = Settings(workbook_path=str(wb), data_dir=str(tmp_path / "data"), speed="instant",
                 sources={k: k in sources for k in SOURCE_BY_KEY}, use_browser=use_browser,
                 window_preset="1 month", **kw)
    return s


def run(settings, web, **kw):
    return Runner(settings, rewrite=web.rewrite, unrewrite=web.unrewrite, today=TODAY, **kw).run()


@pytest.fixture(scope="module")
def plain_run(tmp_path_factory, fake_web):
    tmp = tmp_path_factory.mktemp("plain")
    s = settings_for(tmp)
    return s, run(s, fake_web)


def by_title(report):
    return {e.title: e for e in report.events}


def test_plain_run_finds_and_cleans_events(plain_run):
    s, rep = plain_run
    ev = by_title(rep)
    assert not rep.cancelled and not rep.error
    # BookMyShow: JSON state + cards + follow-on category page + plays
    for title in ("Aakash Gupta Live", "The Comedy Factory", "TOXIC – Abhishek Upmanyu Live", "Aadhe Adhure",
                  "Mumbai Food Truck Festival", "Rahul Dua – ALLOW ME", "Pune Jazz Night"):
        assert title in ev, title
    # District (slug fallback for Delhi) and AllEvents
    for title in ("Sunburn Arena ft. Alan Walker", "Sunday Gourmet Brunch", "Delhi Wine & Cheese Soirée",
                  "Kolkata Literary Meet 2026"):
        assert title in ev, title
    titles = set(ev)
    assert "Snow World" not in titles                      # /activities/ attraction
    assert "New Year's Eve Gala 2027" not in titles        # outside the window
    assert "Online Python Bootcamp" not in titles          # online-only
    assert not any(t.startswith("Chennai Sangeetham") for t in titles)   # JS-only page needs the browser
    assert rep.dropped.get("online-only") == 1


def test_details_are_merged_from_event_pages(plain_run):
    ev = by_title(plain_run[1])
    a = ev["Aakash Gupta Live"]
    assert a.start == datetime(2026, 10, 10, 20, 0) and a.organizer == "OML Entertainment"
    assert (a.price_min, a.price_max) == (999.0, 2499.0) and a.tier == "Premium" and a.activity_type == "Comedy"
    assert a.language == "Hindi, English" and a.duration == "1hr 30mins"
    assert a.platforms == ["BookMyShow", "AllEvents"]       # AllEvents copy merged in
    cf = ev["The Comedy Factory"]                           # embedded state only, separate time field
    assert cf.start == datetime(2026, 10, 14, 21, 0) and cf.organizer == "Comedy Factory Productions"
    assert "Tickets: Silver ₹499, Gold ₹999" in cf.notes
    toxic = ev["TOXIC – Abhishek Upmanyu Live"]
    assert toxic.organizer == "Source: BookMyShow"          # platform listed as organiser = unknown
    assert "Dates: 17, 18, 19 Oct" in toxic.notes and toxic.tier == "Luxury"
    rahul = ev["Rahul Dua – ALLOW ME"]                      # two-city tour page: Mumbai date chosen
    assert rahul.start == datetime(2026, 10, 28, 20, 0) and rahul.venue == "Shanmukhananda Hall"
    sunburn = ev["Sunburn Arena ft. Alan Walker"]           # React Server Component payload
    assert sunburn.organizer == "Percept Live" and (sunburn.price_min, sunburn.price_max) == (1999.0, 15000.0)
    assert "VIP Lounge ₹15,000" in sunburn.notes
    assert ev["Delhi Wine & Cheese Soirée"].city == "Delhi NCR"
    assert ev["Delhi Wine & Cheese Soirée"].activity_type == "F&B"
    lit = ev["Kolkata Literary Meet 2026"]
    assert lit.is_free and lit.tier == "Standard" and lit.organizer == "Kolkata Lit Society"
    assert ev["Aadhe Adhure"].organizer == "Ank Theatre Group" and ev["Aadhe Adhure"].activity_type == "Theatre"


def test_refused_page_is_filled_from_another_platform(plain_run):
    pk = by_title(plain_run[1])["Prateek Kuhad – Silhouettes Tour"]   # BookMyShow answered 403
    assert pk.platforms == ["BookMyShow", "Zomato District"]
    assert pk.start == datetime(2026, 10, 20, 19, 0) and pk.organizer == "Only Much Louder"
    assert (pk.price_min, pk.price_max) == (1499.0, 6999.0)


def test_events_are_sorted_and_written(plain_run):
    s, rep = plain_run
    starts = [e.start for e in rep.events]
    assert starts == sorted(starts)
    wb = load_workbook(rep.saved_to)
    rolling = wb["Rolling Calendar"]
    titles = [rolling.cell(row=r, column=2).value for r in range(4, rolling.max_row + 1)]
    assert len([t for t in titles if t]) == len(rep.events)
    assert rep.new_in_master == len(rep.events) - 1          # Aakash Gupta was typed into the Master by hand


def test_second_run_uses_cache_and_adds_nothing(plain_run, fake_web):
    s, first = plain_run
    hits_before = dict(fake_web.sites["in.bookmyshow.com"].hits)
    again = run(s, fake_web)
    assert again.new_in_master == 0 and len(again.events) == len(first.events)
    hits_after = fake_web.sites["in.bookmyshow.com"].hits
    detail = "/events/aakash-gupta-live/ET00410001"
    assert hits_after.get(detail) == hits_before.get(detail)   # event page served from the cache


@pytest.mark.skipif(browser_path() is None, reason="no Chromium/Chrome/Edge available")
def test_browser_reads_javascript_only_listings(tmp_path, fake_web, monkeypatch):
    if browser_path():
        monkeypatch.setenv("S1_BROWSER_PATH", browser_path())
    s = settings_for(tmp_path, use_browser=True, sources=("bookmyshow",), cities=["Chennai"])
    rep = run(s, fake_web)
    chennai = [e for e in rep.events if e.city == "Chennai"]
    assert len(chennai) == 12                          # all three pages of the infinite scroll
    assert all(e.organizer == "Madras Music Academy" and e.start.hour == 18 for e in chennai)
    assert rep.browser


def test_stop_leaves_the_workbook_untouched(tmp_path, fake_web):
    s = settings_for(tmp_path)
    before = hashlib.sha256(Path(s.workbook_path).read_bytes()).hexdigest()
    stop = threading.Event()

    def progress(frac, text):
        if frac > 0.1:
            stop.set()

    rep = run(s, fake_web, progress_fn=progress, stop_event=stop)
    assert rep.cancelled
    assert hashlib.sha256(Path(s.workbook_path).read_bytes()).hexdigest() == before


def test_a_refusing_site_is_skipped_and_reported(tmp_path, fake_web):
    angry = FakeSite("in.bookmyshow.com")
    angry.routes["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    for slug in ("mumbai", "pune"):
        angry.routes[f"/explore/events-{slug}"] = (429, "text/html", "Too Many Requests")
    angry.start()
    web = FakeWeb({**fake_web.sites, "in.bookmyshow.com": angry})
    try:
        s = settings_for(tmp_path, sources=("bookmyshow", "district"))
        rep = run(s, web)
        assert not rep.cancelled
        assert any("stopped contacting" in w for w in rep.warnings)
        assert "Sunburn Arena ft. Alan Walker" in by_title(rep)     # other sources still delivered
        assert sum(angry.hits.values()) < 15                         # backed off quickly
    finally:
        angry.stop()


def test_diagnostics_report(tmp_path, fake_web):
    s = settings_for(tmp_path, sources=("bookmyshow", "district"))
    lines = []
    assert run_diagnostics(s, lines.append, rewrite=fake_web.rewrite, unrewrite=fake_web.unrewrite) == 0
    text = "\n".join(lines)
    assert "■ BookMyShow (Mumbai)" in text and "robots.txt allows the listing page: yes" in text
    assert "events found on listing:" in text and "fields found on event pages: date" in text
    reports = list((Path(s.data_dir) / "diagnostics").glob("*/report.txt"))
    assert reports and list(reports[0].parent.glob("bookmyshow/listing-*.html"))


def test_offline_run_leaves_the_workbook_alone(tmp_path):
    offline = FakeWeb({})                      # every host unreachable
    s = settings_for(tmp_path)
    before = hashlib.sha256(Path(s.workbook_path).read_bytes()).hexdigest()
    rep = run(s, offline)
    assert not rep.events and rep.failed_sources
    assert any("left unchanged" in w for w in rep.warnings) and not rep.saved_to
    assert hashlib.sha256(Path(s.workbook_path).read_bytes()).hexdigest() == before


def test_partial_failure_is_flagged_in_the_rolling_sheet(tmp_path, fake_web):
    web = FakeWeb({h: site for h, site in fake_web.sites.items() if h != "allevents.in"})
    rep = run(settings_for(tmp_path), web)
    assert rep.failed_sources == ["AllEvents"] and rep.events
    subtitle = load_workbook(rep.saved_to)["Rolling Calendar"]["A2"].value
    assert subtitle.startswith("⚠ Not read this run") and "AllEvents" in subtitle
