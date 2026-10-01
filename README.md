# videoextractorrr

Crawls a website for videos, downloads each one with [yt-dlp](https://github.com/yt-dlp/yt-dlp), uploads it to any
[rclone](https://rclone.org) destination (S3, R2, B2, Google Drive, OneDrive, Dropbox, SFTP, FTP, WebDAV, a local disk…),
then deletes the local copy. It's built for large jobs (hundreds of thousands of videos): all progress lives in a
SQLite database, so you can stop and resume at any time.

> **Use it only on content you own or have permission to copy.** Check the site's terms of service before you start.
> The crawler follows `robots.txt` and rate-limits itself by default. It does not bypass DRM, paywalls or bot protection.

## How it works

```
start_urls ──► crawler ──► pages table ──► video URLs ──► videos table
                                                             │
                     ┌───────────── transfer worker (×N) ◄───┘
                     │  1. yt-dlp download to ./tmp/worker-N
                     │  2. rclone copyto  →  destination.remote
                     │  3. delete local file, mark done
                     └─ failures retry up to max_attempts
```

* **Finding videos:** `<video>`/`<source>` tags, `og:video` meta tags, links to video files, and any page whose URL
  matches `video_page_patterns` (passed to yt-dlp, which supports 1000+ sites).
* **Resumable:** Ctrl+C once lets in-flight items finish; anything interrupted goes back to the queue on the next run.
* **Disk-safe:** peak disk use is about `workers × largest video`, and new downloads pause while free space is below
  `min_free_gb`.

## Setup

1. **Python 3.11+** (3.10 works but yt-dlp has deprecated it).
2. **ffmpeg** to merge separate video and audio streams: `winget install Gyan.FFmpeg`
3. **rclone** for uploads: `winget install Rclone.Rclone`, then run `rclone config` to add your destination
   (for example a remote named `s3` or `gdrive`).
4. Install the program:

   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy config.example.yaml config.yaml
   ```

5. Edit `config.yaml`: set `source.start_urls`, `source.video_page_patterns`, and either
   `destination.local_dir` (save to a folder on this PC, so rclone isn't needed) or `destination.remote`.

### Videos behind a login

Log in to the site in your browser and export its cookies to a `cookies.txt` file (for example with the
"Get cookies.txt LOCALLY" extension). Then set `transfer.cookies_file: cookies.txt`, and add `cookies.txt`
to `.gitignore` because it acts as your login. Videos protected by DRM can't be downloaded this way; get those
from the video host or storage instead.

## Usage

```powershell
python -m vxtract run              # crawl + download/upload at the same time (the usual command)
python -m vxtract status           # progress counts and GB uploaded
python -m vxtract crawl            # only discover and queue videos
python -m vxtract transfer         # only process already-queued videos
python -m vxtract import urls.txt  # skip crawling: queue URLs from a file, one per line
python -m vxtract retry            # requeue everything that failed
python -m vxtract failed --out failed.csv
```

Use `-c other.yaml` to run a separate job with its own database, and `-v` for debug logs.
Logs are written to `logs/videoextractorrr.log`.

## Tuning for ~200,000 videos

* Before a full run, do a trial: set `max_pages: 200`, run it, and check the uploads.
* `transfer.workers` controls throughput. Raise it until your bandwidth is saturated. `crawl.workers` and
  `delay_seconds` control how hard the source site is hit, so keep them gentle.
* At 100 MB per video, 200k videos is about 20 TB. Check the egress and storage pricing for your destination.
* Run it on an always-on machine or VM near the source and destination, not a laptop.

## Tests

```powershell
python -m unittest discover -s tests -v
```
