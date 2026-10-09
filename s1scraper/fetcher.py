"""Polite HTTP fetching.

Everything the scraper downloads goes through :class:`Fetcher`, which keeps the
traffic to each site low and human-paced so the office IP is never flagged:

* one request at a time per site, with a random gap between requests and a
  longer pause every few dozen requests;
* backoff on 429/503 (honouring ``Retry-After``) and a circuit breaker that
  stops contacting a site for the rest of the run after repeated refusals;
* robots.txt rules (RFC 9309 longest-match) are honoured by default;
* responses are cached on disk, so a re-run, or next week's run, does not
  download unchanged event pages again;
* no login, cookies only from the site itself, no third-party services.
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)
ROBOTS_AGENT = "S1EventScraper"


class Cancelled(Exception):
    """Raised inside workers when the user presses Stop."""


@dataclass
class Politeness:
    min_delay: float = 1.5          # seconds between two requests to the same site
    max_delay: float = 4.0
    breather_every: int = 40        # after this many requests to one site ...
    breather_seconds: float = 20.0  # ... pause roughly this long
    timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 8.0
    max_consecutive_blocks: int = 3
    respect_robots_txt: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    listing_cache_hours: float = 6.0
    detail_cache_hours: float = 240.0


# Gaps between two requests to the SAME site. Different sites are read at the
# same time, so a run takes about as long as its busiest site.
SPEED_PRESETS: Dict[str, dict] = {
    "gentle": dict(min_delay=2.0, max_delay=4.5, breather_every=40, breather_seconds=15.0),
    "normal": dict(min_delay=1.0, max_delay=2.5, breather_every=50, breather_seconds=8.0),
    "fast": dict(min_delay=0.6, max_delay=1.4, breather_every=80, breather_seconds=5.0),
    # automated tests against local fake sites only
    "instant": dict(min_delay=0.0, max_delay=0.0, breather_every=0, breather_seconds=0.0, backoff_base=0.05),
}


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    from_cache: bool = False
    blocked: bool = False
    error: str = ""
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.blocked and not self.error


# ------------------------------------------------------------ block detection

# Markers specific enough to trust anywhere in a page.
_STRONG_MARKERS = (
    "cf-chl-", "cf_chl_opt", "/cdn-cgi/challenge-platform", "_incapsula_resource",
    "px-captcha", "captcha-delivery.com", "geo.captcha-delivery", "perimeterx",
    "challenges.cloudflare.com/turnstile",
)
# Generic phrases only count on small pages (a real event page is large).
_WEAK_MARKERS = (
    "access denied", "you don't have permission to access", "attention required! | cloudflare",
    "just a moment...", "request unsuccessful", "please verify you are a human",
    "are you a robot", "unusual traffic", "enable javascript and cookies to continue",
    "checking your browser", "bot detection", "too many requests",
)


# Interstitial pages that can come back with status 200.
_CHALLENGE_PAGE_MARKERS = (
    "just a moment...", "attention required! | cloudflare", "checking your browser",
    "please verify you are a human", "are you a robot", "request unsuccessful",
)


BROWSER_SUFFIX = "#browser"


def browser_key(host: str) -> str:
    """Refusals of the hidden browser are counted apart from plain downloads: sites
    like BookMyShow turn away the automated browser while still serving normal requests."""
    return host + BROWSER_SUFFIX


def looks_blocked(status: int, text: str) -> bool:
    if status in (401, 403, 429):
        return True
    low = (text or "")[:60000].lower()
    if any(m in low for m in _STRONG_MARKERS):
        return True
    size = len(text or "")
    if status >= 400 and size < 25000 and any(m in low for m in _WEAK_MARKERS):
        return True
    # On a normal (200) answer only a tiny challenge page counts: an app shell that merely says
    # "enable JavaScript" (SortMyScene's home page) is not a refusal.
    return status < 400 and size < 8000 and any(m in low for m in _CHALLENGE_PAGE_MARKERS)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ProxyError):
        return "the network or a proxy/firewall refused the connection"
    if isinstance(exc, requests.exceptions.SSLError):
        return "the secure (https) connection failed"
    if isinstance(exc, requests.exceptions.Timeout):
        return "the site did not answer in time"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "no connection (check the internet connection, VPN or firewall)"
    return f"{type(exc).__name__}: {str(exc)[:160]}"


# ------------------------------------------------------------------ robots.txt

class RobotsRules:
    """robots.txt with RFC 9309 semantics (longest match wins, Allow wins ties)."""

    def __init__(self, text: str, agent: str = ROBOTS_AGENT):
        groups: List[Tuple[List[str], List[Tuple[bool, str]]]] = []
        agents: List[str] = []
        rules: List[Tuple[bool, str]] = []
        last_was_agent = False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if agents and (value or key == "allow"):
                    rules.append((key == "allow", value))
            else:
                last_was_agent = False
        if agents or rules:
            groups.append((agents, rules))
        agent = agent.lower()
        specific = [r for a, r in groups if any(x != "*" and x in agent for x in a)]
        chosen = specific or [r for a, r in groups if "*" in a]
        self.rules = [rule for group in chosen for rule in group]

    @staticmethod
    def _match_len(pattern: str, path: str) -> int:
        if not pattern:
            return -1
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = "^" + ".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else "")
        return len(pattern) if re.match(rx, path) else -1

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        best_len, best_allow = -1, True
        for allow, pattern in self.rules:
            n = self._match_len(pattern, path)
            if n > best_len or (n == best_len and allow):
                best_len, best_allow = n, allow
        return best_allow


# ------------------------------------------------------------------- disk cache

class HttpCache:
    """Small sqlite cache of page bodies, safe to share between threads.

    A cache problem never stops a run. If the file stops accepting writes (it was moved or replaced
    while open, for example by a cloud-synced folder), it is reopened once; if that fails too, the run
    carries on without the cache and ``problem`` says why."""

    def __init__(self, path: Optional[Path]):
        self.lock = threading.Lock()
        self.conn = None
        self.path = path
        self.problem = ""
        self._reopened = False
        if path is not None:
            try:
                self._open()
            except (sqlite3.Error, OSError) as exc:
                self._trouble(exc, reopen=False)

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, final_url TEXT, status INTEGER,"
            " fetched REAL, ctype TEXT, body BLOB)"
        )
        self.conn.commit()

    def _trouble(self, exc: Exception, reopen: bool = True) -> None:
        """Called with the lock held (or before the cache is shared)."""
        log.warning("Page cache problem: %s", exc)
        try:
            if self.conn is not None:
                self.conn.close()
        except sqlite3.Error:
            pass
        self.conn = None
        if reopen and not self._reopened and self.path is not None:
            self._reopened = True
            try:
                self._open()
                return
            except (sqlite3.Error, OSError) as again:
                exc = again
                self.conn = None
        self.problem = f"the page cache could not be used ({exc}), so pages were downloaded fresh"

    def get(self, url: str, max_age_s: float) -> Optional[FetchResult]:
        if self.conn is None or max_age_s <= 0:
            return None
        with self.lock:
            try:
                row = self.conn.execute(
                    "SELECT final_url, status, fetched, ctype, body FROM pages WHERE url = ?", (url,)
                ).fetchone() if self.conn is not None else None
            except sqlite3.Error as exc:
                self._trouble(exc)
                return None
        if not row or time.time() - row[2] > max_age_s:
            return None
        try:
            text = zlib.decompress(row[4]).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            return None
        return FetchResult(url, row[0], row[1], text, from_cache=True, content_type=row[3] or "")

    def put(self, res: FetchResult) -> None:
        if self.conn is None:
            return
        body = zlib.compress(res.text.encode("utf-8"), 6)
        with self.lock:
            if self.conn is None:
                return
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?)",
                    (res.url, res.final_url, res.status, time.time(), res.content_type, body),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                self._trouble(exc)

    def prune(self, older_than_days: float = 30) -> None:
        if self.conn is None:
            return
        with self.lock:
            try:
                self.conn.execute("DELETE FROM pages WHERE fetched < ?",
                                  (time.time() - older_than_days * 86400,))
                self.conn.commit()
            except sqlite3.Error as exc:
                self._trouble(exc)

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
                self.conn = None


# --------------------------------------------------------------------- fetcher

@dataclass
class _HostState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_time: float = 0.0
    count: int = 0
    consecutive_blocks: int = 0
    consecutive_failures: int = 0
    disabled_reason: str = ""
    session: Optional[requests.Session] = None
    robots: Optional[RobotsRules] = None
    robots_loaded: bool = False
    stats: Dict[str, int] = field(default_factory=lambda: {"requests": 0, "cached": 0, "blocked": 0, "errors": 0})


class Fetcher:
    def __init__(self, politeness: Politeness, cache: Optional[HttpCache] = None,
                 stop_event: Optional[threading.Event] = None,
                 rewrite: Optional[Callable[[str], str]] = None,
                 unrewrite: Optional[Callable[[str], str]] = None):
        self.p = politeness
        self.cache = cache or HttpCache(None)
        self.stop_event = stop_event or threading.Event()
        # Test seam: serve fake sites locally while every URL keeps its real domain.
        self.rewrite = rewrite
        self.unrewrite = unrewrite or (lambda u: u)
        self._hosts: Dict[str, _HostState] = {}
        self._hosts_lock = threading.Lock()
        self._host_pacing: Dict[str, Tuple[float, float]] = {}

    def set_host_pacing(self, host: str, min_delay: float, max_delay: float) -> None:
        """A wider gap for one site than the speed setting gives (sites that are quick to refuse)."""
        self._host_pacing[host.lower()] = (min_delay, max_delay)

    def reset_cookies(self, host: str) -> None:
        """Forget a site's cookies, e.g. a remembered city that would colour the next city's pages."""
        st = self.host_state(host.lower())
        if st.session is not None:
            st.session.cookies.clear()

    # ---------------------------------------------------------- host state
    def host_state(self, host: str) -> _HostState:
        with self._hosts_lock:
            st = self._hosts.get(host)
            if st is None:
                st = self._hosts[host] = _HostState()
            return st

    def is_disabled(self, url_or_host: str) -> str:
        host = urlsplit(url_or_host).netloc.lower() if "/" in url_or_host else url_or_host.lower()
        return self.host_state(host).disabled_reason

    def note_block(self, host: str, where: str = "") -> bool:
        """Record a refusal from ``host``; returns True when the site is now switched off."""
        st = self.host_state(host)
        st.consecutive_blocks += 1
        st.stats["blocked"] += 1
        if st.consecutive_blocks >= self.p.max_consecutive_blocks and not st.disabled_reason:
            if host.endswith(BROWSER_SUFFIX):
                st.disabled_reason = (
                    f"{host[:-len(BROWSER_SUFFIX)]} refused the browser {st.consecutive_blocks} times in a row; "
                    "stopped using the browser for it this run (normal downloads carry on)."
                )
            else:
                st.disabled_reason = (
                    f"{host} refused {st.consecutive_blocks} requests in a row{where}; stopped contacting it "
                    "for the rest of this run to protect your IP. Try again in a few hours."
                )
            log.warning(st.disabled_reason)
            return True
        return bool(st.disabled_reason)

    def note_success(self, host: str) -> None:
        st = self.host_state(host)
        st.consecutive_blocks = 0
        st.consecutive_failures = 0

    def note_unreachable(self, host: str, error: str) -> None:
        """Two pages in a row that could not be fetched at all: the site is down or unreachable."""
        st = self.host_state(host)
        st.consecutive_failures += 1
        if st.consecutive_failures >= 2 and not st.disabled_reason:
            st.disabled_reason = f"{host} could not be reached - {error}; skipped for the rest of this run."
            log.warning(st.disabled_reason)

    def stats(self) -> Dict[str, Dict[str, int]]:
        with self._hosts_lock:
            return {h: dict(s.stats) for h, s in self._hosts.items()}

    # ---------------------------------------------------------------- pacing
    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while True:
            if self.stop_event.is_set():
                raise Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.25, left))

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise Cancelled()

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        """Hold the site's single request slot, waiting for the polite gap first."""
        st = self.host_state(host)
        while not st.lock.acquire(timeout=0.25):
            self.check_stop()
        try:
            wait = st.next_time - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            yield
        finally:
            st.count += 1
            lo, hi = self.p.min_delay, self.p.max_delay
            floor = self._host_pacing.get(host.split("#", 1)[0])
            if floor and self.p.max_delay > 0:            # tests run with no delay at all
                lo, hi = max(lo, floor[0]), max(hi, floor[1])
            delay = random.uniform(lo, hi)
            if self.p.breather_every and st.count % self.p.breather_every == 0:
                delay += self.p.breather_seconds * random.uniform(0.8, 1.3)
            st.next_time = time.monotonic() + delay
            st.lock.release()

    # ---------------------------------------------------------------- robots
    def allowed_by_robots(self, url: str) -> bool:
        if not self.p.respect_robots_txt:
            return True
        parts = urlsplit(url)
        st = self.host_state(parts.netloc.lower())
        if not st.robots_loaded:
            st.robots_loaded = True
            res = self.get(f"{parts.scheme}://{parts.netloc}/robots.txt", kind="robots", cache_hours=24)
            if res.status == 200 and res.text and "<html" not in res.text[:500].lower():
                st.robots = RobotsRules(res.text)
            elif res.status >= 500 or res.blocked or res.error:
                log.info("robots.txt for %s unavailable (%s); proceeding politely", parts.netloc,
                         res.error or res.status)
        return st.robots.allowed(url) if st.robots else True

    # ------------------------------------------------------------------- get
    def _session(self, st: _HostState) -> requests.Session:
        if st.session is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": self.p.user_agent,
                "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate",
            })
            st.session = s
        return st.session

    def _retry_after(self, resp: requests.Response, attempt: int) -> float:
        value = resp.headers.get("Retry-After", "")
        wait = 0.0
        if value.isdigit():
            wait = float(value)
        elif value:
            try:
                wait = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                wait = 0.0
        if not wait:
            wait = self.p.backoff_base * (2 ** attempt) * random.uniform(1.0, 1.5)
        return min(wait, 180.0)

    def get(self, url: str, kind: str = "page", cache_hours: Optional[float] = None,
            referer: Optional[str] = None, accept_json: bool = False) -> FetchResult:
        """Fetch ``url`` politely. Never raises for HTTP/network problems."""
        self.check_stop()
        real_url = self.rewrite(url) if self.rewrite else url
        if cache_hours is None:
            cache_hours = self.p.listing_cache_hours if kind == "listing" else self.p.detail_cache_hours
        host = urlsplit(url).netloc.lower()   # pacing/refusals/robots belong to the real site
        st = self.host_state(host)
        cached = self.cache.get(real_url, cache_hours * 3600)
        if cached is not None:
            st.stats["cached"] += 1
            cached.url = url
            cached.final_url = self.unrewrite(cached.final_url)
            return cached
        if st.disabled_reason:
            return FetchResult(url, url, 0, "", blocked=True, error=st.disabled_reason)
        if kind != "robots" and not self.allowed_by_robots(url):
            return FetchResult(url, url, 0, "", error="disallowed by the site's robots.txt")

        headers = {
            "Accept": "application/json, text/plain, */*" if accept_json else
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        session = self._session(st)
        last_error = ""
        for attempt in range(self.p.max_retries + 1):
            self.check_stop()
            with self.slot(host):
                st.stats["requests"] += 1
                try:
                    resp = session.get(real_url, headers=headers, timeout=self.p.timeout, allow_redirects=True)
                except requests.RequestException as exc:
                    resp, last_error = None, _friendly_error(exc)
                    log.debug("request to %s failed: %r", url, exc)
            if resp is None:
                st.stats["errors"] += 1
                # connection problems are rarely fixed by waiting long: two quick retries
                if attempt < min(self.p.max_retries, 2):
                    self.sleep(min(3.0, self.p.backoff_base) * (2 ** attempt) * random.uniform(0.5, 1.0))
                    continue
                if kind != "robots":
                    self.note_unreachable(host, last_error)
                return FetchResult(url, url, 0, "", error=last_error)

            if resp.encoding is None or (resp.encoding.lower() == "iso-8859-1" and "charset" not in
                                         resp.headers.get("Content-Type", "").lower()):
                resp.encoding = "utf-8"
            text = resp.text
            ctype = resp.headers.get("Content-Type", "")
            status = resp.status_code
            final_url = self.unrewrite(resp.url)
            if kind != "robots" and looks_blocked(status, text):
                disabled = self.note_block(host, f" (HTTP {status})")
                if status in (429, 503) and attempt < self.p.max_retries and not disabled:
                    self.sleep(self._retry_after(resp, attempt))
                    continue
                return FetchResult(url, final_url, status, text, blocked=True,
                                   error=st.disabled_reason or f"refused with HTTP {status}",
                                   content_type=ctype)
            if status >= 500 and attempt < self.p.max_retries:
                self.sleep(self._retry_after(resp, attempt))
                continue
            self.note_success(host)
            result = FetchResult(url, final_url, status, text, content_type=ctype)
            if 200 <= status < 300:
                stored = FetchResult(real_url, resp.url, status, text, content_type=ctype)
                self.cache.put(stored)
            return result
        return FetchResult(url, url, 0, "", error=last_error or "gave up after retries")

    def close(self) -> None:
        with self._hosts_lock:
            for st in self._hosts.values():
                if st.session is not None:
                    st.session.close()
                    st.session = None
