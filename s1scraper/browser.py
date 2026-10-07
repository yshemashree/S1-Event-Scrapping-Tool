"""Real-browser rendering for pages that only fill in with JavaScript.

Listing grids on BookMyShow/District load more events as you scroll. When a
plain download does not show them, the page is opened in the Edge or Chrome
already installed on the computer (or Playwright's bundled Chromium), scrolled
like a person would, and the JSON the page loads is captured for the
extractor. It runs on one dedicated thread and waits for the same per-site
gap as every other request, so it never adds load on a site.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import random
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from .fetcher import Cancelled, Fetcher, looks_blocked

log = logging.getLogger(__name__)


@dataclass
class BrowserSettings:
    mode: str = "auto"          # "auto": only when a plain download is not enough; "off": never
    headless: bool = True       # False shows the browser window while it works
    channel: str = ""           # "msedge" / "chrome"; empty tries both, then bundled Chromium
    executable_path: str = ""   # explicit browser binary, overrides channel
    max_scrolls: int = 25
    page_timeout: float = 45.0


@dataclass
class RenderResult:
    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    links: List[Tuple[str, str]] = field(default_factory=list)
    json_payloads: List[Tuple[str, Any]] = field(default_factory=list)
    blocked: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.blocked and not self.error


_CLICK_MORE_JS = """
() => {
  const rx = /^\\s*(load|show|view|see)\\s+more(\\s+events)?\\s*$/i;
  const els = Array.from(document.querySelectorAll('button, a, div[role=button], span[role=button]'));
  for (const el of els) {
    const t = (el.innerText || '').trim();
    if (t.length < 30 && rx.test(t)) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) { el.click(); return true; }
    }
  }
  return false;
}
"""


class BrowserService:
    def __init__(self, fetcher: Fetcher, settings: BrowserSettings):
        self.fetcher = fetcher
        self.settings = settings
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._start_error: Optional[str] = None
        self._lock = threading.Lock()
        self.description = ""

    # ------------------------------------------------------------ lifecycle
    def available(self) -> bool:
        if self.settings.mode == "off":
            return False
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="browser", daemon=True)
                self._thread.start()
        while not self._ready.wait(0.25):
            self.fetcher.check_stop()
        return self._start_error is None

    @property
    def start_error(self) -> Optional[str]:
        return self._start_error

    def close(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(None)
            thread.join(timeout=20)

    def _launch(self, pw):
        kwargs = {"headless": self.settings.headless}
        errors = []
        explicit = self.settings.executable_path or os.environ.get("S1_BROWSER_PATH", "")
        if explicit:
            try:
                self.description = f"browser at {explicit}"
                return pw.chromium.launch(executable_path=explicit, **kwargs)
            except Exception as exc:  # noqa: BLE001 - report every launch failure
                errors.append(f"{explicit}: {str(exc).splitlines()[0]}")
        channels = [self.settings.channel] if self.settings.channel else ["msedge", "chrome"]
        for channel in channels + [""]:
            try:
                if channel:
                    browser = pw.chromium.launch(channel=channel, **kwargs)
                    self.description = {"msedge": "Microsoft Edge", "chrome": "Google Chrome"}.get(channel, channel)
                else:
                    browser = pw.chromium.launch(**kwargs)
                    self.description = "Playwright Chromium"
                return browser
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{channel or 'bundled Chromium'}: {str(exc).strip().splitlines()[0][:160]}")
        raise RuntimeError(
            "No usable browser found (" + "; ".join(errors) + "). Install Google Chrome or Microsoft Edge, "
            "or run:  python -m playwright install chromium"
        )

    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._start_error = "Playwright is not installed (pip install playwright)"
            self._ready.set()
            return
        pw = browser = context = None
        try:
            pw = sync_playwright().start()
            browser = self._launch(pw)
            context = browser.new_context(
                locale="en-IN", timezone_id="Asia/Kolkata", viewport={"width": 1366, "height": 900},
            )
            context.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "media", "font") else route.continue_())
            log.info("Browser ready: %s", self.description)
        except Exception as exc:  # noqa: BLE001
            self._start_error = str(exc)
            log.warning("Browser unavailable: %s", exc)
            self._ready.set()
            for closer in (browser, pw):
                try:
                    if closer is not None:
                        closer.close() if closer is browser else closer.stop()
                except Exception:  # noqa: BLE001
                    pass
            return
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                fut, url, kwargs = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    fut.set_result(self._render(context, url, **kwargs))
                except Cancelled:
                    fut.set_result(RenderResult(url=url, error="cancelled"))
                except Exception as exc:  # noqa: BLE001 - a broken page must not kill the thread
                    fut.set_result(RenderResult(url=url, error=f"{type(exc).__name__}: {str(exc)[:300]}"))
        finally:
            for action in (context.close, browser.close, pw.stop):
                try:
                    action()
                except Exception:  # noqa: BLE001
                    pass

    # --------------------------------------------------------------- render
    def render(self, url: str, scroll: bool = True, capture_json: bool = True) -> RenderResult:
        if not self.available():
            return RenderResult(url=url, error=self._start_error or "browser disabled")
        host = urlsplit(url).netloc.lower()
        if self.fetcher.is_disabled(host):
            return RenderResult(url=url, blocked=True, error=self.fetcher.is_disabled(host))
        real_url = self.fetcher.rewrite(url) if self.fetcher.rewrite else url
        fut: Future = Future()
        self._queue.put((fut, real_url, {"scroll": scroll, "capture_json": capture_json, "host": host}))
        while True:
            try:
                res = fut.result(timeout=0.5)
                break
            except FutureTimeout:
                if self.fetcher.stop_event.is_set():
                    fut.cancel()
                    raise Cancelled()
        res.url = url
        un = self.fetcher.unrewrite
        res.final_url = un(res.final_url) if res.final_url else url
        res.links = [(un(h), t) for h, t in res.links]
        res.json_payloads = [(un(u), d) for u, d in res.json_payloads]
        if res.blocked:
            self.fetcher.note_block(host, " (in the browser)")
        elif res.ok:
            self.fetcher.note_success(host)
        return res

    def _render(self, context, url: str, scroll: bool, capture_json: bool, host: str) -> RenderResult:
        responses: list = []
        timeout_ms = int(self.settings.page_timeout * 1000)
        with self.fetcher.slot(host):
            page = context.new_page()
            if capture_json:
                page.on("response", lambda r: responses.append(r) if len(responses) < 200 else None)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = resp.status if resp else 0
                self._settle(page, 8000)
                if scroll:
                    self._scroll(page)
                html = page.content()
                links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => [e.href, (e.innerText || '').slice(0, 400)])")
                payloads: List[Tuple[str, Any]] = []
                for r in responses:
                    try:
                        if r.status != 200 or "json" not in (r.headers.get("content-type") or ""):
                            continue
                        body = r.body()
                        if len(body) > 8_000_000:
                            continue
                        payloads.append((r.url, json.loads(body)))
                    except Exception:  # noqa: BLE001 - bodies of redirects/streams are unavailable
                        continue
                return RenderResult(url=url, final_url=page.url, status=status, html=html, links=links,
                                    json_payloads=payloads, blocked=looks_blocked(status, html))
            finally:
                page.close()

    @staticmethod
    def _settle(page, timeout_ms: int) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - long-polling pages never go idle
            pass

    def _scroll(self, page) -> None:
        still = 0
        for _ in range(max(1, self.settings.max_scrolls)):
            self.fetcher.check_stop()
            before = page.evaluate("[document.querySelectorAll('a[href]').length, document.body ? document.body.scrollHeight : 0]")
            page.mouse.wheel(0, random.randint(1800, 2600))
            # scrollTo + an explicit scroll event: some loaders only listen for the event,
            # which never fires when the content is still shorter than the window
            page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0);"
                          "window.dispatchEvent(new Event('scroll'))")
            page.wait_for_timeout(random.randint(900, 1600))
            clicked = False
            try:
                clicked = bool(page.evaluate(_CLICK_MORE_JS))
            except Exception:  # noqa: BLE001
                pass
            self._settle(page, 4000 if clicked else 2500)
            after = page.evaluate("[document.querySelectorAll('a[href]').length, document.body ? document.body.scrollHeight : 0]")
            if after == before and not clicked:
                still += 1
                if still >= 2:
                    break
            else:
                still = 0
