"""Crawl a website for videos, download each one and upload it to any rclone remote."""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import sys
import threading
from pathlib import Path

from .config import load_config
from .crawler import Crawler
from .db import Store
from .transfer import Transferer


def setup_logging(log_file: str, verbose: bool) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)


def install_stop_handler(stop: threading.Event) -> None:
    def handler(signum, frame):
        if stop.is_set():
            raise SystemExit(130)
        logging.warning("Stopping after current items finish (Ctrl+C again to quit now). Progress is saved.")
        stop.set()

    signal.signal(signal.SIGINT, handler)


def cmd_crawl(cfg, store, stop, args) -> int:
    Crawler(cfg, store, stop).run()
    return cmd_status(cfg, store, stop, args)


def cmd_transfer(cfg, store, stop, args) -> int:
    Transferer(cfg, store, stop).run()
    return cmd_status(cfg, store, stop, args)


def cmd_run(cfg, store, stop, args) -> int:
    """Crawl and transfer at the same time: uploads start as soon as videos are found."""
    crawling = threading.Event()
    crawling.set()

    def crawl():
        try:
            Crawler(cfg, store, stop).run()
        finally:
            crawling.clear()

    t = threading.Thread(target=crawl, name="crawler", daemon=True)
    t.start()
    Transferer(cfg, store, stop, crawling=crawling).run()
    t.join()
    return cmd_status(cfg, store, stop, args)


def cmd_import(cfg, store, stop, args) -> int:
    with open(args.file, encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    added = store.add_videos(urls)
    print(f"Imported {added} new video URLs ({len(urls) - added} already queued).")
    return 0


def cmd_status(cfg, store, stop, args) -> int:
    s = store.stats()
    print("Pages: ", ", ".join(f"{k}={v}" for k, v in sorted(s["pages"].items())) or "none")
    print("Videos:", ", ".join(f"{k}={v}" for k, v in sorted(s["videos"].items())) or "none")
    print(f"Uploaded: {s['bytes_uploaded'] / 1024**3:.2f} GB")
    return 0


def cmd_retry(cfg, store, stop, args) -> int:
    print(f"Requeued {store.retry_failed()} failed videos.")
    return 0


def cmd_failed(cfg, store, stop, args) -> int:
    rows = store.failed_videos()
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "attempts", "error"])
        w.writerows(rows)
    print(f"Wrote {len(rows)} failed videos to {args.out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vxtract", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="crawl and download/upload concurrently (the usual command)")
    sub.add_parser("crawl", help="only crawl and queue video URLs")
    sub.add_parser("transfer", help="only download/upload already-queued videos")
    imp = sub.add_parser("import", help="queue video URLs from a text file, one per line")
    imp.add_argument("file")
    sub.add_parser("status", help="show progress counts")
    sub.add_parser("retry", help="requeue all failed videos")
    fail = sub.add_parser("failed", help="export failed videos to CSV")
    fail.add_argument("--out", default="failed.csv")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg["state"]["log_file"], args.verbose)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    store = Store(cfg["state"]["database"])
    store.reset_in_progress()
    stop = threading.Event()
    install_stop_handler(stop)
    commands = {
        "run": cmd_run, "crawl": cmd_crawl, "transfer": cmd_transfer, "import": cmd_import,
        "status": cmd_status, "retry": cmd_retry, "failed": cmd_failed,
    }
    try:
        return commands[args.command](cfg, store, stop, args)
    finally:
        store.close()
