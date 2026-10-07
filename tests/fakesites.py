"""Local stand-ins for BookMyShow, District and AllEvents used by the end-to-end tests.

Each fake host runs on its own local port; the scraper's ``rewrite``/``unrewrite``
seam maps the real domains onto them, so every URL the scraper sees keeps its
real domain. The pages deliberately mix the data styles real sites use:

* BookMyShow listing: ``window.__INITIAL_STATE__`` JSON plus a few SSR cards,
  category follow-links, an /activities/ attraction and an out-of-window event;
  detail pages with JSON-LD, one with only embedded state, one that refuses
  (403), and a two-city touring page.
* BookMyShow Chennai: a JavaScript-only listing filled by XHR on scroll
  (exercises the real-browser path).
* District: ``__NEXT_DATA__`` listing with epoch timestamps, a 404 city slug
  that must fall back, detail pages carrying React Server Component payloads.
* AllEvents: JSON-LD ItemList listing, "Hosted by" organiser text, and a
  ticket link pointing at BookMyShow (duplicate that must merge).
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Tuple, Union
from urllib.parse import parse_qs, urlsplit, urlunsplit

IST = timezone(timedelta(hours=5, minutes=30))
Response = Tuple[int, str, str]
Route = Union[Response, Callable[[Dict[str, list]], Response]]


class FakeSite:
    def __init__(self, host: str):
        self.host = host
        self.routes: Dict[str, Route] = {}
        self.hits: Dict[str, int] = {}
        self.lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                with site.lock:
                    site.hits[parts.path] = site.hits.get(parts.path, 0) + 1
                route = site.routes.get(parts.path)
                if route is None:
                    status, ctype, body = 404, "text/html", "<html><body><h1>Not found</h1></body></html>"
                elif callable(route):
                    status, ctype, body = route(parse_qs(parts.query))
                else:
                    status, ctype, body = route
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "FakeSite":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def page(self, path: str, body: str, status: int = 200, ctype: str = "text/html") -> None:
        self.routes[path] = (status, ctype, body)


class FakeWeb:
    """All fake hosts plus the URL mapping the scraper uses to reach them."""

    def __init__(self, sites: Dict[str, FakeSite]):
        self.sites = sites
        self.by_port = {s.port: h for h, s in sites.items()}

    def rewrite(self, url: str) -> str:
        p = urlsplit(url)
        site = self.sites.get(p.netloc.lower())
        if site is None:
            return "http://127.0.0.1:9/unreachable"   # every other host: connection refused
        return urlunsplit(("http", f"127.0.0.1:{site.port}", p.path or "/", p.query, ""))

    def unrewrite(self, url: str) -> str:
        p = urlsplit(url)
        if p.hostname == "127.0.0.1" and p.port in self.by_port:
            return urlunsplit(("https", self.by_port[p.port], p.path, p.query, p.fragment))
        return url

    def stop(self) -> None:
        for s in self.sites.values():
            s.stop()


# ----------------------------------------------------------------- helpers

def iso(d: date, hh: int = 20, mm: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).isoformat()


def epoch_ms(d: date, hh: int = 20, mm: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp() * 1000)


def page(title: str, body: str, head: str = "") -> str:
    return (f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>{title}</title>{head}</head>"
            f"<body><header><nav><a href='/'>Home</a> <a href='/about-us'>About</a></nav></header>"
            f"<main>{body}</main><footer>© 2026</footer></body></html>")


def ld(obj) -> str:
    return f"<script type='application/ld+json'>{json.dumps(obj)}</script>"


# ------------------------------------------------------------- BookMyShow

def build_bookmyshow(today: date) -> FakeSite:
    s = FakeSite("in.bookmyshow.com")
    d = lambda n: today + timedelta(days=n)  # noqa: E731
    s.page("/robots.txt", "User-agent: *\nDisallow: /api/\nDisallow: /buytickets/\nAllow: /\n", ctype="text/plain")

    mumbai_cards = [
        {"eventCode": "ET00410001", "title": "Aakash Gupta Live", "slug": "aakash-gupta-live",
         "venueName": "St. Andrew's Auditorium: Mumbai", "displayDate": f"{d(2):%a, %d %b}",
         "price": "₹ 999 onwards", "category": "Comedy Shows"},
        {"eventCode": "ET00410002", "title": "The Comedy Factory", "slug": "the-comedy-factory",
         "venueName": "The Habitat: Mumbai", "displayDate": f"{d(6):%a, %d %b}", "price": "₹ 499 onwards",
         "category": "Comedy Shows"},
        {"eventCode": "ET00410003", "title": "Prateek Kuhad – Silhouettes Tour", "slug": "prateek-kuhad-silhouettes",
         "venueName": "NSCI Dome: Mumbai", "displayDate": f"{d(12):%a, %d %b}", "price": "₹ 1,499 onwards",
         "category": "Music Shows"},
        {"eventCode": "ET00410004", "title": "Mumbai Food Truck Festival", "slug": "mumbai-food-truck-festival",
         "venueName": "Jio World Garden: Mumbai", "displayDate": f"{d(15):%a, %d %b}", "price": "₹ 299 onwards",
         "category": "Food & Drinks"},
        {"eventCode": "ET00410005", "title": "Rahul Dua – ALLOW ME", "slug": "rahul-dua-allow-me",
         "venueName": "Shanmukhananda Hall: Mumbai", "displayDate": f"{d(20):%a, %d %b}", "price": "₹ 799 onwards",
         "category": "Comedy Shows"},
        {"eventCode": "ET00410009", "title": "New Year's Eve Gala 2027", "slug": "nye-gala-2027",
         "venueName": "Taj Lands End: Mumbai", "displayDate": f"{d(120):%a, %d %b %Y}", "price": "₹ 8,000 onwards",
         "category": "Parties"},
    ]
    state = {"explore": {"listings": [{"type": "grid", "data": mumbai_cards}],
                         "filters": [{"name": "Comedy", "url": "/explore/comedy-shows-mumbai"}]}}
    ssr_cards = "".join(
        f"<a href='https://in.bookmyshow.com/events/{c['slug']}/{c['eventCode']}'><div class='card'>"
        f"<img alt='{c['title']}' src='x.jpg'><div>{c['title']}</div><div>{c['venueName']}</div>"
        f"<div>{c['category']}</div></div></a>" for c in mumbai_cards[:2])
    s.page("/explore/events-mumbai", page(
        "Events in Mumbai | BookMyShow",
        "<h1>Events in Mumbai</h1>"
        "<div class='chips'><a href='/explore/comedy-shows-mumbai'>Comedy Shows</a>"
        "<a href='/explore/movies-mumbai'>Movies</a><a href='/explore/events-pune'>Pune</a>"
        "<a href='/explore/activities-mumbai'>Activities</a></div>"
        f"<div class='grid'>{ssr_cards}"
        "<a href='https://in.bookmyshow.com/activities/snow-world/ET00300001'><div>Snow World</div>"
        "<div>Phoenix Marketcity: Mumbai</div><div>₹ 650 onwards</div></a></div>"
        f"<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>"))
    s.page("/explore/comedy-shows-mumbai", page(
        "Comedy Shows in Mumbai | BookMyShow",
        "<div class='grid'>"
        f"<a href='/events/abhishek-upmanyu-toxic/ET00410006'><div class='card'><h3>TOXIC – Abhishek Upmanyu Live</h3>"
        f"<div>{d(9):%a, %d %b} onwards</div><div>Jio World Centre: Mumbai</div><div>₹ 1,999 onwards</div>"
        "<div>Comedy Shows</div></div></a>"
        f"<a href='/events/aakash-gupta-live/ET00410001'><div class='card'><h3>Aakash Gupta Live</h3>"
        f"<div>{d(2):%a, %d %b}</div><div>St. Andrew's Auditorium: Mumbai</div><div>₹ 999 onwards</div></div></a>"
        "</div>"))
    s.page("/explore/plays-mumbai", page(
        "Plays in Mumbai | BookMyShow",
        f"<a href='/plays/aadhe-adhure/ET00410007'><div><h3>Aadhe Adhure</h3><div>{d(4):%d %b} - {d(5):%d %b}</div>"
        "<div>Prithvi Theatre: Mumbai</div><div>₹ 400 onwards</div></div></a>"))
    s.page("/explore/sports-mumbai", page("Sports in Mumbai | BookMyShow", "<p>No events right now</p>"))

    s.page("/events/aakash-gupta-live/ET00410001", page(
        "Aakash Gupta Live - Comedy Shows Tickets | BookMyShow",
        "<h1>Aakash Gupta Live</h1><div>Comedy Shows | Hindi, English | 16yrs + | 1hr 30mins</div>"
        f"<div>{d(2):%a %d %b %Y}</div><div>8:00 PM</div><div>St. Andrew's Auditorium: Mumbai</div>"
        "<div>₹999 onwards</div><button>Book</button><h2>About</h2><p>Aakash Gupta is back with an all new hour of "
        "stand-up. Expect sharp observational comedy.</p><h2>You may also like</h2>"
        "<a href='/events/other-show/ET00499999'>Other Show</a><div>₹199 onwards</div>",
        head=ld({"@context": "http://schema.org", "@type": "Event", "name": "Aakash Gupta Live",
                 "startDate": iso(d(2), 20), "endDate": iso(d(2), 21, 30),
                 "eventStatus": "https://schema.org/EventScheduled",
                 "location": {"@type": "Place", "name": "St. Andrew's Auditorium: Mumbai",
                              "address": {"@type": "PostalAddress", "streetAddress": "St. Dominic Road, Bandra West",
                                          "addressLocality": "Mumbai", "addressRegion": "Maharashtra",
                                          "postalCode": "400050", "addressCountry": "IN"}},
                 "offers": {"@type": "AggregateOffer", "lowPrice": "999", "highPrice": "2499", "priceCurrency": "INR",
                            "url": "https://in.bookmyshow.com/events/aakash-gupta-live/ET00410001",
                            "availability": "https://schema.org/InStock"},
                 "performer": [{"@type": "Person", "name": "Aakash Gupta"}],
                 "organizer": {"@type": "Organization", "name": "OML Entertainment"},
                 "description": "<p>Aakash Gupta is back with an all new hour of stand-up.</p>"})))
    s.page("/events/the-comedy-factory/ET00410002", page(
        "The Comedy Factory | BookMyShow",
        "<h1>The Comedy Factory</h1><div>Stand-up | English | 18yrs +</div>"
        "<script>window.__INITIAL_STATE__ = " + json.dumps({"eventDetail": {
            "eventCode": "ET00410002", "eventName": "The Comedy Factory", "startTime": "21:00",
            "eventDate": d(6).isoformat(), "venue": {"venueName": "The Habitat", "address": "Khar West, Mumbai"},
            "minPrice": 499, "maxPrice": 999, "genre": ["Stand-up"], "language": "English",
            "organiserName": "Comedy Factory Productions",
            "tickets": [{"name": "Silver", "price": 499}, {"name": "Gold", "price": 999}]}}) + ";</script>"))
    # this page refuses plain downloads AND the browser: data must come from District instead
    s.page("/events/prateek-kuhad-silhouettes/ET00410003",
           "<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>You don't have permission "
           "to access this page. Reference #18.abc</body></html>", status=403)
    s.page("/events/mumbai-food-truck-festival/ET00410004", page(
        "Mumbai Food Truck Festival | BookMyShow",
        "<h1>Mumbai Food Truck Festival</h1><div>Food & Drinks | All age groups</div>"
        "<p>Organised by</p><p>Gourmet Events Co.</p>",
        head=ld({"@context": "https://schema.org", "@type": "FoodEvent", "name": "Mumbai Food Truck Festival",
                 "startDate": d(15).isoformat(), "endDate": d(17).isoformat(),
                 "location": {"@type": "Place", "name": "Jio World Garden", "address": "BKC, Mumbai"},
                 "offers": [{"@type": "Offer", "name": "Day Pass", "price": "299", "priceCurrency": "INR"},
                            {"@type": "Offer", "name": "3-Day Pass", "price": "699", "priceCurrency": "INR"}]})))
    s.page("/events/rahul-dua-allow-me/ET00410005", page(
        "Rahul Dua – ALLOW ME | BookMyShow", "<h1>Rahul Dua – ALLOW ME</h1>",
        head=ld([{"@context": "https://schema.org", "@type": "Event", "name": "Rahul Dua – ALLOW ME",
                  "startDate": iso(d(24), 19), "location": {"@type": "Place", "name": "Ganesh Kala Krida Manch",
                                                            "address": {"@type": "PostalAddress",
                                                                        "addressLocality": "Pune"}},
                  "offers": {"@type": "Offer", "price": "999"}},
                 {"@context": "https://schema.org", "@type": "Event", "name": "Rahul Dua – ALLOW ME",
                  "startDate": iso(d(20), 20), "location": {"@type": "Place", "name": "Shanmukhananda Hall",
                                                            "address": {"@type": "PostalAddress",
                                                                        "streetAddress": "Sion",
                                                                        "addressLocality": "Mumbai"}},
                  "offers": {"@type": "Offer", "price": "799"}}])))
    s.page("/events/abhishek-upmanyu-toxic/ET00410006", page(
        "TOXIC – Abhishek Upmanyu Live | BookMyShow",
        "<h1>TOXIC – Abhishek Upmanyu Live</h1><div>Comedy Shows | Hindi | 16yrs + | 2hrs</div>",
        head=ld({"@context": "https://schema.org", "@type": "ComedyEvent", "name": "TOXIC – Abhishek Upmanyu Live",
                 "startDate": iso(d(9), 19), "endDate": iso(d(11), 21),
                 "subEvent": [{"@type": "Event", "startDate": iso(d(9), 19)}, {"@type": "Event", "startDate": iso(d(10), 19)},
                              {"@type": "Event", "startDate": iso(d(11), 19)}],
                 "location": {"@type": "Place", "name": "Jio World Centre",
                              "address": {"@type": "PostalAddress", "addressLocality": "Mumbai"}},
                 "offers": {"@type": "AggregateOffer", "lowPrice": 1999, "highPrice": 5999, "priceCurrency": "INR"},
                 "organizer": {"@type": "Organization", "name": "BookMyShow"}})))
    s.page("/plays/aadhe-adhure/ET00410007", page(
        "Aadhe Adhure | BookMyShow",
        "<h1>Aadhe Adhure</h1><div>Drama | Hindi | 2hrs 10mins</div><p>Presented by Ank Theatre Group</p>",
        head=ld({"@context": "https://schema.org", "@type": "TheaterEvent", "name": "Aadhe Adhure",
                 "startDate": iso(d(4), 18), "endDate": iso(d(5), 21),
                 "location": {"@type": "Place", "name": "Prithvi Theatre",
                              "address": {"@type": "PostalAddress", "streetAddress": "Juhu Church Road, Juhu",
                                          "addressLocality": "Mumbai"}},
                 "offers": {"@type": "Offer", "price": "400", "priceCurrency": "INR"}})))
    s.page("/events/nye-gala-2027/ET00410009", page("NYE", "<h1>NYE</h1>"))

    # ----- Pune: plain cards only
    s.page("/explore/events-pune", page(
        "Events in Pune | BookMyShow",
        f"<a href='/events/pune-jazz-night/ET00430001'><div><h3>Pune Jazz Night</h3><div>{d(3):%a, %d %b}</div>"
        "<div>High Spirits Cafe: Pune</div><div>₹ 600 onwards</div><div>Music Shows</div></div></a>"))
    s.page("/events/pune-jazz-night/ET00430001", page(
        "Pune Jazz Night | BookMyShow", "<h1>Pune Jazz Night</h1><div>Jazz | English | 21yrs +</div>",
        head=ld({"@context": "https://schema.org", "@type": "MusicEvent", "name": "Pune Jazz Night",
                 "startDate": iso(d(3), 21), "location": {"@type": "Place", "name": "High Spirits Cafe",
                                                          "address": "Koregaon Park, Pune"},
                 "offers": {"@type": "Offer", "price": "600", "priceCurrency": "INR"}})))

    # ----- Chennai: JavaScript-only listing, items arrive by XHR as you scroll
    chennai_pages = {}
    for p in range(1, 4):
        items = []
        for i in range(4):
            n = (p - 1) * 4 + i + 1
            items.append({"eventCode": f"ET0042{n:04d}", "title": f"Chennai Sangeetham Night {n}",
                          "slug": f"chennai-sangeetham-night-{n}", "date": d(2 + n).isoformat(),
                          "venueName": "Music Academy: Chennai", "price": f"₹ {300 + n * 100} onwards",
                          "category": "Music Shows"})
        chennai_pages[p] = {"items": items, "hasMore": p < 3}
    s.routes["/api/explore"] = lambda q: (200, "application/json",
                                          json.dumps(chennai_pages.get(int(q.get("page", ["1"])[0]), {"items": []})))
    s.page("/explore/events-chennai", """<!DOCTYPE html><html><head><title>Events in Chennai | BookMyShow</title>
