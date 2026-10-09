import hashlib
from functools import wraps
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone


# Pinned to this file's own directory when running from source (same
# fix/reasoning as config.py's ENV_PATH) rather than a bare relative
# filename resolved against whatever the current working directory
# happens to be at connect time - keeps streamers.db and .env always
# read/written from the same place on every run. For a frozen
# PyInstaller build, anchored to sys.executable's directory instead -
# the actual running .exe's real on-disk location, unlike __file__
# which resolves inside the bundled/extracted location and isn't
# guaranteed to be the identical physical path on every single launch
# (see config.py's ENV_PATH for the full explanation) - so this always
# reads/writes the same streamers.db a frozen build already wrote
# before, instead of it silently appearing empty after a restart.
# Dashboard supplies DASHBOARD_SCOUT_DATA_DIR for packaged launches. This
# is the stable per-user location used for all mutable Scout data, so an
# installer replacement cannot remove the database. Direct/manual source
# launches keep the legacy local path.
_data_dir = os.getenv("DASHBOARD_SCOUT_DATA_DIR", "").strip()
if _data_dir:
    _BASE_DIR = os.path.abspath(_data_dir)
elif getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_NAME = os.path.join(_BASE_DIR, "streamers.db")

# Soft cap on active (non-archived) streamers. Not enforced automatically —
# commands can check get_active_count() against this and warn/prompt the
# user to archive inactive entries before adding more.
MAX_ACTIVE_STREAMERS = 5000

# get_all() is hit constantly (autocomplete on every keystroke, the tracker
# loop, /scout list, /scout stats...). Short TTL cache avoids hammering
# sqlite for data that only actually changes every few minutes.
_ALL_CACHE_TTL = 5
_all_cache = {}
_cache_lock = threading.Lock()


# Serialize local operations with database replacement. Nested helpers use
# the same lock; request/background callers run blocking work in workers.
database_lock = threading.RLock()
database_generation = 0


class StaleDatabaseOperationError(RuntimeError):
    pass


def _database_operation(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        # A staged import has its own SQLite file. Its migrations may run
        # without blocking readers of the live database.
        if fn.__name__ in {"setup", "migrate_database", "migrate_metadata_table", "migrate_category_watchlists_table", "setup_fts", "rebuild_fts_index", "_fts_available"}:
            connection = args[0] if args else kwargs.get("con")
            if isinstance(connection, sqlite3.Connection):
                filename = connection.execute("PRAGMA database_list").fetchone()[2]
                if filename and os.path.abspath(filename) != os.path.abspath(DB_NAME):
                    return fn(*args, **kwargs)
        with database_lock:
            generation = kwargs.pop("_generation", None)
            if generation is not None and generation != database_generation:
                raise StaleDatabaseOperationError("The database changed; retry the action.")
            return fn(*args, **kwargs)
    return locked


@_database_operation
def _invalidate_all_cache():
    with _cache_lock:
        _all_cache.clear()


# ==========================
# DATABASE CONNECTION
# ==========================

# Callers may run db() from different threads (e.g. via asyncio.to_thread),
# so each thread gets its own cached connection instead of paying the
# connect + PRAGMA setup cost on every single query.
_local = threading.local()

# Every connection handed out by db(), across every thread (the tracker
# thread, the FastAPI worker thread pool, ...), tracked here so they can
# all be closed together — see close_all_connections() below. Each
# connection is opened with check_same_thread=False specifically so it's
# safe to .close() from a thread other than the one that created it.
_all_connections = []
_all_connections_lock = threading.Lock()


@_database_operation
def db():

    con = getattr(_local, "con", None)

    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.ProgrammingError:
            _local.con = None


    con = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )

    con.row_factory = sqlite3.Row

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA cache_size=-8000")
    con.execute("PRAGMA foreign_keys=ON")

    _local.con = con

    with _all_connections_lock:
        _all_connections.append(con)

    return con


# BUGFIX: /api/system/import (main.py) replaces streamers.db on disk with
# an uploaded file via shutil.move() while this same process still holds
# the *existing* database open — every thread that's ever called db()
# (the tracker thread included) has its own live connection to it. On
# Windows, moving/renaming onto a file that's still open in the same
# process raises `OSError: [WinError 32] The process cannot access the
# file because it is being used by another process` — POSIX allows a
# rename over an open file, so this never showed up running from source
# on Linux/Mac, only in the packaged Windows build. That OSError wasn't
# an HTTPException, so it fell through to Starlette's default handler,
# which returns a plain-text 500 with no JSON body — exactly matching the
# frontend's generic "Import failed (500)" fallback (ScoutScreen.tsx's
# `body?.detail` ends up undefined, since there was no JSON `detail` to
# read).
#
# close_all_connections() releases every open handle across every thread
# right before that move, so the file is free to be replaced. db()
# already recovers from a closed connection on its own (see the
# ProgrammingError catch above, which just reconnects fresh next time),
# so it's safe to call this at any point — nothing needs to be
# reinitialized afterwards for the running process to keep working, since
# it's about to restart anyway.
@_database_operation
def close_all_connections():
    with _all_connections_lock:
        cons = list(_all_connections)
        _all_connections.clear()
    for con in cons:
        try:
            con.close()
        except sqlite3.Error:
            pass
    _local.con = None


# ==========================
# MIGRATIONS
# ==========================

@_database_operation
def migrate_database(con):

    cursor = con.cursor()

    cursor.execute(
        "PRAGMA table_info(streamers)"
    )

    columns = {
        row[1]
        for row in cursor.fetchall()
    }


    migrations = {

        "twitch_id": "TEXT",
        "current_username": "TEXT",
        "identity_checked_at": "TEXT",
        "profile_image": "TEXT",

        "discovered": "TEXT",

        "category": "TEXT",

        "followers": "INTEGER DEFAULT 0",

        "average_viewers": "INTEGER DEFAULT 0",

        "current_viewers": "INTEGER DEFAULT 0",

        "live_status": "TEXT DEFAULT 'Offline'",

        "community_rating": "INTEGER DEFAULT 0",

        "content_rating": "INTEGER DEFAULT 0",

        "raid_rating": "INTEGER DEFAULT 0",

        "raid_score": "INTEGER DEFAULT 0",
        "manual_raid_score": "INTEGER DEFAULT 0",

        "peak_viewers": "INTEGER DEFAULT 0",

        "notes": "TEXT",

        "last_live": "TEXT",

        "last_updated": "TEXT",

        "notified_live": "INTEGER DEFAULT 0",

        "archived": "INTEGER DEFAULT 0",

        "archived_at": "TEXT",
    }


    for column, definition in migrations.items():

        if column not in columns:

            cursor.execute(
                f"""
                ALTER TABLE streamers
                ADD COLUMN {column} {definition}
                """
            )

    if "manual_raid_score" not in columns:
        # Preserve the previous bonus on migration; subsequent automatic
        # calculations must not overwrite the operator's manual input.
        con.execute("UPDATE streamers SET manual_raid_score=COALESCE(raid_score,0)")

    con.commit()


@_database_operation
def migrate_category_watchlists_table(con):
    """Runs after category_watchlists exists — adds check_interval_seconds
    for pre-existing rows created before per-watchlist cadence was
    configurable (see get_category_watches_due). Same
    PRAGMA-table_info-then-ALTER pattern as migrate_database above."""

    cursor = con.cursor()

    cursor.execute("PRAGMA table_info(category_watchlists)")

    columns = {row[1] for row in cursor.fetchall()}

    if "check_interval_seconds" not in columns:
        cursor.execute(
            """
            ALTER TABLE category_watchlists
            ADD COLUMN check_interval_seconds INTEGER DEFAULT 300
            """
        )

    con.commit()


@_database_operation
def migrate_metadata_table(con):
    """Runs after streamer_metadata exists — adds any new metadata columns."""

    cursor = con.cursor()

    cursor.execute("PRAGMA table_info(streamer_metadata)")

    metadata_columns = {row[1] for row in cursor.fetchall()}

    metadata_migrations = {
        "favourite": "INTEGER DEFAULT 0",
        "tags": "TEXT DEFAULT ''",
        "priority": "TEXT DEFAULT 'Watch'",
        "alias": "TEXT DEFAULT ''",
        "notify_enabled": "INTEGER DEFAULT 1",
        "x_url": "TEXT DEFAULT ''",
        # Manually-entered Instagram profile URL. Was "discord_url" — the
        # app never scrapes Discord links (Twitch bios/panels essentially
        # never contain them), so this column is renamed in purpose to
        # Instagram, which is actually present in the scraped platform
        # list (see twitch_api.SOCIAL_PATTERNS). Existing databases keep
        # the underlying `discord_url` column name (SQLite migrations
        # don't rename columns here) — it now just holds the Instagram
        # URL instead. Any previously-saved Discord URL is left as-is in
        # that column but the app no longer reads/writes it as Discord.
        "discord_url": "TEXT DEFAULT ''",
        # Manually-entered YouTube / Kick presence links, so a roster
        # entry can track a streamer across platforms, not just Twitch.
        "youtube_url": "TEXT DEFAULT ''",
        "kick_url": "TEXT DEFAULT ''",
        # Outreach lifecycle stage — separate from `priority` (which ranks
        # how much attention a streamer deserves) and from `streamers.archived`
        # (which is a storage/visibility toggle). This tracks where a
        # streamer sits in the raid-outreach pipeline, e.g. having been
        # messaged or actually raided. Defaults to 'active' so existing
        # rows behave exactly as before this column existed.
        "outreach_status": "TEXT DEFAULT 'active'",
        # Scraped (not user-entered) channel About-tab bio text and any
        # social links found in that bio + the channel's profile panels.
        # scraped_social_links is stored as a JSON object (label -> url),
        # e.g. {"Twitter / X": "https://x.com/...", "Instagram": "..."}.
        # Kept separate from the existing user-entered x_url/discord_url
        # columns so a manual edit never gets silently overwritten by a
        # re-scrape, and vice versa.
        "bio": "TEXT DEFAULT ''",
        "scraped_social_links": "TEXT DEFAULT ''",
        "social_scraped_at": "TEXT",
        # Best-effort location/timezone scraped from the same bio text as
        # above (see twitch_api._extract_location / _guess_timezone).
        # scraped_location is the raw self-reported text (e.g. "London,
        # UK"); scraped_timezone is the IANA name resolved from it, or
        # NULL when nothing recognizable was found in the bio.
        "scraped_location": "TEXT DEFAULT ''",
        "scraped_timezone": "TEXT",
        # Best-effort age scraped from the same bio/panel text as
        # location above (see twitch_api._extract_age) — self-reported
        # only (e.g. "Age: 27" or "27yo" somewhere in bio/panels), NULL
        # when nothing recognizable was found.
        "scraped_age": "INTEGER",
        # Manually-entered location/timezone — separate from the
        # scraped_location/scraped_timezone pair above (same reasoning as
        # x_url/discord_url vs the scraped social links: a re-scrape must
        # never silently overwrite something the person typed in, and a
        # manual edit must never block a future scrape from updating the
        # scraped_* columns). `location` is free text (e.g. "Auckland,
        # NZ"); `timezone` is an IANA zone name (e.g. "Pacific/Auckland")
        # picked from a dropdown, used for the same local-time display as
        # scraped_timezone. Falls back to scraped_location/scraped_timezone
        # wherever neither manual field has been set — see
        # serializers.streamer_to_dict.
        "location": "TEXT DEFAULT ''",
        "timezone": "TEXT",
    }

    for column, definition in metadata_migrations.items():

        if column not in metadata_columns:

            cursor.execute(
                f"""
                ALTER TABLE streamer_metadata
                ADD COLUMN {column} {definition}
                """
            )

    # resolved_location is a generated column mirroring
    # COALESCE(NULLIF(location, ''), scraped_location) — the same
    # fallback expression used throughout search_streamers/get_paginated
    # for the location filter/sort. Plain B-tree indexes can't be built
    # on that expression directly (SQLite only indexes columns or
    # expressions matched verbatim), so materializing it as a stored
    # column lets idx_metadata_resolved_location below actually get used
    # for ORDER BY/WHERE on location instead of a full table scan, which
    # matters once the roster gets large. Added after the migrations
    # above since it must come after `location`/`scraped_location` exist.
    # PRAGMA table_info (used for metadata_columns above) omits generated
    # columns in SQLite — table_xinfo is needed to see resolved_location
    # and avoid re-adding it (and erroring) on every subsequent startup.
    cursor.execute("PRAGMA table_xinfo(streamer_metadata)")
    all_columns_including_generated = {row[1] for row in cursor.fetchall()}

    if "resolved_location" not in all_columns_including_generated:

        cursor.execute(
            """
            ALTER TABLE streamer_metadata
            ADD COLUMN resolved_location TEXT
            GENERATED ALWAYS AS (COALESCE(NULLIF(location, ''), scraped_location, ''))
            VIRTUAL
            """
        )

    con.commit()

@_database_operation
def recalculate_raid_score(username):

    streamer = get_streamer(username)

    if not streamer:
        return False


    import scoring

    score = scoring.calculate_raid_score(
        streamer
    )

    return update_raid_score(
        username,
        score,
        manual=False,
    )

# ==========================
# SETUP
# ==========================

