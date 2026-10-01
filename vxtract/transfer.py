"""Download each queued video with yt-dlp, upload it with rclone, then delete the local copy."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import yt_dlp

from .db import Store

log = logging.getLogger(__name__)


class TransferError(Exception):
    pass


def check_tools(remote: str) -> None:
    if not shutil.which("rclone"):
        raise SystemExit("rclone is not installed or not on PATH. See README.md > Setup.")
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found: videos with separate audio/video streams will fail to merge.")
    if not remote:
        raise SystemExit("destination.remote is empty in config.yaml.")


class Transferer:
    def __init__(self, cfg: dict, store: Store, stop: threading.Event, crawling: threading.Event | None = None):
        self.opts = cfg["transfer"]
        self.dest = cfg["destination"]
        self.store = store
        self.stop = stop
        self.crawling = crawling  # set while a crawler is still feeding the queue
        self.temp_root = Path(self.opts["temp_dir"]).resolve()
        self.done_count = 0
        self._count_lock = threading.Lock()

    def run(self) -> None:
        check_tools(self.dest["remote"])
        self.temp_root.mkdir(parents=True, exist_ok=True)
        threads = [threading.Thread(target=self._worker, args=(i,), name=f"xfer-{i}", daemon=True)
                   for i in range(self.opts["workers"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.info("Transfer workers finished (%d videos this run)", self.done_count)

    def _wait_for_disk(self) -> None:
        min_free = self.opts["min_free_gb"] * 1024**3
        while not self.stop.is_set() and shutil.disk_usage(self.temp_root).free < min_free:
            log.warning("Free disk below %s GB, waiting for uploads to clear space", self.opts["min_free_gb"])
            time.sleep(30)

    def _worker(self, n: int) -> None:
        work_dir = self.temp_root / f"worker-{n}"
        while not self.stop.is_set():
            row = self.store.claim_video()
            if not row:
                if self.crawling is not None and self.crawling.is_set():
                    time.sleep(5)  # crawler may add more
                    continue
                return
            vid, url = row
            self._wait_for_disk()
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True)
            try:
                path = self.download(url, work_dir)
                size = path.stat().st_size
                remote_path = self.upload(path)
                self.store.video_done(vid, remote_path, size)
                with self._count_lock:
                    self.done_count += 1
                log.info("Done #%d %s -> %s (%.1f MB)", vid, url, remote_path, size / 1e6)
            except Exception as e:  # noqa: BLE001 - record and move on to the next video
                log.warning("Failed #%d %s: %s", vid, url, e)
                self.store.video_failed(vid, str(e), self.opts["max_attempts"])
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

    def download(self, url: str, work_dir: Path) -> Path:
        ydl_opts = {
            "format": self.opts["format"],
            "outtmpl": str(work_dir / self.opts["filename_template"]),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 5,
            "fragment_retries": 10,
            "restrictfilenames": False,
            "windowsfilenames": True,
        }
        if self.opts["rate_limit"]:
            ydl_opts["ratelimit"] = yt_dlp.utils.parse_bytes(self.opts["rate_limit"])
        if self.opts["cookies_file"]:
            ydl_opts["cookiefile"] = self.opts["cookies_file"]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        files = [p for p in work_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise TransferError("yt-dlp produced no file")
        return max(files, key=lambda p: p.stat().st_size)

    def upload(self, path: Path) -> str:
        remote_path = self.dest["remote"].rstrip("/") + "/" + path.name
        cmd = ["rclone", "copyto", str(path), remote_path, *self.dest["rclone_flags"]]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise TransferError(f"rclone exit {result.returncode}: {result.stderr.strip()[-1000:]}")
        return remote_path
