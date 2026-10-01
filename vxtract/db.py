"""SQLite-backed queue for pages and videos, so a 200k-video job can stop and resume."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url        TEXT PRIMARY KEY,
    depth      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | crawling | done | failed | skipped
    error      TEXT,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS pages_status ON pages(status, depth);

CREATE TABLE IF NOT EXISTS videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    page_url    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | working | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    remote_path TEXT,
    bytes       INTEGER,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS videos_status ON videos(status);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execmany(self, sql: str, rows) -> int:
        with self._lock:
            before = self._conn.total_changes
            self._conn.execute("BEGIN")
            self._conn.executemany(sql, rows)
            self._conn.execute("COMMIT")
            return self._conn.total_changes - before

    def reset_in_progress(self) -> None:
        """Rows left mid-flight by a crash or Ctrl+C go back to the queue."""
        self._exec("UPDATE pages SET status='pending' WHERE status='crawling'")
        self._exec("UPDATE videos SET status='pending' WHERE status='working'")

    # ---- pages ----

    def add_pages(self, urls, depth: int) -> int:
        now = time.time()
        return self._execmany(
            "INSERT OR IGNORE INTO pages(url, depth, updated_at) VALUES (?, ?, ?)",
            [(u, depth, now) for u in urls],
        )

    def claim_page(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT url, depth FROM pages WHERE status='pending' ORDER BY depth LIMIT 1"
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE pages SET status='crawling', updated_at=? WHERE url=?", (time.time(), row[0])
                )
            return row

    def finish_page(self, url: str, status: str, error: str | None = None) -> None:
        self._exec("UPDATE pages SET status=?, error=?, updated_at=? WHERE url=?", (status, error, time.time(), url))

    def page_count(self) -> int:
        return self._exec("SELECT COUNT(*) FROM pages")[0][0]

    def pages_active(self) -> bool:
        return bool(self._exec("SELECT 1 FROM pages WHERE status IN ('pending','crawling') LIMIT 1"))

    # ---- videos ----

    def add_videos(self, urls, page_url: str | None = None) -> int:
        now = time.time()
        return self._execmany(
            "INSERT OR IGNORE INTO videos(url, page_url, updated_at) VALUES (?, ?, ?)",
            [(u, page_url, now) for u in urls],
        )

    def claim_video(self):
        with self._lock:
            row = self._conn.execute("SELECT id, url FROM videos WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                self._conn.execute(
                    "UPDATE videos SET status='working', attempts=attempts+1, updated_at=? WHERE id=?",
                    (time.time(), row[0]),
                )
            return row

    def video_done(self, vid: int, remote_path: str, size: int) -> None:
        self._exec(
            "UPDATE videos SET status='done', error=NULL, remote_path=?, bytes=?, updated_at=? WHERE id=?",
            (remote_path, size, time.time(), vid),
        )

    def video_failed(self, vid: int, error: str, max_attempts: int) -> None:
        """Requeue until max_attempts is reached, then mark failed."""
        self._exec(
            "UPDATE videos SET status=CASE WHEN attempts < ? THEN 'pending' ELSE 'failed' END,"
            " error=?, updated_at=? WHERE id=?",
            (max_attempts, error[:2000], time.time(), vid),
        )

    def retry_failed(self) -> int:
        with self._lock:
            return self._conn.execute(
                "UPDATE videos SET status='pending', attempts=0 WHERE status='failed'"
            ).rowcount

    def failed_videos(self):
        return self._exec("SELECT url, attempts, error FROM videos WHERE status='failed' ORDER BY id")

    # ---- reporting ----

    def stats(self) -> dict:
        pages = dict(self._exec("SELECT status, COUNT(*) FROM pages GROUP BY status"))
        videos = dict(self._exec("SELECT status, COUNT(*) FROM videos GROUP BY status"))
        uploaded = self._exec("SELECT COALESCE(SUM(bytes), 0) FROM videos WHERE status='done'")[0][0]
        return {"pages": pages, "videos": videos, "bytes_uploaded": uploaded}