@_database_operation
def setup(con=None):

    con = con if con is not None else db()


    # ==========================
    # MAIN STREAMER TABLE
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS streamers(

            id INTEGER PRIMARY KEY,

            username TEXT UNIQUE,

            url TEXT,

            profile_image TEXT,

            discovered TEXT,

            category TEXT,

            followers INTEGER DEFAULT 0,

            average_viewers INTEGER DEFAULT 0,

            current_viewers INTEGER DEFAULT 0,

            live_status TEXT DEFAULT 'Offline',

            community_rating INTEGER DEFAULT 0,

            content_rating INTEGER DEFAULT 0,

            raid_rating INTEGER DEFAULT 0,

            raid_score INTEGER DEFAULT 0,

            peak_viewers INTEGER DEFAULT 0,

            notes TEXT,

            last_live TEXT,

            last_updated TEXT,

            notified_live INTEGER DEFAULT 0,

            archived INTEGER DEFAULT 0,

            archived_at TEXT
        )
        """
    )


    migrate_database(con)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_streamers_twitch_id ON streamers(twitch_id) WHERE twitch_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_streamers_current_username ON streamers(current_username)")
    con.execute("""CREATE TABLE IF NOT EXISTS streamer_username_history(
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        previous_username TEXT NOT NULL,
        new_username TEXT,
        source TEXT NOT NULL CHECK(source IN ('observed','manual')),
        observed_at TEXT,
        recorded_at TEXT NOT NULL
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_username_history_owner ON streamer_username_history(username,id)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_username_history_manual ON streamer_username_history(username,previous_username) WHERE source='manual'")
    con.execute("""CREATE TABLE IF NOT EXISTS streamer_activity(
        username TEXT PRIMARY KEY,
        provider_username TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK(source_type IN ('fetched','copied')),
        retrieved_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""")



    # ==========================
    # STREAMER METADATA
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS streamer_metadata(

            username TEXT PRIMARY KEY,

            favourite INTEGER DEFAULT 0,

            tags TEXT DEFAULT '',

            priority TEXT DEFAULT 'Watch',

            alias TEXT DEFAULT '',

            notify_enabled INTEGER DEFAULT 1,

            x_url TEXT DEFAULT '',

            discord_url TEXT DEFAULT '',

            outreach_status TEXT DEFAULT 'active',

            bio TEXT DEFAULT '',

            scraped_social_links TEXT DEFAULT '',

            social_scraped_at TEXT
        )
        """
    )

    migrate_metadata_table(con)


    # ==========================
    # VIEWER HISTORY
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS viewer_history(

            id INTEGER PRIMARY KEY,

            username TEXT,

            viewers INTEGER,

            timestamp TEXT
        )
        """
    )



    # ==========================
    # STREAM SESSIONS
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stream_sessions(

            id INTEGER PRIMARY KEY,

            username TEXT,

            started TEXT,

            ended TEXT,

            peak_viewers INTEGER DEFAULT 0,

            average_viewers INTEGER DEFAULT 0
        )
        """
    )



    # ==========================
    # CATEGORY HISTORY
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS category_history(

            id INTEGER PRIMARY KEY,

            username TEXT,

            category TEXT,

            timestamp TEXT
        )
        """
    )



    # ==========================
    # EVENTS
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS scout_events(

            id INTEGER PRIMARY KEY,

            username TEXT,

            event TEXT,

            timestamp TEXT
        )
        """
    )



    # ==========================
    # RAID HISTORY
    # ==========================

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raid_history(

            id INTEGER PRIMARY KEY,

            username TEXT,

            raid_score INTEGER,

            result TEXT,

            timestamp TEXT
        )
        """
    )



    # ==========================
    # SAVED FILTER PRESETS
    # ==========================
    # Named, reusable combinations of the roster filter/sort controls
    # (search text, priority, category, live-only, tags, sort) so a
    # combo like "Live + High priority + VALORANT" can be saved once and
    # reapplied from the rail instead of being rebuilt by hand each time.
    # `filters` stores the filter state as a JSON string — kept schemaless
    # here since the filter shape lives in the frontend, not the DB.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS filter_presets(

            id INTEGER PRIMARY KEY,

            name TEXT UNIQUE,

            filters TEXT,

            created_at TEXT
        )
        """
    )



    # ==========================
    # CATEGORY WATCHLISTS
    # ==========================
    # Follows a Twitch category itself (rather than individual streamers)
    # so new/trending channels streaming it can be surfaced without having
    # to already know their name. `seen_usernames` is a comma-separated
    # cache of usernames already surfaced for this category, so a repeat
    # check can tell "new since last time" apart from "still trending" —
    # kept schemaless/inline here for the same reason filter_presets
    # stores its filters as a JSON blob: this is small, per-row state that
    # doesn't need its own table.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS category_watchlists(

            id INTEGER PRIMARY KEY,

            category TEXT UNIQUE,

            min_viewers INTEGER DEFAULT 0,

            seen_usernames TEXT DEFAULT '',

            created_at TEXT,

            last_checked TEXT,

            check_interval_seconds INTEGER DEFAULT 300
        )
        """
    )

    migrate_category_watchlists_table(con)



    # ==========================
    # ALERT RULES
    # ==========================
    # A rule fires either when a specific tracked streamer goes live
    # (kind='streamer_live', target=username) or when a Discover search
    # matches at least one result (kind='discover_match', target=JSON
    # filters, same shape /api/discover already accepts). `channels` is a
    # comma-separated subset of {webhook, browser, email} — browser
    # delivery reuses the existing notifier SSE feed, webhook/email are
    # handled by alerts.py. `last_triggered`/`last_result_hash` let a
    # discover_match rule avoid re-notifying for the exact same result set
    # on every poll.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_rules(

            id INTEGER PRIMARY KEY,

            kind TEXT NOT NULL,

            target TEXT NOT NULL,

            channels TEXT DEFAULT 'browser',

            webhook_url TEXT DEFAULT '',

            email TEXT DEFAULT '',

            enabled INTEGER DEFAULT 1,

            created_at TEXT,

            last_triggered TEXT,

            last_result_hash TEXT
        )
        """
    )



    # ==========================
    # ALERT DELIVERY LOG
    # ==========================
    # Short history of fired alerts, shown in the Alerts modal so a
    # webhook/email failure is visible instead of silent.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_deliveries(

            id INTEGER PRIMARY KEY,

            rule_id INTEGER,

            channel TEXT,

            summary TEXT,

            ok INTEGER DEFAULT 1,

            error TEXT,

            timestamp TEXT
        )
        """
    )



    # ==========================
    # STREAMER RESPONSE TRACKING
    # ==========================
    # Logs whether/how a scouted streamer has acknowledged being scouted —
    # followed back, replied to a message, reacted, etc. Separate from
    # `outreach_status` (which is the current pipeline stage): a streamer
    # can have several response events over time (e.g. "followed back"
    # then later "replied") while outreach_status just reflects where
    # things stand right now.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS streamer_responses(

            id INTEGER PRIMARY KEY,

            username TEXT NOT NULL,

            response_type TEXT NOT NULL,

            note TEXT DEFAULT '',

            timestamp TEXT
        )
        """
    )



    # ==========================
    # DISCOVER SEARCH HISTORY
    # ==========================
    # Remembers recent /api/discover filter combos (deduped by filter
    # content, most-recent-use bumped to the top) so the Discover modal
    # can offer "recent searches" instead of the person re-entering the
    # same filters every time. Schemaless filters blob for the same
    # reason filter_presets/category_watchlists are: the filter shape is
    # owned by the frontend/discover endpoint, not the DB.

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS discover_search_history(

            id INTEGER PRIMARY KEY,

            filters TEXT NOT NULL,

            filters_hash TEXT UNIQUE,

            last_used TEXT
        )
        """
    )



    # ==========================
    # RECENTLY VIEWED STREAMERS
    # ==========================
    # Timestamped log of streamer detail-panel opens, independent of
    # `discovered` (when a streamer was first tracked) — lets the UI show
    # a quick-access "recently viewed" list. One row per view; old rows
    # beyond what any query needs are trimmed opportunistically rather
    # than on every insert (see add_recently_viewed).

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS recently_viewed(

            id INTEGER PRIMARY KEY,

            username TEXT NOT NULL,

            viewed_at TEXT
        )
        """
    )



    # ==========================
    # STREAMER BLACKLIST
    # ==========================
    # A hard exclude, deliberately separate from streamer_metadata.priority
    # ("Ignore" — an attention ranking that still leaves a streamer visible
    # everywhere, including Discover). Blacklisting a username means it
    # should never reappear in Discover results again, whether or not it's
    # ever been tracked — so this is its own table keyed only on username,
    # not a column on streamer_metadata (which only exists for streamers
    # that have been tracked at least once via ensure_metadata()).

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS streamer_blacklist(

            username TEXT PRIMARY KEY,

            blacklisted_at TEXT
        )
        """
    )

    # User-defined social platforms — a label + URL-matching pattern pair
    # (e.g. "Linktree" / "linktree.com/") that gets merged into
    # twitch_api.SOCIAL_PATTERNS at scrape time, alongside the built-in
    # platforms (Twitter/X, Discord, YouTube, etc.), so bio/panel links to
    # a platform this app doesn't otherwise recognize can still be
    # detected and labelled instead of being silently dropped as
    # unrecognized. `pattern` is stored as plain text and compiled with
    # re.IGNORECASE at merge time (see twitch_api.get_effective_social_patterns).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_social_platforms(

            label TEXT PRIMARY KEY,

            pattern TEXT NOT NULL,

            created_at TEXT
        )
        """
    )



    # ==========================
    # INDEXES
    # ==========================

    indexes = [

        """
        CREATE INDEX IF NOT EXISTS
        idx_streamers_username
        ON streamers(username)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_streamers_category
        ON streamers(category)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_streamers_archived_followers
        ON streamers(archived, followers DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_streamers_archived_status
        ON streamers(archived, live_status)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_streamers_last_live
        ON streamers(last_live)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_viewer_history_username
        ON viewer_history(username,id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_sessions_username
        ON stream_sessions(username,id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_category_history_username
        ON category_history(username,id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_events_username
        ON scout_events(username,id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_raids_username
        ON raid_history(username,id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_metadata_favourite
        ON streamer_metadata(favourite)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_metadata_priority
        ON streamer_metadata(priority)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_metadata_resolved_location
        ON streamer_metadata(resolved_location)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_alert_deliveries_rule
        ON alert_deliveries(rule_id, id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_responses_username
        ON streamer_responses(username, id DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_discover_history_last_used
        ON discover_search_history(last_used DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_recently_viewed_username
        ON recently_viewed(username, viewed_at DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_recently_viewed_viewed_at
        ON recently_viewed(viewed_at DESC)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_blacklist_username
        ON streamer_blacklist(username)
        """,

        """
        CREATE INDEX IF NOT EXISTS
        idx_custom_social_platforms_label
        ON custom_social_platforms(label)
        """,

        """
        CREATE INDEX IF NOT EXISTS idx_streamers_live_followers
        ON streamers(archived, live_status, followers DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_streamers_category_followers
        ON streamers(archived, category, followers DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_streamers_last_live
        ON streamers(archived, last_live DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_streamers_raid_score
        ON streamers(archived, raid_score DESC)
        """,
    ]


    for index in indexes:
        con.execute(index)


    con.execute("INSERT OR IGNORE INTO streamer_metadata(username) SELECT username FROM streamers")
    con.commit()

    setup_fts(con)

    return True

# ==========================
# FULL-TEXT SEARCH (FTS5)
# ==========================
# search_streamers()'s `query` clause used to be three LOWER(...) LIKE
# '%needle%' scans (username/alias/notes) — each one un-indexable (a
# leading wildcard defeats any B-tree index) and O(rows) per keystroke.
# Fine for a small roster, but it re-scans the entire table on every
# search once tracked streamers run into the thousands.
#
# streamers_fts is an external-content FTS5 index over exactly those
# same three fields (s.username, s.notes, m.alias), kept in sync with
# INSERT/UPDATE/DELETE triggers on the two source tables below rather
# than by touching every Python mutation function individually — any
# existing/future write path (add_streamer, update_notes, set_alias,
# remove_streamer, direct SQL, etc.) stays correct for free. "External
# content" means the FTS index stores only the token index, not a
# second copy of the text — content=streamers with a manual join back
# to streamer_metadata for alias via the trigger UPDATE below.
#
# search_streamers() below tries the FTS path first and falls back to
# the original LIKE clause if the table is missing (e.g. a build linked
# against an SQLite without FTS5 compiled in — rare, but not worth a
# hard failure over) or a query contains characters FTS5's query syntax
# rejects (e.g. a bare leading '-').

@_database_operation
def _fts_available(con):
    try:
        con.execute("SELECT 1 FROM streamers_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


@_database_operation
def setup_fts(con):
    """Maintain a denormalized FTS5 search index for the fields users
    actually search. The index is deliberately rebuilt only when its schema
    is old/missing; normal writes stay incremental through triggers."""
    try:
        existing = [r[1] for r in con.execute("PRAGMA table_info(streamers_fts)").fetchall()]
    except sqlite3.OperationalError:
        existing = []
    required = ["username", "notes", "alias", "category", "location", "tags"]
    old_trigger = con.execute("SELECT sql FROM sqlite_master WHERE name='trg_streamers_fts_au'").fetchone()
    identity_index_old = bool(old_trigger and "current_username" not in old_trigger[0])
    schema = con.execute("SELECT sql FROM sqlite_master WHERE name='streamers_fts'").fetchone()
    old_contentless = bool(schema and re.search(r"content\s*=\s*['\"]['\"]", schema[0], re.I))
    rebuild = not existing or existing != required or old_contentless or identity_index_old
    triggers = ("trg_streamers_fts_ai", "trg_streamers_fts_ad", "trg_streamers_fts_au",
                "trg_metadata_fts_alias_ai", "trg_metadata_fts_alias_au", "trg_metadata_fts_ad")
    for name in triggers:
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
    try:
        if rebuild:
            con.execute("DROP TABLE IF EXISTS streamers_fts")
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS streamers_fts
            USING fts5(username, notes, alias, category, location, tags)
        """)
    except sqlite3.OperationalError as e:
        __import__("logging").getLogger(__name__).warning("FTS5 unavailable, falling back to LIKE search: %s", e)
        return

    # A content-backed table makes deletes independent of token snapshots.
    # Metadata deletion, scraped-location changes and missing metadata all
    # update the same index entry; reused streamer ids cannot inherit tokens.
    select_new = """SELECT new.id,new.username || ' ' || COALESCE(new.current_username,''),COALESCE(new.notes,''),COALESCE(m.alias,''),
                    COALESCE(new.category,''),COALESCE(m.resolved_location,''),COALESCE(m.tags,'')
                    FROM streamers s LEFT JOIN streamer_metadata m ON m.username=s.username
                    WHERE s.id=new.id"""
    select_meta = """SELECT s.id,s.username || ' ' || COALESCE(s.current_username,''),COALESCE(s.notes,''),COALESCE(m.alias,''),
                    COALESCE(s.category,''),COALESCE(m.resolved_location,''),COALESCE(m.tags,'')
                    FROM streamers s LEFT JOIN streamer_metadata m ON m.username=s.username
                    WHERE s.username={username}"""
    fields = "rowid,username,notes,alias,category,location,tags"
    con.executescript(f"""
        CREATE TRIGGER trg_streamers_fts_ai AFTER INSERT ON streamers BEGIN
            INSERT INTO streamers_fts({fields}) {select_new};
        END;
        CREATE TRIGGER trg_streamers_fts_ad AFTER DELETE ON streamers BEGIN
            DELETE FROM streamers_fts WHERE rowid=old.id;
        END;
        CREATE TRIGGER trg_streamers_fts_au AFTER UPDATE OF username,current_username,notes,category ON streamers BEGIN
            DELETE FROM streamers_fts WHERE rowid=old.id;
            INSERT INTO streamers_fts({fields}) {select_new};
        END;
        CREATE TRIGGER trg_metadata_fts_alias_ai AFTER INSERT ON streamer_metadata BEGIN
            DELETE FROM streamers_fts WHERE rowid IN (SELECT id FROM streamers WHERE username=new.username);
            INSERT INTO streamers_fts({fields}) {select_meta.format(username='new.username')};
        END;
        CREATE TRIGGER trg_metadata_fts_alias_au AFTER UPDATE OF alias,tags,location,scraped_location ON streamer_metadata BEGIN
            DELETE FROM streamers_fts WHERE rowid IN (SELECT id FROM streamers WHERE username=new.username);
            INSERT INTO streamers_fts({fields}) {select_meta.format(username='new.username')};
        END;
        CREATE TRIGGER trg_metadata_fts_ad AFTER DELETE ON streamer_metadata BEGIN
            DELETE FROM streamers_fts WHERE rowid IN (SELECT id FROM streamers WHERE username=old.username);
            INSERT INTO streamers_fts({fields}) {select_meta.format(username='old.username')};
        END;
    """)
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM streamers_fts").fetchone()[0]
    source_count = con.execute("SELECT COUNT(*) FROM streamers").fetchone()[0]
    if rebuild or count != source_count:
        rebuild_fts_index(con)


@_database_operation
def rebuild_fts_index(con=None):
    """Fully repopulates streamers_fts from the current streamers/
    streamer_metadata contents. Called automatically on first setup
    (empty index) and safe to call manually (e.g. a future maintenance
    command) if the index is ever suspected to have drifted — clears and
    re-inserts every row rather than trying to reconcile in place."""

    own_con = con is None
    con = con or db()

    if not _fts_available(con):
        return False

    con.execute("DELETE FROM streamers_fts")

    con.execute(
        """
        INSERT INTO streamers_fts(rowid, username, notes, alias, category, location, tags)
        SELECT s.id, s.username || ' ' || COALESCE(s.current_username,''), COALESCE(s.notes, ''),
               COALESCE(m.alias, ''), COALESCE(s.category, ''),
               COALESCE(m.resolved_location, ''), COALESCE(m.tags, '')
        FROM streamers s
        LEFT JOIN streamer_metadata m ON s.username = m.username
        """
    )

    con.commit()

    if own_con:
        pass  # thread-local connection — nothing extra to close here

    return True


@_database_operation
def _fts_match_expr(query):
    """Turns free-typed search text into a safe FTS5 MATCH expression:
    each whitespace-separated term becomes a quoted-prefix token ANDed
    together, e.g. `ninja war` -> '"ninja"* AND "war"*'. Quoting every
    term sidesteps FTS5's own query-syntax operators (AND/OR/NOT/-/^/:
    etc.) so a search for e.g. "co-op" or "test-streamer" is treated as
    literal text instead of raising a syntax error or being misparsed as
    an exclusion. The trailing '*' gives prefix matching so "nin" finds
    "ninja", matching the substring-ish feel of the old LIKE search."""

    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return None
    escaped = [t.replace('"', '""') for t in terms]
    return " AND ".join(f'"{t}"*' for t in escaped)


# ==========================
# STREAMER CRUD
# ==========================

@_database_operation
def add_streamer(data, twitch_id=None):

    data = list(data)
    data[0] = data[0].lower()

    # Index i here corresponds to column i in the streamers table
    # (see INSERT column order below). data[0] (username) is always
    # supplied by callers, so padding starts from index 1.
    defaults = [
        None,       # 0 username (never used as a pad — always supplied)
        None,       # 1 url
        None,       # 2 profile_image
        None,       # 3 discovered
        None,       # 4 category
        0,          # 5 followers
        0,          # 6 average_viewers
        0,          # 7 current_viewers
        "Offline",  # 8 live_status
        None,       # 9 notes
        0,          # 10 community_rating
        0,          # 11 content_rating
        0,          # 12 raid_rating
        0,          # 13 raid_score
        0,          # 14 peak_viewers
        None,       # 15 last_live
        None,       # 16 last_updated
    ]

    while len(data) < 17:
        data.append(defaults[len(data)])


    with db() as con:

        inserted = con.execute(
            """
            INSERT OR IGNORE INTO streamers
            (
                username,
                url,
                profile_image,
                discovered,
                category,
                followers,
                average_viewers,
                current_viewers,
                live_status,
                notes,
                community_rating,
                content_rating,
                raid_rating,
                raid_score,
                peak_viewers,
                last_live,
                last_updated
            )

            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            data[:17],
        )
        if inserted.rowcount:
            con.execute("UPDATE streamers SET manual_raid_score=COALESCE(raid_score,0) WHERE username=?", (data[0],))
            if twitch_id is not None:
                numeric_id, login = _validated_identity(twitch_id, data[0])
                if not _observe_identity(con, data[0], numeric_id, login, datetime.now(timezone.utc).isoformat()):
                    raise ValueError("This Twitch ID is already tracked under another username.")


    _invalidate_all_cache()

    ensure_metadata(
        data[0]
    )

@_database_operation
def get_all(include_archived=False):

    cache_key = "with_archived" if include_archived else "active"

    now = time.monotonic()

    with _cache_lock:
        cached = _all_cache.get(cache_key)
        if cached is not None and now - cached["ts"] < _ALL_CACHE_TTL:
            return cached["data"]

    with db() as con:

        if include_archived:
            rows = con.execute(
                """
                SELECT *
                FROM streamers
                ORDER BY followers DESC
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT *
                FROM streamers
                WHERE archived=0
                ORDER BY followers DESC
                """
            ).fetchall()

    with _cache_lock:
        _all_cache[cache_key] = {"data": rows, "ts": now}

    return rows


@_database_operation
def get_active_count():
    """Fast COUNT(*) for the active (non-archived) roster — used to warn
    the user as they approach MAX_ACTIVE_STREAMERS without pulling every row."""

    with db() as con:

        result = con.execute(
            """
            SELECT COUNT(*)
            FROM streamers
            WHERE archived=0
            """
        ).fetchone()

    return result[0] if result else 0


@_database_operation
def get_paginated(
    page=1,
    page_size=25,
    include_archived=False,
    sort_by="followers",
    ascending=False,
):
    """DB-level pagination — pulls only the requested page instead of
    loading the whole table, so /scout list stays fast at any size.

    Joins streamer_metadata (LEFT JOIN, same as search_streamers) purely
    so sort_by="location" works here too — location itself has no filter
    in this unfiltered listing (that's what search_streamers is for)."""

    sort_columns = {
        "followers": "s.followers",
        "username": "s.username",
        "category": "s.category",
        "raid_score": "s.raid_score",
        "current_viewers": "s.current_viewers",
        "average_viewers": "s.average_viewers",
        "discovered": "s.discovered",
        "last_live": "s.last_live",
        # resolved_location is a generated column mirroring
        # serializers.effective_location (manual location wins, falling
        # back to the scraped one) — see migrate_metadata_table. Sorting
        # on the materialized column (rather than recomputing the
        # COALESCE/NULLIF here) is what lets idx_metadata_resolved_location
        # actually be used.
        "location": "m.resolved_location",
        # Boolean expression (0/1), not the raw TEXT column — sorting on
        # the string 'Live'/'Offline' directly would order alphabetically
        # rather than by actual live status, and would flip depending on
        # `ascending` in a way that isn't meaningful. idx_streamers_archived_live
        # (archived, live_status) still covers this.
        "live": "(s.live_status='Live')",
    }

    column = sort_columns.get(sort_by, "s.followers")
    direction = "ASC" if ascending else "DESC"

    offset = max(page - 1, 0) * page_size

    where = "" if include_archived else "WHERE s.archived=0"

    with db() as con:

        rows = con.execute(
            f"""
            SELECT s.*
            FROM streamers s
            LEFT JOIN streamer_metadata m
            ON s.username=m.username
            {where}
            ORDER BY {column} {direction}, s.username ASC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()

        total = con.execute(
            f"""
            SELECT COUNT(*)
            FROM streamers s
            LEFT JOIN streamer_metadata m
            ON s.username=m.username
            {where}
            """
        ).fetchone()[0]

    return rows, total


@_database_operation
def search_streamers(
    query=None,
    category=None,
    location=None,
    priority=None,
    favourite=None,
    live_only=False,
    min_followers=None,
    max_followers=None,
    tags=None,
    include_archived=False,
    sort_by="followers",
    ascending=False,
    page=1,
    page_size=25,
):
    """Advanced filter/search used by /scout search and friends. Combines
    text search (username/alias/notes), category, location, priority,
    favourite, live status, follower range, and tags — all pushed down to
    SQL so it stays responsive with thousands of rows. Returns (rows,
    total_matches).

    `location` matches against the effective location (manual value when
    set, else the scraped one — same fallback as
    serializers.effective_location) so filtering behaves consistently
    with what's actually shown on a card."""

    sort_columns = {
        "followers": "s.followers",
        "username": "s.username",
        "category": "s.category",
        "raid_score": "s.raid_score",
        "current_viewers": "s.current_viewers",
        "average_viewers": "s.average_viewers",
        "discovered": "s.discovered",
        "last_live": "s.last_live",
        # See get_paginated() above — same generated column.
        "location": "m.resolved_location",
        # See get_paginated() above — boolean expression, not the raw
        # TEXT column, so ordering is by actual live status not alpha.
        "live": "(s.live_status='Live')",
    }

    column = sort_columns.get(sort_by, "s.followers")
    direction = "ASC" if ascending else "DESC"

    clauses = []
    params = []

    if not include_archived:
        clauses.append("s.archived=0")

    # Text search: FTS5 (idx into streamers_fts) when available, since a
    # LIKE '%needle%' scan can't use any index and re-reads every row on
    # every keystroke once the roster is large. Falls back to the
    # original LIKE clause if the FTS table doesn't exist (SQLite build
    # without FTS5) or the query can't be turned into a valid MATCH
    # expression (empty after stripping whitespace).
    use_fts = False
    with db() as _fts_con:
        if query and _fts_available(_fts_con):
            match_expr = _fts_match_expr(query)
            if match_expr:
                use_fts = True

    if query and use_fts:
        clauses.append(
            "s.id IN (SELECT rowid FROM streamers_fts WHERE streamers_fts MATCH ?)"
        )
        params.append(_fts_match_expr(query))
    elif query:
        clauses.append(
            "(LOWER(s.username) LIKE ? OR LOWER(s.current_username) LIKE ? OR LOWER(m.alias) LIKE ? OR LOWER(s.notes) LIKE ?)"
        )
        needle = f"%{query.lower().strip()}%"
        params.extend([needle, needle, needle, needle])

    if category:
        clauses.append("LOWER(s.category) LIKE ?")
        params.append(f"%{category.lower().strip()}%")

    if location:
        clauses.append(
            "LOWER(m.resolved_location) LIKE ?"
        )
        params.append(f"%{location.lower().strip()}%")

    if priority:
        clauses.append("m.priority=?")
        params.append(priority.capitalize())

    if favourite is not None:
        clauses.append("m.favourite=?")
        params.append(1 if favourite else 0)

    if live_only:
        clauses.append("s.live_status='Live'")

    if min_followers is not None:
        clauses.append("s.followers>=?")
        params.append(min_followers)

    if max_followers is not None:
        clauses.append("s.followers<=?")
        params.append(max_followers)

    if tags:
        if isinstance(tags, str):
            tags = [tags]
        tags = [t.lower().strip() for t in tags if t.strip()]
        for tag in tags:
            clauses.append("(',' || LOWER(m.tags) || ',') LIKE ?")
            params.append(f"%,{tag},%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    offset = max(page - 1, 0) * page_size

    # Search relevance wins ties without sacrificing the user's explicit
    # sort. Exact username first, then username prefix, then the selected
    # sort. This avoids making a common search feel random on large rosters.
    order_sql = f"{column} {direction}, s.username ASC"
    order_params = []
    if query and query.strip():
        needle = query.strip().lower()
        order_sql = "CASE WHEN LOWER(s.username)=? THEN 0 WHEN LOWER(s.username) LIKE ? THEN 1 ELSE 2 END, " + order_sql
        order_params.extend([needle, needle + "%"])

    with db() as con:

        rows = con.execute(
            f"""
            SELECT s.*
            FROM streamers s
            LEFT JOIN streamer_metadata m ON s.username=m.username
            {where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*params, *order_params, page_size, offset),
        ).fetchall()

        total = con.execute(
            f"""
            SELECT COUNT(*)

            FROM streamers s

            LEFT JOIN streamer_metadata m
            ON s.username=m.username

            {where}
            """,
            params,
        ).fetchone()[0]

    return rows, total



@_database_operation
def get_streamer(username):

    with db() as con:

        return con.execute(
            """
            SELECT *
            FROM streamers
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        ).fetchone()



@_database_operation
def get_profile_image(username):

    with db() as con:

        result = con.execute(
            """
            SELECT profile_image
            FROM streamers
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        ).fetchone()


    return result[0] if result else None



@_database_operation
def remove_streamer(username):

    username = username.lower()

    tables = [

        "streamer_username_history",
        "streamer_activity",

        "viewer_history",

        "stream_sessions",

        "category_history",

        "scout_events",

        "raid_history",

        "streamer_metadata",

        "streamer_responses",

        "recently_viewed",

        "streamers",
    ]


    deleted = 0


    with db() as con:

        for table in tables:

            result = con.execute(
                f"""
                DELETE FROM {table}
                WHERE username=?
                """,
                (
                    username,
                ),
            )

            deleted += result.rowcount


    _invalidate_all_cache()

    return deleted > 0


# ==========================
# ARCHIVE SYSTEM
# ==========================
# Archiving keeps history (viewer_history, sessions, events, ratings, etc.)
# while removing a streamer from the "active" working set used by get_all(),
# autocomplete, and /scout list — so a growing roster of long-inactive
# streamers doesn't slow down everyday commands. Archived rows are still
# queryable directly (get_streamer, get_all(include_archived=True), search).

@_database_operation
def archive_streamer(username):

    username = username.lower()

    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamers
            SET archived=1, archived_at=?
            WHERE username=? AND archived=0
            """,
            (
                datetime.now().isoformat(),
                username,
            ),
        )

    _invalidate_all_cache()

    return cursor.rowcount > 0


@_database_operation
def unarchive_streamer(username):

    username = username.lower()

    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamers
            SET archived=0, archived_at=NULL
            WHERE username=? AND archived=1
            """,
            (
                username,
            ),
        )

    _invalidate_all_cache()

    return cursor.rowcount > 0


@_database_operation
def archive_inactive_streamers(days=90):
    """Bulk-archive streamers offline for longer than `days`. Returns the
    number of rows archived. Intended for scheduled maintenance or an admin
    command once the active roster grows large."""

    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamers

            SET archived=1, archived_at=?

            WHERE archived=0

            AND live_status='Offline'

            AND
            (
                last_live IS NULL

                OR

                replace(last_live, 'T', ' ')
                < datetime('now', ?)
            )
            """,
            (
                datetime.now().isoformat(),
                f"-{days} days",
            ),
        )

    _invalidate_all_cache()

    return cursor.rowcount


@_database_operation
def get_archived_streamers(limit=100, offset=0):

    with db() as con:

        return con.execute(
            """
            SELECT *
            FROM streamers
            WHERE archived=1
            ORDER BY archived_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


@_database_operation
def get_archived_count():

    with db() as con:

        result = con.execute(
            """
            SELECT COUNT(*)
            FROM streamers
            WHERE archived=1
            """
        ).fetchone()

    return result[0] if result else 0


# ==========================
# NOTES
# ==========================

@_database_operation
def update_notes(username, notes):

    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamers
            SET notes=?
            WHERE username=?
            """,
            (
                notes,
                username.lower(),
            ),
        )

    _invalidate_all_cache()

    return cursor.rowcount > 0



# ==========================
# PROFILE IMAGE
# ==========================

@_database_operation
def update_profile_image(username, image_url):

    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamers
            SET profile_image=?
            WHERE username=?
            """,
            (
                image_url,
                username.lower(),
            ),
        )

    _invalidate_all_cache()

    return cursor.rowcount > 0



# ==========================
# RATINGS
# ==========================

@_database_operation
def update_rating(username, category, score):

    columns = {

        "community": "community_rating",

        "content": "content_rating",

        "raid": "raid_rating",

    }


    column = columns.get(
        category.lower()
    )


    if not column:
        return False



    with db() as con:

        exists = con.execute(
            """
            SELECT id
            FROM streamers
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        ).fetchone()


        if not exists:
            return False


        con.execute(
            f"""
            UPDATE streamers
            SET {column}=?
            WHERE username=?
            """,
            (
                score,
                username.lower(),
            ),
        )


    _invalidate_all_cache()

    return True



# ==========================
# RAID SCORE
# ==========================

@_database_operation
def update_raid_score(username, score, manual=True):
    with db() as con:
        if manual:
            cursor = con.execute("UPDATE streamers SET raid_score=?,manual_raid_score=? WHERE username=?", (score, score, username.lower()))
        else:
            cursor = con.execute("UPDATE streamers SET raid_score=? WHERE username=?", (score, username.lower()))
    _invalidate_all_cache()
    return cursor.rowcount > 0


# ==========================
# TWITCH DATA UPDATE
# ==========================

@_database_operation
def update_twitch_data(
    username,
    category,
    followers,
    viewers,
    status,
):

    username = username.lower()

    now = datetime.now().isoformat()


    existing = get_streamer(username)


    if not existing:
        return False


    old_category = existing["category"] or ""


    # ==========================
    # VIEWER TRACKING
    # ==========================

    if viewers > 0:

        add_viewer_sample(
            username,
            viewers,
        )

        update_peak_viewers(
            username,
            viewers,
        )

        average = get_average_viewers(
            username
        )

    else:

        average = existing["average_viewers"] or 0



    # ==========================
    # CATEGORY SAFETY
    # ==========================

    if not category:

        category = old_category



    with db() as con:

        con.execute(
            """
            UPDATE streamers

            SET

                category=?,

                followers=COALESCE(?, followers),

                average_viewers=?,

                current_viewers=?,

                live_status=?,

                last_updated=?,

                last_live =
                    CASE
                        WHEN ? > 0
                        THEN ?
                        ELSE last_live
                    END


            WHERE username=?

            """,
            (
                category,
                followers,
                average,
                viewers,
                status,
                now,
                viewers,
                now,
                username,
            ),
        )



    _invalidate_all_cache()

    if category != old_category:

        add_category_history(
            username,
            category,
        )


    return True



@_database_operation
def update_last_live(username):

    with db() as con:

        con.execute(
            """
            UPDATE streamers
            SET last_live=?
            WHERE username=?
            """,
            (
                datetime.now().isoformat(),
                username.lower(),
            ),
        )


# ==========================
# LIVE NOTIFICATION FLAG
# ==========================
# Prevents spamming a 🔴 "went live" notification on every 5-minute tracker
# poll — set once when the notification fires, cleared once the streamer
# goes back offline.

@_database_operation
def mark_live_notified(username):

    with db() as con:

        con.execute(
            """
            UPDATE streamers
            SET notified_live=1
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        )

    _invalidate_all_cache()


@_database_operation
def clear_live_notified(username):

    with db() as con:

        con.execute(
            """
            UPDATE streamers
            SET notified_live=0
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        )

    _invalidate_all_cache()


@_database_operation
def was_live_notified(username):

    with db() as con:

        result = con.execute(
            """
            SELECT notified_live
            FROM streamers
            WHERE username=?
            """,
            (
                username.lower(),
            ),
        ).fetchone()

    return bool(result[0]) if result else False



# ==========================
# VIEWER HISTORY
# ==========================

@_database_operation
def add_viewer_sample(username, viewers):

    with db() as con:

        con.execute(
            """
            INSERT INTO viewer_history
            (
                username,
                viewers,
                timestamp
            )

            VALUES (?,?,?)
            """,
            (
                username.lower(),
                viewers,
                datetime.now().isoformat(),
            ),
        )



@_database_operation
def get_average_viewers(username):

    with db() as con:

        result = con.execute(
            """
            SELECT AVG(viewers)

            FROM viewer_history

            WHERE username=?

            AND datetime(
                replace(timestamp,'T',' ')
            ) >= datetime(
                'now',
                '-30 days'
            )

            """,
            (
                username.lower(),
            ),
        ).fetchone()


    if result and result[0]:

        return round(
            result[0]
        )


    return 0

@_database_operation
def update_peak_viewers(username, viewers):

    with db() as con:

        con.execute(
            """
            UPDATE streamers

            SET peak_viewers =
                CASE

                    WHEN ? > peak_viewers
                    THEN ?

                    ELSE peak_viewers

                END

            WHERE username=?

            """,
            (
                viewers,
                viewers,
                username.lower(),
            ),
        )



@_database_operation
def get_viewer_history(username, limit=50):

    with db() as con:

        return con.execute(
            """
            SELECT
                viewers,
                timestamp

            FROM viewer_history

            WHERE username=?

            ORDER BY id DESC

            LIMIT ?

            """,
            (
                username.lower(),
                limit,
            ),
        ).fetchall()



# ==========================
# CATEGORY HISTORY
# ==========================

@_database_operation
def add_category_history(username, category):

    if not category:
        return


    with db() as con:

        con.execute(
            """
            INSERT INTO category_history
            (
                username,
                category,
                timestamp
            )

            VALUES (?,?,?)

            """,
            (
                username.lower(),
                category,
                datetime.now().isoformat(),
            ),
        )



@_database_operation
def get_category_history(username, limit=25):

    with db() as con:

        return con.execute(
            """
            SELECT
                category,
                timestamp

            FROM category_history

            WHERE username=?

            ORDER BY id DESC

            LIMIT ?

            """,
            (
                username.lower(),
                limit,
            ),
        ).fetchall()



# ==========================
# EVENTS
# ==========================

@_database_operation
def add_event(username, event):

    with db() as con:

        con.execute(
            """
            INSERT INTO scout_events
            (
                username,
                event,
                timestamp
            )

            VALUES (?,?,?)

            """,
            (
                username.lower(),
                event,
                datetime.now().isoformat(),
            ),
        )



@_database_operation
def get_events(username, limit=25):

    with db() as con:

        return con.execute(
            """
            SELECT
                event,
                timestamp

            FROM scout_events

            WHERE username=?

            ORDER BY id DESC

            LIMIT ?

            """,
            (
                username.lower(),
                limit,
            ),
        ).fetchall()



# ==========================
# STREAM SESSIONS
# ==========================

@_database_operation
def create_stream_session(username):

    with db() as con:

        con.execute(
            """
            INSERT INTO stream_sessions
            (
                username,
                started,
                ended,
                peak_viewers,
                average_viewers
            )

            VALUES (?,?,?,?,?)

            """,
            (
                username.lower(),
                datetime.now().isoformat(),
                None,
                0,
                0,
            ),
        )



@_database_operation
def end_stream_session(
    username,
    peak_viewers,
    average_viewers,
):

    with db() as con:

        session = con.execute(
            """
            SELECT id

            FROM stream_sessions

            WHERE username=?

            AND ended IS NULL

            ORDER BY id DESC

            LIMIT 1

            """,
            (
                username.lower(),
            ),
        ).fetchone()


        if session:

            con.execute(
                """
                UPDATE stream_sessions

                SET

                    ended=?,

                    peak_viewers=?,

                    average_viewers=?


                WHERE id=?

                """,
                (
                    datetime.now().isoformat(),
                    peak_viewers,
                    average_viewers,
                    session[0],
                ),
            )



@_database_operation
def get_stream_sessions(username, limit=25):

    with db() as con:

        return con.execute(
            """
            SELECT

                id,

                started,

                ended,

                peak_viewers,

                average_viewers


            FROM stream_sessions


            WHERE username=?


            ORDER BY id DESC


            LIMIT ?

            """,
            (
                username.lower(),
                limit,
            ),
        ).fetchall()



@_database_operation
def get_session_viewer_curve(username, session_id):
    # Returns the viewer_history datapoints that fall within one specific
    # stream session's start/end window, oldest first — i.e. the actual
    # viewer curve for that single session rather than an all-time average.
    # viewer_history isn't tagged with a session id, so this joins on the
    # session's own started/ended timestamps (ended IS NULL means the
    # session is still live, so we bound to "now" via NULL-safe comparison).

    with db() as con:

        session = con.execute(
            """
            SELECT id, started, ended
            FROM stream_sessions
            WHERE id=? AND username=?
            """,
            (
                session_id,
                username.lower(),
            ),
        ).fetchone()

        if not session:
            return None

        rows = con.execute(
            """
            SELECT viewers, timestamp
            FROM viewer_history
            WHERE username=?
              AND timestamp >= ?
              AND (? IS NULL OR timestamp <= ?)
            ORDER BY id ASC
            """,
            (
                username.lower(),
                session["started"],
                session["ended"],
                session["ended"],
            ),
        ).fetchall()

        return {
            "session": dict(session),
            "points": [dict(r) for r in rows],
        }

# ==========================
# STREAMER METADATA
# ==========================

@_database_operation
def ensure_metadata(username):

    username = username.lower()

    with db() as con:

        con.execute(
            """
            INSERT OR IGNORE INTO streamer_metadata
            (
                username,
                favourite,
                tags,
                priority,
                alias,
                notify_enabled,
                x_url,
                discord_url,
                youtube_url,
                kick_url,
                outreach_status
            )

            VALUES (?,?,?,?,?,?,?,?,?,?,?)

            """,
            (
                username,
                0,
                "",
                "Watch",
                "",
                1,
                "",
                "",
                "",
                "",
                "active",
            ),
        )



@_database_operation
def get_streamer_metadata(username):

    username = username.lower()

    ensure_metadata(username)


    with db() as con:

        result = con.execute(
            """
            SELECT

                favourite,

                tags,

                priority,

                alias,

                notify_enabled,

                x_url,

                discord_url,

                youtube_url,

                kick_url,

                outreach_status,

                bio,

                scraped_social_links,

                social_scraped_at,

                scraped_location,

                scraped_timezone,

                location,

                timezone,

                scraped_age


            FROM streamer_metadata


            WHERE username=?

            """,
            (
                username,
            ),
        ).fetchone()



    return _metadata_to_dict(result)


def _metadata_to_dict(result):
    if not result:

        return {

            "favourite": False,

            "tags": [],

            "priority": "Watch",

            "alias": "",

            "notify_enabled": True,

            "x_url": "",

            "instagram_url": "",

            "youtube_url": "",

            "kick_url": "",

            "outreach_status": "active",

            "bio": "",

            "scraped_social_links": {},

            "social_scraped_at": None,

            "scraped_location": "",

            "scraped_timezone": None,

            "location": "",

            "timezone": None,

            "scraped_age": None,

        }



    scraped_links = {}
    if result[11]:
        try:
            scraped_links = json.loads(result[11])
        except (TypeError, ValueError):
            scraped_links = {}

    return {

        "favourite": bool(result[0]),

        "tags":
            [
                tag.strip()

                for tag in (result[1] or "").split(",")

                if tag.strip()
            ],

        "priority": result[2] or "Watch",

        "alias": result[3] or "",

        "notify_enabled": bool(result[4]) if result[4] is not None else True,

        "x_url": result[5] or "",

        # Column is still named discord_url in SQLite (see
        # migrate_metadata_table) but now holds the Instagram URL.
        "instagram_url": result[6] or "",

        "youtube_url": result[7] or "",

        "kick_url": result[8] or "",

        "outreach_status": result[9] or "active",

        "bio": result[10] or "",

        "scraped_social_links": scraped_links,

        "social_scraped_at": result[12],

        "scraped_location": result[13] or "",

        "scraped_timezone": result[14],

        # Manually-entered — never touched by set_scraped_social().
        "location": result[15] or "",

        "timezone": result[16],

        "scraped_age": result[17],

    }


@_database_operation
def get_tracked_locations_bulk(usernames):
    """Batched companion to streamer_exists()+get_streamer_metadata() for
    Discover result enrichment. Given a list of usernames, returns a dict
    mapping each already-tracked username (lowercased) to its best-effort
    location (manual `location`, falling back to `scraped_location`).
    Usernames not present in `streamers` are simply absent from the dict.

    A single `WHERE username IN (...)` lookup replaces what would
    otherwise be one streamer_exists() + one get_streamer_metadata() call
    (each its own query) per result.
    """

    lowered = list(dict.fromkeys(u.lower() for u in usernames if u))

    if not lowered:
        return {}

    placeholders = ",".join("?" for _ in lowered)

    with db() as con:

        rows = con.execute(
            f"""
            SELECT
                s.username,
                COALESCE(m.resolved_location, '') AS resolved_location
            FROM streamers s
            LEFT JOIN streamer_metadata m ON m.username = s.username
            WHERE s.username IN ({placeholders})
            """,
            lowered,
        ).fetchall()

    return {row["username"]: row["resolved_location"] or "" for row in rows}


@_database_operation
def set_scraped_social(username, bio, social_links, location=None, timezone=None, age=None):
    """Stores the scraped (not user-entered) bio text and social links
    dict from twitch_api.get_channel_social(), plus a timestamp. Separate
    from set_social_links() (the user-entered x_url/instagram_url/
    youtube_url/kick_url fields) so a re-scrape never overwrites
    something the person typed in manually, and vice versa.

    location/timezone are the best-effort values extracted from that same
    bio text (see twitch_api._extract_location / _guess_timezone); age is
    the same best-effort extraction for a self-reported age (see
    twitch_api._extract_age). All three are optional and left as
    None/empty when nothing recognizable was found, which also clears
    out a stale value from a previous scrape."""

    username = username.lower()

    if not streamer_exists(username):
        return False
    ensure_metadata(username)

    with db() as con:
        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET bio=?, scraped_social_links=?, social_scraped_at=?, scraped_location=?, scraped_timezone=?, scraped_age=?

            WHERE username=?
            """,
            (
                bio or "",
                json.dumps(social_links or {}),
                datetime.now().isoformat(),
                location or "",
                timezone,
                age,
                username,
            ),
        )

    return cursor.rowcount > 0


# Platforms whose scraped link (see twitch_api.SOCIAL_PATTERNS labels)
# should also backfill a roster tracking field — keeps the manually-
# entered youtube_url/kick_url fields populated automatically from the
# same bio/panel scrape that fills scraped_social_links, without ever
# touching x_url/instagram_url (those stay purely manual, unchanged
# from before) or overwriting a link the person already typed in by
# hand for YouTube/Kick.
_SCRAPED_PLATFORM_TO_FIELD = {
    "YouTube": "youtube_url",
    "Kick": "kick_url",
}


@_database_operation
def backfill_scraped_platform_links(username, social_links):
    """Given the label->url dict from a bio/panel scrape, fills any of
    youtube_url/kick_url that are still empty with the scraped link for
    that platform. A field the person has already set manually (via
    set_social_links) is left untouched — this only ever fills a blank,
    never overwrites."""

    if not social_links:
        return

    username = username.lower()

    if not streamer_exists(username):
        return False
    ensure_metadata(username)

    current_meta = get_streamer_metadata(username)

    updates = {}
    for label, field in _SCRAPED_PLATFORM_TO_FIELD.items():
        url = social_links.get(label)
        if not url:
            continue
        if not current_meta.get(field, ""):
            updates[field] = url

    if updates:
        set_social_links(
            username,
            youtube_url=updates.get("youtube_url"),
            kick_url=updates.get("kick_url"),
        )



@_database_operation
def set_favourite(username, value):

    username = username.lower()

    ensure_metadata(username)


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET favourite=?

            WHERE username=?

            """,
            (
                1 if value else 0,

                username,
            ),
        )


    return cursor.rowcount > 0



@_database_operation
def set_notify_enabled(username, value):

    username = username.lower()

    ensure_metadata(username)


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET notify_enabled=?

            WHERE username=?

            """,
            (
                1 if value else 0,

                username,
            ),
        )


    return cursor.rowcount > 0



@_database_operation
def set_social_links(username, x_url=None, instagram_url=None, youtube_url=None, kick_url=None):
    """Updates whichever of x_url/instagram_url/youtube_url/kick_url are
    provided (None = leave unchanged), so a caller can set just one
    without clobbering the others. instagram_url is stored in the
    `discord_url` column (see migrate_metadata_table) — the app no
    longer scrapes/uses Discord links, so that column now holds the
    manually-entered Instagram URL instead."""

    username = username.lower()

    ensure_metadata(username)

    fields = []
    values = []

    if x_url is not None:
        fields.append("x_url=?")
        values.append(x_url)

    if instagram_url is not None:
        fields.append("discord_url=?")
        values.append(instagram_url)

    if youtube_url is not None:
        fields.append("youtube_url=?")
        values.append(youtube_url)

    if kick_url is not None:
        fields.append("kick_url=?")
        values.append(kick_url)

    if not fields:
        return True

    values.append(username)

    with db() as con:
        cursor = con.execute(
            f"""
            UPDATE streamer_metadata

            SET {", ".join(fields)}

            WHERE username=?
            """,
            values,
        )

    return cursor.rowcount > 0


@_database_operation
def _normalize_location(location, con):
    """Best-effort cleanup of a manually-entered location string before
    it's stored: trims surrounding whitespace, collapses internal runs
    of whitespace to a single space, and title-cases it (so "new york,
    ny" and "NEW YORK, NY" both save as "New York, Ny" — imperfect for
    things like "NZ"/postal-style abbreviations, but consistent, which
    is what the filter dropdown actually needs). An empty/whitespace-only
    string normalizes to "" (still a valid "clear the field" value, same
    as before).

    Also dedupes near-duplicates that differ only by case/whitespace
    against locations already on file: if a case-insensitive match for
    the normalized text already exists (manual or scraped, across any
    streamer), that existing spelling is reused instead of adding a
    second, differently-capitalized entry for what's really the same
    place — keeps the filter dropdown from accumulating "Auckland, NZ"
    / "auckland, nz" / "Auckland,  NZ" as three separate options."""
    if location is None:
        return None
    collapsed = re.sub(r"\s+", " ", location).strip()
    if not collapsed:
        return ""
    existing = con.execute(
        """
        SELECT location AS loc FROM streamer_metadata WHERE location != '' AND location IS NOT NULL
        UNION
        SELECT scraped_location AS loc FROM streamer_metadata WHERE scraped_location != '' AND scraped_location IS NOT NULL
        """
    ).fetchall()
    for row in existing:
        if row["loc"] and row["loc"].strip().lower() == collapsed.lower():
            return row["loc"]
    return collapsed.title()


@_database_operation
def set_location(username, location=None, timezone=None):
    """Updates the manually-entered location/timezone (None = leave
    unchanged, same pattern as set_social_links). Free-text `location`
    (e.g. "Auckland, NZ") plus an IANA `timezone` name picked from a
    dropdown (e.g. "Pacific/Auckland") — stored in the location/timezone
    columns, entirely separate from scraped_location/scraped_timezone so
    a re-scrape never overwrites a manual edit and vice versa. Passing an
    empty string clears that field (distinct from None, which leaves it
    untouched). `location` is normalized (trimmed, whitespace-collapsed,
    title-cased, deduped against existing near-identical entries) before
    being stored — see _normalize_location."""

    username = username.lower()

    ensure_metadata(username)

    fields = []
    values = []

    with db() as con:
        if location is not None:
            fields.append("location=?")
            values.append(_normalize_location(location, con))

        if timezone is not None:
            fields.append("timezone=?")
            values.append(timezone or None)

        if not fields:
            return True

        values.append(username)

        cursor = con.execute(
            f"""
            UPDATE streamer_metadata

            SET {", ".join(fields)}

            WHERE username=?
            """,
            values,
        )

    return cursor.rowcount > 0


@_database_operation
def streamers_exist_bulk(usernames):
    """Batched companion to streamer_exists() — returns the subset of
    `usernames` (lowercased) that are present in the `streamers` table,
    in one query instead of one streamer_exists() call per username."""

    lowered = list(dict.fromkeys(u.lower() for u in usernames if u))

    if not lowered:
        return set()

    placeholders = ",".join("?" for _ in lowered)

    with db() as con:

        rows = con.execute(
            f"""
            SELECT username
            FROM streamers
            WHERE username IN ({placeholders})
            """,
            lowered,
        ).fetchall()

    return {row["username"] for row in rows}


@_database_operation
def set_location_bulk(usernames, location=None, timezone=None):
    """Bulk companion to set_location() — applies the same
    location/timezone pair (None = leave unchanged, empty string =
    clear, same semantics as set_location) to every username in one
    call instead of one UPDATE per streamer, for a bulk-edit selection
    in the UI. Returns the number of rows actually updated. Usernames
    not present in the `streamers` table are skipped (matches the
    single-streamer endpoint's streamer_exists() gate — set_location()
    itself has no such check and would otherwise happily create orphan
    metadata rows for untracked usernames)."""

    lowered = list(dict.fromkeys(u.lower() for u in usernames if u))

    if not lowered or (location is None and timezone is None):
        return 0

    tracked = streamers_exist_bulk(lowered)
    lowered = [u for u in lowered if u in tracked]

    if not lowered:
        return 0

    for username in lowered:
        ensure_metadata(username)

    updated = 0

    with db() as con:
        # location is normalized per-username (dedup against existing
        # near-identical entries, same as set_location) so this can't be
        # collapsed into one executemany with a single shared params
        # tuple — _normalize_location may legitimately return different
        # stored text per row even though the input is identical.
        for username in lowered:
            row_fields = []
            row_values = []

            if location is not None:
                row_fields.append("location=?")
                row_values.append(_normalize_location(location, con))

            if timezone is not None:
                row_fields.append("timezone=?")
                row_values.append(timezone or None)

            row_values.append(username)

            cursor = con.execute(
                f"""
                UPDATE streamer_metadata

                SET {", ".join(row_fields)}

                WHERE username=?
                """,
                row_values,
            )

            if cursor.rowcount > 0:
                updated += 1

    return updated


@_database_operation
def set_tags_bulk(usernames, tags):
    """Bulk companion to set_tags() — applies the same tag list to every
    username in one call, for a multi-select bulk-edit action in the UI
    instead of one PUT per streamer. `tags` uses the same
    cleaned/lowercased/sorted/comma-joined storage convention as
    set_tags(). Usernames not present in the `streamers` table are
    skipped (matches the single-streamer endpoint's streamer_exists()
    gate). Returns the number of rows actually updated."""

    lowered = list(dict.fromkeys(u.lower() for u in usernames if u))

    if not lowered:
        return 0

    tracked = streamers_exist_bulk(lowered)
    lowered = [u for u in lowered if u in tracked]

    if not lowered:
        return 0

    for username in lowered:
        ensure_metadata(username)

    cleaned = ",".join(
        sorted(
            {
                tag.strip().lower()
                for tag in tags
                if tag.strip()
            }
        )
    )

    updated = 0

    with db() as con:
        for username in lowered:
            cursor = con.execute(
                """
                UPDATE streamer_metadata

                SET tags=?

                WHERE username=?
                """,
                (cleaned, username),
            )

            if cursor.rowcount > 0:
                updated += 1

    return updated


@_database_operation
def set_priority_bulk(usernames, priority):
    """Bulk companion to set_priority() — applies the same priority to
    every username in one call, for a multi-select bulk-edit action in
    the UI instead of one PUT per streamer. Usernames not present in the
    `streamers` table are skipped (matches the single-streamer
    endpoint's streamer_exists() gate). Returns the number of rows
    actually updated, or -1 if `priority` isn't one of the allowed
    values (mirrors set_priority()'s False-on-invalid contract)."""

    priority = priority.capitalize()

    allowed = {"High", "Medium", "Watch", "Ignore"}

    if priority not in allowed:
        return -1

    lowered = list(dict.fromkeys(u.lower() for u in usernames if u))

    if not lowered:
        return 0

    tracked = streamers_exist_bulk(lowered)
    lowered = [u for u in lowered if u in tracked]

    if not lowered:
        return 0

    for username in lowered:
        ensure_metadata(username)

    updated = 0

    with db() as con:
        for username in lowered:
            cursor = con.execute(
                """
                UPDATE streamer_metadata

                SET priority=?

                WHERE username=?
                """,
                (priority, username),
            )

            if cursor.rowcount > 0:
                updated += 1

    return updated


@_database_operation
def toggle_notify(username):
    username = username.lower()
    ensure_metadata(username)
    with db() as con:
        con.execute("UPDATE streamer_metadata SET notify_enabled=1-COALESCE(notify_enabled,1) WHERE username=?", (username,))
        return bool(con.execute("SELECT notify_enabled FROM streamer_metadata WHERE username=?", (username,)).fetchone()[0])


@_database_operation
def toggle_favourite(username):
    username = username.lower()
    ensure_metadata(username)
    with db() as con:
        con.execute("UPDATE streamer_metadata SET favourite=1-COALESCE(favourite,0) WHERE username=?", (username,))
        return bool(con.execute("SELECT favourite FROM streamer_metadata WHERE username=?", (username,)).fetchone()[0])


@_database_operation
def get_favourites(include_archived=False, limit=None):

    where = "m.favourite=1" if include_archived else "m.favourite=1 AND s.archived=0"

    # `limit` is optional (None = unbounded, existing behavior for every
    # caller except the Dashboard widget) — kept as a plain Python-level
    # LIMIT clause appended only when requested, so the default query
    # shape/plan for existing callers doesn't change at all.
    limit_clause = "LIMIT ?" if limit else ""
    params = (limit,) if limit else ()

    with db() as con:

        return con.execute(
            f"""
            SELECT s.*

            FROM streamers s

            JOIN streamer_metadata m

            ON s.username=m.username


            WHERE {where}


            ORDER BY s.followers DESC

            {limit_clause}

            """,
            params,
        ).fetchall()



@_database_operation
def set_tags(username, tags):

    username = username.lower()

    ensure_metadata(username)


    cleaned = ",".join(
        sorted(
            {
                tag.strip().lower()

                for tag in tags

                if tag.strip()
            }
        )
    )


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET tags=?

            WHERE username=?

            """,
            (
                cleaned,

                username,
            ),
        )


    return cursor.rowcount > 0



@_database_operation
def search_tags(tags, match_all=False, include_archived=False):
    """Find streamers by tag. `tags` may be a single string or a list of
    tags; matching is case-insensitive. `match_all=True` requires every
    tag to be present, otherwise any matching tag qualifies (OR)."""

    if isinstance(tags, str):
        tags = [tags]

    tags = [t.lower().strip() for t in tags if t.strip()]

    if not tags:
        return []

    tag_clauses = " AND ".join(["',' || LOWER(m.tags) || ',' LIKE ?"] * len(tags)) \
        if match_all else \
        " OR ".join(["',' || LOWER(m.tags) || ',' LIKE ?"] * len(tags))

    clauses = f"({tag_clauses})"

    if not include_archived:
        clauses += " AND s.archived=0"

    params = tuple(f"%,{tag},%" for tag in tags)

    with db() as con:

        return con.execute(
            f"""
            SELECT s.*

            FROM streamers s

            JOIN streamer_metadata m

            ON s.username=m.username


            WHERE {clauses}


            ORDER BY s.followers DESC

            """,
            params,
        ).fetchall()


@_database_operation
def get_all_tags():
    """Distinct list of every tag currently in use, for autocomplete."""

    with db() as con:

        rows = con.execute(
            """
            SELECT tags
            FROM streamer_metadata
            WHERE tags != ''
            """
        ).fetchall()

    tag_set = set()

    for row in rows:
        for tag in (row[0] or "").split(","):
            tag = tag.strip()
            if tag:
                tag_set.add(tag)

    return sorted(tag_set)



@_database_operation
def set_priority(username, priority):

    username = username.lower()

    priority = priority.capitalize()


    allowed = {

        "High",

        "Medium",

        "Watch",

        "Ignore",

    }


    if priority not in allowed:

        return False



    ensure_metadata(username)


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET priority=?

            WHERE username=?

            """,
            (
                priority,

                username,
            ),
        )


    return cursor.rowcount > 0


# ==========================
# STREAMER BLACKLIST
# ==========================
# Hard exclude — separate from priority="Ignore" above (which is just an
# attention ranking; an Ignore-priority streamer still shows up everywhere,
# including Discover). A blacklisted username is filtered out of Discover
# results server-side (see twitch_api.search_streams's blacklist param) so
# it never reappears there again, regardless of tracked/priority status.
# Not tied to streamer_metadata since blacklisting works for usernames that
# have never been tracked (and therefore have no metadata row at all).

@_database_operation
def add_to_blacklist(username):
    username = username.lower().strip()
    if not username:
        return False
    with db() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO streamer_blacklist
            (username, blacklisted_at)
            VALUES (?, ?)
            """,
            (username, datetime.now().isoformat()),
        )
    return True


@_database_operation
def remove_from_blacklist(username):
    username = username.lower().strip()
    with db() as con:
        cursor = con.execute(
            """
            DELETE FROM streamer_blacklist
            WHERE username=?
            """,
            (username,),
        )
    return cursor.rowcount > 0


@_database_operation
def is_blacklisted(username):
    username = username.lower().strip()
    with db() as con:
        row = con.execute(
            """
            SELECT 1 FROM streamer_blacklist
            WHERE username=?
            """,
            (username,),
        ).fetchone()
    return row is not None


@_database_operation
def get_blacklist():
    """All blacklisted usernames, most recently blacklisted first."""
    with db() as con:
        rows = con.execute(
            """
            SELECT username, blacklisted_at
            FROM streamer_blacklist
            ORDER BY blacklisted_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@_database_operation
def get_blacklist_set():
    """Just the usernames, as a set — for fast membership checks against a
    page of Discover candidates without a query per candidate."""
    with db() as con:
        rows = con.execute(
            "SELECT username FROM streamer_blacklist"
        ).fetchall()
    return {row[0] for row in rows}


# ==========================
# CUSTOM SOCIAL PLATFORMS
# ==========================
# Settings-defined platforms (label + URL-matching pattern) merged into
# twitch_api.SOCIAL_PATTERNS at scrape time — see
# twitch_api.get_effective_social_patterns(). Keyed on label so adding
# the same label again just replaces its pattern (INSERT OR REPLACE)
# rather than erroring or creating a duplicate.

@_database_operation
def add_custom_social_platform(label, pattern):
    label = label.strip()
    pattern = pattern.strip()
    if not label or not pattern:
        return False
    with db() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO custom_social_platforms
            (label, pattern, created_at)
            VALUES (?, ?, ?)
            """,
            (label, pattern, datetime.now().isoformat()),
        )
    return True


@_database_operation
def remove_custom_social_platform(label):
    label = label.strip()
    with db() as con:
        cursor = con.execute(
            """
            DELETE FROM custom_social_platforms
            WHERE label=?
            """,
            (label,),
        )
    return cursor.rowcount > 0


@_database_operation
def get_custom_social_platforms():
    """All custom platforms, in the order they were added (oldest
    first) — matches the order new patterns get appended to
    SOCIAL_PATTERNS at merge time."""
    with db() as con:
        rows = con.execute(
            """
            SELECT label, pattern, created_at
            FROM custom_social_platforms
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


# Outreach lifecycle stages, beyond archive/active: tracks where a
# streamer sits in the raid-outreach pipeline. Deliberately separate from
# `priority` (attention ranking) and `streamers.archived` (storage
# visibility) — a streamer can be "contacted" at any priority and stay
# fully active/unarchived the whole time.
OUTREACH_STATUSES = {
    "active",
    "contacted",
    "raided",
    "declined",
}


@_database_operation
def set_outreach_status(username, status):

    username = username.lower()

    status = status.strip().lower()

    if status not in OUTREACH_STATUSES:

        return False


    ensure_metadata(username)


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET outreach_status=?

            WHERE username=?

            """,
            (
                status,

                username,
            ),
        )


    return cursor.rowcount > 0


@_database_operation
def get_by_outreach_status(status, include_archived=False):
    """Streamers currently in a given outreach stage, for outreach-tracking
    views (e.g. 'show everyone I've contacted but haven't raided yet')."""

    status = status.strip().lower()

    archive_clause = "" if include_archived else "AND s.archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT s.*

            FROM streamers s

            JOIN streamer_metadata m ON m.username = s.username

            WHERE m.outreach_status=?

            {archive_clause}

            ORDER BY s.followers DESC
            """,
            (
                status,
            ),
        ).fetchall()


@_database_operation
def set_alias(username, alias):

    username = username.lower()

    ensure_metadata(username)


    with db() as con:

        cursor = con.execute(
            """
            UPDATE streamer_metadata

            SET alias=?

            WHERE username=?

            """,
            (
                alias,

                username,
            ),
        )


    return cursor.rowcount > 0



# ==========================
# RAID HISTORY
# ==========================

@_database_operation
def add_raid_history(
    username,
    raid_score,
    result
):

    with db() as con:

        con.execute(
            """
            INSERT INTO raid_history
            (
                username,

                raid_score,

                result,

                timestamp
            )


            VALUES (?,?,?,?)

            """,
            (
                username.lower(),

                raid_score,

                result,

                datetime.now().isoformat(),
            ),
        )



@_database_operation
def get_raid_history(username, limit=25):

    with db() as con:

        return con.execute(
            """
            SELECT

                raid_score,

                result,

                timestamp


            FROM raid_history


            WHERE username=?


            ORDER BY id DESC


            LIMIT ?

            """,
            (
                username.lower(),

                limit,
            ),
        ).fetchall()



# ==========================
# STATS / FILTER HELPERS
# ==========================

@_database_operation
def get_recent_streamers(limit=10, include_archived=False):

    where = "" if include_archived else "WHERE archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT *

            FROM streamers

            {where}

            ORDER BY datetime(discovered) DESC

            LIMIT ?

            """,
            (
                limit,
            ),
        ).fetchall()



@_database_operation
def get_inactive_streamers(days=30, include_archived=False):

    archive_clause = "" if include_archived else "AND archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT *

            FROM streamers


            WHERE live_status='Offline'

            {archive_clause}


            AND
            (
                last_live IS NULL

                OR

                replace(
                    last_live,
                    'T',
                    ' '
                )

                < datetime(
                    'now',
                    ?
                )
            )


            ORDER BY followers DESC

            """,
            (
                f"-{days} days",
            ),
        ).fetchall()



@_database_operation
def get_scout_stats(include_archived=False):
    """Aggregate counts/sums computed in SQL instead of loading every row —
    stays fast regardless of roster size."""

    where = "" if include_archived else "WHERE archived=0"

    with db() as con:

        row = con.execute(
            f"""
            SELECT

                COUNT(*) AS total,

                SUM(CASE WHEN live_status='Live' THEN 1 ELSE 0 END) AS live,

                SUM(followers) AS followers


            FROM streamers

            {where}

            """
        ).fetchone()

    return {
        "total": row[0] or 0,
        "live": row[1] or 0,
        "followers": row[2] or 0,
        "archived": get_archived_count(),
    }


@_database_operation
def get_random_streamer(include_archived=False):
    """Picks one random row via SQL rather than loading the whole table
    just to call random.choice() on it."""

    where = "" if include_archived else "WHERE archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT *
            FROM streamers
            {where}
            ORDER BY RANDOM()
            LIMIT 1
            """
        ).fetchone()


@_database_operation
def get_nearest_by_viewers(username, viewers, limit=10, include_archived=False):
    """Streamers with the closest current_viewers count to `viewers`,
    excluding `username`, ranked in SQL instead of scanning every row
    in Python."""

    archive_clause = "" if include_archived else "AND archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT *,
                ABS(current_viewers - ?) AS diff

            FROM streamers

            WHERE LOWER(username) != ?

            {archive_clause}

            ORDER BY diff ASC

            LIMIT ?
            """,
            (viewers, username.lower(), limit),
        ).fetchall()


@_database_operation
def get_category_stats(include_archived=False, limit=200, offset=0):

    where = "" if include_archived else "WHERE archived=0"

    with db() as con:

        return con.execute(
            f"""
            SELECT

                category,

                COUNT(*) AS amount,

                SUM(followers) AS followers


            FROM streamers

            {where}

            GROUP BY category


            ORDER BY amount DESC

            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


@_database_operation
def get_location_stats(include_archived=False, limit=200, offset=0):
    """Mirrors get_category_stats() but groups by the effective location
    (manual location when set, else scraped_location — see
    serializers.effective_location) for the roster/Discover location
    filter dropdown. Streamers with no location at all (neither manual
    nor scraped) are excluded, same as how an empty category never
    really appears.

    `limit`/`offset` cap the result set for very large rosters — the
    dropdown only ever needs the most common values, and an unbounded
    GROUP BY over the whole streamers table doesn't scale to a large
    tracked roster (same concern as get_category_stats above)."""

    where = "s.archived=0" if not include_archived else "1=1"

    with db() as con:

        return con.execute(
            f"""
            SELECT

                NULLIF(m.resolved_location, '') AS location,

                COUNT(*) AS amount

            FROM streamers s

            LEFT JOIN streamer_metadata m
            ON s.username=m.username

            WHERE {where}
            AND m.resolved_location IS NOT NULL
            AND m.resolved_location != ''

            GROUP BY location

            ORDER BY amount DESC

            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()


@_database_operation
def get_map_points(include_archived=False):
    """Per-streamer location/timezone/live-status rows for the raid map
    (see main.py's GET /api/locations/map) — unlike get_location_stats
    above (which only returns aggregate {location, amount} pairs for a
    filter dropdown), the map needs to list *which* streamers sit in
    each bucket, so this returns one row per streamer that has a
    location on file. Country/region bucketing itself happens in
    main.py (from the timezone) rather than here, same division of
    labor as the rest of this module — database.py returns rows,
    grouping/shaping for a specific view lives in the route.

    `effective_timezone` mirrors resolved_location's manual-then-scraped
    fallback (COALESCE(NULLIF(timezone,''), scraped_timezone)) but isn't
    itself a stored generated column, so it's computed inline here
    rather than added as one purely for this one query."""

    where = "s.archived=0" if not include_archived else "1=1"

    with db() as con:

        return con.execute(
            f"""
            SELECT

                s.username AS username,
                s.profile_image AS profile_image,
                s.live_status AS live_status,
                s.followers AS followers,
                m.resolved_location AS location,
                COALESCE(NULLIF(m.timezone, ''), m.scraped_timezone) AS timezone

            FROM streamers s

            LEFT JOIN streamer_metadata m
            ON s.username=m.username

            WHERE {where}
            AND m.resolved_location IS NOT NULL
            AND m.resolved_location != ''

            ORDER BY s.followers DESC
            """
        ).fetchall()



# ==========================
# DATABASE CLEANUP
# ==========================

@_database_operation
def vacuum_database():

    con = db()

    con.commit()

    con.execute("VACUUM")

    con.commit()

# ==========================
# EXPORT HELPER
# ==========================

@_database_operation
def streamer_exists(username):

    with db() as con:

        result = con.execute(
            """
            SELECT 1

            FROM streamers

            WHERE username=?

            """,
            (
                username.lower(),
            ),
        ).fetchone()


    return result is not None



# ==========================
# SAVED FILTER PRESETS
# ==========================

@_database_operation
def save_filter_preset(name, filters_json):
    """Creates or overwrites (by name) a saved filter preset. `filters_json`
    is a pre-serialized JSON string owned by the caller (main.py) — this
    layer just persists it."""

    name = name.strip()

    with db() as con:

        con.execute(
            """
            INSERT INTO filter_presets(name, filters, created_at)

            VALUES(?, ?, ?)

            ON CONFLICT(name) DO UPDATE SET

                filters=excluded.filters

            """,
            (
                name,

                filters_json,

                datetime.now().isoformat(),
            ),
        )


    return True


@_database_operation
def get_filter_presets():

    with db() as con:

        return con.execute(
            """
            SELECT id, name, filters, created_at

            FROM filter_presets

            ORDER BY name COLLATE NOCASE ASC

            """
        ).fetchall()


@_database_operation
def delete_filter_preset(name):

    with db() as con:

        cursor = con.execute(
            """
            DELETE FROM filter_presets

            WHERE name=?

            """,
            (
                name.strip(),
            ),
        )


    return cursor.rowcount > 0



# ==========================
# DISCOVER SEARCH HISTORY
# ==========================
# Recent /api/discover filter combos, most-recently-used first, so the
# Discover modal can offer "recent searches" instead of re-entering the
# same filters each time. Deduped by a hash of the filter content: running
# the same search again just bumps its `last_used` rather than creating a
# duplicate row.

_DISCOVER_HISTORY_LIMIT = 10  # rows kept — trimmed on write, not on read


@_database_operation
def _discover_filters_hash(filters):
    return hashlib.sha256(
        json.dumps(filters, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@_database_operation
def add_discover_search(filters):
    """Records (or bumps) a Discover search in history. `filters` is a
    plain dict of the filter values the person searched with; empty/blank
    values should already be stripped by the caller so near-duplicate
    entries (e.g. category='' vs omitted) collapse to the same hash."""

    filters_hash = _discover_filters_hash(filters)
    now = datetime.now().isoformat()

    with db() as con:

        con.execute(
            """
            INSERT INTO discover_search_history(filters, filters_hash, last_used)

            VALUES(?, ?, ?)

            ON CONFLICT(filters_hash) DO UPDATE SET

                last_used=excluded.last_used

            """,
            (
                json.dumps(filters),
                filters_hash,
                now,
            ),
        )

        # Trim to the most recent N so the table can't grow unbounded —
        # cheap enough to run on every write given the tiny row count.
        con.execute(
            """
            DELETE FROM discover_search_history

            WHERE id NOT IN (
                SELECT id FROM discover_search_history
                ORDER BY last_used DESC, id DESC
                LIMIT ?
            )
            """,
            (_DISCOVER_HISTORY_LIMIT,),
        )

    return True


@_database_operation
def get_discover_search_history(limit=10):

    with db() as con:

        rows = con.execute(
            """
            SELECT id, filters, last_used

            FROM discover_search_history

            ORDER BY last_used DESC, id DESC

            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return rows


@_database_operation
def delete_discover_search(entry_id):

    with db() as con:

        cursor = con.execute(
            """
            DELETE FROM discover_search_history

            WHERE id=?
            """,
            (entry_id,),
        )

    return cursor.rowcount > 0


@_database_operation
def clear_discover_search_history():

    with db() as con:

        con.execute("DELETE FROM discover_search_history")

    return True



# ==========================
# RECENTLY VIEWED STREAMERS
# ==========================
# Timestamped log of streamer detail-panel opens so the UI can offer a
# quick-access "recently viewed" list, distinct from `discovered` (first
# tracked) or raid history. Repeat views of the same streamer each get
# their own row (simplest + cheapest); reads dedupe to one entry per
# streamer, keeping only the most recent view.

_RECENTLY_VIEWED_LOG_LIMIT = 200  # raw rows kept before trimming


@_database_operation
def add_recently_viewed(username):
    username = username.strip().lower()
    if not username:
        return False

    with db() as con:

        con.execute(
            """
            INSERT INTO recently_viewed(username, viewed_at)

            VALUES(?, ?)
            """,
            (
                username,
                datetime.now().isoformat(),
            ),
        )

        # Opportunistic trim — keeps the log bounded without needing a
        # separate cleanup job; cheap relative to the insert itself.
        con.execute(
            """
            DELETE FROM recently_viewed

            WHERE id NOT IN (
                SELECT id FROM recently_viewed
                ORDER BY viewed_at DESC, id DESC
                LIMIT ?
            )
            """,
            (_RECENTLY_VIEWED_LOG_LIMIT,),
        )

    return True


@_database_operation
def get_recently_viewed(limit=10, include_archived=False):
    """Most-recently-viewed streamers, deduped to one (latest) entry per
    username, joined back against `streamers` so removed/renamed rows
    don't surface a dangling entry."""

    archive_clause = "" if include_archived else "AND s.archived=0"

    with db() as con:

        rows = con.execute(
            f"""
            SELECT s.*, MAX(rv.viewed_at) AS viewed_at

            FROM recently_viewed rv

            JOIN streamers s
            ON s.username=rv.username

            WHERE 1=1
            {archive_clause}

            GROUP BY s.username

            ORDER BY viewed_at DESC

            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return rows



# ==========================
# STREAMER SUGGESTIONS
# ==========================
# "Similar to your favourites" — scores every non-favourited, non-archived
# streamer by how much it overlaps (same category + shared tags) with the
# person's current favourites, entirely in SQL/Python over already-loaded
# rows rather than a new Twitch call. Reuses the same tags/category
# columns search_streamers and set_tags already work with.

@_database_operation
def get_streamer_suggestions(limit=10):
    """Returns up to `limit` non-favourited, non-archived streamers most
    similar to the person's favourited roster, ranked by shared category
    + tag overlap. Streamers already favourited are always excluded.
    Returns [] if there are no favourites to base suggestions on."""

    with db() as con:

        favourite_rows = con.execute(
            """
            SELECT s.category, m.tags

            FROM streamer_metadata m

            JOIN streamers s
            ON s.username=m.username

            WHERE m.favourite=1
            AND s.archived=0
            """
        ).fetchall()

        if not favourite_rows:
            return []

        fav_categories = set()
        fav_tags = set()
        for row in favourite_rows:
            if row["category"]:
                fav_categories.add(row["category"].strip().lower())
            for tag in (row["tags"] or "").split(","):
                tag = tag.strip().lower()
                if tag:
                    fav_tags.add(tag)

        if not fav_categories and not fav_tags:
            return []

        candidate_rows = con.execute(
            """
            SELECT s.*, m.tags AS m_tags

            FROM streamers s

            LEFT JOIN streamer_metadata m
            ON s.username=m.username

            WHERE s.archived=0

            AND (m.favourite IS NULL OR m.favourite=0)
            """
        ).fetchall()

    scored = []
    for row in candidate_rows:
        category = (row["category"] or "").strip().lower()
        tags = {
            t.strip().lower()
            for t in (row["m_tags"] or "").split(",")
            if t.strip()
        }

        category_match = 1 if category and category in fav_categories else 0
        shared_tags = tags & fav_tags

        score = (category_match * 2) + len(shared_tags)
        if score <= 0:
            continue

        scored.append((score, row))

    scored.sort(key=lambda pair: (-pair[0], -(pair[1]["followers"] or 0)))

    return [row for _score, row in scored[:limit]]



# ==========================
# CATEGORY WATCHLISTS
# ==========================
# Following a category (rather than an individual streamer) surfaces new
# or trending channels in it. `seen_usernames` remembers who's already
# been surfaced for that category so a later check can report only what's
# new since last time, alongside the full current trending list.

# Mirrors main.py's DEFAULT_WATCHLIST_INTERVAL — used here only as a
# fallback for a row whose check_interval_seconds is NULL/0 (shouldn't
# normally happen given the column's own DEFAULT, but get_category_watches_due
# stays defensive rather than dividing/comparing against a falsy interval).
DEFAULT_CATEGORY_WATCH_INTERVAL = 300


@_database_operation
def add_category_watch(category, min_viewers=0):

    category = category.strip()

    if not category:

        return False


    with db() as con:

        con.execute(
            """
            INSERT INTO category_watchlists(category, min_viewers, seen_usernames, created_at, last_checked)

            VALUES (?,?,?,?,NULL)

            ON CONFLICT(category) DO UPDATE SET

                min_viewers=excluded.min_viewers

            """,
            (
                category,

                min_viewers or 0,

                "",

                datetime.now().isoformat(),
            ),
        )


    return True


@_database_operation
def remove_category_watch(category):

    with db() as con:

        cursor = con.execute(
            """
            DELETE FROM category_watchlists

            WHERE category=?

            """,
            (
                category.strip(),
            ),
        )


    return cursor.rowcount > 0


@_database_operation
def get_category_watches():

    with db() as con:

        return con.execute(
            """
            SELECT id, category, min_viewers, seen_usernames, created_at, last_checked, check_interval_seconds

            FROM category_watchlists

            ORDER BY category COLLATE NOCASE ASC

            """
        ).fetchall()


@_database_operation
def get_category_watch(category):

    with db() as con:

        return con.execute(
            """
            SELECT id, category, min_viewers, seen_usernames, created_at, last_checked, check_interval_seconds

            FROM category_watchlists

            WHERE category=?

            """,
            (
                category.strip(),
            ),
        ).fetchone()


@_database_operation
def set_category_watch_interval(category, check_interval_seconds):
    """Sets the per-watchlist cadence (in seconds) the background cron
    loop (see main.py's category_watch_cron_wrapper) uses to decide when
    this watchlist is next due for an automatic check — replaces the old
    one-size-fits-all DISCOVER_ALERT_INTERVAL-style fixed cadence with a
    value configurable per watchlist. Returns False if the category
    isn't on the watchlist."""

    with db() as con:

        cursor = con.execute(
            """
            UPDATE category_watchlists

            SET check_interval_seconds=?

            WHERE category=?
            """,
            (
                check_interval_seconds,
                category.strip(),
            ),
        )

    return cursor.rowcount > 0


@_database_operation
def get_category_watches_due():
    """Returns every category watchlist row whose own
    check_interval_seconds has elapsed since last_checked (or that has
    never been checked), for the background cron loop to pick up —
    each watchlist runs on its own configured cadence instead of a
    single fixed interval shared by all of them.

    BUGFIX: this previously compared last_checked against SQLite's
    julianday('now') directly in SQL. julianday('now') is always UTC,
    but last_checked is stored via datetime.now().isoformat() — naive
    *local* time (same convention every other timestamp column in this
    file uses, e.g. created_at just above). On any server not running
    in UTC, that mismatch is exactly the server's UTC offset: e.g. at
    UTC+2 the loop would treat rows as un-due for ~2 extra hours after
    they were actually due (or the reverse west of UTC), silently
    breaking the whole point of a configurable short interval.
    Comparing in Python against datetime.now() instead keeps the exact
    same naive-local-time convention on both sides."""

    with db() as con:

        rows = con.execute(
            """
            SELECT id, category, min_viewers, seen_usernames, created_at, last_checked, check_interval_seconds

            FROM category_watchlists

            ORDER BY category COLLATE NOCASE ASC
            """
        ).fetchall()

    now = datetime.now()
    due = []

    for row in rows:
        if not row["last_checked"]:
            due.append(row)
            continue

        try:
            last_checked = datetime.fromisoformat(row["last_checked"])
        except (TypeError, ValueError):
            # Unparseable timestamp — treat as due rather than silently
            # never checking this watchlist again.
            due.append(row)
            continue

        interval = row["check_interval_seconds"] or DEFAULT_CATEGORY_WATCH_INTERVAL
        comparison_now = datetime.now(last_checked.tzinfo) if last_checked.tzinfo else now
        if (comparison_now - last_checked).total_seconds() >= interval:
            due.append(row)

    return due


@_database_operation
def update_category_watch_seen(category, seen_usernames):
    """Overwrites the remembered set of usernames already surfaced for this
    category, and stamps last_checked. `seen_usernames` is a list; stored
    comma-joined, matching the tags column's storage convention."""

    with db() as con:

        con.execute(
            """
            UPDATE category_watchlists

            SET seen_usernames=?, last_checked=?

            WHERE category=?

            """,
            (
                ",".join(seen_usernames),

                datetime.now().isoformat(),

                category.strip(),
            ),
        )



# ==========================
# ALERT RULES
# ==========================
# kind is 'streamer_live' (target=username) or 'discover_match'
# (target=JSON-encoded discover filters). channels is a comma-separated
# subset of 'webhook', 'browser', 'email'.

ALERT_KINDS = {"streamer_live", "discover_match"}
ALERT_CHANNELS = {"webhook", "browser", "email"}


@_database_operation
def add_alert_rule(kind, target, channels, webhook_url="", email=""):

    with db() as con:

        cursor = con.execute(
            """
            INSERT INTO alert_rules
            (kind, target, channels, webhook_url, email, enabled, created_at, last_triggered, last_result_hash)

            VALUES (?,?,?,?,?,1,?,NULL,NULL)

            """,
            (
                kind,

                target,

                ",".join(channels),

                webhook_url or "",

                email or "",

                datetime.now().isoformat(),
            ),
        )

        return cursor.lastrowid


@_database_operation
def get_alert_rules(enabled_only=False):

    clause = "WHERE enabled=1" if enabled_only else ""

    with db() as con:

        return con.execute(
            f"""
            SELECT *

            FROM alert_rules

            {clause}

            ORDER BY id DESC
            """
        ).fetchall()


@_database_operation
def get_alert_rule(rule_id):

    with db() as con:

        return con.execute(
            """
            SELECT * FROM alert_rules WHERE id=?
            """,
            (
                rule_id,
            ),
        ).fetchone()


@_database_operation
def delete_alert_rule(rule_id):

    with db() as con:

        cursor = con.execute(
            """
            DELETE FROM alert_rules WHERE id=?
            """,
            (
                rule_id,
            ),
        )

    return cursor.rowcount > 0


@_database_operation
def set_alert_rule_enabled(rule_id, enabled):

    with db() as con:

        cursor = con.execute(
            """
            UPDATE alert_rules SET enabled=? WHERE id=?
            """,
            (
                1 if enabled else 0,

                rule_id,
            ),
        )

    return cursor.rowcount > 0


@_database_operation
def mark_alert_triggered(rule_id, result_hash=None):

    with db() as con:

        con.execute(
            """
            UPDATE alert_rules

            SET last_triggered=?, last_result_hash=COALESCE(?, last_result_hash)

            WHERE id=?
            """,
            (
                datetime.now().isoformat(),

                result_hash,

                rule_id,
            ),
        )


@_database_operation
def add_alert_delivery(rule_id, channel, summary, ok=True, error=None):

    with db() as con:

        con.execute(
            """
            INSERT INTO alert_deliveries (rule_id, channel, summary, ok, error, timestamp)

            VALUES (?,?,?,?,?,?)
            """,
            (
                rule_id,

                channel,

                summary,

                1 if ok else 0,

                error,

                datetime.now().isoformat(),
            ),
        )


@_database_operation
def get_alert_deliveries(rule_id=None, limit=50):

    with db() as con:

        if rule_id is not None:

            return con.execute(
                """
                SELECT * FROM alert_deliveries

                WHERE rule_id=?

                ORDER BY id DESC

                LIMIT ?
                """,
                (
                    rule_id,

                    limit,
                ),
            ).fetchall()

        return con.execute(
            """
            SELECT * FROM alert_deliveries

            ORDER BY id DESC

            LIMIT ?
            """,
            (
                limit,
            ),
        ).fetchall()



# ==========================
# STREAMER RESPONSE TRACKING
# ==========================
# Logs whether a scouted streamer has acknowledged being scouted (followed
# back, replied to a message, reacted, etc). Multiple entries per streamer
# are expected over time — this is a log, not a single status field.

RESPONSE_TYPES = {
    "followed_back",
    "replied",
    "reacted",
    "declined",
    "no_response",
}


@_database_operation
def add_streamer_response(username, response_type, note=""):

    username = username.lower()

    with db() as con:

        cursor = con.execute(
            """
            INSERT INTO streamer_responses (username, response_type, note, timestamp)

            VALUES (?,?,?,?)
            """,
            (
                username,

                response_type,

                note or "",

                datetime.now().isoformat(),
            ),
        )

        return cursor.lastrowid


@_database_operation
def get_streamer_responses(username, limit=25):

    username = username.lower()

    with db() as con:

        return con.execute(
            """
            SELECT * FROM streamer_responses

            WHERE username=?

            ORDER BY id DESC

            LIMIT ?
            """,
            (
                username,

                limit,
            ),
        ).fetchall()


@_database_operation
def get_latest_response(username):

    username = username.lower()

    with db() as con:

        return con.execute(
            """
            SELECT * FROM streamer_responses

            WHERE username=?

            ORDER BY id DESC

            LIMIT 1
            """,
            (
                username,
            ),
        ).fetchone()


@_database_operation
def delete_streamer_response(response_id, username=None):

    with db() as con:

        cursor = con.execute(
            """
            DELETE FROM streamer_responses WHERE id=? AND (? IS NULL OR username=?)
            """,
            (
                response_id, username.lower() if username else None, username.lower() if username else None,
            ),
        )

    return cursor.rowcount > 0

@_database_operation
def get_metadata_bulk(usernames):
    names = list(dict.fromkeys(u.lower() for u in usernames if u))
    lookup = {}
    with db() as con:
        for offset in range(0, len(names), 400):
            batch = names[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = con.execute(f"""SELECT username, favourite, tags, priority, alias,
                notify_enabled, x_url, discord_url, youtube_url, kick_url,
                outreach_status, bio, scraped_social_links, social_scraped_at,
                scraped_location, scraped_timezone, location, timezone, scraped_age
                FROM streamer_metadata WHERE username IN ({placeholders})""", batch).fetchall()
            lookup.update((row[0], _metadata_to_dict(tuple(row)[1:])) for row in rows)
    return lookup


@_database_operation
def get_average_viewers_bulk(usernames):
    names = list(dict.fromkeys(u.lower() for u in usernames if u))
    lookup = {}
    with db() as con:
        for offset in range(0, len(names), 400):
            batch = names[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = con.execute(f"""SELECT username, AVG(viewers) FROM viewer_history
                WHERE username IN ({placeholders}) AND datetime(replace(timestamp,'T',' '))
                >= datetime('now','-30 days') GROUP BY username""", batch).fetchall()
            lookup.update((row[0], round(row[1] or 0)) for row in rows)
    return lookup


@_database_operation
def get_tracking_snapshot():
    return get_all(), database_generation


@_database_operation
def get_total_count():
    return db().execute("SELECT COUNT(*) FROM streamers").fetchone()[0]


# Twitch IDs anchor identity; existing username keys stay stable for all local data.
def _validated_identity(twitch_id, login):
    if not isinstance(twitch_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", twitch_id):
        raise ValueError("Twitch ID must be a positive numeric string (up to 20 digits).")
    login = str(login).strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
        raise ValueError("Invalid Twitch username.")
    return twitch_id, login


@_database_operation
def get_twitch_identity_snapshot(usernames):
    result = {}
    names = list(dict.fromkeys(name.lower() for name in usernames))
    con = db()
    for offset in range(0, len(names), 500):
        batch = names[offset:offset + 500]
        rows = con.execute(f"SELECT username,twitch_id,current_username,identity_checked_at FROM streamers WHERE username IN ({','.join('?' for _ in batch)})", batch)
        result.update({row['username']: dict(row) for row in rows})
    return result, database_generation


def _observe_identity(con, username, twitch_id, login, now):
    row = con.execute("SELECT twitch_id,current_username FROM streamers WHERE username=?", (username,)).fetchone()
    if row is None:
        return True  # An untracked lookup has no persistent record yet.
    if row['twitch_id'] and row['twitch_id'] != twitch_id:
        return False  # Never silently rebind a record to a reused username.
    if row['twitch_id'] == twitch_id and row['current_username'] == login:
        # Avoid rewriting the FTS index and URL for unchanged identities.
        con.execute('UPDATE streamers SET identity_checked_at=? WHERE username=?', (now, username))
        return True
    owner = con.execute("SELECT username FROM streamers WHERE twitch_id=? AND username!=?", (twitch_id, username)).fetchone()
    if owner:
        return False
    previous = row['current_username'] or username
    if previous != login:
        source = 'observed' if row['twitch_id'] else 'manual'
        if source == 'manual':
            con.execute("INSERT OR IGNORE INTO streamer_username_history(username,previous_username,source,recorded_at) VALUES(?,?,'manual',?)", (username, previous, now))
        else:
            con.execute("INSERT INTO streamer_username_history(username,previous_username,new_username,source,observed_at,recorded_at) VALUES(?,?,?,'observed',?,?)", (username, previous, login, now, now))
    con.execute("UPDATE streamers SET twitch_id=?,current_username=?,identity_checked_at=?,url=? WHERE username=?", (twitch_id, login, now, 'https://twitch.tv/' + login, username))
    return True


@_database_operation
def observe_twitch_identities(users, expected_identities=None):
    """Apply a whole lookup batch atomically, avoiding one commit per streamer."""
    accepted = set()
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        for username, user in users.items():
            if expected_identities is not None:
                current = con.execute('SELECT username,twitch_id,current_username,identity_checked_at FROM streamers WHERE username=?', (username.lower(),)).fetchone()
                if (dict(current) if current else None) != expected_identities.get(username.lower()):
                    continue
            twitch_id, login = _validated_identity(user['user_id'], user['username'])
            if _observe_identity(con, username.lower(), twitch_id, login, now):
                accepted.add(username.lower())
    _invalidate_all_cache()
    return accepted


@_database_operation
def save_twitch_identity(username, twitch_id, login):
    twitch_id, login = _validated_identity(twitch_id, login)
    if not streamer_exists(username):
        raise ValueError("Streamer is no longer tracked.")
    accepted = observe_twitch_identities({username: {'user_id':twitch_id, 'username':login}})
    if username.lower() not in accepted:
        raise ValueError("That Twitch ID belongs to another tracked record, or differs from this record's saved ID. IDs do not change when usernames change.")
    return get_username_identity(username)


@_database_operation
def get_username_identity(username):
    row = get_streamer(username)
    if row is None:
        return None
    history = db().execute("SELECT id,previous_username,new_username,source,observed_at,recorded_at FROM streamer_username_history WHERE username=? ORDER BY id DESC LIMIT 500", (username.lower(),)).fetchall()
    return {'twitch_id':row['twitch_id'], 'current_username':row['current_username'] or row['username'],
            'checked_at':row['identity_checked_at'], 'history':[dict(entry) for entry in history]}


@_database_operation
def add_previous_username(username, previous_username):
    if not streamer_exists(username):
        raise ValueError("Streamer is no longer tracked.")
    name = str(previous_username).strip().lower().lstrip('@')
    if not re.fullmatch(r"[a-z0-9_]{1,25}", name):
        raise ValueError("Previous username must contain 1–25 letters, digits, or underscores.")
    row = get_streamer(username)
    if name == (row['current_username'] or row['username']):
        raise ValueError("That is the current username, not a previous one.")
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.execute("INSERT OR IGNORE INTO streamer_username_history(username,previous_username,source,recorded_at) VALUES(?,?,'manual',?)", (username.lower(), name, now))
    return get_username_identity(username)


@_database_operation
def remove_previous_username(username, history_id):
    with db() as con:
        return bool(con.execute("DELETE FROM streamer_username_history WHERE username=? AND id=? AND source='manual'", (username.lower(), history_id)).rowcount)


@_database_operation
def find_twitch_identity_owner(twitch_id):
    row = db().execute("SELECT username FROM streamers WHERE twitch_id=?", (twitch_id,)).fetchone()
    return row['username'] if row else None


@_database_operation
def get_activity_snapshot(username):
    identity = get_username_identity(username)
    if identity is None:
        return None
    row = db().execute('SELECT * FROM streamer_activity WHERE username=?', (username.lower(),)).fetchone()
    return {'current_username': identity['current_username'], 'snapshot':
            {**json.loads(row['payload']), 'provider_username': row['provider_username'],
             'source_type': row['source_type'], 'retrieved_at': row['retrieved_at']} if row else None}


@_database_operation
def save_activity_snapshot(username, provider_username, payload, source_type, expected_retrieved_at=None):
    identity = get_username_identity(username)
    if identity is None or identity['current_username'] != provider_username:
        raise StaleDatabaseOperationError('The channel changed during this lookup. Retry for the current username.')
    existing = db().execute('SELECT retrieved_at FROM streamer_activity WHERE username=?', (username.lower(),)).fetchone()
    if (existing['retrieved_at'] if existing else None) != expected_retrieved_at:
        raise StaleDatabaseOperationError('Newer activity was saved while this request was running. Reopen the details before retrying.')
    with db() as con:
        con.execute('''INSERT INTO streamer_activity(username,provider_username,source_type,retrieved_at,payload)
            VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET
            provider_username=excluded.provider_username,source_type=excluded.source_type,
            retrieved_at=excluded.retrieved_at,payload=excluded.payload''',
            (username.lower(), provider_username, source_type, datetime.now(timezone.utc).isoformat(), json.dumps(payload)))
    return get_activity_snapshot(username)


@_database_operation
def get_tracked_identity_names(usernames):
    """Map current public names to immutable local record keys for discovery actions."""
    names = list(dict.fromkeys(u.lower() for u in usernames))
    result = {}
    con = db()
    for offset in range(0,len(names),400):
        batch = names[offset:offset+400]
        marks = ','.join('?' for _ in batch)
        rows = con.execute(f"SELECT username,current_username FROM streamers WHERE COALESCE(current_username,username) IN ({marks})",batch)
        result.update({row['current_username'] or row['username']:row['username'] for row in rows})
    return result
