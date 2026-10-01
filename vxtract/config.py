from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULTS = {
    "source": {
        "start_urls": [],
        "allowed_domains": [],
        "include_patterns": [],
        "exclude_patterns": [],
        "video_page_patterns": [],
        "video_extensions": ["mp4", "webm", "mov", "m4v", "mkv", "m3u8"],
        "max_depth": 10,
        "max_pages": 500000,
    },
    "crawl": {
        "workers": 4,
        "delay_seconds": 1.0,
        "respect_robots_txt": True,
        "timeout_seconds": 30,
        "user_agent": "videoextractorrr/1.0",
    },
    "transfer": {
        "workers": 4,
        "temp_dir": "./tmp",
        "min_free_gb": 20,
        "max_attempts": 3,
        "format": "bestvideo*+bestaudio/best",
        "rate_limit": "",
        "filename_template": "%(title).80B [%(id)s].%(ext)s",
        "cookies_file": "",
        "cookies_from_browser": "",
    },
    "destination": {
        "local_dir": "",
        "remote": "",
        "rclone_flags": ["--retries", "3", "--low-level-retries", "10"],
    },
    "state": {
        "database": "./videoextractorrr.db",
        "log_file": "./logs/videoextractorrr.log",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}. Copy config.example.yaml to {path} first.")
    with path.open(encoding="utf-8") as f:
        cfg = _merge(DEFAULTS, yaml.safe_load(f) or {})

    src = cfg["source"]
    if not src["allowed_domains"]:
        src["allowed_domains"] = sorted({urlparse(u).hostname for u in src["start_urls"] if urlparse(u).hostname})
    src["allowed_domains"] = [d.lower() for d in src["allowed_domains"]]
    src["video_extensions"] = [e.lower().lstrip(".") for e in src["video_extensions"]]
    return cfg
