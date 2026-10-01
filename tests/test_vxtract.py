import http.server
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

from vxtract.config import DEFAULTS, _merge
from vxtract.crawler import Crawler, extract
from vxtract.db import Store
from vxtract.transfer import Transferer

EXTS = ["mp4", "webm", "m3u8"]


class ExtractTests(unittest.TestCase):
    def test_finds_links_and_videos(self):
        html = """
        <meta property="og:video" content="/media/og.mp4">
        <video src="v1.webm"></video>
        <video><source src="https://cdn.example.com/v2.m3u8?token=1"></video>
        <a href="/page2#top">next</a>
        <a href="clip.MP4">clip</a>
        <a href="mailto:x@example.com">mail</a>
        """
        links, videos = extract(html, "https://example.com/dir/index.html", EXTS)
        self.assertEqual(links, {"https://example.com/page2"})
        self.assertEqual(videos, {
            "https://example.com/media/og.mp4",
            "https://example.com/dir/v1.webm",
            "https://cdn.example.com/v2.m3u8?token=1",
            "https://example.com/dir/clip.MP4",
        })


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dedupe_and_retry_flow(self):
        self.assertEqual(self.store.add_videos(["a", "b", "a"]), 2)
        vid, url = self.store.claim_video()
        self.store.video_failed(vid, "boom", max_attempts=2)
        self.assertEqual(self.store.stats()["videos"], {"pending": 2})

        # Second failure hits max_attempts and sticks.
        for _ in range(2):
            row = self.store.claim_video()
            if row[1] == "a":
                self.store.video_failed(row[0], "boom", max_attempts=2)
            else:
                self.store.video_done(row[0], "remote:b", 10)
        self.assertEqual(self.store.stats()["videos"], {"done": 1, "failed": 1})
        self.assertEqual(self.store.retry_failed(), 1)

    def test_reset_in_progress(self):
        self.store.add_videos(["a"])
        self.store.claim_video()
        self.store.reset_in_progress()
        self.assertEqual(self.store.stats()["videos"], {"pending": 1})


class CrawlAndTransferTests(unittest.TestCase):
    """End-to-end against a local HTTP server, with rclone mocked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "site"
        root.mkdir()
        (root / "index.html").write_text('<a href="p2.html">2</a><a href="private/x.html">x</a>')
        (root / "p2.html").write_text('<video src="clip.mp4"></video><a href="index.html">home</a>')
        (root / "private").mkdir()
        (root / "private" / "x.html").write_text('<video src="secret.mp4"></video>')
        (root / "robots.txt").write_text("User-agent: *\nDisallow: /private/\n")
        (root / "clip.mp4").write_bytes(b"\x00" * 1024)

        handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        handler.log_message = lambda *a: None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

        self.cfg = _merge(DEFAULTS, {
            "source": {"start_urls": [self.base + "/index.html"], "allowed_domains": ["127.0.0.1"]},
            "crawl": {"workers": 2, "delay_seconds": 0},
            "transfer": {"workers": 1, "temp_dir": str(Path(self.tmp.name) / "tmp"), "min_free_gb": 0},
            "destination": {"local_dir": str(Path(self.tmp.name) / "out")},
        })
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def test_crawl_respects_robots_then_transfers(self):
        stop = threading.Event()
        Crawler(self.cfg, self.store, stop).run()
        s = self.store.stats()
        self.assertEqual(s["videos"], {"pending": 1})  # secret.mp4 blocked by robots.txt
        self.assertEqual(s["pages"].get("skipped"), 1)

        Transferer(self.cfg, self.store, stop).run()

        saved = list((Path(self.tmp.name) / "out").rglob("*.mp4"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].stat().st_size, 1024)
        self.assertEqual(self.store.stats()["videos"], {"done": 1})
        # Local copy is deleted after upload.
        leftovers = [p for p in Path(self.cfg["transfer"]["temp_dir"]).rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