<style>.card{display:block;height:420px;margin:8px;border:1px solid #ccc}</style></head>
<body><div id='root'>Loading…</div><div id='end' style='height:50px'></div>
<script>
let page = 0, loading = false, more = true;
async function load(){ if (loading || !more) return; loading = true; page += 1;
  const r = await fetch('/api/explore?city=chennai&page=' + page); const d = await r.json();
  const root = document.getElementById('root'); if (page === 1) root.innerHTML = '';
  for (const it of d.items){ const a = document.createElement('a');
    a.href = '/events/' + it.slug + '/' + it.eventCode; a.className = 'card';
    a.innerHTML = '<div>' + it.title + '</div><div>' + it.venueName + '</div><div>' + it.price + '</div>';
    root.appendChild(a); }
  more = d.hasMore; loading = false; }
window.addEventListener('scroll', () => { if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 200) load(); });
load();
</script></body></html>""")
    for page_no, payload in chennai_pages.items():
        for it in payload["items"]:
            n = int(it["eventCode"][-4:])
            s.page(f"/events/{it['slug']}/{it['eventCode']}", page(
                it["title"] + " | BookMyShow", f"<h1>{it['title']}</h1><div>Carnatic | Tamil | 2hrs</div>",
                head=ld({"@context": "https://schema.org", "@type": "MusicEvent", "name": it["title"],
                         "startDate": iso(d(2 + n), 18, 30),
                         "location": {"@type": "Place", "name": "The Music Academy",
                                      "address": {"@type": "PostalAddress", "streetAddress": "TTK Road, Alwarpet",
                                                  "addressLocality": "Chennai"}},
                         "offers": {"@type": "Offer", "price": str(300 + n * 100), "priceCurrency": "INR"},
                         "organizer": {"@type": "Organization", "name": "Madras Music Academy"}})))
    return s


# ---------------------------------------------------------------- District

def build_district(today: date) -> FakeSite:
    s = FakeSite("www.district.in")
    d = lambda n: today + timedelta(days=n)  # noqa: E731
    s.page("/robots.txt", "User-agent: *\nDisallow:\n", ctype="text/plain")

    def next_page(title: str, events: list) -> str:
        data = {"props": {"pageProps": {"initialData": {"events": events}}}, "page": "/events/[slug]"}
        cards = "".join(f"<a href='/events/{e['slug']}'><div class='card'><h3>{e['title']}</h3></div></a>"
                        for e in events[:1])
        return page(title, f"<h1>{title}</h1>{cards}<a href='/events/comedy-shows-in-mumbai'>Comedy</a>"
                           "<a href='/events/upcoming-events-in-pune'>Pune</a>"
                           f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(data)}</script>")

    mumbai = [
        {"id": 101, "title": "Sunburn Arena ft. Alan Walker", "slug": f"sunburn-arena-alan-walker-{d(10):%b%d-%Y}".lower() + "-buy-tickets",
         "startTime": epoch_ms(d(10), 16), "endTime": epoch_ms(d(10), 23),
         "venue": {"name": "Jio World Garden", "city": "Mumbai"}, "priceDisplayString": "₹1,999 onwards",
         "category": "Music"},
        {"id": 102, "title": "Prateek Kuhad Silhouettes Tour – Mumbai", "slug": "prateek-kuhad-silhouettes-tour-mumbai-buy-tickets",
         "startTime": epoch_ms(d(12), 19), "venue": {"name": "NSCI Dome", "city": "Mumbai"},
         "priceDisplayString": "₹1,499 onwards", "category": "Music"},
        {"id": 103, "title": "Sunday Gourmet Brunch", "slug": "sunday-gourmet-brunch-mumbai-buy-tickets",
         "startTime": epoch_ms(d(5), 12, 30), "venue": {"name": "Taj Mahal Palace", "city": "Mumbai"},
         "priceDisplayString": "₹5,500", "category": "Food & Drinks"},
    ]
    s.page("/events/upcoming-events-in-mumbai", next_page("Artists in your District", mumbai))
    s.page("/events/comedy-shows-in-mumbai", next_page("Comedy in Mumbai", []))
    s.page("/events/upcoming-events-in-pune", next_page("Artists in your District", []))
    # the first Delhi spelling does not exist; the scraper must fall back to the next
    delhi = [{"id": 201, "title": "Delhi Wine & Cheese Soirée", "slug": "delhi-wine-cheese-soiree-buy-tickets",
              "startTime": epoch_ms(d(7), 19), "venue": {"name": "The Lodhi", "city": "New Delhi"},
              "priceDisplayString": "₹4,500 onwards", "category": "Food & Drinks"}]
    s.page("/events/upcoming-events-in-delhi", next_page("Artists in your District", delhi))

    def rsc(obj: dict) -> str:
        payload = "1:I[\"chunks/app.js\",[],\"\"]\n3:" + json.dumps(obj) + "\n"
        return f"<script>self.__next_f.push([1,{json.dumps(payload)}])</script>"

    s.page(f"/events/{mumbai[0]['slug']}", page(
        "Sunburn Arena ft. Alan Walker | District",
        "<h1>Sunburn Arena ft. Alan Walker</h1>" + rsc({"event": {
            "name": "Sunburn Arena ft. Alan Walker", "startTime": iso(d(10), 16), "endTime": iso(d(10), 23),
            "venue": {"name": "Jio World Garden", "address": "G Block, BKC, Mumbai"},
            "organizer": {"name": "Percept Live"}, "category": "EDM",
            "tickets": [{"name": "General Access", "price": 1999}, {"name": "Fan Pit", "price": 4999},
                        {"name": "VIP Lounge", "price": 15000}],
            "description": "Alan Walker returns to India for a one-night-only arena show."}})))
    s.page(f"/events/{mumbai[1]['slug']}", page(
        "Prateek Kuhad Silhouettes Tour – Mumbai | District", "<h1>Prateek Kuhad Silhouettes Tour – Mumbai</h1>",
        head=ld({"@context": "https://schema.org", "@type": "MusicEvent", "name": "Prateek Kuhad Silhouettes Tour – Mumbai",
                 "startDate": iso(d(12), 19), "location": {"@type": "Place", "name": "NSCI Dome",
                                                           "address": "Lala Lajpatrai Marg, Worli, Mumbai"},
                 "offers": {"@type": "AggregateOffer", "lowPrice": "1499", "highPrice": "6999", "priceCurrency": "INR"},
                 "organizer": {"@type": "Organization", "name": "Only Much Louder"}})))
    s.page(f"/events/{mumbai[2]['slug']}", page(
        "Sunday Gourmet Brunch | District", "<h1>Sunday Gourmet Brunch</h1><p>Hosted by</p><p>Taj Hotels</p>",
        head=ld({"@context": "https://schema.org", "@type": "FoodEvent", "name": "Sunday Gourmet Brunch",
                 "startDate": iso(d(5), 12, 30), "location": {"@type": "Place", "name": "Taj Mahal Palace",
                                                              "address": "Apollo Bunder, Colaba, Mumbai"},
                 "offers": {"@type": "Offer", "price": "5500", "priceCurrency": "INR"}})))
    s.page(f"/events/{delhi[0]['slug']}", page(
        "Delhi Wine & Cheese Soirée | District", "<h1>Delhi Wine & Cheese Soirée</h1>",
        head=ld({"@context": "https://schema.org", "@type": "Event", "name": "Delhi Wine & Cheese Soirée",
                 "startDate": iso(d(7), 19), "location": {"@type": "Place", "name": "The Lodhi",
                                                          "address": "Lodhi Road, New Delhi"},
                 "offers": {"@type": "Offer", "price": "4500", "priceCurrency": "INR"},
                 "organizer": {"@type": "Organization", "name": "Sula Selections"}})))
    return s


# --------------------------------------------------------------- AllEvents

def build_allevents(today: date) -> FakeSite:
    s = FakeSite("allevents.in")
    d = lambda n: today + timedelta(days=n)  # noqa: E731
    items = [
        {"@type": "Event", "name": "Kolkata Literary Meet 2026", "startDate": iso(d(17), 10),
         "endDate": iso(d(19), 18), "url": "https://allevents.in/kolkata/kolkata-literary-meet-2026/80001234567",
         "location": {"@type": "Place", "name": "Victoria Memorial", "address": "Queen's Way, Maidan, Kolkata"},
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "INR"}},
        {"@type": "Event", "name": "Online Python Bootcamp", "startDate": iso(d(8), 18),
         "url": "https://allevents.in/kolkata/online-python-bootcamp/80001234999",
         "location": {"@type": "VirtualLocation", "url": "https://zoom.us/j/1"}, "eventAttendanceMode": "OnlineEventAttendanceMode"},
    ]
    s.page("/kolkata/all", page("Events in Kolkata | AllEvents", "<h1>Events in Kolkata</h1>",
                                head=ld({"@context": "https://schema.org", "@type": "ItemList",
                                         "itemListElement": [{"@type": "ListItem", "position": i + 1, "item": it}
                                                             for i, it in enumerate(items)]})))
    s.page("/kolkata/kolkata-literary-meet-2026/80001234567", page(
        "Kolkata Literary Meet 2026 | AllEvents",
        "<h1>Kolkata Literary Meet 2026</h1><div>Hosted by</div><div>Kolkata Lit Society</div>",
        head=ld(dict(items[0], **{"@context": "https://schema.org", "description": "Three days of authors, talks and readings."}))))
    s.page("/kolkata/online-python-bootcamp/80001234999", page("Online Python Bootcamp", "<h1>Online</h1>",
                                                                  head=ld(dict(items[1], **{"@context": "https://schema.org"}))))
    # Mumbai: the same Aakash Gupta show as on BookMyShow; tickets link to BookMyShow
    s.page("/mumbai/all", page("Events in Mumbai | AllEvents", "<h1>Events in Mumbai</h1>",
                               head=ld({"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
                                   {"@type": "ListItem", "position": 1, "item": {
                                       "@type": "Event", "name": "Aakash Gupta Live in Mumbai",
                                       "startDate": iso(d(2), 20),
                                       "url": "https://allevents.in/mumbai/aakash-gupta-live-in-mumbai/80009876543",
                                       "location": {"@type": "Place", "name": "St Andrews Auditorium",
                                                    "address": "Bandra West, Mumbai"}}}]})))
    s.page("/mumbai/aakash-gupta-live-in-mumbai/80009876543", page(
        "Aakash Gupta Live in Mumbai | AllEvents", "<h1>Aakash Gupta Live in Mumbai</h1>",
        head=ld({"@context": "https://schema.org", "@type": "Event", "name": "Aakash Gupta Live in Mumbai",
                 "startDate": iso(d(2), 20), "location": {"@type": "Place", "name": "St Andrews Auditorium",
                                                          "address": "Bandra West, Mumbai"},
                 "offers": {"@type": "Offer", "price": "999",
                            "url": "https://in.bookmyshow.com/events/aakash-gupta-live/ET00410001"}})))
    return s


def start_fake_web(today: date) -> FakeWeb:
    sites = {}
    for build in (build_bookmyshow, build_district, build_allevents):
        site = build(today).start()
        sites[site.host] = site
    return FakeWeb(sites)
