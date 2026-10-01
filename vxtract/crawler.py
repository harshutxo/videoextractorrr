"""Polite multi-threaded crawler that records video URLs into the Store."""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .db import Store

log = logging.getLogger(__name__)

VIDEO_META = ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream")


def normalize(url: str) -> str:
    url, _ = urldefrag(url.strip())
    return url


def extract(html: str, base_url: str, video_exts) -> tuple[set[str], set[str]]:
    """Return (page links, direct video URLs) found in an HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    videos: set[str] = set()
    ext_re = re.compile(r"\.(" + "|".join(map(re.escape, video_exts)) + r")(\?|$)", re.I)

    def absolute(href):
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")):
            return None
        url = normalize(urljoin(base_url, href))
        return url if urlparse(url).scheme in ("http", "https") else None

    for tag in soup.find_all(["video", "source"]):
        if url := absolute(tag.get("src")):
            videos.add(url)
    for tag in soup.find_all("meta"):
        if (tag.get("property") or tag.get("name") or "").lower() in VIDEO_META:
            if url := absolute(tag.get("content")):
                videos.add(url)
    for tag in soup.find_all("a"):
        url = absolute(tag.get("href"))
        if not url:
            continue
        if ext_re.search(urlparse(url).path):
            videos.add(url)
        else:
            links.add(url)
    return links, videos


class Robots:
    def __init__(self, user_agent: str, timeout: float):
        self._ua = user_agent
        self._timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._cache:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    resp = requests.get(origin + "/robots.txt", timeout=self._timeout,
                                        headers={"User-Agent": self._ua})
                    if resp.status_code in (401, 403):
                        rp.disallow_all = True
                    elif resp.ok:
                        rp.parse(resp.text.splitlines())
                    else:
                        rp.allow_all = True
                except requests.RequestException:
                    rp.allow_all = True
                self._cache[origin] = rp
            rp = self._cache[origin]
        return rp.can_fetch(self._ua, url)

    def crawl_delay(self, url: str) -> float:
        p = urlparse(url)
        rp = self._cache.get(f"{p.scheme}://{p.netloc}")
        delay = rp.crawl_delay(self._ua) if rp else None
        return float(delay or 0)


class Throttle:
    """Enforce a minimum gap between requests to the same host, across threads."""

    def __init__(self):
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, delay: float) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0))
            self._next[host] = slot + delay
        if slot > now:
            time.sleep(slot - now)


class Crawler:
    def __init__(self, cfg: dict, store: Store, stop: threading.Event):
        self.src = cfg["source"]
        self.opts = cfg["crawl"]
        self.store = store
        self.stop = stop
        self.robots = Robots(self.opts["user_agent"], self.opts["timeout_seconds"])
        self.throttle = Throttle()
        self.include = [re.compile(p) for p in self.src["include_patterns"]]
        self.exclude = [re.compile(p) for p in self.src["exclude_patterns"]]
        self.video_pages = [re.compile(p) for p in self.src["video_page_patterns"]]
        self.domains = self.src["allowed_domains"]
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = self.opts["user_agent"]
            self._local.session = s
        return self._local.session

    def in_scope(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in self.domains):
            return False
        if self.include and not any(p.search(url) for p in self.include):
            return False
        return not any(p.search(url) for p in self.exclude)

    def is_video_page(self, url: str) -> bool:
        return any(p.search(url) for p in self.video_pages)

    def seed(self) -> None:
        self.store.add_pages([normalize(u) for u in self.src["start_urls"]], depth=0)

    def run(self) -> None:
        self.seed()
        threads = [threading.Thread(target=self._worker, name=f"crawl-{i}", daemon=True)
                   for i in range(self.opts["workers"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.info("Crawl finished")

    def _worker(self) -> None:
        idle_since = None
        while not self.stop.is_set():
            row = self.store.claim_page()
            if not row:
                # Other workers may still be adding pages; give up after a quiet spell.
                if not self.store.pages_active():
                    return
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > 60:
                    return
                time.sleep(1)
                continue
            idle_since = None
            url, depth = row
            try:
                status = self._crawl_page(url, depth)
                self.store.finish_page(url, status)
            except Exception as e:  # noqa: BLE001 - one bad page must not kill the worker
                log.warning("Page failed %s: %s", url, e)
                self.store.finish_page(url, "failed", str(e)[:500])

    def _crawl_page(self, url: str, depth: int) -> str:
        if self.opts["respect_robots_txt"] and not self.robots.allowed(url):
            return "skipped"
        if self.is_video_page(url):
            self.store.add_videos([url], page_url=url)

        delay = max(self.opts["delay_seconds"], self.robots.crawl_delay(url))
        self.throttle.wait(urlparse(url).netloc, delay)
        resp = self._session().get(url, timeout=self.opts["timeout_seconds"])
        if resp.status_code == 429:
            header = resp.headers.get("Retry-After", "")
            retry = int(header) if header.isdigit() else 60
            log.warning("429 from %s, backing off %ss", url, retry)
            time.sleep(retry)
            return "pending"
        resp.raise_for_status()
        if "html" not in resp.headers.get("Content-Type", ""):
            return "done"

        links, videos = extract(resp.text, resp.url, self.src["video_extensions"])
        if videos:
            added = self.store.add_videos(videos, page_url=url)
            if added:
                log.info("+%d videos from %s", added, url)

        if depth < self.src["max_depth"] and self.store.page_count() < self.src["max_pages"]:
            self.store.add_pages([u for u in links if self.in_scope(u)], depth + 1)
        return "done"
