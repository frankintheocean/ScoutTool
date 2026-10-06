import asyncio
import anyio
import atexit
import json
import math
import tempfile
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import database as db
import twitch_api
from twitch_api import TwitchRateLimitedError
import notifier
import alerts
from scoring import calculate_raid_score, get_raid_candidates
from serializers import streamer_to_dict, streamers_to_list
from tracker import tracker_wrapper
from discover_cache import get_cached_discover, set_cached_discover, invalidate_all as invalidate_discover_cache
from logger import logger
from app_version import APP_VERSION, CHANGELOG
from errors import AppError, ErrorCode, register_exception_handlers
from validation import validate_limit, validate_discover_filters, validate_timezone
from auth import BasicAuthMiddleware, LocalServiceTokenMiddleware, _is_authorized as _basic_auth_is_authorized

# Matches twitch.tv/<username> (with or without scheme/www/query string),
# pulled out of arbitrary pasted text so "Quick-add from URL" can accept a
# bare username, a full link, or a link buried in a sentence alike.
TWITCH_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?twitch\.tv/([a-zA-Z0-9_]{2,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)


def parse_twitch_username(text):
    """Extracts a Twitch username from a pasted twitch.tv URL, or falls
    back to treating the whole (trimmed) input as a bare username. Returns
    None if nothing usable was found."""
    if not text:
        return None
    text = text.strip()
    match = TWITCH_URL_RE.search(text)
    if match:
        return match.group(1).lower()
    # No twitch.tv URL found — if it looks like a bare username, allow it.
    bare = text.lstrip("@")
    if re.fullmatch(r"[a-zA-Z0-9_]{2,25}", bare):
        return bare.lower()
    return None

# BUGFIX: resolve frontend/ relative to the actual resource root instead of
# `Path(__file__).parent.parent`. That worked when running from source (cwd
# = backend/, so ../frontend is correct) but broke the packaged Windows
# build entirely: PyInstaller's frozen module path for main.py does not sit
# two directories below the bundle root the way the source tree does, so
# `.parent.parent` pointed at a directory that doesn't exist, FRONTEND_DIR
# never existed, the StaticFiles mount below was silently skipped, and
# every request (including "/") 404'd - exactly what the bug report shows.
# launcher.py already chdir()s into the correct resource base
# (sys._MEIPASS when frozen) before importing this module, so resolving
# against sys._MEIPASS directly here matches that and fixes both cases:
# unpacked next to backend/ from source, or bundled at the dist root
# per build/scoutbot.spec's `datas = [(frontend_path, "frontend")]`.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    FRONTEND_DIR = Path(sys._MEIPASS) / "frontend"
else:
    FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


DISCOVER_ALERT_INTERVAL = 300  # 5 minutes — independent of TRACK_INTERVAL

# How often the cron loop itself wakes up to check which watchlists are
# due — NOT the per-watchlist cadence. Each watchlist's own
# check_interval_seconds (configurable per watchlist, see database.py's
# get_category_watches_due) decides whether it actually gets checked on
# a given tick; this is just the tick granularity. Kept short so a
# watchlist configured for e.g. a 60s interval isn't stuck waiting on a
# coarser shared poll.
CATEGORY_WATCH_CRON_TICK = 30


async def discover_alert_wrapper():
    """Background loop polling every enabled discover_match alert rule.
    Kept as its own task (rather than folded into tracker.py's loop) since
    it hits a different Twitch endpoint on its own cadence and one
    crashing shouldn't affect the other."""
    while True:
        try:
            await asyncio.sleep(DISCOVER_ALERT_INTERVAL)
            await alerts.check_discover_match_rules()
        except asyncio.CancelledError:
            logger.info("Discover alert loop stopped")
            break
        except Exception as e:
            logger.error(f"Discover alert loop error: {e}")


async def category_watch_cron_wrapper():
    """Background loop that automatically runs
    /api/watchlists/{category}/check-style checks for every category
    watchlist, on that watchlist's own configured
    check_interval_seconds — replacing the old behavior where a
    watchlist only ever got checked when someone hit the manual /check
    endpoint. Wakes up every CATEGORY_WATCH_CRON_TICK seconds and asks
    the DB which watchlists are actually due (see
    get_category_watches_due) rather than tracking per-watchlist timers
    in memory, so a changed interval or a restart just takes effect on
    the next tick with no extra bookkeeping. New matches are pushed onto
    the existing SSE/WebSocket notifier feed (same transport
    push_alert_notification already uses) so a connected browser learns
    about them without polling. Kept as its own task, same reasoning as
    discover_alert_wrapper: a different Twitch endpoint, its own
    cadence, and one loop crashing shouldn't take the other down."""
    while True:
        try:
            await asyncio.sleep(CATEGORY_WATCH_CRON_TICK)
            due = await asyncio.to_thread(db.get_category_watches_due)
            for watch in due:
                category = watch["category"]
                try:
                    results = await _run_category_watch_check(category, watch)
                except TwitchRateLimitedError as e:
                    logger.warning(f"Watchlist cron check rate-limited for '{category}': {e}")
                    continue
                except Exception as e:
                    logger.error(f"Watchlist cron check failed for '{category}': {e}")
                    continue

                new_items = [r for r in results if r.get("is_new")]
                if new_items:
                    top = new_items[0]
                    await notifier.push_alert_notification({
                        "type": "watchlist_new_match",
                        "category": category,
                        "summary": f"{len(new_items)} new streamer(s) live in '{category}'",
                        "body": f"Watchlist '{category}': {len(new_items)} new live streamer(s), "
                                f"top: {top.get('display_name', top.get('username'))}.",
                        "count": len(new_items),
                        "items": new_items[:10],
                    })
        except asyncio.CancelledError:
            logger.info("Category watchlist cron loop stopped")
            break
        except Exception as e:
            logger.error(f"Category watchlist cron loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.setup)
    logger.info(f"Active streamers on record: {await asyncio.to_thread(db.get_active_count)}")
    task = asyncio.create_task(tracker_wrapper())
    discover_alert_task = asyncio.create_task(discover_alert_wrapper())
    category_watch_task = asyncio.create_task(category_watch_cron_wrapper())
    try:
        yield
    finally:
        task.cancel()
        discover_alert_task.cancel()
        category_watch_task.cancel()
        await asyncio.gather(task, discover_alert_task, category_watch_task, return_exceptions=True)
        await twitch_api.close_session()
        await asyncio.to_thread(db.close_all_connections)



app = FastAPI(title="TwitchScoutBot Web", version=APP_VERSION, lifespan=lifespan)

# ==========================
# BACKGROUND JOBS / RECOVERY STATE
# ==========================
# Small bounded executor for maintenance work. Network/Twitch work keeps its
# existing async paths; this pool is reserved for local filesystem/SQLite
# operations so a backup, export, or integrity check never blocks FastAPI's
# request thread.
MAINTENANCE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scout-maint")
atexit.register(lambda: MAINTENANCE_EXECUTOR.shutdown(wait=False, cancel_futures=True))
MAINTENANCE_JOBS = {}
MAINTENANCE_LOCK = threading.RLock()
BACKUP_DIR = Path(db.DB_NAME).parent / "backups"
BACKUP_RETENTION = 10


def _job_create(kind):
    job_id = uuid.uuid4().hex
    with MAINTENANCE_LOCK:
        MAINTENANCE_JOBS[job_id] = {
            "id": job_id, "kind": kind, "status": "queued", "progress": 0,
            "message": "Queued", "started_at": None, "completed_at": None,
            "error": None,
        }
    return job_id


def _job_update(job_id, **values):
    with MAINTENANCE_LOCK:
        if job_id in MAINTENANCE_JOBS:
            MAINTENANCE_JOBS[job_id].update(values)


def _run_job(job_id, kind, fn):
    _job_update(job_id, status="running", started_at=datetime.now(timezone.utc).isoformat(), progress=5, message="Starting")
    try:
        result = fn(lambda p, m: _job_update(job_id, progress=max(0, min(100, int(p))), message=m))
        _job_update(job_id, status="complete", progress=100, message="Complete", completed_at=datetime.now(timezone.utc).isoformat(), result=result)
    except Exception as exc:
        logger.exception("Maintenance job failed: %s", kind)
        _job_update(job_id, status="failed", message="Failed", completed_at=datetime.now(timezone.utc).isoformat(), error=str(exc))


def _submit_job(kind, fn):
    with MAINTENANCE_LOCK:
        # Repeated backup clicks share the active job rather than growing
        # the executor's unbounded queue or overwriting recovery points.
        for job in MAINTENANCE_JOBS.values():
            if job["kind"] == kind and job["status"] in ("queued", "running"):
                return job["id"]
        finished = [key for key, job in MAINTENANCE_JOBS.items() if job["status"] in ("complete", "failed")]
        for key in finished[:-99]:
            del MAINTENANCE_JOBS[key]
        job_id = _job_create(kind)
        MAINTENANCE_EXECUTOR.submit(_run_job, job_id, kind, fn)
        return job_id


def _database_integrity(path):
    con = sqlite3.connect(path, timeout=15)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        ok = bool(row and str(row[0]).lower() == "ok")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return ok, row[0] if row else "no result", tables
    finally:
        con.close()


def _create_backup(progress):
    with db.database_lock:
        return _create_backup_locked(progress)


def _create_backup_locked(progress):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(db.DB_NAME):
        raise RuntimeError("No ScoutBot database exists yet")
    progress(20, "Checking database integrity")
    ok, detail, _ = _database_integrity(db.DB_NAME)
    if not ok:
        raise RuntimeError(f"Database integrity check failed: {detail}")
    progress(45, "Creating safe SQLite backup")
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
    target = BACKUP_DIR / f"streamers-{stamp}.db"
    src = sqlite3.connect(db.DB_NAME, timeout=30)
    dst = sqlite3.connect(str(target), timeout=30)
    try:
        src.backup(dst)
    except Exception:
        dst.close()
        target.unlink(missing_ok=True)
        raise
    finally:
        dst.close(); src.close()
    progress(80, "Checking backup")
    ok, detail, _ = _database_integrity(str(target))
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Backup integrity check failed: {detail}")
    backups = sorted(BACKUP_DIR.glob("streamers-*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in backups[BACKUP_RETENTION:]:
        old.unlink(missing_ok=True)
    progress(100, "Backup verified")
    return {"path": str(target), "filename": target.name}


def _validate_import_database(path):
    try:
        ok, detail, tables = _database_integrity(path)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Database integrity check failed: {detail}")
        if "streamers" not in tables:
            raise HTTPException(status_code=400, detail="That database does not contain a ScoutBot streamers table")
        con = sqlite3.connect(path)
        try:
            count = con.execute("SELECT COUNT(*) FROM streamers").fetchone()[0]
            columns = [r[1] for r in con.execute("PRAGMA table_info(streamers)")]
            if not {"id", "username", "url"}.issubset(columns):
                raise HTTPException(status_code=400, detail="That streamers table is missing required ScoutBot columns (id, username, url)")
        finally:
            con.close()
        return {"valid": True, "streamers": int(count), "columns": columns, "integrity": detail}
    except sqlite3.DatabaseError as exc:
        raise HTTPException(status_code=400, detail="That file isn't a valid ScoutBot SQLite database") from exc


def _stage_database(raw):
    fd, path = tempfile.mkstemp(prefix="scout-import-", suffix=".db", dir=Path(db.DB_NAME).parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
        info = _validate_import_database(path)
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            db.setup(con)  # Validate migrations before touching live data.
            con.execute("PRAGMA journal_mode=DELETE")
        finally:
            con.close()
        return path, info
    except Exception as exc:
        for suffix in ("", "-wal", "-shm"):
            Path(path + suffix).unlink(missing_ok=True)
        if isinstance(exc, sqlite3.DatabaseError):
            raise HTTPException(status_code=400, detail="The uploaded database could not be migrated safely") from exc
        raise


def _replace_database(staged):
    # Every database operation holds this lock for its complete transaction.
    # No request/tracker worker can reopen the old file during replacement.
    with db.database_lock:
        if os.path.exists(db.DB_NAME):
            _create_backup(lambda _p, _m: None)
        db.close_all_connections()
        for suffix in ("-wal", "-shm"):
            Path(db.DB_NAME + suffix).unlink(missing_ok=True)
        os.replace(staged, db.DB_NAME)
        db.database_generation += 1
        db._invalidate_all_cache()
        invalidate_discover_cache()
        twitch_api.invalidate_custom_social_patterns_cache()
        twitch_api.invalidate_social_cache()


def _preview_database(raw):
    path, info = _stage_database(raw)
    try:
        info["current_streamers"] = db.get_total_count()
        info["streamer_count_delta"] = info["streamers"] - info["current_streamers"]
        return info
    finally:
        Path(path).unlink(missing_ok=True)


def _import_files(env_raw, db_raw):
    staged = None
    env_tmp = None
    old_env = None
    had_env = os.path.exists(config.ENV_PATH)
    env_written = False
    try:
        if env_raw is not None:
            _validate_uploaded_env(env_raw)
        if db_raw is not None:
            staged, _ = _stage_database(db_raw)
        # Both files have passed validation before either live file changes.
        if env_raw is not None:
            old_env = Path(config.ENV_PATH).read_bytes() if had_env else None
            fd, env_tmp = tempfile.mkstemp(prefix="scout-env-", dir=Path(config.ENV_PATH).parent)
            with os.fdopen(fd, "wb") as out:
                out.write(env_raw)
            os.replace(env_tmp, config.ENV_PATH)
            env_written = True
        if staged is not None:
            _replace_database(staged)
        return (["env"] if env_raw is not None else []) + (["db"] if db_raw is not None else [])
    except Exception:
        if env_written:
            if had_env:
                Path(env_tmp).write_bytes(old_env)
                os.replace(env_tmp, config.ENV_PATH)
            else:
                Path(config.ENV_PATH).unlink(missing_ok=True)
        raise
    finally:
        for path in (staged, env_tmp):
            if path:
                Path(path).unlink(missing_ok=True)


_IMPORT_LOCK = asyncio.Lock()


def _parse_env_preview(raw):
    text = raw.decode("utf-8")
    keys = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return {"valid": True, "keys": sorted(set(k for k in keys if k))}


@app.get("/api/jobs/{job_id}")
def maintenance_job(job_id: str):
    with MAINTENANCE_LOCK:
        job = MAINTENANCE_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Maintenance job not found")
        return dict(job)


@app.post("/api/maintenance/backup")
def create_database_backup():
    return {"ok": True, "job_id": _submit_job("database_backup", _create_backup)}


@app.get("/api/maintenance/backups")
def list_database_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(BACKUP_DIR.glob("streamers-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
        items.append({"filename": path.name, "size": path.stat().st_size, "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    return {"items": items[:BACKUP_RETENTION]}


@app.post("/api/maintenance/import-preview")
async def import_preview(env_file: Optional[UploadFile] = File(None), db_file: Optional[UploadFile] = File(None)):
    if env_file is None and db_file is None:
        raise HTTPException(status_code=400, detail="Choose a file to preview")
    result = {"env": None, "db": None}
    if env_file:
        raw = await env_file.read()
        _validate_uploaded_env(raw)
        result["env"] = _parse_env_preview(raw)
    if db_file:
        raw = await db_file.read()
        result["db"] = await asyncio.to_thread(_preview_database, raw)
    return result


@app.post("/api/maintenance/restore/{filename}")
def restore_database_backup(filename: str):
    # Filename-only addressing prevents path traversal. Restores are always
    # preceded by a fresh backup of the current live database.
    if Path(filename).name != filename or not filename.startswith("streamers-") or not filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Invalid backup filename")
    source = BACKUP_DIR / filename
    if not source.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    staged, info = _stage_database(source.read_bytes())
    try:
        _replace_database(staged)
    finally:
        Path(staged).unlink(missing_ok=True)
    logger.info("Restored database backup %s (%s streamers)", filename, info["streamers"])
    return {"ok": True, "restored": filename, "streamers": info["streamers"]}


@app.get("/api/maintenance/duplicates")
def maintenance_duplicates():
    # Exact usernames are protected by the database UNIQUE constraint. The
    # useful duplicate candidates are therefore aliases and social URLs that
    # point at multiple records, which commonly appears after imports.
    groups = []
    with db.database_lock, db.db() as con:
        alias_rows = con.execute("""
            SELECT LOWER(TRIM(alias)) value, GROUP_CONCAT(username) users, COUNT(*) amount
            FROM streamer_metadata
            WHERE TRIM(alias) <> ''
            GROUP BY LOWER(TRIM(alias)) HAVING COUNT(*) > 1
            ORDER BY amount DESC, value LIMIT 100
        """).fetchall()
        for r in alias_rows:
            groups.append({"type":"alias", "value":r[0], "usernames":r[1].split(","), "count":r[2]})
    return {"items": groups}


@app.get("/api/maintenance/diagnostics")
def maintenance_diagnostics():
    db_size = os.path.getsize(db.DB_NAME) if os.path.exists(db.DB_NAME) else 0
    db_ok = False
    detail = "database not created"
    tables = set()
    if os.path.exists(db.DB_NAME):
        try:
            db_ok, detail, tables = _database_integrity(db.DB_NAME)
        except Exception as exc:
            detail = str(exc)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    latest = max(BACKUP_DIR.glob("streamers-*.db"), key=lambda x: x.stat().st_mtime, default=None)
    with MAINTENANCE_LOCK:
        running = sum(1 for j in MAINTENANCE_JOBS.values() if j.get("status") in ("queued", "running"))
    return {
        "database": {"healthy": db_ok, "integrity": detail, "size": db_size, "tables": len(tables)},
        "database_path": db.DB_NAME,
        "backup": {"count": len(list(BACKUP_DIR.glob("streamers-*.db"))), "latest": latest.name if latest else None},
        "jobs_running": running,
        "twitch_configured": config.is_configured(),
        "version": APP_VERSION,
        "python": sys.version.split()[0],
    }


# Starlette's middleware stack runs last-added-outermost, so CORS (added
# second, below) wraps BasicAuth (added first) — CORS preflight (OPTIONS)
# requests are handled before auth is ever checked, same as before auth
# existed, while every real request still passes through BasicAuth
# before reaching route handlers. See auth.py: a no-op unless both
# WEB_USERNAME/WEB_PASSWORD are set, which is the default (localhost-only)
# setup's behavior, unchanged.
app.add_middleware(BasicAuthMiddleware)

# Additive, independent of BasicAuthMiddleware above — both must pass when
# both are active (this is a no-op unless Dashboard's Electron shell set
# DASHBOARD_LOCAL_TOKEN on this process's env at spawn; see auth.py). Also
# added before CORS below, same as BasicAuthMiddleware, so CORS preflight
# is unaffected.
app.add_middleware(LocalServiceTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Consistent {"detail": ..., "error": {"code", "message", "status", ...}}
# shape across every endpoint — see errors.py. Purely additive: existing
# HTTPException raises below are unchanged and still produce a valid
# `detail` string, they just get an `error.code` alongside it now.
register_exception_handlers(app)


@app.exception_handler(db.StaleDatabaseOperationError)
async def stale_database_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def not_found(username):
    raise HTTPException(status_code=404, detail=f"'{username}' is not tracked")


def rate_limited(e: TwitchRateLimitedError):
    """Turns a TwitchRateLimitedError into a real 429 response (with a
    standard Retry-After header, plus the same value in the JSON body for
    frontends that don't read headers) instead of the previous behavior of
    just retrying silently and, on exhaustion, folding it into a generic
    502. Lets the UI show "Twitch is rate-limiting us, retry in Ns"
    instead of a bare "request failed"."""
    retry_after = max(int(e.retry_after), 1)
    raise HTTPException(
        status_code=429,
        detail=f"Twitch is rate-limiting requests — try again in {retry_after}s",
        headers={"Retry-After": str(retry_after)},
    )



# VOD downloads are intentionally handled locally. This recreates the extension's
# useful workflow inside ScoutBot without routing Twitch media through a third-party
# service: yt-dlp resolves Twitch's public playback playlist and writes the file to
# the user's normal Downloads directory. Jobs are in-memory because the worker
# process owns them; the UI polls status and a restart simply cancels stale work.
VOD_DOWNLOAD_JOBS = {}
VOD_JOB_LOCK = threading.Lock()

def _downloads_dir():
    home = Path.home() / "Downloads"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception:
        home = Path.cwd() / "downloads"
        home.mkdir(parents=True, exist_ok=True)
    return home

def _resolve_thumbnail(url, width=120, height=68):
    """Twitch's Videos API returns thumbnail_url as a template containing
    literal '%{width}x%{height}' placeholders (e.g.
    '...thumb0-%{width}x%{height}.jpg') that the caller is expected to
    substitute with real pixel dimensions before the URL is usable —
    left as-is, the <img> src is simply invalid and never loads, so no
    preview ever showed. Clip thumbnails already come back as concrete
    URLs with no placeholder, so this is a no-op for them."""
    if not url:
        return url
    return url.replace("%{width}", str(width)).replace("%{height}", str(height))

def _video_payload(v):
    return {
        "id": v.get("id"), "title": v.get("title") or "Untitled VOD",
        "url": v.get("url"), "thumbnail_url": _resolve_thumbnail(v.get("thumbnail_url")),
        "duration": v.get("duration"), "created_at": v.get("created_at"),
        "published_at": v.get("published_at"), "view_count": v.get("view_count", 0),
        "type": v.get("type"),
    }

# Resolved once at import time, not per-download — aria2c, if the user
# happens to have it installed and on PATH, downloads noticeably faster
# than yt-dlp's own (also-concurrent) native downloader: it reuses/
# pipelines HTTP connections more aggressively and its congestion
# handling is more tolerant of many parallel small requests, which is
# exactly the fragment-per-HTTP-request pattern Twitch's HLS VODs/clips
# use. It's never a required dependency — nothing here installs it or
# fails without it — this only opts into it when it's already present,
# and falls straight back to yt-dlp's built-in concurrent-fragment
# downloader (still fast, still parallel) otherwise.
_ARIA2C_PATH = shutil.which("aria2c")

def _fastest_download_opts():
    """The fastest yt-dlp download configuration available without
    requiring anything beyond what's already in requirements.txt.

    - concurrent_fragment_downloads: Twitch VODs/clips are HLS —
      dozens to thousands of small .ts fragments — and yt-dlp fetches
      them one at a time by default. 16 concurrent fetches is well
      past the point of diminishing returns for a typical connection
      while staying below the range where Twitch's CDN starts
      resetting/throttling a single client's burst of parallel
      connections (past a few dozen, more concurrency measurably
      *loses* throughput to retries, not gains it).
    - http_chunk_size: concurrent_fragment_downloads alone only helps
      *already-fragmented* sources (HLS/DASH). A clip served as one
      continuous progressive-download URL has nothing to parallelize
      without this — setting a chunk size makes yt-dlp split even a
      single-file download into ranged chunks and fetch those
      concurrently too, so clips get the same speedup as VODs instead
      of silently downloading single-threaded.
    - buffersize: yt-dlp's native (non-external) downloader defaults to
      a 1KB read buffer per stream-copy loop; the resulting syscall
      overhead is measurable at the sustained throughput a fast
      connection can otherwise reach. Bumped to 16KB.
    - retries/fragment_retries: kept high (not just yt-dlp's default of
      10) since a dropped fragment on a fast, high-concurrency transfer
      should be retried rather than let it fail the whole download.
    """
    opts = {
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10 * 1024 * 1024,
        "buffersize": 16 * 1024,
        "retries": 15,
        "fragment_retries": 15,
    }
    if _ARIA2C_PATH:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            # -x: max connections per server, -s/-k: split each fragment
            # into further parallel pieces — aria2c's own equivalent of
            # yt-dlp's concurrent_fragment_downloads, one layer deeper.
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }
    return opts

def _prune_stale_vod_jobs_locked():
    """Removes terminal (complete/failed) VOD download jobs older than an
    hour — well past the ~1s polling window the UI needs, so an in-progress
    poll can never lose the job it's watching. Caller must already hold
    VOD_JOB_LOCK.

    Previously only called from start_vod_download(), so a session where the
    user downloads once and never starts another download left that single
    finished job sitting in memory for the rest of the (potentially
    multi-day, since this is a desktop app people leave open) process
    lifetime. Now also invoked periodically from the tracker loop's existing
    10-minute tick (see tracker.py), so cleanup doesn't depend on another
    download ever being started.
    """
    cutoff = time.time() - 3600
    for stale_id in [
        jid for jid, j in VOD_DOWNLOAD_JOBS.items()
        if j.get("status") in ("complete", "failed") and j.get("_created_ts", cutoff) < cutoff
    ]:
        del VOD_DOWNLOAD_JOBS[stale_id]


def prune_stale_vod_jobs():
    """Lock-acquiring wrapper for _prune_stale_vod_jobs_locked(), for callers
    (e.g. the tracker loop) that don't already hold VOD_JOB_LOCK."""
    with VOD_JOB_LOCK:
        _prune_stale_vod_jobs_locked()


def _run_vod_download(job_id, url, start_time=None, end_time=None):
    with VOD_JOB_LOCK:
        job = VOD_DOWNLOAD_JOBS.get(job_id)
        if not job: return
        job.update(status="downloading", progress=0, error=None, elapsed=0, eta=None, speed=None)
    try:
        import yt_dlp
        suffix = f" [{start_time or 0:g}-{end_time if end_time is not None else 'end'}]" if start_time is not None or end_time is not None else ""
        outtmpl = str(_downloads_dir() / ("%(uploader)s - %(title)s [%(id)s]" + suffix + ".%(ext)s"))
        def hook(d):
            with VOD_JOB_LOCK:
                current = VOD_DOWNLOAD_JOBS.get(job_id)
                if not current: return
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    downloaded = d.get("downloaded_bytes", 0)
                    if total: current["progress"] = round(downloaded * 100 / total, 1)
                    current["status"] = "downloading"
                    # yt-dlp already tracks these per-hook-call — elapsed time
                    # since this download started, current transfer speed
                    # (bytes/sec), and its own ETA estimate (seconds
                    # remaining) — so this reuses them instead of
                    # recomputing from scratch. Twitch VODs are fetched as
                    # many small HLS segments, so 'eta'/'speed' can be None
                    # briefly between fragments; the UI treats that as
                    # "estimating" rather than a hard error. Note: with
                    # external_downloader=aria2c (see _fastest_download_opts),
                    # yt-dlp doesn't get per-fragment progress from aria2c the
                    # same way, so these may stay None for that path — the
                    # download itself is still fast, just less granular to
                    # report on mid-transfer.
                    current["elapsed"] = d.get("elapsed")
                    current["eta"] = d.get("eta")
                    current["speed"] = d.get("speed")
                elif d.get("status") == "finished":
                    current["progress"] = 100
                    current["file"] = d.get("filename")
                    current["eta"] = 0
        opts = {
            "outtmpl": outtmpl, "progress_hooks": [hook], "noplaylist": True, "quiet": True,
            **_fastest_download_opts(),
        }
        if start_time is not None or end_time is not None:
            def ranges(_info, _ydl):
                yield {"start_time": start_time or 0, "end_time": end_time if end_time is not None else float("inf")}
            opts["download_ranges"] = ranges
        with yt_dlp.YoutubeDL(opts) as ydl:
            if ydl.download([url]):
                raise RuntimeError("The download did not complete successfully")
        with VOD_JOB_LOCK:
            VOD_DOWNLOAD_JOBS[job_id].update(status="complete", progress=100, eta=0, completed_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        logger.exception("VOD download failed")
        with VOD_JOB_LOCK:
            if job_id in VOD_DOWNLOAD_JOBS:
                VOD_DOWNLOAD_JOBS[job_id].update(status="failed", error=str(exc))

    finally:
        with VOD_JOB_LOCK:
            _start_queued_vod_downloads()


def with_metadata(rows):
    lookup = db.get_metadata_bulk([row["username"] for row in rows])
    averages = db.get_average_viewers_bulk([row["username"] for row in rows if not row["average_viewers"]])
    return streamers_to_list(rows, lookup, averages)


# ==========================
# STREAMERS: LIST / SEARCH / PAGINATION
# ==========================

@app.get("/api/streamers")
def list_streamers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    include_archived: bool = False,
    sort_by: str = "followers",
    ascending: bool = False,
):
    rows, total = db.get_paginated(
        page=page,
        page_size=page_size,
        include_archived=include_archived,
        sort_by=sort_by,
        ascending=ascending,
    )
    return {
        "items": with_metadata(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/streamers/search")
def search_streamers(
    q: str = None,
    category: str = None,
    location: str = None,
    priority: str = None,
    favourite: bool = None,
    live_only: bool = False,
    min_followers: int = None,
    max_followers: int = None,
    tags: str = None,
    include_archived: bool = False,
    sort_by: str = "followers",
    ascending: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
):
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    rows, total = db.search_streamers(
        query=q,
        category=category,
        location=location,
        priority=priority,
        favourite=favourite,
        live_only=live_only,
        min_followers=min_followers,
        max_followers=max_followers,
        tags=tag_list,
        include_archived=include_archived,
        sort_by=sort_by,
        ascending=ascending,
        page=page,
        page_size=page_size,
    )
    return {
        "items": with_metadata(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/streamers/{username}/vods")
async def get_streamer_vods(username: str, limit: int = 30):
    limit = validate_limit(limit)
    row = await asyncio.to_thread(db.get_streamer, username)
    if not row: not_found(username)
    user = await twitch_api.get_user_id(username)
    if not user: raise HTTPException(status_code=404, detail="Twitch channel not found")
    videos = await twitch_api.get_channel_videos(user["user_id"], limit)
    return {"items": [_video_payload(v) for v in videos]}

@app.get("/api/streamers/{username}/clips")
async def get_streamer_clips(username: str, limit: int = 30):
    limit = validate_limit(limit)
    row = await asyncio.to_thread(db.get_streamer, username)
    if not row: not_found(username)
    user = await twitch_api.get_user_id(username)
    if not user: raise HTTPException(status_code=404, detail="Twitch channel not found")
    clips = await twitch_api.get_channel_clips(user["user_id"], limit)
    return {"items": clips}

@app.post("/api/vod-downloads")
def start_vod_download(payload: dict):
    url = str(payload.get("url") or "").strip()
    # Clips come back from Twitch's Get Clips endpoint with a
    # clips.twitch.tv URL (e.g. https://clips.twitch.tv/SomeClipSlug),
    # not a twitch.tv/<channel>/... one — the old www.-only pattern
    # rejected every clip download with "URL is required" even though
    # a valid clip was selected. VODs still come from twitch.tv/videos/…
    # and www.twitch.tv/…, both still matched. Any twitch.tv subdomain
    # is accepted; the domain itself stays pinned to twitch.tv.
    if not re.match(r"^https?://(?:[\w-]+\.)?twitch\.tv/", url, re.I):
        raise HTTPException(status_code=400, detail="A Twitch VOD or clip URL is required")
    start_time = payload.get("start_time")
    end_time = payload.get("end_time")
    try:
        start_time = float(start_time) if start_time not in (None, "") else None
        end_time = float(end_time) if end_time not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid start or end time")
    if any(value is not None and not math.isfinite(value) for value in (start_time, end_time)):
        raise HTTPException(status_code=400, detail="Start and end times must be finite")
    if start_time is not None and start_time < 0: raise HTTPException(status_code=400, detail="Start time cannot be negative")
    if end_time is not None and end_time <= 0: raise HTTPException(status_code=400, detail="End time must be positive")
    if start_time is not None and end_time is not None and end_time <= start_time:
        raise HTTPException(status_code=400, detail="End time must be after start time")
    job_id = uuid.uuid4().hex
    with VOD_JOB_LOCK:
        _prune_stale_vod_jobs_locked()
        request_key = (url, start_time, end_time)
        for existing in VOD_DOWNLOAD_JOBS.values():
            if existing.get("_request_key") == request_key and existing["status"] in ("queued", "downloading"):
                return {k: v for k, v in existing.items() if not k.startswith("_")}
        VOD_DOWNLOAD_JOBS[job_id] = {"id": job_id, "status": "queued", "progress": 0, "error": None, "file": None, "elapsed": None, "eta": None, "speed": None, "created_at": datetime.now(timezone.utc).isoformat(), "_created_ts": time.time(), "_request_key": request_key}
    with VOD_JOB_LOCK:
        _start_queued_vod_downloads()
        return {k: v for k, v in VOD_DOWNLOAD_JOBS[job_id].items() if not k.startswith("_")}

@app.get("/api/vod-downloads/{job_id}")
def get_vod_download(job_id: str):
    with VOD_JOB_LOCK:
        job = VOD_DOWNLOAD_JOBS.get(job_id)
        if not job: raise HTTPException(status_code=404, detail="Download job not found")
        return {k: v for k, v in job.items() if not k.startswith("_")}

@app.get("/api/streamers/{username}")
def get_streamer(username: str):
    row = db.get_streamer(username)
    if not row:
        not_found(username)
    metadata = db.get_streamer_metadata(username)
    latest_response = db.get_latest_response(username)
    return streamer_to_dict(row, metadata, latest_response)


class AddStreamerBody(BaseModel):
    username: str


@app.get("/api/parse-url")
def parse_url(text: str):
    """Quick-add helper: given arbitrary pasted text (a twitch.tv URL, or
    just a username), returns the extracted username so the frontend can
    pre-fill the add-streamer flow. Does not touch Twitch or the DB."""
    username = parse_twitch_username(text)
    if not username:
        raise HTTPException(status_code=422, detail="Could not find a Twitch username or twitch.tv link in that text")
    return {"username": username}


@app.post("/api/streamers")
async def add_streamer(body: AddStreamerBody):
    # Guest mode: browsing (viewing the roster, discover results, stats,
    # etc.) never required Twitch credentials to begin with, but adding a
    # streamer does a live Twitch lookup, so it's the one action that
    # can't work without them. Fail clearly here instead of letting it
    # fall through to a generic 502 from the Twitch call below.
    generation = db.database_generation
    if not config.is_configured():
        raise HTTPException(
            status_code=401,
            detail="Connect a Twitch app (Settings → General) to add streamers — browsing doesn't require it",
        )

    username = parse_twitch_username(body.username) or body.username.strip().lstrip("@").lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    if await asyncio.to_thread(db.streamer_exists, username):
        raise HTTPException(status_code=409, detail=f"'{username}' is already tracked")

    if await asyncio.to_thread(db.get_active_count) >= db.MAX_ACTIVE_STREAMERS:
        raise HTTPException(
            status_code=400,
            detail=f"Active streamer soft limit ({db.MAX_ACTIVE_STREAMERS}) reached — archive some first",
        )

    try:
        data = await twitch_api.get_streamer_data(username)
    except TwitchRateLimitedError as e:
        rate_limited(e)
    except Exception as e:
        logger.error(f"Twitch lookup failed for {username}: {e}")
        raise HTTPException(status_code=502, detail="Twitch API request failed — check TWITCH_CLIENT_ID/SECRET")

    if not data:
        raise HTTPException(status_code=404, detail=f"Twitch user '{username}' not found")

    from datetime import datetime

    await asyncio.to_thread(db.add_streamer, [
        data["username"].lower(),
        f"https://twitch.tv/{data['username']}",
        data.get("profile_image"),
        datetime.now().isoformat(),
        data.get("category", "Unknown"),
        data.get("followers", 0),
        0,
        data.get("live_viewers", 0),
        data.get("live_status", "Offline"),
    ], _generation=generation)

    # Kick off the bio/panel social-link scrape in the background rather
    # than awaiting it here — it's a separate, best-effort GraphQL call
    # (see twitch_api.get_channel_social) that shouldn't add latency to
    # "add streamer" or fail the request if Twitch's web GQL endpoint is
    # slow/unavailable. Errors are already swallowed inside the scrape
    # itself; this just also guards the DB write in case the streamer
    # gets removed again before the task runs.
    async def _scrape_and_store():
        try:
            social = await twitch_api.get_channel_social(username)
            if not social.get("_failed"):
                await asyncio.to_thread(db.set_scraped_social, 
                    username,
                    social.get("bio", ""),
                    social.get("social_links", {}),
                    location=social.get("location"),
                    timezone=social.get("timezone"),
                    age=social.get("age"),
                    _generation=generation,
                )
        except Exception as e:
            logger.warning(f"⚠️ Background social scrape failed for {username}: {e}")

    asyncio.create_task(_scrape_and_store())

    row = await asyncio.to_thread(db.get_streamer, username)
    return streamer_to_dict(row, await asyncio.to_thread(db.get_streamer_metadata, username))


@app.delete("/api/streamers/{username}")
def remove_streamer(username: str):
    if not db.remove_streamer(username):
        not_found(username)
    return {"ok": True}


@app.post("/api/streamers/{username}/archive")
def archive_streamer(username: str):
    if not db.archive_streamer(username):
        raise HTTPException(status_code=400, detail="Already archived or not found")
    return {"ok": True}


@app.post("/api/streamers/{username}/unarchive")
def unarchive_streamer(username: str):
    if not db.unarchive_streamer(username):
        raise HTTPException(status_code=400, detail="Not archived or not found")
    return {"ok": True}


@app.post("/api/streamers/{username}/refresh")
async def refresh_streamer(username: str):
    generation = db.database_generation
    if not await asyncio.to_thread(db.streamer_exists, username):
        not_found(username)

    try:
        data = await twitch_api.get_streamer_data(username)
    except TwitchRateLimitedError as e:
        rate_limited(e)
    except Exception as e:
        logger.error(f"Twitch lookup failed for {username}: {e}")
        raise HTTPException(status_code=502, detail="Twitch API request failed — check TWITCH_CLIENT_ID/SECRET")

    if not data:
        raise HTTPException(status_code=404, detail="Twitch data unavailable")

    await asyncio.to_thread(db.update_twitch_data, 
        username,
        data.get("category", "Unknown"),
        data.get("followers", 0),
        data.get("live_viewers", 0),
        data.get("live_status", "Offline"),
        _generation=generation,
    )
    if data.get("profile_image"):
        await asyncio.to_thread(db.update_profile_image, username, data["profile_image"], _generation=generation)

    row = await asyncio.to_thread(db.get_streamer, username)
    new_score = await asyncio.to_thread(calculate_raid_score, row)
    await asyncio.to_thread(db.update_raid_score, username, new_score, manual=False, _generation=generation)

    row = await asyncio.to_thread(db.get_streamer, username)
    return streamer_to_dict(row, await asyncio.to_thread(db.get_streamer_metadata, username))


# ==========================
# NOTES / RATINGS / TAGS / ALIAS
# ==========================

class NotesBody(BaseModel):
    notes: str


@app.put("/api/streamers/{username}/notes")
def update_notes(username: str, body: NotesBody):
    if not db.update_notes(username, body.notes):
        not_found(username)
    return {"ok": True}


class SocialLinksBody(BaseModel):
    x_url: str = None
    instagram_url: str = None
    youtube_url: str = None
    kick_url: str = None


@app.put("/api/streamers/{username}/social")
def update_social_links(username: str, body: SocialLinksBody):
    if not db.streamer_exists(username):
        not_found(username)
    db.set_social_links(
        username,
        x_url=body.x_url,
        instagram_url=body.instagram_url,
        youtube_url=body.youtube_url,
        kick_url=body.kick_url,
    )
    return {"ok": True}


class LocationBody(BaseModel):
    location: str = None
    timezone: str = None


@app.put("/api/streamers/{username}/location")
def update_location(username: str, body: LocationBody):
    """Manually-entered location/timezone — separate from the scraped
    ones (see database.set_location), so this never clobbers, and is
    never clobbered by, a bio re-scrape. Either field may be sent alone;
    an empty string clears that field, omitting it (null) leaves it
    unchanged."""
    if not db.streamer_exists(username):
        not_found(username)
    if body.timezone:
        validate_timezone(body.timezone)
    db.set_location(
        username,
        location=body.location,
        timezone=body.timezone,
    )
    return {"ok": True}


class BulkLocationBody(BaseModel):
    usernames: list[str]
    location: str = None
    timezone: str = None


@app.put("/api/streamers/location/bulk")
def update_location_bulk(body: BulkLocationBody):
    """Bulk variant of PUT /api/streamers/{username}/location — applies
    the same location/timezone pair to every username in `usernames` in
    one request, for a multi-select bulk-edit action in the UI instead
    of one PUT per streamer. Same field semantics as the single-streamer
    endpoint: either field may be sent alone, an empty string clears
    that field, omitting it (null) leaves it unchanged. Usernames that
    aren't tracked are silently skipped (same streamer_exists() gate the
    single-streamer endpoint applies) rather than failing the whole
    batch — the response's `updated` count reflects how many were
    actually tracked and written."""
    if not body.usernames:
        raise HTTPException(status_code=400, detail="usernames is required")
    if body.location is None and body.timezone is None:
        raise HTTPException(status_code=400, detail="location or timezone is required")
    if body.timezone:
        validate_timezone(body.timezone)

    updated = db.set_location_bulk(
        body.usernames,
        location=body.location,
        timezone=body.timezone,
    )
    return {"ok": True, "updated": updated}


@app.post("/api/streamers/{username}/social/scrape")
async def scrape_social_links(username: str):
    """Scrapes the channel's About-tab bio and profile panels for social
    links (see twitch_api.get_channel_social) and stores them separately
    from the user-entered x_url/instagram_url/youtube_url/kick_url
    fields. Also backfills youtube_url/kick_url from the scrape when
    those roster fields are still blank (see
    database.backfill_scraped_platform_links) — x_url/instagram_url are
    never touched by a scrape. Best-effort: never raises on scrape
    failure — a channel with no bio/panels, or a transient lookup
    failure, just leaves scraped_social_links empty rather than
    erroring the request."""
    generation = db.database_generation
    if not await asyncio.to_thread(db.streamer_exists, username):
        not_found(username)

    social = await twitch_api.get_channel_social(username)
    if not social.get("_failed"):
        await asyncio.to_thread(db.set_scraped_social, 
            username,
            social.get("bio", ""),
            social.get("social_links", {}),
            location=social.get("location"),
            timezone=social.get("timezone"),
            age=social.get("age"),
            _generation=generation,
        )
        await asyncio.to_thread(db.backfill_scraped_platform_links, username, social.get("social_links", {}), _generation=generation)

    row = await asyncio.to_thread(db.get_streamer, username)
    return streamer_to_dict(row, await asyncio.to_thread(db.get_streamer_metadata, username))


@app.post("/api/streamers/social/scrape-all")
async def scrape_all_social_links():
    """Re-scrapes bio/panel social links (and location/timezone) for
    every currently-tracked, non-archived streamer in one request — the
    bulk equivalent of POST /streamers/{username}/social/scrape, so this
    doesn't have to be done one streamer/usercard at a time. Runs the
    per-streamer scrapes concurrently (same underlying scraper, which is
    already best-effort/never-raises — see twitch_api.get_channel_social)
    and reports a simple success/failure count rather than failing the
    whole request if a handful of channels have nothing to find or
    transiently fail."""
    generation = db.database_generation
    usernames = [row["username"] for row in await asyncio.to_thread(db.get_all, include_archived=False)]

    scrape_slots = asyncio.Semaphore(8)

    async def _scrape_one(username):
        async with scrape_slots:
            return await _scrape_one_bounded(username)

    async def _scrape_one_bounded(username):
        try:
            social = await twitch_api.get_channel_social(username)
            if not social.get("_failed"):
                await asyncio.to_thread(db.set_scraped_social, 
                    username,
                    social.get("bio", ""),
                    social.get("social_links", {}),
                    location=social.get("location"),
                    timezone=social.get("timezone"),
                    age=social.get("age"),
                    _generation=generation,
                )
                await asyncio.to_thread(db.backfill_scraped_platform_links, username, social.get("social_links", {}), _generation=generation)
            return not social.get("_failed", False)
        except Exception as e:
            logger.warning(f"⚠️ Bulk re-scrape failed for {username}: {e}")
            return False

    results = await asyncio.gather(*(_scrape_one(u) for u in usernames))
    succeeded = sum(1 for r in results if r)

    return {
        "ok": True,
        "total": len(usernames),
        "succeeded": succeeded,
        "failed": len(usernames) - succeeded,
    }


class RatingBody(BaseModel):
    category: str  # community | content | raid
    score: int


@app.put("/api/streamers/{username}/rating")
def update_rating(username: str, body: RatingBody):
    if body.category not in ("community", "content", "raid"):
        raise HTTPException(status_code=400, detail="category must be community, content, or raid")
    if not (1 <= body.score <= 5):
        raise HTTPException(status_code=400, detail="score must be 1-5")
    if not db.update_rating(username, body.category, body.score):
        not_found(username)
    db.recalculate_raid_score(username)
    return {"ok": True}


class RaidScoreBody(BaseModel):
    score: int


@app.put("/api/streamers/{username}/raidscore")
def set_manual_raid_score(username: str, body: RaidScoreBody):
    if not (0 <= body.score <= 100):
        raise HTTPException(status_code=400, detail="score must be 0-100")
    if not db.update_raid_score(username, body.score):
        not_found(username)
    return {"ok": True}


@app.post("/api/streamers/{username}/calculate_raid")
def calc_raid(username: str):
    row = db.get_streamer(username)
    if not row:
        not_found(username)
    score = calculate_raid_score(row)
    db.update_raid_score(username, score, manual=False)
    db.add_raid_history(username, score, "calculated")
    return {"username": username, "raid_score": score}


class TagsBody(BaseModel):
    tags: str  # comma-separated


@app.put("/api/streamers/{username}/tags")
def set_tags(username: str, body: TagsBody):
    if not db.streamer_exists(username):
        not_found(username)
    tags = [t.strip() for t in body.tags.split(",") if t.strip()]
    db.set_tags(username, tags)
    return {"ok": True, "tags": tags}


class BulkTagsBody(BaseModel):
    usernames: list[str]
    tags: str  # comma-separated


@app.put("/api/streamers/tags/bulk")
def set_tags_bulk(body: BulkTagsBody):
    """Bulk variant of PUT /api/streamers/{username}/tags — applies the
    same tag list to every username in `usernames` in one request, for
    a multi-select bulk-edit action in the UI instead of one PUT per
    streamer. Usernames that aren't tracked are silently skipped (same
    streamer_exists() gate the single-streamer endpoint applies) rather
    than failing the whole batch — the response's `updated` count
    reflects how many were actually tracked and written."""
    if not body.usernames:
        raise HTTPException(status_code=400, detail="usernames is required")

    tags = [t.strip() for t in body.tags.split(",") if t.strip()]
    updated = db.set_tags_bulk(body.usernames, tags)
    return {"ok": True, "updated": updated, "tags": sorted({t.lower() for t in tags})}


@app.get("/api/tags")
def all_tags():
    return {"tags": db.get_all_tags()}


@app.get("/api/tags/search")
def tag_search(tag: str, match_all: bool = False, include_archived: bool = False):
    rows = db.search_tags([t.strip() for t in tag.split(",")], match_all, include_archived)
    return {"items": with_metadata(rows)}


class AliasBody(BaseModel):
    alias: str


@app.put("/api/streamers/{username}/alias")
def set_alias(username: str, body: AliasBody):
    if not db.streamer_exists(username):
        not_found(username)
    db.set_alias(username, body.alias)
    return {"ok": True}


# ==========================
# FAVOURITES / PRIORITY / NOTIFY
# ==========================

@app.post("/api/streamers/{username}/favourite/toggle")
def toggle_favourite(username: str):
    if not db.streamer_exists(username):
        not_found(username)
    new_value = db.toggle_favourite(username)
    return {"favourite": new_value}


@app.get("/api/favourites")
def favourites(include_archived: bool = False, limit: Optional[int] = None):
    # `limit` is optional and unvalidated-by-default to preserve the
    # existing unbounded response every current caller (roster's
    # Favourites view, the manage-favourites list, etc.) relies on —
    # only the Dashboard's Favourites widget passes one, to cut payload
    # size on a large favourites list instead of fetching everything
    # and slicing client-side. Reuses the same bounds (1-100) as the
    # app's other capped list endpoints when a value is given.
    validated_limit = validate_limit(limit) if limit is not None else None
    rows = db.get_favourites(include_archived, validated_limit)
    return {"items": with_metadata(rows)}


class PriorityBody(BaseModel):
    priority: str  # High | Medium | Watch | Ignore


@app.put("/api/streamers/{username}/priority")
def set_priority(username: str, body: PriorityBody):
    valid = {"High", "Medium", "Watch", "Ignore"}
    if body.priority not in valid:
        raise HTTPException(status_code=400, detail=f"priority must be one of {sorted(valid)}")
    if not db.streamer_exists(username):
        not_found(username)
    db.set_priority(username, body.priority)
    return {"ok": True}


class BulkPriorityBody(BaseModel):
    usernames: list[str]
    priority: str  # High | Medium | Watch | Ignore


@app.put("/api/streamers/priority/bulk")
def set_priority_bulk(body: BulkPriorityBody):
    """Bulk variant of PUT /api/streamers/{username}/priority — applies
    the same priority to every username in `usernames` in one request,
    for a multi-select bulk-edit action in the UI instead of one PUT
    per streamer. Usernames that aren't tracked are silently skipped
    (same streamer_exists() gate the single-streamer endpoint applies)
    rather than failing the whole batch — the response's `updated`
    count reflects how many were actually tracked and written."""
    valid = {"High", "Medium", "Watch", "Ignore"}
    if body.priority.capitalize() not in valid:
        raise HTTPException(status_code=400, detail=f"priority must be one of {sorted(valid)}")
    if not body.usernames:
        raise HTTPException(status_code=400, detail="usernames is required")

    updated = db.set_priority_bulk(body.usernames, body.priority)
    return {"ok": True, "updated": updated}


@app.post("/api/streamers/{username}/notify/toggle")
def toggle_notify(username: str):
    if not db.streamer_exists(username):
        not_found(username)
    new_value = db.toggle_notify(username)
    return {"notify_enabled": new_value}


# ==========================
# STREAMER BLACKLIST
# ==========================
# Hard exclude — separate from priority="Ignore" (see database.py's
# add_to_blacklist docstring). Unlike priority/favourite/notify above,
# this doesn't require db.streamer_exists(): blacklisting works for any
# username, tracked or not, since the whole point is keeping a name out
# of Discover results even if it's never been added to the roster.

def _start_queued_vod_downloads():
    # Caller holds VOD_JOB_LOCK. Reserve workers before starting threads;
    # concurrent requests cannot exceed two active download workers.
    active = sum(1 for job in VOD_DOWNLOAD_JOBS.values() if job.get("_started") and job["status"] in ("queued", "downloading"))
    for job in VOD_DOWNLOAD_JOBS.values():
        if active >= 2: break
        if job["status"] != "queued" or job.get("_started"): continue
        job["_started"] = True
        url, start_time, end_time = job["_request_key"]
        try:
            threading.Thread(target=_run_vod_download, args=(job["id"], url, start_time, end_time), daemon=True).start()
            active += 1
        except Exception as exc:
            job.update(status="failed", error=str(exc))


class BlacklistBody(BaseModel):
    username: str


@app.get("/api/blacklist")
def get_blacklist():
    return {"items": db.get_blacklist()}


@app.post("/api/blacklist")
def add_to_blacklist(body: BlacklistBody):
    username = body.username.strip().lstrip("@").lower()
    if not username:
        raise AppError(status_code=422, detail="username is required", code=ErrorCode.VALIDATION_ERROR)
    db.add_to_blacklist(username)
    invalidate_discover_cache()
    return {"ok": True, "username": username}


@app.delete("/api/blacklist/{username}")
def remove_from_blacklist(username: str):
    if not db.remove_from_blacklist(username):
        raise AppError(status_code=404, detail=f"'{username}' is not blacklisted", code=ErrorCode.NOT_FOUND)
    invalidate_discover_cache()
    return {"ok": True}


# ==========================
# CUSTOM SOCIAL PLATFORMS
# ==========================
# Settings-defined platforms (label + URL-matching pattern, e.g.
# "Linktree" / "linktree.com/") merged into twitch_api.SOCIAL_PATTERNS
# at scrape time, alongside the built-in platforms — see
# twitch_api.get_effective_social_patterns(). This lets a bio/panel link
# to a platform this app doesn't otherwise recognize get labelled and
# surfaced instead of dropped as unrecognized.

class CustomSocialPlatformBody(BaseModel):
    label: str
    pattern: str


@app.get("/api/social-platforms")
def get_custom_social_platforms():
    return {"items": db.get_custom_social_platforms()}


@app.post("/api/social-platforms")
def add_custom_social_platform(body: CustomSocialPlatformBody):
    label = body.label.strip()
    pattern = body.pattern.strip()
    if not label:
        raise AppError(status_code=422, detail="label is required", code=ErrorCode.VALIDATION_ERROR)
    if not pattern:
        raise AppError(status_code=422, detail="pattern is required", code=ErrorCode.VALIDATION_ERROR)
    db.add_custom_social_platform(label, pattern)
    twitch_api.invalidate_custom_social_patterns_cache()
    # BUGFIX: also clear the 6h scraped-bio cache, not just the 30s
    # pattern cache — otherwise streamers scraped before this platform
    # existed keep missing scraped_social_links for it (and therefore
    # never show its icon on their card) until that cache happens to expire.
    twitch_api.invalidate_social_cache()
    return {"ok": True, "label": label, "pattern": pattern}


@app.delete("/api/social-platforms/{label}")
def remove_custom_social_platform(label: str):
    if not db.remove_custom_social_platform(label):
        raise AppError(status_code=404, detail=f"'{label}' is not a custom social platform", code=ErrorCode.NOT_FOUND)
    twitch_api.invalidate_custom_social_patterns_cache()
    twitch_api.invalidate_social_cache()
    return {"ok": True}


# ==========================
# OUTREACH LIFECYCLE
# ==========================
# Tracks where a streamer sits in the raid-outreach pipeline — separate
# from Priority (attention ranking) and Archive (storage visibility).

class OutreachStatusBody(BaseModel):
    status: str  # active | contacted | raided | declined


@app.put("/api/streamers/{username}/outreach")
def set_outreach_status(username: str, body: OutreachStatusBody):
    status = body.status.strip().lower()
    if status not in db.OUTREACH_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(db.OUTREACH_STATUSES)}")
    if not db.streamer_exists(username):
        not_found(username)
    db.set_outreach_status(username, status)
    return {"ok": True, "outreach_status": status}


@app.get("/api/outreach/{status}")
def outreach_by_status(status: str, include_archived: bool = False):
    status = status.strip().lower()
    if status not in db.OUTREACH_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(db.OUTREACH_STATUSES)}")
    rows = db.get_by_outreach_status(status, include_archived)
    return {"items": with_metadata(rows)}


# ==========================
# DISCOVERY / SEARCH LIVE TWITCH
# ==========================

@app.get("/api/discover")
async def discover(
    category: str = None,
    min_viewers: int = None,
    max_viewers: int = None,
    broadcaster_type: str = None,
    language: str = None,
    tags: str = None,
    exclude_tags: str = None,
    min_followers: int = None,
    max_followers: int = None,
    created_after: str = None,
    created_before: str = None,
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str = None,
):
    """Discover was previously hard-capped at a single page (up to ~100
    Twitch results, trimmed to `limit`) with no way to see more. `cursor`
    now threads through to twitch_api.search_streams's Twitch pagination
    cursor: pass the `next_cursor` from a previous response back in here
    to fetch the next page. Omit it (or pass nothing) to start over.

    `tags` includes only streams matching at least one of the given tags;
    `exclude_tags` drops any stream matching at least one of the given
    tags (e.g. exclude 'vtuber' to filter out vtuber streamers). Both are
    comma-separated. `created_after`/`created_before` filter by the
    channel's Twitch account creation date (ISO date, e.g. '2020-01-01').

    First-page results (no cursor) are cached for a short TTL per unique
    filter combo — see discover_cache.py — so repeatedly re-running the
    same search (reopening the modal, a saved Discover alert polling,
    etc.) doesn't re-hit Twitch every time. Paginated "load more" requests
    always go live, since a cursor is only valid for one specific page."""
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    exclude_tag_list = [t.strip() for t in exclude_tags.split(",")] if exclude_tags else None

    cache_filters = dict(
        category=category, min_viewers=min_viewers, max_viewers=max_viewers,
        broadcaster_type=broadcaster_type, language=language, tags=tag_list,
        exclude_tags=exclude_tag_list, min_followers=min_followers,
        max_followers=max_followers, created_after=created_after,
        created_before=created_before, limit=limit,
    )

    if cursor is None:
        cached = get_cached_discover(**cache_filters)
        if cached is not None:
            return cached

    try:
        results, next_cursor = await twitch_api.search_streams(
            category=category,
            min_viewers=min_viewers,
            max_viewers=max_viewers,
            broadcaster_type=broadcaster_type,
            language=language,
            tags=tag_list,
            exclude_tags=exclude_tag_list,
            min_followers=min_followers,
            max_followers=max_followers,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            cursor=cursor,
            blacklist=await asyncio.to_thread(db.get_blacklist_set),
        )
    except TwitchRateLimitedError as e:
        rate_limited(e)
    except Exception as e:
        logger.error(f"Twitch discover failed: {e}")
        raise HTTPException(status_code=502, detail="Twitch API request failed — check TWITCH_CLIENT_ID/SECRET")

    # Live Twitch search results carry no location data of their own
    # (Twitch's stream-search API doesn't expose one, and bio-scraping
    # every result here would be far more Twitch calls than results
    # shown). For a result that's already tracked, this app already has a
    # best-effort location on file (manual or scraped) — attach it so
    # Discover's location filter/sort has something to work with for
    # those entries; untracked results simply have none.
    #
    # Batched into a single WHERE username IN (...) lookup rather than
    # one streamer_exists() + one get_streamer_metadata() call per result
    # (each its own query) — keeps this to one DB round trip regardless
    # of how many results come back.
    tracked_locations = await asyncio.to_thread(db.get_tracked_locations_bulk, [r["username"] for r in results])
    for r in results:
        r["already_tracked"] = r["username"] in tracked_locations
        r["location"] = tracked_locations.get(r["username"], "")

    payload = {"items": results, "next_cursor": next_cursor}

    if cursor is None:
        set_cached_discover(payload, **cache_filters)

    return payload


@app.get("/api/categories")
def category_stats(
    include_archived: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    rows = db.get_category_stats(include_archived, limit=limit, offset=offset)
    return {"items": [dict(r) for r in rows]}


@app.get("/api/locations")
def location_stats(
    include_archived: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Populates the roster/Discover location filter dropdown — same
    shape as /api/categories, grouped by each tracked streamer's
    effective location (manual, falling back to scraped). `limit`/
    `offset` cap the result set for very large rosters, same as
    /api/categories — both defaulted generously (200) so this is a
    no-op for any roster smaller than that."""
    rows = db.get_location_stats(include_archived, limit=limit, offset=offset)
    return {"items": [dict(r) for r in rows]}


# Coarse country/region label per IANA timezone "area" (the part before
# the first "/", e.g. "America", "Europe", "Asia") — used to bucket the
# raid map below by continent/ocean-region when a streamer only has a
# timezone (not a parseable country) on file. Deliberately a label for
# the broad IANA area rather than a full tz-name-to-country table (which
# would need a real geocoding dependency to keep accurate) — "avoid
# unnecessary dependencies" per the brief. Falls back to the raw area
# name (title-cased) for anything not listed.
_TZ_AREA_LABELS = {
    "America": "Americas",
    "Europe": "Europe",
    "Asia": "Asia",
    "Africa": "Africa",
    "Australia": "Australia",
    "Pacific": "Pacific",
    "Atlantic": "Atlantic",
    "Indian": "Indian Ocean",
    "Antarctica": "Antarctica",
}


def _map_region_for(timezone):
    """Buckets a streamer onto the raid map: IANA timezone's area (e.g.
    "America/Chicago" -> "Americas") when a timezone is on file, else
    "Unspecified" so a location-only streamer (location set, timezone
    guess missed) still shows up somewhere instead of vanishing from the
    map entirely."""
    if not timezone or "/" not in timezone:
        return "Unspecified"
    area = timezone.split("/", 1)[0]
    return _TZ_AREA_LABELS.get(area, area.replace("_", " "))


@app.get("/api/locations/map")
def locations_map(include_archived: bool = False):
    """Powers the Raid Map: groups tracked streamers with a location on
    file into coarse regions (see _map_region_for) for a lightweight
    geocoded-ish overview, without pulling in a mapping/geocoding
    dependency — regions are derived from each streamer's own
    IANA timezone (already stored/guessed via /api/guess-timezone), not
    a new lookup. Each region lists its streamers (username, live
    status, followers, on-file location/timezone) so the map can drill
    down from a region into who's actually there."""
    rows = db.get_map_points(include_archived=include_archived)

    regions: dict[str, dict] = {}
    for row in rows:
        region = _map_region_for(row["timezone"])
        bucket = regions.setdefault(region, {"region": region, "count": 0, "live_count": 0, "streamers": []})
        bucket["count"] += 1
        is_live = row["live_status"] == "Live"
        if is_live:
            bucket["live_count"] += 1
        bucket["streamers"].append({
            "username": row["username"],
            "profile_image": row["profile_image"],
            "live": is_live,
            "followers": row["followers"] or 0,
            "location": row["location"],
            "timezone": row["timezone"],
        })

    items = sorted(regions.values(), key=lambda b: b["count"], reverse=True)
    return {"items": items}


@app.get("/api/guess-timezone")
def guess_timezone(location: str = ""):
    """Best-effort IANA timezone guess for a free-text location string,
    reusing the same LOCATION_TZ_LOOKUP table used server-side when
    resolving a scraped bio location (see twitch_api._guess_timezone).
    Powers the manual location editor's auto-suggest: as the person
    types a location, the frontend calls this to pre-select the
    timezone dropdown instead of requiring a manual pick from the full
    IANA list. Returns {"timezone": None} rather than erroring when
    nothing matches — a miss here just means the dropdown stays as the
    person left it."""
    return {"timezone": twitch_api._guess_timezone(location)}


# ==========================
# DISCOVER SEARCH HISTORY
# ==========================
# Recent Discover filter combos, most-recent first, so the modal can
# offer "recent searches" instead of the person re-entering filters every
# time. The frontend calls POST after a successful search and GET when
# opening the modal; recording is a separate, best-effort call rather
# than folded into GET /api/discover itself so a search that errors out
# (rate limited, upstream failure) doesn't get remembered as if it
# succeeded.

@app.get("/api/discover/history")
def discover_search_history(limit: int = 10):
    limit = validate_limit(limit)
    rows = db.get_discover_search_history(limit)
    return {
        "items": [
            {
                "id": r["id"],
                "filters": json.loads(r["filters"]),
                "last_used": r["last_used"],
            }
            for r in rows
        ]
    }


class DiscoverHistoryBody(BaseModel):
    filters: dict = {}


@app.post("/api/discover/history")
def save_discover_search(body: DiscoverHistoryBody):
    filters = validate_discover_filters(body.filters)
    db.add_discover_search(filters)
    return {"ok": True}


@app.delete("/api/discover/history/{entry_id}")
def delete_discover_search(entry_id: int):
    if not db.delete_discover_search(entry_id):
        raise AppError(status_code=404, detail=f"No search history entry with id {entry_id}", code=ErrorCode.NOT_FOUND)
    return {"ok": True}


@app.delete("/api/discover/history")
def clear_discover_search_history():
    db.clear_discover_search_history()
    return {"ok": True}


# ==========================
# RECENTLY VIEWED STREAMERS
# ==========================
# Quick-access list of streamers whose detail panel was recently opened.
# The frontend logs a view (POST) each time selectStreamer() loads a
# streamer, and reads the list (GET) for the rail. Kept distinct from
# `/api/recent` (which sorts by `discovered`, i.e. when a streamer was
# first tracked, not when it was last looked at).

@app.get("/api/recently-viewed")
def recently_viewed(limit: int = 10, include_archived: bool = False):
    limit = validate_limit(limit)
    rows = db.get_recently_viewed(limit, include_archived)
    return {"items": with_metadata(rows)}


class RecentlyViewedBody(BaseModel):
    username: str


@app.post("/api/recently-viewed")
def log_recently_viewed(body: RecentlyViewedBody):
    username = body.username.strip().lower()
    if not username:
        raise AppError(status_code=400, detail="username is required", code=ErrorCode.VALIDATION_ERROR)
    if not db.streamer_exists(username):
        not_found(username)
    db.add_recently_viewed(username)
    return {"ok": True}


# ==========================
# STREAMER SUGGESTIONS
# ==========================
# "Streamers similar to ones you've favourited" — ranks the rest of the
# (non-favourited, non-archived) roster by category + tag overlap with
# the person's current favourites. All local (roster metadata already on
# hand), no extra Twitch calls. Returns an empty list rather than an
# error when there's nothing to base suggestions on (no favourites yet,
# or none with a category/tags set) — an empty state is the frontend's
# job to explain, not a failure.

@app.get("/api/suggestions")
def streamer_suggestions(limit: int = 10):
    limit = validate_limit(limit)
    rows = db.get_streamer_suggestions(limit)
    return {"items": with_metadata(rows)}


# ==========================
# CATEGORY WATCHLISTS
# ==========================
# Follows a Twitch category (rather than an individual streamer), so new
# or trending channels streaming it can be surfaced without already
# knowing their name. Reuses twitch_api.search_streams, the same lookup
# Discover already relies on — no new Twitch integration needed.

DEFAULT_WATCHLIST_INTERVAL = 300  # 5 minutes — matches the previous fixed cadence; now just the default


class CategoryWatchBody(BaseModel):
    category: str
    min_viewers: int = 0
    check_interval_seconds: int = DEFAULT_WATCHLIST_INTERVAL


class CategoryWatchIntervalBody(BaseModel):
    check_interval_seconds: int


@app.get("/api/watchlists")
def list_category_watches():
    rows = db.get_category_watches()
    return {
        "items": [
            {
                "id": r["id"],
                "category": r["category"],
                "min_viewers": r["min_viewers"] or 0,
                "seen_count": len([u for u in (r["seen_usernames"] or "").split(",") if u]),
                "created_at": r["created_at"],
                "last_checked": r["last_checked"],
                "check_interval_seconds": r["check_interval_seconds"] or DEFAULT_WATCHLIST_INTERVAL,
            }
            for r in rows
        ]
    }


@app.post("/api/watchlists")
def create_category_watch(body: CategoryWatchBody):
    category = body.category.strip()
    if not category:
        raise HTTPException(status_code=400, detail="category is required")
    if body.check_interval_seconds < 30:
        raise HTTPException(status_code=400, detail="check_interval_seconds must be at least 30")
    db.add_category_watch(category, body.min_viewers or 0)
    db.set_category_watch_interval(category, body.check_interval_seconds)
    return {"ok": True}


@app.delete("/api/watchlists/{category}")
def delete_category_watch(category: str):
    if not db.remove_category_watch(category):
        raise HTTPException(status_code=404, detail=f"'{category}' is not on the watchlist")
    return {"ok": True}


@app.put("/api/watchlists/{category}/interval")
def set_category_watch_interval(category: str, body: CategoryWatchIntervalBody):
    """Sets this watchlist's own check cadence for the background cron
    loop (see category_watch_cron_wrapper) — each watchlist can run on
    a different interval instead of sharing one fixed cadence."""
    if body.check_interval_seconds < 30:
        raise HTTPException(status_code=400, detail="check_interval_seconds must be at least 30")
    if not db.set_category_watch_interval(category, body.check_interval_seconds):
        raise HTTPException(status_code=404, detail=f"'{category}' is not on the watchlist")
    return {"ok": True}


async def _run_category_watch_check(category: str, watch, limit: int = 25):
    """Shared core of POST /api/watchlists/{category}/check and the
    background cron loop (category_watch_cron_wrapper) — looks up
    currently-live channels in this category (same Twitch lookup
    Discover uses), diffs them against the usernames already seen for
    this watch to flag which are new since the last check, and updates
    the remembered "seen" set. `watch` is the already-fetched
    category_watchlists row (avoids a redundant lookup when the caller
    already has it, as the cron loop does)."""
    min_viewers = watch["min_viewers"] or 0
    previously_seen = {u for u in (watch["seen_usernames"] or "").split(",") if u}

    results, _ = await twitch_api.search_streams(
        category=category,
        min_viewers=min_viewers or None,
        limit=limit,
    )

    current_usernames = {r["username"].lower() for r in results if r.get("username")}

    # PERF: was one db.streamer_exists() call per result (N queries per
    # check). Now on the background cron loop this runs automatically
    # every CATEGORY_WATCH_CRON_TICK for every due watchlist instead of
    # only on a manual click, so N-queries-per-check adds up fast with
    # several watchlists configured on a short interval. One batched
    # lookup instead.
    tracked = await asyncio.to_thread(db.streamers_exist_bulk, current_usernames)

    for r in results:
        uname = (r.get("username") or "").lower()
        r["already_tracked"] = uname in tracked
        r["is_new"] = uname not in previously_seen

    await asyncio.to_thread(db.update_category_watch_seen, category, sorted(current_usernames))

    return results


@app.post("/api/watchlists/{category}/check")
async def check_category_watch(category: str, limit: int = 25):
    limit = validate_limit(limit)
    watch = await asyncio.to_thread(db.get_category_watch, category)
    if not watch:
        raise HTTPException(status_code=404, detail=f"'{category}' is not on the watchlist")

    try:
        results = await _run_category_watch_check(category, watch, limit)
    except TwitchRateLimitedError as e:
        rate_limited(e)
    except Exception as e:
        logger.error(f"Watchlist check failed for '{category}': {e}")
        raise HTTPException(status_code=502, detail="Twitch API request failed — check TWITCH_CLIENT_ID/SECRET")

    return {
        "category": category,
        "items": results,
        "new_count": sum(1 for r in results if r["is_new"]),
    }


# ==========================
# NOTIFICATION / ALERT RULES
# ==========================
# See alerts.py for delivery/evaluation and database.py for storage.
# Two kinds: 'streamer_live' (target = tracked username, fires from the
# tracker's Offline->Live transition) and 'discover_match' (target = JSON
# Discover filters, polled periodically). Delivery channels are any
# combination of webhook / browser / email.

def alert_rule_to_dict(row):
    d = dict(row)
    d["enabled"] = bool(d.get("enabled", 1))
    d["channels"] = [c for c in (d.get("channels") or "").split(",") if c]
    if d["kind"] == "discover_match":
        try:
            d["target"] = json.loads(d["target"])
        except (TypeError, ValueError):
            pass
    return d


class AlertRuleBody(BaseModel):
    kind: str  # streamer_live | discover_match
    target: str | dict  # username, or a discover filters dict
    channels: list[str]
    webhook_url: str = ""
    email: str = ""


@app.get("/api/alerts/rules")
def list_alert_rules():
    rows = db.get_alert_rules()
    return {"items": [alert_rule_to_dict(r) for r in rows]}


@app.post("/api/alerts/rules")
def create_alert_rule(body: AlertRuleBody):
    kind = body.kind.strip().lower()
    if kind not in db.ALERT_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(db.ALERT_KINDS)}")

    channels = {c.strip().lower() for c in body.channels if c.strip()}
    if not channels:
        raise HTTPException(status_code=400, detail="At least one channel is required")
    if not channels.issubset(db.ALERT_CHANNELS):
        raise HTTPException(status_code=400, detail=f"channels must be a subset of {sorted(db.ALERT_CHANNELS)}")
    if "webhook" in channels and not body.webhook_url.strip():
        raise HTTPException(status_code=400, detail="webhook_url is required for the webhook channel")
    if "email" in channels and not body.email.strip():
        raise HTTPException(status_code=400, detail="email is required for the email channel")

    if kind == "streamer_live":
        target = (body.target or "").strip().lower() if isinstance(body.target, str) else ""
        if not target:
            raise HTTPException(status_code=400, detail="target must be a username for a streamer_live rule")
        if not db.streamer_exists(target):
            not_found(target)
    else:
        filters = body.target if isinstance(body.target, dict) else {}
        if not filters:
            raise HTTPException(status_code=400, detail="target must be a filters object for a discover_match rule")
        target = json.dumps(filters)

    rule_id = db.add_alert_rule(kind, target, channels, webhook_url=body.webhook_url.strip(), email=body.email.strip())
    return alert_rule_to_dict(db.get_alert_rule(rule_id))


@app.post("/api/alerts/rules/{rule_id}/toggle")
def toggle_alert_rule(rule_id: int):
    rule = db.get_alert_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail=f"No alert rule with id {rule_id}")
    new_value = not bool(rule["enabled"])
    db.set_alert_rule_enabled(rule_id, new_value)
    return {"enabled": new_value}


@app.delete("/api/alerts/rules/{rule_id}")
def remove_alert_rule(rule_id: int):
    if not db.delete_alert_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"No alert rule with id {rule_id}")
    return {"ok": True}


@app.get("/api/alerts/deliveries")
def list_alert_deliveries(rule_id: int = None, limit: int = 50):
    rows = db.get_alert_deliveries(rule_id, limit)
    return {"items": [dict(r) for r in rows]}


# ==========================
# STREAMER RESPONSE TRACKING
# ==========================
# Logs whether a scouted streamer has acknowledged being scouted (followed
# back, replied, reacted, declined, or explicitly no response). Separate
# from outreach_status (the current pipeline stage) — this is a
# timestamped log, so a streamer can have several response events over
# the course of being scouted.

class StreamerResponseBody(BaseModel):
    response_type: str
    note: str = ""


@app.post("/api/streamers/{username}/responses")
def add_response(username: str, body: StreamerResponseBody):
    if not db.streamer_exists(username):
        not_found(username)
    response_type = body.response_type.strip().lower()
    if response_type not in db.RESPONSE_TYPES:
        raise HTTPException(status_code=400, detail=f"response_type must be one of {sorted(db.RESPONSE_TYPES)}")
    response_id = db.add_streamer_response(username, response_type, body.note.strip())
    return {"id": response_id, "ok": True}


@app.get("/api/streamers/{username}/responses")
def list_responses(username: str, limit: int = 25):
    limit = validate_limit(limit)
    if not db.streamer_exists(username):
        not_found(username)
    rows = db.get_streamer_responses(username, limit)
    return {"items": [dict(r) for r in rows]}


@app.delete("/api/streamers/{username}/responses/{response_id}")
def remove_response(username: str, response_id: int):
    if not db.delete_streamer_response(response_id, username):
        raise HTTPException(status_code=404, detail=f"No response with id {response_id}")
    return {"ok": True}


# ==========================
# RAID SUITABILITY
# ==========================

@app.get("/api/raid/candidates")
def raid_candidates(limit: int = 5):
    limit = validate_limit(limit)
    rows = get_raid_candidates(limit)
    return {"items": with_metadata(rows)}


@app.get("/api/raid/{username}/history")
def raid_history(username: str, limit: int = 25):
    limit = validate_limit(limit)
    if not db.streamer_exists(username):
        not_found(username)
    return {"items": [dict(r) for r in db.get_raid_history(username, limit)]}


# ==========================
# STATS
# ==========================

@app.get("/api/stats")
def stats(include_archived: bool = False):
    return db.get_scout_stats(include_archived)


# 🧩 Combined Dashboard fetch — the Dashboard view (see frontend
# renderDashboard()) previously fired 4-5 separate requests in parallel
# every time it loaded (stats, live-now search, favourites, raid
# candidates, recently-viewed), each a fast local DB read on its own but
# adding up to several full HTTP round trips just to paint one screen.
# All five sources are synchronous/local (no Twitch calls — see
# get_raid_candidates/db.search_streamers/db.get_favourites/
# db.get_recently_viewed/db.get_scout_stats), so combining them into one
# handler is safe: nothing here can rate-limit or block on an external
# call the way e.g. /api/discover can. Limits mirror the widget size
# options the frontend already offers (3/6/10), reusing the same
# validate_limit bounds as the equivalent standalone endpoints.
@app.get("/api/dashboard-summary")
def dashboard_summary(
    live_limit: int = Query(default=6, ge=1, le=100),
    favourites_limit: int = Query(default=6, ge=1, le=100),
    raid_limit: int = Query(default=6, ge=1, le=100),
    recently_viewed_limit: int = Query(default=6, ge=1, le=100),
    include_archived: bool = False,
):
    live_rows, _ = db.search_streamers(
        live_only=True,
        include_archived=include_archived,
        sort_by="current_viewers",
        ascending=False,
        page=1,
        page_size=live_limit,
    )
    favourite_rows = db.get_favourites(include_archived, favourites_limit)
    raid_rows = get_raid_candidates(raid_limit)
    recently_viewed_rows = db.get_recently_viewed(recently_viewed_limit, include_archived)

    return {
        "stats": db.get_scout_stats(include_archived),
        "live_now": {"items": with_metadata(live_rows)},
        "favourites": {"items": with_metadata(favourite_rows)},
        "raid": {"items": with_metadata(raid_rows)},
        "recently_viewed": {"items": with_metadata(recently_viewed_rows)},
    }


@app.get("/api/streamers/{username}/history")
def streamer_history(username: str, limit: int = 25):
    limit = validate_limit(limit)
    if not db.streamer_exists(username):
        not_found(username)
    return {
        "viewer_history": [dict(r) for r in db.get_viewer_history(username, limit)],
        "category_history": [dict(r) for r in db.get_category_history(username, limit)],
        "events": [dict(r) for r in db.get_events(username, limit)],
        "sessions": [dict(r) for r in db.get_stream_sessions(username, limit)],
    }


@app.get("/api/streamers/{username}/sessions/{session_id}/curve")
def streamer_session_curve(username: str, session_id: int):
    # Single-session viewer curve (start to end) for the session replay
    # view, as opposed to the aggregate peak/average shown per row in the
    # sessions list above.
    if not db.streamer_exists(username):
        not_found(username)
    result = db.get_session_viewer_curve(username, session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@app.get("/api/random")
def random_streamer(include_archived: bool = False):
    row = db.get_random_streamer(include_archived)
    if not row:
        raise HTTPException(status_code=404, detail="No streamers tracked yet")
    return streamer_to_dict(row, db.get_streamer_metadata(row["username"]))


@app.get("/api/streamers/{username}/nearest")
def nearest_by_viewers(username: str, limit: int = 10, include_archived: bool = False):
    limit = validate_limit(limit)
    row = db.get_streamer(username)
    if not row:
        not_found(username)
    rows = db.get_nearest_by_viewers(username, row["current_viewers"] or 0, limit, include_archived)
    return {"items": with_metadata(rows)}


@app.get("/api/inactive")
def inactive_streamers(days: int = 30, include_archived: bool = False):
    rows = db.get_inactive_streamers(days, include_archived)
    return {"items": with_metadata(rows)}


@app.get("/api/recent")
def recent_streamers(limit: int = 10, include_archived: bool = False):
    limit = validate_limit(limit)
    rows = db.get_recent_streamers(limit, include_archived)
    return {"items": with_metadata(rows)}


@app.get("/api/archived")
def archived_streamers(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    rows = db.get_archived_streamers(limit, offset)
    return {"items": with_metadata(rows), "total": db.get_archived_count()}


# ==========================
# VERSION / CHANGELOG
# ==========================

@app.get("/api/version")
def version():
    return {"version": APP_VERSION, "changelog": CHANGELOG}


# ==========================
# SAVED FILTER PRESETS
# ==========================
# Lets the roster's filter/sort combo (search text, priority, category,
# live-only, tags, sort) be saved once under a name (e.g. "Live + High
# priority + VALORANT") and reapplied from the rail later. The filter
# shape itself is owned by the frontend — this just stores/returns
# whatever JSON object it's given under a unique name.

class FilterPresetBody(BaseModel):
    name: str
    filters: dict


@app.get("/api/filter-presets")
def list_filter_presets():
    rows = db.get_filter_presets()
    return {
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "filters": json.loads(r["filters"]) if r["filters"] else {},
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@app.post("/api/filter-presets")
def create_filter_preset(body: FilterPresetBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required")
    db.save_filter_preset(name, json.dumps(body.filters))
    return {"ok": True}


@app.delete("/api/filter-presets/{name}")
def remove_filter_preset(name: str):
    if not db.delete_filter_preset(name):
        raise HTTPException(status_code=404, detail=f"No preset named '{name}'")
    return {"ok": True}


# ==========================
# SETTINGS: TWITCH CREDENTIALS
# ==========================
# Lets the person paste in their Twitch app's Client ID/Secret from the
# browser on first run instead of hand-editing a .env file — see
# config.save_twitch_credentials(). The frontend checks "configured" on
# load and shows a setup screen if it's false.

@app.get("/api/settings/twitch")
def get_twitch_settings():
    return {
        "configured": config.is_configured(),
        # Client ID isn't secret and is useful to show it's already set;
        # the secret itself is never sent back to the browser once saved.
        "client_id": config.TWITCH_CLIENT_ID,
        # Rate-limit telemetry (see twitch_api._record_rate_limit_headers) —
        # a point-in-time snapshot of Twitch's own Ratelimit-Limit/
        # Ratelimit-Remaining/Ratelimit-Reset headers from the most recent
        # Helix call, plus a `low` flag so the UI can warn proactively
        # instead of only finding out via a 429. All fields are null until
        # the first Twitch call of the process completes.
        "rate_limit": twitch_api.get_rate_limit_snapshot(),
    }


# ==========================
# SETTINGS: ACCESS / AUTH STATUS
# ==========================
# Read-only status for the Settings > General panel — never returns the
# actual username/password (those live only in .env / config.py and are
# set by editing .env, same as Twitch credentials used to be before the
# in-app setup screen existed; auth is meant to be configured once before
# exposing the app beyond localhost, not toggled from the browser).

@app.get("/api/settings/auth")
def get_auth_settings():
    return {
        "enabled": config.auth_enabled(),
        "host": config.WEB_HOST,
    }


class TwitchCredentialsBody(BaseModel):
    client_id: str
    client_secret: str


@app.post("/api/settings/twitch")
async def set_twitch_settings(body: TwitchCredentialsBody):
    client_id = body.client_id.strip()
    client_secret = body.client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Both Client ID and Client Secret are required")

    # Verify the credentials actually work before saving them, so a typo
    # doesn't get written to .env and silently break Twitch calls.
    async with _IMPORT_LOCK:
        try:
            session = await twitch_api.get_session()
            async with session.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "client_credentials",
            }) as response:
                result = await response.json()
                token = result.get("access_token") if response.status == 200 else None
        except Exception as e:
            logger.error("Twitch credential check failed (%s)", type(e).__name__)
            raise HTTPException(status_code=502, detail="Couldn't reach Twitch to verify those credentials — check your internet connection and try again")
        if not token:
            raise HTTPException(status_code=400, detail="Twitch rejected those credentials — double-check the Client ID and Secret")
        await asyncio.to_thread(config.save_twitch_credentials, client_id, client_secret)
    return {"ok": True}


# ==========================
# SYSTEM / APP LIFECYCLE
# ==========================
# Lets the frontend's Settings > General "Exit ScoutBot" button shut the
# whole app down (server + the console window it runs in) instead of the
# person having to switch back to the minimised console and close it by
# hand — see launcher.py for the minimise-on-start half of this.

@app.post("/api/system/exit")
def system_exit():
    import os
    import signal
    import threading

    def _shutdown():
        # Give the response time to actually reach the browser before the
        # process dies.
        time.sleep(0.3)
        callback = getattr(app.state, "shutdown_callback", None)
        if callback:
            callback()
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_shutdown, daemon=True).start()
    return {"ok": True}


# ==========================
# IMPORT EXISTING .env / streamers.db
# ==========================
# Lets someone who already has a Scout setup elsewhere (another ScoutBot
# install, an older backup, etc.) bring their Twitch credentials and
# tracked roster into this one via upload instead of having to close the
# app and manually copy files into scout-backend/ on disk. Both files are
# fully optional and independent of each other — uploading just one is
# fine. Only ever writes into this backend's own already-fixed .env/
# streamers.db locations (config.ENV_PATH / db.DB_NAME); never accepts an
# arbitrary destination path from the request.
#
# streamers.db is actively held open by this same running process (see
# database.py's per-thread cached connections and WAL mode) — swapping the
# file out from under those connections mid-request is exactly the kind
# of thing that produces silent corruption or a half-migrated schema, so
# this deliberately does not try to hot-swap it in place. Instead: the
# uploaded file is validated (must actually be a SQLite DB with a
# recognizable `streamers` table) and written to disk, then the same
# graceful-restart path /api/system/exit already uses is triggered so the
# next process start opens the new file fresh through the normal
# connect + migrate_database() path — the same as if it had been placed
# there by hand before launching, per the README. The Electron host
# already auto-restarts the backend on relaunch (see
# electron/main/scoutBackend.ts); a person running from source sees the
# process exit and can restart it themselves (`npm run scout:backend`).

def _validate_uploaded_env(raw: bytes) -> None:
    """Light sanity check — an .env is just KEY=VALUE lines, so this only
    guards against obviously-wrong uploads (e.g. a binary file, or a
    streamers.db accidentally uploaded as the .env)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="That doesn't look like a text .env file")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise HTTPException(status_code=400, detail="That doesn't look like a valid .env file (expected KEY=VALUE lines)")


@app.post("/api/system/import")
async def system_import(
    request: Request,
    env_file: Optional[UploadFile] = File(None),
    db_file: Optional[UploadFile] = File(None),
):
    if env_file is None and db_file is None:
        raise HTTPException(status_code=400, detail="Choose a .env file, a streamers.db file, or both")

    env_raw = await env_file.read() if env_file is not None else None
    db_raw = await db_file.read() if db_file is not None else None
    async with _IMPORT_LOCK:
        imported = await asyncio.to_thread(_import_files, env_raw, db_raw)
        if "env" in imported:
            from dotenv import dotenv_values
            values = await asyncio.to_thread(dotenv_values, config.ENV_PATH)
            config.TWITCH_CLIENT_ID = str(values.get("TWITCH_CLIENT_ID") or "")
            config.TWITCH_CLIENT_SECRET = str(values.get("TWITCH_CLIENT_SECRET") or "")
            twitch_api.set_credentials(config.TWITCH_CLIENT_ID, config.TWITCH_CLIENT_SECRET)

    # Standalone ScoutBot keeps the FastAPI server inside the same native
    # pywebview process. Dashboard/Electron can safely relaunch the backend
    # after an import, but a standalone desktop process cannot hand its live
    # window over to a replacement uvicorn process. Reload the mutable state
    # in place instead so the native window stays open.
    if os.getenv("SCOUTBOT_STANDALONE") == "1":
        if "db" in imported:
            await asyncio.to_thread(db._invalidate_all_cache)
            invalidate_discover_cache()
        logger.info(f"Imported {', '.join(imported)} via /api/system/import — standalone state reloaded")
        return {"ok": True, "imported": imported, "restarting": False}

    logger.info(f"Imported {', '.join(imported)} via /api/system/import — restarting to load it")

    # BUGFIX: previously killed this process (os.kill(..., SIGTERM)) and
    # relied entirely on something external to relaunch it — fine under
    # Dashboard/Electron (electron/main/scoutBackend.ts relaunches uvicorn
    # on exit) but README_DASHBOARD.md also documents running this backend
    # standalone (`python -m uvicorn main:app` / `npm run scout:backend`)
    # for development or independent use, and nothing supervises the
    # process in that case. There, the kill just took the server offline
    # with no restart at all — the uploaded .env/streamers.db were written
    # to disk correctly (imported: true) but never actually got loaded,
    # since nothing was left running to read them.
    #
    # (A same-process os.execv() re-exec was tried first instead of
    # spawning a new process, to avoid a port-reuse gap — but re-exec'ing
    # `python -m uvicorn ...` from inside a request handler that's already
    # mid-import of this very module corrupts the interpreter's module
    # state, e.g. a circular "partially initialized module 'logging'"
    # crash on the freshly-exec'd process. Spawning a genuinely new child
    # process instead, then exiting this one, avoids that.)
    #
    # This now spawns a replacement `python -m uvicorn main:app` process
    # (same working directory, so it picks up the exact
    # config.py/database.py path resolution the running instance already
    # uses) *before* exiting, so a fresh process is already coming up
    # whether or not Dashboard/Electron is there to relaunch one. Under
    # Electron, its own child.on('exit', ...) relaunch in
    # scoutBackend.ts still fires too — startScoutBackend() there already
    # no-ops if something is already answering on the configured port
    # (see its `alreadyUp` check), so the two don't fight each other.
    #
    # Host/port for the new process come from the incoming request's own
    # URL (request.url.hostname/port) rather than config.WEB_HOST/
    # WEB_PORT — those are only .env-derived *defaults*; the currently
    # running instance may have been launched with an explicit
    # --host/--port overriding them (same as package.json's
    # `scout:backend` script does with --host/--port flags), and this
    # must rebind to whatever address is actually currently in use, not
    # whatever .env happens to say.
    restart_host = (request.scope.get("server") or (config.WEB_HOST, config.WEB_PORT))[0]
    restart_port = request.url.port or config.WEB_PORT

    def _restart():
        time.sleep(0.3)
        subprocess.Popen(
            [sys.executable, "-c",
             "import os,socket,sys,time; deadline=time.monotonic()+30; "
             "host=sys.argv[1]; port=int(sys.argv[2]); "
             "exec('while time.monotonic()<deadline:\n try:\n  s=socket.create_connection((host,port),timeout=.2); s.close(); time.sleep(.1)\n except OSError: break\nelse: sys.exit(1)'); "
             "os.execv(sys.executable,[sys.executable,'-m','uvicorn','main:app','--host',host,'--port',str(port)])",
             restart_host, str(restart_port)],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_restart, daemon=True).start()
    return {"ok": True, "imported": imported, "restarting": True}


# ==========================
# LIVE NOTIFICATION FEED (SSE)
# ==========================

@app.get("/api/events")
async def event_stream():
    queue = notifier.subscribe()

    async def gen():
        try:
            for event in notifier.recent_events():
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            notifier.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ==========================
# LIVE NOTIFICATION FEED (WebSocket)
# ==========================
# Alternate transport for the same event feed /api/events (SSE) serves —
# additive only, SSE is untouched and remains the primary feed for
# existing clients. A WebSocket is opened here specifically for future
# bidirectional real-time features (e.g. live typing/collab, if
# multi-user is ever supported): a plain SSE stream is one-way
# (server -> client), so anything the client needs to *send* back in
# real time (not just poll/PUT for) needs a socket instead. On
# connect, this replays the same recent-event backlog SSE does, then
# streams live notifier events same as SSE; any JSON message *received*
# from the client is relayed to other connected clients via
# notifier.broadcast_client_event() rather than interpreted server-side,
# since there's no concrete bidirectional feature wired up yet.
def _is_ws_authorized(websocket: WebSocket) -> bool:
    """Same Basic Auth check auth.BasicAuthMiddleware applies to every
    HTTP route, applied by hand here since that middleware never sees
    WebSocket connections (see the SECURITY FIX note on
    websocket_events below). Reuses auth._is_authorized directly so the
    credential-comparison logic (including its constant-time compare)
    isn't duplicated."""
    return _basic_auth_is_authorized(websocket.headers.get("authorization"))


def _is_ws_local_token_authorized(websocket: WebSocket) -> bool:
    """Same local-launch token check auth.LocalServiceTokenMiddleware
    applies to every HTTP route, applied by hand here for the same reason
    _is_ws_authorized is above: BaseHTTPMiddleware (which
    LocalServiceTokenMiddleware is built on, same as BasicAuthMiddleware)
    never sees WebSocket connections."""
    from auth import LOCAL_TOKEN_HEADER
    import secrets as _secrets
    return _secrets.compare_digest(websocket.headers.get(LOCAL_TOKEN_HEADER, "").encode("utf-8"), config.LOCAL_AUTH_TOKEN.encode("utf-8"))


@app.websocket("/api/ws")
async def websocket_events(websocket: WebSocket):
    # SECURITY FIX: BaseHTTPMiddleware (auth.BasicAuthMiddleware, added
    # via app.add_middleware above) only ever sees ASGI "http" scope
    # requests — Starlette's websocket scope bypasses it entirely, so
    # this endpoint was reachable with zero authentication even with
    # WEB_USERNAME/WEB_PASSWORD set, unlike every other route including
    # the SSE feed it mirrors. Enforce the same check explicitly here
    # before accepting the connection.
    if config.auth_enabled() and not _is_ws_authorized(websocket):
        logger.warning("Rejected unauthenticated WebSocket connection to /api/ws")
        await websocket.close(code=1008)  # policy violation
        return
    # Same gap for the local-launch token (see LocalServiceTokenMiddleware):
    # this websocket route bypasses BaseHTTPMiddleware entirely, so it needs
    # its own explicit check too, mirroring the Basic Auth one above.
    if config.LOCAL_AUTH_TOKEN and not _is_ws_local_token_authorized(websocket):
        logger.warning("Rejected WebSocket connection to /api/ws with missing/invalid local service token")
        await websocket.close(code=1008)  # policy violation
        return

    await websocket.accept()
    queue = notifier.subscribe()
    notifier.register_ws(websocket, queue)

    # BUGFIX: the send loop previously swallowed every exception
    # (including WebSocketDisconnect, which is itself an Exception
    # subclass — the old `except (WebSocketDisconnect, Exception)` was
    # redundant) and returned silently on a failed send. That left the
    # outer receive loop below blocked on receive_json() indefinitely
    # for a client that's actually gone (e.g. network drop without a
    # clean close handshake), leaking its queue subscription and its
    # entry in notifier._ws_clients until the process restarts. This
    # cancels the whole connection's outer task via a sentinel Future
    # the instant the send side fails, so cleanup in `finally` below
    # always runs.
    done = asyncio.Event()

    async def _send_loop():
        try:
            for event in notifier.recent_events():
                await websocket.send_json(event)
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"WebSocket send loop ending for a client: {e}")
        finally:
            done.set()

    send_task = asyncio.create_task(_send_loop())
    try:
        while True:
            receive_task = asyncio.create_task(websocket.receive_json())
            done_wait_task = asyncio.create_task(done.wait())
            finished, pending = await asyncio.wait(
                {receive_task, done_wait_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
                # Suppress "Task was destroyed but it is pending" — a
                # cancelled task must still be awaited once for its
                # CancelledError to be consumed instead of surfacing as
                # an unretrieved-exception warning on GC.
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if done_wait_task in finished:
                break
            data = receive_task.result()
            await notifier.broadcast_client_event(data, sender=websocket)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.error(f"WebSocket connection error: {e}")
    finally:
        notifier.unsubscribe(queue)
        notifier.unregister_ws(websocket)
        children = [send_task, locals().get("receive_task"), locals().get("done_wait_task")]
        children = [child for child in children if child is not None]
        for child in children:
            child.cancel()
        # ASGI shutdown may cancel the surrounding AnyIO scope repeatedly.
        # Shield the join so all child tasks finish before returning.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*children, return_exceptions=True)



# ==========================
# STATIC FRONTEND
# ==========================


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
