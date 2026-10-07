import threading
import time

import pytest

from fakesites import FakeSite, FakeWeb
from s1scraper.fetcher import Cancelled, Fetcher, HttpCache, Politeness, RobotsRules, looks_blocked


def make(site_routes, **politeness):
    site = FakeSite("example.test")
    for path, route in site_routes.items():
        site.routes[path] = route
    site.start()
    web = FakeWeb({"example.test": site})
    opts = dict(min_delay=0.0, max_delay=0.0, breather_every=0, backoff_base=0.05)
    opts.update(politeness)
    fetcher = Fetcher(Politeness(**opts), HttpCache(None), threading.Event(), web.rewrite, web.unrewrite)
    return site, fetcher


def test_robots_longest_match_and_allow_wins_ties():
    # RFC 9309: the longest matching rule wins; on equal length Allow wins
    rules = RobotsRules("User-agent: *\nDisallow: /\nAllow: /events/\nDisallow: /events/private$\n"
                        "Disallow: /events/*?sort=\n\nUser-agent: Googlebot\nAllow: /\n")
    assert RobotsRules("User-agent: *\nAllow: /events/\nDisallow: /*?sort=\n").allowed("https://x/events/a?sort=1")
    assert rules.allowed("https://x/events/show-1")
    assert not rules.allowed("https://x/explore")
    assert not rules.allowed("https://x/events/private")
    assert rules.allowed("https://x/events/private-party")
    assert not rules.allowed("https://x/events/a?sort=date")
    assert RobotsRules("User-agent: *\nDisallow:\n").allowed("https://x/anything")
    assert RobotsRules("User-agent: S1EventScraper\nDisallow: /\n\nUser-agent: *\nAllow: /\n").allowed("https://x/a") is False


def test_looks_blocked():
    assert looks_blocked(403, "")
    assert looks_blocked(429, "")
    assert looks_blocked(200, "<html><title>Just a moment...</title></html>")
    assert looks_blocked(200, "<script src='/cdn-cgi/challenge-platform/x'></script>" + "x" * 100000)
    assert not looks_blocked(200, "<html>" + "real event page about access denied " * 2000 + "</html>")
    assert not looks_blocked(404, "<h1>Not found</h1>")


def test_pacing_between_requests_to_one_site():
    site, f = make({"/a": (200, "text/html", "ok"), "/b": (200, "text/html", "ok")}, min_delay=0.4, max_delay=0.4)
    try:
        t0 = time.monotonic()
        assert f.get("https://example.test/a", cache_hours=0).ok
        assert f.get("https://example.test/b", cache_hours=0).ok
        assert time.monotonic() - t0 >= 0.4
    finally:
        site.stop()


def test_retries_503_then_succeeds():
    calls = {"n": 0}

    def flaky(q):
        calls["n"] += 1
        return (503, "text/html", "busy") if calls["n"] < 3 else (200, "text/html", "<html>fine</html>")

    site, f = make({"/flaky": flaky})
    try:
        res = f.get("https://example.test/flaky", cache_hours=0)
        assert res.ok and res.text == "<html>fine</html>" and calls["n"] == 3
    finally:
        site.stop()


def test_circuit_breaker_stops_contacting_a_refusing_site():
    site, f = make({"/x": (429, "text/html", "slow down")}, max_consecutive_blocks=3, max_retries=1)
    try:
        for _ in range(3):
            res = f.get("https://example.test/x", cache_hours=0)
            assert res.blocked
        hits_before = site.hits.get("/x", 0)
        res = f.get("https://example.test/x", cache_hours=0)
        assert res.blocked and "stopped contacting" in res.error
        assert site.hits.get("/x", 0) == hits_before          # no further network traffic
        assert f.is_disabled("example.test")
    finally:
        site.stop()


def test_success_resets_the_refusal_count():
    site, f = make({"/no": (403, "text/html", "no"), "/yes": (200, "text/html", "<html>yes</html>")},
                   max_consecutive_blocks=2)
    try:
        f.get("https://example.test/no", cache_hours=0)
        f.get("https://example.test/yes", cache_hours=0)
        f.get("https://example.test/no", cache_hours=0)
        assert not f.is_disabled("example.test")
    finally:
        site.stop()


def test_robots_txt_is_respected_and_can_be_disabled():
    routes = {"/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private/\n"),
              "/private/page": (200, "text/html", "secret")}
    site, f = make(routes)
    try:
        res = f.get("https://example.test/private/page", cache_hours=0)
        assert not res.ok and "robots.txt" in res.error and "/private/page" not in site.hits
    finally:
        site.stop()
    site, f = make(routes, respect_robots_txt=False)
    try:
        assert f.get("https://example.test/private/page", cache_hours=0).ok
    finally:
        site.stop()


def test_disk_cache_avoids_refetching(tmp_path):
    site = FakeSite("example.test")
    site.page("/e", "<html>event</html>")
    site.start()
    web = FakeWeb({"example.test": site})
    cache = HttpCache(tmp_path / "c.sqlite3")
    p = Politeness(min_delay=0, max_delay=0, breather_every=0)
    try:
        f1 = Fetcher(p, cache, threading.Event(), web.rewrite, web.unrewrite)
        assert not f1.get("https://example.test/e", cache_hours=1).from_cache
        f2 = Fetcher(p, cache, threading.Event(), web.rewrite, web.unrewrite)   # e.g. next run
        res = f2.get("https://example.test/e", cache_hours=1)
        assert res.from_cache and res.text == "<html>event</html>"
        assert res.final_url == "https://example.test/e"
        assert site.hits["/e"] == 1
        assert not f2.get("https://example.test/e", cache_hours=0).from_cache
    finally:
        cache.close()
        site.stop()


def test_stop_interrupts_waiting():
    site, f = make({"/a": (200, "text/html", "ok")}, min_delay=30, max_delay=30, respect_robots_txt=False)
    try:
        f.get("https://example.test/a", cache_hours=0)
        threading.Timer(0.3, f.stop_event.set).start()
        t0 = time.monotonic()
        with pytest.raises(Cancelled):
            f.get("https://example.test/a", cache_hours=0)
        assert time.monotonic() - t0 < 5
    finally:
        site.stop()


def test_network_errors_are_reported_not_raised():
    f = Fetcher(Politeness(min_delay=0, max_delay=0, max_retries=1, backoff_base=0.01), HttpCache(None),
                threading.Event(), lambda u: "http://127.0.0.1:9/nothing")
    res = f.get("https://unreachable.test/", cache_hours=0)
    assert not res.ok and res.error


def test_unreachable_site_is_skipped_after_two_pages():
    f = Fetcher(Politeness(min_delay=0, max_delay=0, backoff_base=0.01, respect_robots_txt=False), HttpCache(None),
                threading.Event(), lambda u: "http://127.0.0.1:9/nothing")
    assert f.get("https://down.test/a", cache_hours=0).error
    assert not f.is_disabled("down.test")
    assert f.get("https://down.test/b", cache_hours=0).error
    assert "could not be reached" in f.is_disabled("down.test")
    # the third page must not touch the network at all (a refused connection
    # alone takes ~2 s on Windows, so only this call is timed)
    t0 = time.monotonic()
    res = f.get("https://down.test/c", cache_hours=0)
    assert res.blocked and time.monotonic() - t0 < 0.5
