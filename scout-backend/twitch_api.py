import asyncio
import re
import time
import uuid
from datetime import date, datetime, timezone

import aiohttp

from discover_cache import shared_search, invalidate_all as invalidate_discover_cache

import database as db
from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
from logger import logger

CLIENT_ID = TWITCH_CLIENT_ID
CLIENT_SECRET = TWITCH_CLIENT_SECRET


class TwitchUnavailableError(Exception):
    """Raised when a Twitch API call could not complete (auth failure or
    rate limit exhausted its retries) so the route layer can return a
    proper error status instead of a silently empty result set."""
    pass


class TwitchRateLimitedError(TwitchUnavailableError):
    """Raised when Twitch returned 429 on every retry attempt. Carries the
    seconds the caller should wait before trying again, so main.py can put
    that in a Retry-After header/response body for the frontend instead of
    the backend just retrying silently and leaving the UI guessing."""
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Twitch rate limit hit, retry after {retry_after}s")


def set_credentials(client_id, client_secret):
    """Called from config.save_twitch_credentials() after the person enters
    credentials in the app, so a fresh token gets requested with the new
    values on the very next call instead of waiting for a restart."""
    global CLIENT_ID, CLIENT_SECRET, access_token, token_expiry
    CLIENT_ID = client_id
    CLIENT_SECRET = client_secret
    access_token = None
    token_expiry = 0
    follower_cache.clear()
    _search_pages.clear()
    invalidate_discover_cache()

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_BATCH_SIZE = 100

USER_CACHE_TIME = 86400       # 24h
FOLLOWER_CACHE_TIME = 21600   # 6h
CATEGORY_CACHE_TIME = 21600   # 6h


# ==========================
# GLOBAL STATE
# ==========================

access_token = None
token_expiry = 0

session = None

token_lock = asyncio.Lock()
session_lock = asyncio.Lock()

user_cache = {}       # username -> {"data": {...}, "timestamp": ...}
follower_cache = {}   # user_id -> {"value": int, "timestamp": ...}
category_cache = {}   # category name (lower) -> {"value": game_id, "timestamp": ...}

# Structured rate-limit telemetry — Twitch's Helix API returns
# Ratelimit-Limit/Ratelimit-Remaining/Ratelimit-Reset on every response
# (200 or 429 alike). Previously that info was only ever looked at
# reactively, on a 429, to compute a retry wait. Tracking it on *every*
# call instead lets /api/settings/twitch surface the current quota so
# the frontend can warn before hitting 429s, not just after. Module-level
# (not per-request) since it reflects Twitch's one shared per-app quota,
# same scope as access_token above; last-write-wins under concurrent
# requests is fine here since it's a point-in-time snapshot, not a total.
rate_limit_state = {
    "limit": None,       # int or None if no call has completed yet
    "remaining": None,   # int
    "reset_at": None,    # unix timestamp the window resets, or None
    "updated_at": None,  # unix timestamp of the last observed header set
    "last_status": None, # HTTP status of the call that produced this snapshot
}


def _record_rate_limit_headers(headers, status):
    """Best-effort parse of Twitch's rate-limit headers off any Helix
    response. Missing/unparseable headers leave the previous snapshot in
    place rather than clobbering it with Nones — a single malformed
    response shouldn't erase telemetry from calls around it."""
    limit = headers.get("Ratelimit-Limit")
    remaining = headers.get("Ratelimit-Remaining")
    reset = headers.get("Ratelimit-Reset")

    if limit is None and remaining is None and reset is None:
        return

    try:
        if limit is not None:
            rate_limit_state["limit"] = int(limit)
        if remaining is not None:
            rate_limit_state["remaining"] = int(remaining)
        if reset is not None:
            rate_limit_state["reset_at"] = int(reset)
        rate_limit_state["updated_at"] = time.time()
        rate_limit_state["last_status"] = status
    except (TypeError, ValueError):
        logger.warning(f"⚠️ Could not parse Twitch rate-limit headers: {headers}")


def get_rate_limit_snapshot():
    """Read-only snapshot for /api/settings/twitch. Adds a `low` flag
    (remaining below 10% of limit, or below 20 as an absolute floor when
    the limit itself is unknown) so the frontend can show a warning
    without duplicating the threshold logic itself."""
    limit = rate_limit_state["limit"]
    remaining = rate_limit_state["remaining"]

    low = False
    if remaining is not None:
        if limit:
            low = remaining <= max(1, limit * 0.1)
        else:
            low = remaining <= 20

    return {
        "limit": limit,
        "remaining": remaining,
        "reset_at": rate_limit_state["reset_at"],
        "updated_at": rate_limit_state["updated_at"],
        "last_status": rate_limit_state["last_status"],
        "low": low,
    }


# ==========================
# SESSION
# ==========================

async def get_session():
    global session

    async with session_lock:
        if session is None or session.closed:
            session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

    return session


async def close_session():
    global session

    async with session_lock:
        if session:
            await session.close()
            session = None


# ==========================
# TOKEN
# ==========================

async def get_access_token():
    global access_token, token_expiry

    if not CLIENT_ID or not CLIENT_SECRET:
        return None
    if access_token and time.time() < token_expiry:
        return access_token

    async with token_lock:
        if access_token and time.time() < token_expiry:
            return access_token

        s = await get_session()

        credentials = (CLIENT_ID, CLIENT_SECRET)
        async with s.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        ) as response:
            data = await response.json()

            if "access_token" not in data:
                logger.error(f"Twitch authentication failed: {data}")
                return None

            if credentials != (CLIENT_ID, CLIENT_SECRET):
                return None
            access_token = data["access_token"]
            token_expiry = time.time() + data.get("expires_in", 0) - 300

            logger.info("✅ Twitch access token refreshed")
            return access_token


# ==========================
# API REQUEST
# ==========================

def _rate_limit_wait(headers):
    now = time.time()
    try:
        reset = int(headers.get("Ratelimit-Reset", now + 5))
    except (TypeError, ValueError, OverflowError):
        return 5
    return max(reset - int(now), 5)


async def twitch_request(endpoint, params, refresh_on_unauthorized=True):
    """Retry authentication/rate limits within a three-request budget."""
    global access_token
    token = await get_access_token()
    if not token:
        return None
    headers = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"}
    s = await get_session()
    rate_limit_retries = 0

    for attempt in range(3):
        async with s.get(
            f"https://api.twitch.tv/helix/{endpoint}", headers=headers, params=params,
        ) as response:
            status = response.status
            if status == 401:
                if not refresh_on_unauthorized:
                    return None
            else:
                _record_rate_limit_headers(response.headers, status)
                if status == 429:
                    wait = _rate_limit_wait(response.headers)
                elif status == 200:
                    return await response.json()
                else:
                    logger.error(f"Twitch API error {status}: {endpoint}")
                    return None

        # Release the response/connection before token refresh or backoff.
        if status == 401:
            logger.warning("Twitch token expired")
            # Another in-flight request may already have refreshed this token.
            if access_token == token:
                access_token = None
            token = await get_access_token()
            if not token:
                return None
            headers.update({"Client-ID": CLIENT_ID, "Authorization": f"Bearer {token}"})
            continue

        if rate_limit_retries >= 1 or attempt == 2:
            logger.warning("Rate limited by Twitch, retry-after %ss", wait)
            raise TwitchRateLimitedError(wait)
        short_wait = min(wait, 3)
        logger.warning("Rate limited. Waiting %ss (retry 1/1)", short_wait)
        await asyncio.sleep(short_wait)
        rate_limit_retries += 1
    return None


# ==========================
# CACHE HELPERS
# ==========================

_CACHE_MAX_ENTRIES = 50000
_CACHE_SWEEP_WRITES = 128
# The four module-owned dictionaries have stable identities for the process.
_cache_write_counts = {}


def _store_cache(cache, key, value, ttl):
    """Amortize expiration scans over writes; never evict for an update."""
    cache_id = id(cache)
    writes = _cache_write_counts.get(cache_id, 0) + 1
    _cache_write_counts[cache_id] = writes % _CACHE_SWEEP_WRITES
    if writes >= _CACHE_SWEEP_WRITES:
        now = time.time()
        for old in list(cache):
            entry = cache.get(old)
            if entry and now - entry["timestamp"] >= ttl:
                cache.pop(old, None)
    if key not in cache:
        while len(cache) >= _CACHE_MAX_ENTRIES:
            cache.pop(next(iter(cache)))
    cache[key] = value


def cache_valid(entry, expiry):
    return time.time() - entry["timestamp"] < expiry


# ==========================
# SINGLE USER LOOKUP
# ==========================

async def get_user_id(username):
    username = username.lower()

    cached = user_cache.get(username)
    if cached and cache_valid(cached, USER_CACHE_TIME):
        return cached["data"]

    response = await twitch_request("users", {"login": username})

    if not response or not response.get("data"):
        logger.warning(f"User not found: {username}")
        return None

    user = response["data"][0]

    data = {
        "user_id": user["id"],
        "username": user["login"],
        "display_name": user["display_name"],
        "profile_image": user.get("profile_image_url", ""),
    }

    _store_cache(user_cache, username, {"data": data, "timestamp": time.time()}, USER_CACHE_TIME)
    return data


# ==========================
# VODS / CLIPS
# ==========================

async def get_channel_videos(user_id, first=30):
    """Return recent archived videos, highlights and uploads for a channel.
    Uses the same authenticated Helix path as the rest of ScoutBot.
    """
    response = await twitch_request("videos", {"user_id": user_id, "first": max(1, min(int(first), 100))})
    return (response or {}).get("data", [])

async def get_channel_clips(user_id, first=30):
    """Return recent clips. Twitch limits the clip API to a date window, so
    keep the request bounded to the last 30 days.
    """
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    response = await twitch_request("clips", {
        "broadcaster_id": user_id,
        "first": max(1, min(int(first), 100)),
        "started_at": start.isoformat().replace("+00:00", "Z"),
        "ended_at": end.isoformat().replace("+00:00", "Z"),
    })
    return (response or {}).get("data", [])


# ==========================
# FOLLOWERS
# ==========================

async def get_followers(user_id):
    """Returns a channel's follower count, or None if it couldn't be
    determined. `channels/followers` requires a user token belonging to
    the broadcaster or one of their moderators (Twitch tightened this in
    2023) — an app-only token, which is all this app ever has, gets a 401
    for any channel that isn't the app's own. That's expected here, not a
    transient failure, so it's logged once per process (not per call) and
    treated the same as any other lookup failure: unknown, not zero.
    Callers must not treat None as 0 — a real 0-follower channel and an
    unreadable one are different things, and collapsing them together
    previously made any min_followers filter above 0 reject everyone."""
    cached = follower_cache.get(user_id)
    if cached and cache_valid(cached, FOLLOWER_CACHE_TIME):
        return cached["value"]

    response = await shared_search(
        lambda: twitch_request("channels/followers", {"broadcaster_id": user_id}, refresh_on_unauthorized=False),
        cache=False, source="twitch-follower-lookup", user_id=user_id, client_id=CLIENT_ID,
    )

    if response is None:
        global _follower_lookup_warned
        if not _follower_lookup_warned:
            logger.warning(
                "⚠️ Could not fetch follower counts (channels/followers needs a "
                "broadcaster/moderator user token, which this app doesn't have) "
                "— follower counts will show as unavailable and won't be used "
                "to filter or exclude results."
            )
            _follower_lookup_warned = True
        _store_cache(follower_cache, user_id, {"value": None, "timestamp": time.time()}, FOLLOWER_CACHE_TIME)
        return None

    followers = response.get("total", 0)
    _store_cache(follower_cache, user_id, {"value": followers, "timestamp": time.time()}, FOLLOWER_CACHE_TIME)
    return followers


_follower_lookup_warned = False
_unhandled_panel_types_warned = set()  # panel __typename values already logged, so each is only warned once per process


# ==========================
# BULK USERS
# ==========================

async def get_bulk_users(usernames):
    results = {}
    missing = []

    for username in usernames:
        username = username.lower()

        cached = user_cache.get(username)
        if cached and cache_valid(cached, USER_CACHE_TIME):
            results[username] = cached["data"]
        else:
            missing.append(username)

    for i in range(0, len(missing), MAX_BATCH_SIZE):
        batch = missing[i:i + MAX_BATCH_SIZE]
        params = [("login", username) for username in batch]

        response = await twitch_request("users", params)
        if not response:
            continue

        for user in response.get("data", []):
            data = {
                "user_id": user["id"],
                "username": user["login"],
                "display_name": user["display_name"],
                "profile_image": user.get("profile_image_url", ""),
            }

            key = user["login"].lower()
            _store_cache(user_cache, key, {"data": data, "timestamp": time.time()}, USER_CACHE_TIME)
            results[key] = data

    return results


# ==========================
# BULK STREAMER DATA
# ==========================

async def get_bulk_streamer_data(usernames):
    users = await get_bulk_users(usernames)
    if not users:
        return {}

    results = {}
    user_list = list(users.values())

    for i in range(0, len(user_list), MAX_BATCH_SIZE):
        batch = user_list[i:i + MAX_BATCH_SIZE]
        ids = [user["user_id"] for user in batch]

        channels, streams = await asyncio.gather(
            twitch_request("channels", [("broadcaster_id", uid) for uid in ids]),
            twitch_request("streams", [("user_id", uid) for uid in ids]),
        )

        if channels is None or streams is None:
            continue  # An unavailable stream lookup must never mark a channel offline.
        categories = {}
        viewers = {}

        if channels:
            for channel in channels.get("data", []):
                categories[channel["broadcaster_id"]] = channel.get("game_name", "Unknown")

        if streams:
            for stream in streams.get("data", []):
                viewers[stream["user_id"]] = stream.get("viewer_count", 0)

        counts = await asyncio.gather(*(get_followers(user["user_id"]) for user in batch))
        followers = dict(zip(ids, counts))

        for user in batch:
            count = viewers.get(user["user_id"], 0)

            results[user["username"].lower()] = {
                "username": user["username"],
                "display_name": user["display_name"],
                "profile_image": user["profile_image"],
                "user_id": user["user_id"],
                "followers": followers.get(user["user_id"], 0),
                "category": categories.get(user["user_id"], "Unknown"),
                "average_viewers": 0,
                "average": 0,  # kept for backwards compatibility
                "live_viewers": count,
                "live_status": "Live" if user["user_id"] in viewers else "Offline",
            }

    return results


# ==========================
# SINGLE STREAMER COMPATIBILITY
# ==========================

async def get_streamer_data(username):
    data = await get_bulk_streamer_data([username])
    return data.get(username.lower())


# ==========================
# CATEGORY LOOKUP (for discovery search)
# ==========================

async def get_category_id(name):
    """Resolves a Twitch category/game name to its game_id. Tries an exact
    match first, then falls back to fuzzy category search.

    Unlike the streams paging in search_streams (which already tolerates a
    single bad page via retries/headroom), this used to give up on the very
    first failed response from either lookup — so one transient non-200 from
    Twitch (a momentary 5xx, or a request that raced a concurrent token
    refresh from another in-flight call, e.g. the discover_match alert loop
    polling in the background) permanently killed the whole category-based
    Discover search with silent zero results and nothing logged as an error,
    even though a plain retry moments later would have succeeded. Both
    lookups now get one bounded retry on a failed (None) response before
    being treated as a genuine miss."""

    key = name.lower()
    cached = category_cache.get(key)
    if cached and cache_valid(cached, CATEGORY_CACHE_TIME):
        return cached["value"]

    async def _lookup(endpoint, params):
        for attempt in range(2):
            response = await twitch_request(endpoint, params)
            if response is not None:
                return response
            if attempt == 0:
                logger.warning(f"⚠️ Category lookup '{endpoint}' failed for '{name}', retrying once")
        return None

    response = await _lookup("games", {"name": name})

    game_id = None
    if response and response.get("data"):
        game_id = response["data"][0]["id"]
    else:
        response = await _lookup("search/categories", {"query": name, "first": 1})
        if response and response.get("data"):
            game_id = response["data"][0]["id"]

    if game_id:
        _store_cache(category_cache, key, {"value": game_id, "timestamp": time.time()}, CATEGORY_CACHE_TIME)

    return game_id


# ==========================
# USERS BY ID (needed for broadcaster_type, not returned by /streams)
# ==========================

async def get_users_by_id(user_ids):
    results = {}

    for i in range(0, len(user_ids), MAX_BATCH_SIZE):
        batch = user_ids[i:i + MAX_BATCH_SIZE]
        params = [("id", uid) for uid in batch]

        response = await shared_search(
            lambda: twitch_request("users", params), source="twitch-search-users",
            params=params, client_id=CLIENT_ID,
        )
        if not response:
            continue

        for user in response.get("data", []):
            results[user["id"]] = user

    return results


# ==========================
# STREAMER DISCOVERY SEARCH
# ==========================
# Finds LIVE Twitch streamers that aren't necessarily already tracked,
# filtered by category, viewer count range, and broadcaster type
# (affiliate / partner / standard). Used by /scout discover.

DISCOVER_MAX_PAGES = 10  # safety cap: 10 pages * 100 = 1000 streams scanned max
DISCOVER_MAX_PAGES_LOW_VIEWER = 40  # 40 pages * 100 = 4000 streams scanned max


def _parse_created_bound(value):
    """Parses an account-creation-date filter bound or a user's own
    `created_at` into a comparable datetime. Accepts a bare date
    ('2020-01-01'), a full ISO 8601 timestamp (as Twitch returns for
    `created_at`, e.g. '2016-03-14T18:32:24Z'), or an already-parsed
    datetime/date. Returns None (never raises) for anything empty or
    unparseable, so a bad/missing value just disables that side of the
    filter instead of erroring the whole search out."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


_search_pages = {}
_SEARCH_PAGE_TTL = 300

async def _search_page(entries, upstream, limit, followers_loaded):
    results = entries[:limit]
    if not followers_loaded:
        counts = await asyncio.gather(*(get_followers(r["user_id"]) for r in results))
        for r, count in zip(results, counts):
            r["followers"] = count or 0
    for r in results:
        if r.get("followers") is None:
            r["followers"] = 0
    if len(entries) > limit:
        now = time.time()
        for key in list(_search_pages):
            saved = _search_pages.get(key)
            if saved and now - saved[0] >= _SEARCH_PAGE_TTL:
                _search_pages.pop(key, None)
        while len(_search_pages) >= 100:
            oldest = next(iter(_search_pages), None)
            if oldest is None: break
            _search_pages.pop(oldest, None)
        cursor = "scout:" + uuid.uuid4().hex
        _search_pages[cursor] = (now, entries[limit:], upstream, followers_loaded)
        return results, cursor
    return results, upstream

async def search_streams(
    category=None,
    min_viewers=None,
    max_viewers=None,
    broadcaster_type=None,
    language=None,
    tags=None,
    exclude_tags=None,
    min_followers=None,
    max_followers=None,
    created_after=None,
    created_before=None,
    limit=25,
    cursor=None,
    blacklist=None,
):
    """`language` is a Twitch language code (e.g. 'en', 'es') and is pushed
    down to the Get Streams request itself. `tags` are Twitch's own stream
    tags (the labels shown under a live stream, e.g. 'chatty', 'chill') —
    these aren't filterable server-side, so results are matched locally
    against each stream's `tags` array (case-insensitive, ANY match).
    `exclude_tags` works the same way but drops a stream if it has ANY of
    the given tags (e.g. exclude 'vtuber' to filter out vtuber streams).

    `blacklist`, if given, is an iterable of usernames (case-insensitive)
    to hard-exclude from results — a separate, stronger filter than
    priority="Ignore" (which still lets a streamer appear in Discover).
    Applied at the same point as the tag filters, before candidates are
    counted toward `limit`/headroom, so a blacklisted streamer never
    displaces a real result and never reappears in Discover.

    `min_followers`/`max_followers` filter by channel follower count.
    Twitch's Get Streams endpoint has no follower filter, so — like
    tags — this is applied locally, after follower counts are fetched
    for the trimmed candidate set below (fetching followers for every
    raw stream on the page would be far more Twitch calls than results
    actually shown).

    `created_after`/`created_before` filter by the channel's Twitch
    account creation date (ISO 8601 date/datetime strings, e.g.
    '2020-01-01'). Also applied locally against each user's `created_at`
    from Get Users, alongside the broadcaster_type filter — no extra
    Twitch calls beyond the user fetch already being done.

    `cursor`, when passed, resumes paging from where a previous call left
    off (its returned `next_cursor`), so Discover isn't capped at a single
    ~100-stream page. Returns (results, next_cursor) — next_cursor is None
    once Twitch has no more pages or the viewer floor has been hit."""

    if cursor and cursor.startswith("scout:"):
        saved = _search_pages.get(cursor)
        if not saved or time.time() - saved[0] >= _SEARCH_PAGE_TTL:
            raise TwitchUnavailableError("Discover page expired; run the search again.")
        denied = {u.lower() for u in (blacklist or [])}
        entries = [dict(entry) for entry in saved[1] if entry["username"].lower() not in denied]
        return await _search_page(entries, saved[2], limit, saved[3])

    params_base = {"first": 100, "type": "live"}

    if category:
        game_id = await get_category_id(category)
        if not game_id:
            logger.warning(f"⚠️ No Twitch category found matching '{category}'")
            return [], None
        params_base["game_id"] = game_id

    if language:
        params_base["language"] = language.lower()

    def _normalize_tag_set(value):
        if not value:
            return None
        if isinstance(value, str):
            value = [value]
        normalized = {t.strip().lower() for t in value if t.strip()}
        return normalized or None

    normalized_tags = _normalize_tag_set(tags)
    normalized_exclude_tags = _normalize_tag_set(exclude_tags)
    normalized_blacklist = _normalize_tag_set(blacklist)

    has_follower_filter = min_followers is not None or max_followers is not None

    created_after_dt = _parse_created_bound(created_after)
    created_before_dt = _parse_created_bound(created_before)
    has_created_filter = created_after_dt is not None or created_before_dt is not None

    normalized_type = broadcaster_type.lower() if broadcaster_type else None
    if normalized_type == "any":
        normalized_type = None

    # Twitch returns streams sorted by viewer_count descending, so once we
    # drop below min_viewers we can stop paging entirely.
    candidates = []
    seen_user_ids = set()  # guards against the same streamer appearing on
    # two different pages of this same search (Twitch's live list can
    # reorder mid-paginate as viewer counts change, so a streamer can
    # shift across the page boundary and get fetched twice) — without
    # this, a deep-paging search (follower filter, tight max_viewers,
    # etc.) can return the same streamer more than once, since the
    # frontend's own dedup (mergeDiscoverItems) only guards across
    # separate "Load more" calls, not within a single response.
    pages_fetched = 0
    next_cursor = cursor
    exhausted = False

    # Gather a few extra candidates beyond `limit` since some will get
    # filtered out below by broadcaster_type (and, if set, tags/followers/
    # account age — narrower filters, so pull in more headroom when any
    # of them is requested).
    target_candidates = max(limit * 4, limit + 25)
    if (normalized_tags or normalized_exclude_tags or normalized_blacklist
            or has_follower_filter or has_created_filter or normalized_type):
        # broadcaster_type narrows the pool same as the others above — a
        # popular category filtered down to just "standard" (non-affiliate,
        # non-partner) streamers can have very few matches near the top of
        # the viewer-sorted list, so without extra headroom here the search
        # exhausts its page budget before finding any and returns nothing
        # even though matches exist further down.
        target_candidates = max(target_candidates, limit * 8, limit + 75)
    if has_follower_filter:
        # Follower count is filtered *after* candidates are gathered (it
        # needs a per-channel lookup, done once candidates are trimmed to
        # a manageable set — see below), so the gathering loop below has
        # no visibility into how many of its `target_candidates` will
        # actually survive that filter. A tight range like Min/Max
        # followers 0-100 can reject nearly all of even a few hundred
        # candidates, since most live streamers well above the very top
        # of a category have already cleared 100 followers. Without
        # extra headroom here, the loop stops as soon as it has "enough"
        # candidates by viewer count alone, then the follower filter
        # collapses that set down to zero or near-zero — even though
        # `max_pages` would have allowed paging much deeper to find
        # more. Give follower-filtered searches the deepest headroom of
        # any filter combo.
        target_candidates = max(target_candidates, limit * 16, limit + 150)

    # Twitch's Get Streams results are sorted by viewer_count descending,
    # so a max_viewers filter (with no min_viewers floor to stop early via
    # hit_floor) has to page *past* every higher-viewer stream before it
    # can reach ones that actually match — in a large category that can
    # be well beyond the normal page budget, even though matches exist.
    # Give this combo a much deeper page budget rather than giving up
    # early and reporting no matches when there may be plenty further in.
    max_pages = DISCOVER_MAX_PAGES
    if (max_viewers is not None and min_viewers is None) or has_follower_filter:
        # A follower filter also needs the deeper budget: it's applied
        # after gathering (see target_candidates above), so reaching
        # enough surviving candidates can require paging well past the
        # normal 1,000-stream budget, the same way a low max_viewers
        # with no min_viewers floor does.
        max_pages = DISCOVER_MAX_PAGES_LOW_VIEWER

    while len(candidates) < target_candidates and pages_fetched < max_pages:
        params = dict(params_base)
        if next_cursor:
            params["after"] = next_cursor

        # Viewer/tag/follower filters are local; their upstream pages overlap.
        # Share raw pages across those searches while keeping matching/paging intact.
        response = await shared_search(
            lambda: twitch_request("streams", params), source="twitch-stream-page",
            params=params, client_id=CLIENT_ID,
        )
        pages_fetched += 1

        if not response or not response.get("data"):
            exhausted = True
            break

        hit_floor = False

        for stream in response["data"]:
            viewers = stream.get("viewer_count", 0)

            if min_viewers is not None and viewers < min_viewers:
                hit_floor = True
                break

            if max_viewers is not None and viewers > max_viewers:
                continue

            if normalized_blacklist:
                login = str(stream.get("user_login", "")).strip().lower()
                if login in normalized_blacklist:
                    continue

            if normalized_tags or normalized_exclude_tags:
                stream_tags = {
                    str(t).strip().lower() for t in (stream.get("tags") or [])
                }
                if normalized_tags and not (normalized_tags & stream_tags):
                    continue
                if normalized_exclude_tags and (normalized_exclude_tags & stream_tags):
                    continue

            uid = stream.get("user_id")
            if uid in seen_user_ids:
                continue
            seen_user_ids.add(uid)

            candidates.append(stream)

        next_cursor = response.get("pagination", {}).get("cursor")

        if hit_floor or not next_cursor:
            exhausted = True
            break

    if not candidates:
        return [], (None if exhausted else next_cursor)

    # Fetch full user objects for broadcaster_type + profile images + account
    # creation date.
    user_ids = [s["user_id"] for s in candidates]
    users = await get_users_by_id(user_ids)

    entries = []

    for stream in candidates:
        user = users.get(stream["user_id"])
        if not user:
            continue

        b_type = user.get("broadcaster_type", "") or "standard"

        if normalized_type and normalized_type != "any" and b_type != normalized_type:
            continue

        created_at = user.get("created_at") or ""
        if has_created_filter:
            created_dt = _parse_created_bound(created_at)
            if created_dt is None:
                continue
            if created_after_dt is not None and created_dt < created_after_dt:
                continue
            if created_before_dt is not None and created_dt > created_before_dt:
                continue

        entry = {
            "username": stream["user_login"],
            "display_name": stream["user_name"],
            "profile_image": user.get("profile_image_url", ""),
            "user_id": stream["user_id"],
            "category": stream.get("game_name", "Unknown"),
            "live_viewers": stream.get("viewer_count", 0),
            "live_status": "Live",
            "broadcaster_type": b_type,
            "title": stream.get("title", ""),
            "started_at": stream.get("started_at"),
            "language": stream.get("language", ""),
            "tags": stream.get("tags", []) or [],
            "account_created_at": created_at,
        }

        entries.append(entry)

    if not entries:
        return [], (None if exhausted else next_cursor)

    # Follower count filtering needs each candidate's follower count before
    # trimming to `limit`, since without it we'd trim first and only then
    # discover some of those didn't meet the follower bounds. So when a
    # follower filter is active, fetch followers for every broadcaster_type-
    # filtered candidate up front rather than only the final trimmed page;
    # otherwise (the common case) keep the original cheap behavior of only
    # fetching followers for the <= limit entries actually being returned.
    if has_follower_filter:
        follower_tasks = {
            e["user_id"]: asyncio.create_task(get_followers(e["user_id"]))
            for e in entries
        }
        for e in entries:
            e["followers"] = await follower_tasks[e["user_id"]]

        # A None here means the lookup failed/isn't permitted (see
        # get_followers), not that the channel genuinely has 0 followers.
        # Treating None as 0 would make any min_followers filter above 0
        # reject every result — so an unknown count just skips the bounds
        # check instead of being excluded by it.
        filtered = []
        for e in entries:
            if e["followers"] is None:
                filtered.append(e)
                continue
            if min_followers is not None and e["followers"] < min_followers:
                continue
            if max_followers is not None and e["followers"] > max_followers:
                continue
            filtered.append(e)
        entries = filtered

    if not entries:
        return [], (None if exhausted else next_cursor)

    return await _search_page(entries, None if exhausted else next_cursor, limit, has_follower_filter)


# ==========================
# CHANNEL BIO + PANEL SOCIAL LINKS
# ==========================
# Helix (the app's own Client-ID/Secret credentials) has no endpoint for a
# channel's "About" bio text or its profile panels — those only exist on
# twitch.tv's own site and aren't part of the public REST API at all. To
# read them we use the same GraphQL endpoint the twitch.tv frontend itself
# calls, with Twitch's long-standing public web Client-ID (not a secret;
# it's shipped in twitch.tv's own client-side JS and is the same ID used
# by browser extensions and other unauthenticated-read tools). This is a
# read-only, unauthenticated query — it doesn't use or need the person's
# own TWITCH_CLIENT_ID/SECRET, so it works even before/without those being
# configured.

GQL_URL = "https://gql.twitch.tv/gql"
GQL_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

SOCIAL_CACHE_TIME = 21600  # 6h — bio/panels change rarely

social_cache = {}   # username -> {"data": {...}, "timestamp": ...}

# Recognized social platforms, matched against panel/bio link URLs so they
# can be labelled and deduped instead of shown as a flat, unlabelled list.
SOCIAL_PATTERNS = [
    ("Twitter / X", re.compile(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)", re.IGNORECASE)),
    ("Discord", re.compile(r"discord\.(?:gg|com/invite)/([A-Za-z0-9-]+)", re.IGNORECASE)),
    ("YouTube", re.compile(r"youtube\.com/(?:c/|channel/|@)?([A-Za-z0-9_-]+)", re.IGNORECASE)),
    ("Instagram", re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.IGNORECASE)),
    ("TikTok", re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)", re.IGNORECASE)),
    ("Facebook", re.compile(r"facebook\.com/([A-Za-z0-9.]+)", re.IGNORECASE)),
    ("Kick", re.compile(r"kick\.com/([A-Za-z0-9_-]+)", re.IGNORECASE)),
    ("Reddit", re.compile(r"reddit\.com/(?:u|user|r)/([A-Za-z0-9_-]+)", re.IGNORECASE)),
    ("Threads", re.compile(r"threads\.net/@([A-Za-z0-9_.]+)", re.IGNORECASE)),
    ("Bluesky", re.compile(r"bsky\.app/profile/([A-Za-z0-9_.-]+)", re.IGNORECASE)),
    ("Twitch", re.compile(r"twitch\.tv/([A-Za-z0-9_]+)", re.IGNORECASE)),
]

# Settings-defined platforms (see database.get_custom_social_platforms)
# merged into SOCIAL_PATTERNS at scrape time via
# get_effective_social_patterns() below, so a custom label/pattern (e.g.
# "Linktree" / "linktree.com/") is recognized the same way the built-in
# platforms are — without needing a code change or restart. Cached for a
# short time so every scraped URL/bio doesn't re-hit the DB; cleared
# whenever a custom platform is added/removed (see database.py callers
# in main.py) by simply letting the short TTL expire, matching how
# social_cache itself is a soft, time-bounded cache elsewhere in this
# file.
_custom_patterns_cache = {"patterns": None, "timestamp": 0}
_CUSTOM_PATTERNS_CACHE_TIME = 30  # seconds


def get_effective_social_patterns():
    """SOCIAL_PATTERNS plus any settings-defined custom platforms,
    compiled and appended in the order they were added. Custom entries
    are appended (never inserted before the built-ins), so a hardcoded
    lookup of a built-in platform by label (e.g. the "Twitch" pattern
    used by _extract_social_links) stays correct regardless of how many
    custom platforms exist. An invalid custom pattern (bad regex) is
    skipped with a warning rather than raising, since one bad entry
    shouldn't break scraping for every channel."""
    cached = _custom_patterns_cache["patterns"]
    if cached is not None and (time.time() - _custom_patterns_cache["timestamp"]) < _CUSTOM_PATTERNS_CACHE_TIME:
        return SOCIAL_PATTERNS + cached

    compiled = []
    try:
        for entry in db.get_custom_social_platforms():
            label = (entry.get("label") or "").strip()
            raw_pattern = (entry.get("pattern") or "").strip()
            if not label or not raw_pattern:
                continue
            try:
                compiled.append((label, re.compile(re.escape(raw_pattern), re.IGNORECASE)))
            except re.error as e:
                logger.warning(f"⚠️ Skipping invalid custom social platform pattern for '{label}': {e}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to load custom social platforms: {e}")

    _custom_patterns_cache["patterns"] = compiled
    _custom_patterns_cache["timestamp"] = time.time()
    return SOCIAL_PATTERNS + compiled


def invalidate_custom_social_patterns_cache():
    """Called after a custom platform is added/removed (see main.py) so
    the next scrape picks up the change immediately instead of waiting
    out the short cache TTL."""
    _custom_patterns_cache["patterns"] = None
    _custom_patterns_cache["timestamp"] = 0


def invalidate_social_cache():
    """Clears the per-streamer scraped-bio cache (social_cache, 6h TTL).

    BUGFIX: adding/removing a custom social platform used to only clear
    _custom_patterns_cache, not this one. A streamer scraped before a new
    custom platform/pattern existed keeps their old scraped_social_links
    (missing the new label) for up to 6h, so the platform's link/icon
    never appears on their card no matter how many times the custom icon
    image is changed afterward — it looks like a broken icon lookup, but
    the label was simply never re-scraped in. Called alongside
    invalidate_custom_social_patterns_cache() (see main.py) so the very
    next profile view re-scrapes with the up-to-date pattern set."""
    social_cache.clear()


# Generic URL matcher used to pull links out of free-text bio content,
# since a bio itself has no structured field list the way panels do.
BIO_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

# BUGFIX: panel/bio text on Twitch is rendered as Markdown, and the most
# common way a streamer adds a social link to a panel is exactly the way
# Twitch's own panel editor encourages: linking descriptive text like
# "Instagram" or "Twitter" to a URL, e.g. "[Instagram](https://instagram.
# com/someuser)". The visible text has no URL in it at all — the URL only
# exists inside the `(...)` part of that markdown syntax. Neither
# BIO_URL_RE (which needs the text to start with http(s)://) nor
# _find_bare_social_mentions (which needs a recognizable domain to appear
# directly in the visible text) can see a URL that's tucked inside
# markdown link syntax like this, so any panel built this way — which is
# an extremely common pattern for "Social Media" panels — was silently
# producing zero links even though the panel visibly links out to a
# social account when clicked. This pulls the URL out of the parens
# regardless of what the link text says.
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)", re.IGNORECASE)


def _find_markdown_links(text):
    """Extracts URLs from Markdown-style [text](url) links in panel/bio
    text, which BIO_URL_RE and _find_bare_social_mentions both miss since
    the URL isn't part of the visible text."""
    if not text:
        return []
    return MARKDOWN_LINK_RE.findall(text)



def _find_bare_social_mentions(text):
    """BUGFIX: BIO_URL_RE only matches text starting with http(s)://, but
    a very common way people write a social link in a bio/panel is the
    bare domain with no protocol at all — "twitter.com/someuser" instead
    of "https://twitter.com/someuser". Twitch's own bio/panel renderer
    auto-linkifies that bare text into a clickable hyperlink on the
    channel page, so from a viewer's perspective it *is* a link — but
    BIO_URL_RE never saw it as one, so it was silently dropped instead
    of being scraped. Each SOCIAL_PATTERNS regex already tolerates a
    missing protocol (they match on the domain itself), so this reuses
    those same patterns directly against the raw text to catch exactly
    the mentions BIO_URL_RE misses."""
    if not text:
        return []
    found = []
    for _, pattern in get_effective_social_patterns():
        for match in pattern.finditer(text):
            found.append(match.group(0))
    return found


def _classify_link(url):
    """Labels a URL with a known platform name if it matches one
    (built-in or settings-defined custom), else None (caller decides
    whether to keep unrecognized links)."""
    for label, pattern in get_effective_social_patterns():
        if pattern.search(url):
            return label
    return None


def _extract_social_links(urls, self_username=None):
    """De-dupes a list of raw URLs into a label->url map, keeping the
    first URL seen per platform. Skips only the channel's own Twitch
    link (already shown elsewhere in the UI) — a Twitch link to a
    *different* channel (e.g. a co-host/raid-target panel) is kept,
    since it's a legitimate distinct social link and was previously
    always dropped regardless of whose channel it pointed to."""
    found = {}
    self_username = (self_username or "").strip().lower()
    for url in urls:
        url = url.strip().rstrip(".,)")
        if not url:
            continue
        label = _classify_link(url)
        if not label:
            continue
        if label == "Twitch":
            # Looked up by label (not a fixed index) so this stays
            # correct regardless of how many settings-defined custom
            # platforms are appended after the built-ins — see
            # get_effective_social_patterns().
            twitch_pattern = next(p for lbl, p in SOCIAL_PATTERNS if lbl == "Twitch")
            match = twitch_pattern.search(url)
            linked_user = (match.group(1) if match else "").lower()
            if not linked_user or linked_user == self_username:
                continue
        if label not in found:
            found[label] = url
    return found


# Best-effort city/region -> IANA timezone lookup used to turn a
# self-reported location string (bio text — Twitch's public API has no
# structured "location" field for a user, so this is the only source
# available) into a timezone the app can display. Deliberately a small
# static table rather than a geocoding dependency: covers common
# streamer-hub cities/regions/countries so "London, UK" or "Los Angeles"
# resolve, without adding a network call or a new package. Keys are
# lowercased for matching; longer/more specific keys are checked first
# by LOCATION_TZ_LOOKUP's iteration order in _guess_timezone so "new york"
# doesn't get shadowed by a broader "usa" style entry appearing first.
LOCATION_TZ_LOOKUP = [
    # City-level (checked before broader country/region matches)
    ("los angeles", "America/Los_Angeles"),
    ("san francisco", "America/Los_Angeles"),
    ("seattle", "America/Los_Angeles"),
    ("portland", "America/Los_Angeles"),
    ("san diego", "America/Los_Angeles"),
    ("las vegas", "America/Los_Angeles"),
    ("phoenix", "America/Phoenix"),
    ("denver", "America/Denver"),
    ("chicago", "America/Chicago"),
    ("dallas", "America/Chicago"),
    ("houston", "America/Chicago"),
    ("austin", "America/Chicago"),
    ("new york", "America/New_York"),
    ("nyc", "America/New_York"),
    ("boston", "America/New_York"),
    ("atlanta", "America/New_York"),
    ("miami", "America/New_York"),
    ("orlando", "America/New_York"),
    ("toronto", "America/Toronto"),
    ("vancouver", "America/Vancouver"),
    ("montreal", "America/Toronto"),
    ("mexico city", "America/Mexico_City"),
    ("sao paulo", "America/Sao_Paulo"),
    ("são paulo", "America/Sao_Paulo"),
    ("buenos aires", "America/Argentina/Buenos_Aires"),
    ("london", "Europe/London"),
    ("manchester", "Europe/London"),
    ("dublin", "Europe/Dublin"),
    ("paris", "Europe/Paris"),
    ("berlin", "Europe/Berlin"),
    ("munich", "Europe/Berlin"),
    ("hamburg", "Europe/Berlin"),
    ("madrid", "Europe/Madrid"),
    ("barcelona", "Europe/Madrid"),
    ("rome", "Europe/Rome"),
    ("milan", "Europe/Rome"),
    ("amsterdam", "Europe/Amsterdam"),
    ("stockholm", "Europe/Stockholm"),
    ("oslo", "Europe/Oslo"),
    ("copenhagen", "Europe/Copenhagen"),
    ("helsinki", "Europe/Helsinki"),
    ("warsaw", "Europe/Warsaw"),
    ("prague", "Europe/Prague"),
    ("vienna", "Europe/Vienna"),
    ("zurich", "Europe/Zurich"),
    ("athens", "Europe/Athens"),
    ("istanbul", "Europe/Istanbul"),
    ("moscow", "Europe/Moscow"),
    ("dubai", "Asia/Dubai"),
    ("mumbai", "Asia/Kolkata"),
    ("delhi", "Asia/Kolkata"),
    ("bangalore", "Asia/Kolkata"),
    ("karachi", "Asia/Karachi"),
    ("bangkok", "Asia/Bangkok"),
    ("jakarta", "Asia/Jakarta"),
    ("manila", "Asia/Manila"),
    ("singapore", "Asia/Singapore"),
    ("hong kong", "Asia/Hong_Kong"),
    ("shanghai", "Asia/Shanghai"),
    ("beijing", "Asia/Shanghai"),
    ("seoul", "Asia/Seoul"),
    ("tokyo", "Asia/Tokyo"),
    ("osaka", "Asia/Tokyo"),
    ("sydney", "Australia/Sydney"),
    ("melbourne", "Australia/Melbourne"),
    ("brisbane", "Australia/Brisbane"),
    ("perth", "Australia/Perth"),
    ("auckland", "Pacific/Auckland"),
    ("wellington", "Pacific/Auckland"),
    ("cairo", "Africa/Cairo"),
    ("johannesburg", "Africa/Johannesburg"),
    ("lagos", "Africa/Lagos"),
    ("nairobi", "Africa/Nairobi"),
    # Region/country-level fallbacks (broad, checked after city names)
    ("california", "America/Los_Angeles"),
    ("washington state", "America/Los_Angeles"),
    ("texas", "America/Chicago"),
    ("florida", "America/New_York"),
    ("ontario", "America/Toronto"),
    ("quebec", "America/Toronto"),
    ("brazil", "America/Sao_Paulo"),
    ("united kingdom", "Europe/London"),
    ("scotland", "Europe/London"),
    ("wales", "Europe/London"),
    ("ireland", "Europe/Dublin"),
    ("germany", "Europe/Berlin"),
    ("france", "Europe/Paris"),
    ("spain", "Europe/Madrid"),
    ("italy", "Europe/Rome"),
    ("netherlands", "Europe/Amsterdam"),
    ("sweden", "Europe/Stockholm"),
    ("norway", "Europe/Oslo"),
    ("denmark", "Europe/Copenhagen"),
    ("finland", "Europe/Helsinki"),
    ("poland", "Europe/Warsaw"),
    ("russia", "Europe/Moscow"),
    ("india", "Asia/Kolkata"),
    ("pakistan", "Asia/Karachi"),
    ("thailand", "Asia/Bangkok"),
    ("indonesia", "Asia/Jakarta"),
    ("philippines", "Asia/Manila"),
    ("china", "Asia/Shanghai"),
    ("south korea", "Asia/Seoul"),
    ("japan", "Asia/Tokyo"),
    ("australia", "Australia/Sydney"),
    ("new zealand", "Pacific/Auckland"),
    ("south africa", "Africa/Johannesburg"),
    ("nigeria", "Africa/Lagos"),
    ("kenya", "Africa/Nairobi"),
    ("egypt", "Africa/Cairo"),
    # US/UK/Canada country-level catch-alls last, since almost every
    # more specific US/CA city or state above should match first.
    ("united states", "America/New_York"),
    ("usa", "America/New_York"),
    ("u.s.a", "America/New_York"),
    ("canada", "America/Toronto"),
]

# Recognizes short "location line" patterns commonly used in Twitch bios,
# e.g. "📍 London, UK", "Based in Los Angeles", "From Toronto, Canada".
# Deliberately conservative (a handful of common lead-in phrases/emoji)
# rather than trying to find a location anywhere in free-form bio text,
# to avoid false positives on unrelated sentences that happen to contain
# a place name.
#
# Split into two tiers so a pin-emoji marker (📍/🌍/🌎/🌏 — used almost
# exclusively for an actual location in Twitch bios) can be trusted as a
# real self-reported location even when LOCATION_TZ_LOOKUP doesn't
# recognize the place (see _extract_location) — a wrong-guess risk that's
# acceptable given how unambiguous a pin emoji is. Text lead-ins ("based
# in"/"location:"/"from") are less unambiguous (e.g. "based in my gaming
# chair 🎮"), so those stay on the bare-line tier and still need the
# timezone-table gate to avoid showing non-places.
_LOCATION_MARKED_RE = re.compile(
    r"(?:📍|🌍|🌎|🌏)\s*"
    r"([A-Za-z][A-Za-z .'-]{1,40}(?:,\s*[A-Za-z][A-Za-z .'-]{1,40})?)",
    re.IGNORECASE,
)
_LOCATION_LINE_RE = re.compile(
    r"(?:^|\n|📍|🌍|🌎|🌏)\s*"
    r"(?:based in|location:|from)?\s*"
    r"([A-Za-z][A-Za-z .'-]{1,40}(?:,\s*[A-Za-z][A-Za-z .'-]{1,40})?)",
    re.IGNORECASE,
)


def _guess_timezone(location_text):
    """Matches a free-text location string against LOCATION_TZ_LOOKUP and
    returns an IANA timezone name, or None if nothing matched. Case-
    insensitive substring match against the whole location string (not
    just single tokens) so both "LA" phrasing variants like "Los Angeles,
    CA" and "California" resolve correctly."""
    if not location_text:
        return None
    haystack = location_text.lower()
    for needle, tz_name in LOCATION_TZ_LOOKUP:
        if needle in haystack:
            return tz_name
    return None


def _extract_location(bio_text):
    """Best-effort extraction of a self-reported location from bio text,
    e.g. "📍 London, UK" or "Based in Los Angeles, CA". Twitch's public
    API has no structured location field, so this only ever recovers
    what a streamer has typed into their bio in a recognizable way — it
    intentionally returns None rather than guessing on bios that don't
    contain one of the recognized lead-ins/emoji, since a wrong guess is
    worse than no location shown at all.

    A pin-emoji line (📍/🌍/🌎/🌏) is trusted as a real location on its own
    — it's shown even if LOCATION_TZ_LOOKUP doesn't cover that particular
    place, so streamers outside the built-in city/region table (e.g. "📍
    Lisbon, Portugal") still get a location shown; they just won't have a
    local time next to it (see _guess_timezone). A text lead-in ("based
    in"/"location:"/"from") or a bare unmarked line is less unambiguous
    (e.g. "based in my gaming chair 🎮"), so those are only accepted when
    the candidate happens to resolve to a known place — otherwise this
    would show arbitrary bio text as a "location", which isn't one."""
    if not bio_text:
        return None
    for line in bio_text.split("\n"):
        match = _LOCATION_MARKED_RE.search(line)
        if match:
            candidate = match.group(1).strip(" .-")
            if candidate and len(candidate) >= 2:
                return candidate
    for line in bio_text.split("\n"):
        match = _LOCATION_LINE_RE.search(line)
        if not match:
            continue
        candidate = match.group(1).strip(" .-")
        if not candidate or len(candidate) < 2:
            continue
        if _guess_timezone(candidate):
            return candidate
    return None


# Recognizes short "age line" patterns commonly used in Twitch bios/
# panels, e.g. "Age: 27", "27 years old", "27y/o", "27yo", "I'm 27",
# "I'm 27 years old", "I am 27", "27-year-old", "aged 27", "turning 27".
# Twitch has no structured age field for a user (same situation as
# location — see _extract_location above), so this only ever recovers a
# self-reported age a streamer has typed somewhere in their bio or a
# panel. Kept deliberately narrow (a handful of common phrasings) rather
# than matching any bare number, to avoid false positives on unrelated
# numbers (follower goals, stream schedule times, heights like "5'11",
# etc.) that happen to appear in bio/panel text. Bounded to a plausible
# human age range (13-99) for the same reason.
_AGE_RE = re.compile(
    r"(?:\bage|\bedad)\s*[:\-]?\s*(\d{1,2})\b"
    r"|\bi\s*['’‘ʼ]?\s*m\s+(\d{1,2})\b"
    r"|\bi\s+am\s+(\d{1,2})\b"
    r"|\b(?:aged|turning|turned)\s+(\d{1,2})\b"
    r"|(\d{1,2})[\s-]*(?:years?[\s-]*(?:old|young)|yrs?[\s-]*old|y\s*/\s*o|yo\b)",
    re.IGNORECASE,
)


def _extract_age(*texts):
    """Best-effort extraction of a self-reported age from bio/panel text.
    Checks each given text in order (bio first, then panel title/
    description — same precedence as the location scrape) and returns
    the first plausible match as an int, or None if nothing recognizable
    was found. A wrong guess is worse than no age shown, so this only
    accepts a match against one of the recognized "Age: N" / "N years
    old" / "Nyo" / "I'm N" / "aged N" phrasings, and only within a
    plausible age range — never a bare number pulled from elsewhere in
    the text."""
    for text in texts:
        if not text:
            continue
        for match in _AGE_RE.finditer(text):
            raw = next((g for g in match.groups() if g), None)
            if not raw:
                continue
            try:
                age = int(raw)
            except ValueError:
                continue
            if 13 <= age <= 99:
                return age
    return None


async def get_channel_social(username):
    """Fetches a channel's About-tab bio text and profile panels, and
    returns the bio text plus any recognized social links found in either
    (panels first, then bio text) as a label->url dict. Best-effort: on
    any failure this logs and returns an empty result rather than raising,
    since this is supplementary display data, not core tracking data."""
    username = username.lower()

    cached = social_cache.get(username)
    if cached and cache_valid(cached, SOCIAL_CACHE_TIME):
        return cached["data"]

    result = {"bio": "", "social_links": {}, "location": None, "timezone": None, "age": None}

    # ChannelPanels is keyed by the channel's numeric user id, not its
    # login — passing channelLogin here silently matched nothing, so
    # panels (and therefore most social links) never came back. Resolve
    # the id first via the existing Helix lookup/cache.
    user_info = await get_user_id(username)
    if not user_info:
        result["_failed"] = True
        return result

    # BUGFIX: previously sent as `persistedQuery` (a hash referencing a
    # query already cached server-side by Twitch's own web client). Those
    # hashes are tied to Twitch's current web client build and rotate
    # whenever Twitch ships one — when a hash falls out of that cache,
    # Twitch returns HTTP 200 with a GQL-level `{"errors": [{"message":
    # "PersistedQueryNotFound"}]}` body instead of data. The old code only
    # checked `response.status != 200`, so this failure was invisible: it
    # silently fell through to the empty-result path below for every
    # channel, regardless of whether they actually had social links set.
    # Sending the full query document inline (not just its hash) avoids
    # depending on Twitch's persisted-query cache at all, so this can't
    # silently break again the same way.
    # BUGFIX (v3.2.1): the previous query guessed at inline-fragment type
    # names (CustomizationImagePanel / CustomizationVideoPanel /
    # CustomizationTextPanel) that don't exist in Twitch's actual schema —
    # every request failed outright with a GQL-level "Unknown type
    # ..." error (see bot.log), for every panel on every streamer, not
    # just ones with a particular panel style. Confirmed via the app's
    # own log output, not guessed. Since Twitch's GQL schema is
    # undocumented and introspection is blocked, the type-safe fix is to
    # not name a type at all: request title/description/linkURL as
    # plain fields directly on whatever `panel` resolves to, instead of
    # a `... on <GuessedTypeName>` fragment. This works for any concrete
    # panel type that has these fields (regardless of what Twitch
    # actually calls it), and a genuinely absent field is a normal
    # null rather than a query-level "Unknown type" failure that takes
    # every other field on the same operation down with it.
    # BUGFIX (v3.2.2): the previous query wrapped title/description/
    # linkURL in a nested `panel { ... }` selection inside each `panels`
    # list entry. But per Twitch's actual schema, `user.panels` already
    # returns a list of `Panel` objects directly — there's no nested
    # `panel` field to descend into. Every request failed outright with
    # "Cannot query field \"panel\" on type \"Panel\"" (see bot.log),
    # which took the whole ChannelPanels operation down and meant panel
    # text (and therefore any age/location self-reported only in a
    # panel, e.g. a "Facts about me" panel, rather than in the bio) was
    # never scraped. Querying the fields directly on each `Panel` list
    # entry (no nested wrapper) matches the real schema.
    # BUGFIX (v3.2.3, root cause, confirmed via bot.log + Twitch's own
    # published schema): the v3.2.2 fix still failed, now with "Cannot
    # query field \"title\" on type \"Panel\"". `Panel` is a GraphQL
    # *interface*, not a concrete object — confirmed against Twitch's own
    # schema (mirrored at kawcco.com/twitch-graphql-api, since Twitch
    # blocks live introspection), which lists `Panel` under Interfaces
    # and `DefaultPanel` as the concrete object implementing it. Fields
    # specific to a concrete type (title/description/linkURL) can only be
    # selected through an inline fragment on that type — requesting them
    # directly on the interface, with no type condition, is what both
    # this error and the earlier "Unknown type 'CustomizationImagePanel'"
    # one (v3.2.0/v3.2.1) came from: guessing at type names instead of
    # confirming them. `__typename` is the one field that's always valid
    # directly on an interface (it's a universal GraphQL meta-field), so
    # it stays outside the fragment; title/description/linkURL move
    # inside `... on DefaultPanel` — the type Twitch uses for the
    # customization panels (image/video/text/link) streamers set up via
    # their channel's Panels editor, which is what this scraper cares
    # about. A panel of a different concrete type (e.g. an installed
    # Extension's panel) simply won't match this fragment and yields no
    # title/description/linkURL, the same as before.
    channel_panels_query = """
        query ChannelPanels($id: ID!) {
            user(id: $id) {
                panels {
                    id
                    __typename
                    ... on DefaultPanel {
                        title
                        description
                        linkURL
                    }
                }
            }
        }
    """

    # BUGFIX: Twitch's dedicated "Social Links" feature (Creator Dashboard
    # → About → Social Links — the row of icon+label links like
    # "instagram" / "tik tok" / "discord" shown on the About page) is a
    # *third*, fully separate source of social links from bio text and
    # custom panels. It's its own structured field on the channel
    # (`socialMedias`, each with a `name` and `url`) — not something a
    # streamer types into bio/panel text at all, so no amount of URL or
    # Markdown-link scanning of `description` could ever find these; the
    # query simply never asked for this field. Added here as its own
    # query so these show up too, regardless of whether the streamer also
    # has a bio or panel links set.
    channel_socials_query = """
        query ChannelSocials($login: String!) {
            channel(name: $login) {
                socialMedias {
                    name
                    url
                }
            }
        }
    """

    channel_shell_query = """
        query ChannelShell($login: String!) {
            user(login: $login) {
                id
                description
            }
        }
    """

    query = [
        {
            "operationName": "ChannelPanels",
            "query": channel_panels_query,
            "variables": {"id": user_info["user_id"]},
        },
        {
            "operationName": "ChannelShell",
            "query": channel_shell_query,
            "variables": {"login": username},
        },
        {
            "operationName": "ChannelSocials",
            "query": channel_socials_query,
            "variables": {"login": username},
        },
    ]

    try:
        s = await get_session()
        # BUGFIX: gql.twitch.tv sits behind bot/traffic filtering that the
        # public REST Helix endpoint (api.twitch.tv) does not have. Helix
        # calls elsewhere in this file work with just Client-ID because
        # that's a documented, allow-listed API — but gql.twitch.tv expects
        # requests to look like they came from the twitch.tv web client
        # itself. Without a browser-like User-Agent and an Origin/Referer
        # of https://www.twitch.tv, Twitch's edge rejects the request
        # (typically HTTP 403) before it ever reaches the query resolver.
        # That 403 was already handled below and logged, but silently —
        # so every panel/bio lookup was failing at the transport level,
        # not because of anything about the query itself or the panel
        # parsing logic, which is why fixing the query shape alone never
        # actually restored scraping. Sending the same headers the real
        # twitch.tv site sends avoids the block entirely.
        headers = {
            "Client-ID": GQL_PUBLIC_CLIENT_ID,
            "Content-Type": "text/plain;charset=UTF-8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.twitch.tv",
            "Referer": "https://www.twitch.tv/",
        }

        async with s.post(GQL_URL, headers=headers, json=query) as response:
            if response.status != 200:
                logger.warning(f"⚠️ Social/panel lookup failed for {username}: HTTP {response.status}")
                result["_failed"] = True
                return result

            payload = await response.json()

        urls = []
        had_gql_errors = False
        # Collected alongside urls so age (like location) can be scraped
        # from panel title/description text, not just the bio — a panel
        # is a very common place for a streamer to list an age line (e.g.
        # a "Facts about me" panel).
        panel_texts = []

        for entry in payload if isinstance(payload, list) else [payload]:
            entry = entry or {}

            # BUGFIX: a 200 response can still carry a GQL-level `errors`
            # array (e.g. "PersistedQueryNotFound", or a field error on a
            # single operation) with no usable `data` for that operation.
            # Previously this was never checked, so a failed operation was
            # indistinguishable from a channel that simply has no bio/
            # panels — both produced an empty result silently. Now it's
            # logged so a systemic failure (bad query, blocked endpoint,
            # etc.) is visible instead of looking identical to "no links
            # set" for every channel.
            if entry.get("errors"):
                had_gql_errors = True
                logger.warning(f"⚠️ Social/panel lookup GQL error for {username}: {entry['errors']}")

            data = entry.get("data") or {}

            user = data.get("user")
            if user:
                description = user.get("description") or ""
                if description and not result["bio"]:
                    result["bio"] = description.strip()
                urls.extend(BIO_URL_RE.findall(description))
                urls.extend(_find_markdown_links(description))
                urls.extend(_find_bare_social_mentions(description))

            # ChannelSocials returns the structured "Social Links" list
            # (set via Creator Dashboard → About → Social Links) as
            # data.channel.socialMedias, each with its own `name` and
            # `url` — no text parsing needed, these are already discrete
            # URLs. Unlike bio/panel links (free text, so anything not
            # matching a known platform is likely just an unrelated URL
            # mention), every entry here was deliberately curated by the
            # streamer as a social link — including ones on platforms
            # outside SOCIAL_PATTERNS (e.g. a "wishlist" link) — so those
            # are kept too, labelled with Twitch's own name for it,
            # instead of being silently dropped for not matching a known
            # platform pattern.
            channel = data.get("channel")
            if channel:
                for social in channel.get("socialMedias") or []:
                    social = social or {}
                    social_url = social.get("url") or ""
                    if not social_url:
                        continue
                    if _classify_link(social_url):
                        urls.append(social_url)
                    else:
                        social_name = (social.get("name") or "").strip()
                        if social_name and social_name not in result["social_links"]:
                            result["social_links"][social_name] = social_url

            # ChannelPanels (queried by numeric id) returns panels nested
            # under data.user.panels; fall back to data.channel.panels in
            # case a future response shape puts them there instead, so
            # this keeps working either way.
            # BUGFIX (v3.2.2): each entry in `panels` is now the `Panel`
            # object itself (see query BUGFIX above), not a wrapper with
            # a nested `panel` field — read fields straight off `panel`.
            panels = (user or {}).get("panels") or (data.get("channel") or {}).get("panels") or []
            for panel in panels:
                panel_data = panel or {}
                # BUGFIX: a panel's outbound link isn't always in
                # `linkURL` — image-based "link out" panels use that
                # field, but the title/description text of any panel type
                # can itself contain plain or Markdown-style links (e.g.
                # a "Social Media" text panel listing "[Instagram](url)
                # [Twitter](url)"), which previously were only scanned
                # from `description` and never from `title`. Scanning
                # both catches links regardless of which panel type or
                # field the streamer's client put them in.
                link_url = panel_data.get("linkURL") or ""
                if link_url:
                    urls.append(link_url)
                for text_field in ("description", "title"):
                    text = panel_data.get(text_field) or ""
                    if text:
                        urls.extend(BIO_URL_RE.findall(text))
                        urls.extend(_find_markdown_links(text))
                        urls.extend(_find_bare_social_mentions(text))
                        panel_texts.append(text)

                # NOTE: previously this logged a warning for any panel
                # __typename outside a hardcoded allowlist of names this
                # code guessed at (see BUGFIX above for why that whole
                # approach was wrong — the guessed names weren't valid
                # GraphQL types at all, which is what actually broke
                # scraping for every streamer, not a gap in the
                # allowlist). Now that title/description/linkURL are
                # requested as plain fields with no type name involved,
                # every panel type gets the same fields attempted and
                # there's nothing left to special-case per type.
                if panel_data.get("__typename") and not (link_url or panel_data.get("description") or panel_data.get("title")) \
                        and panel_data.get("__typename") not in _unhandled_panel_types_warned:
                    # A panel with no title/description/linkURL at all
                    # either genuinely has nothing set, or is a panel
                    # shape where none of these fields apply/resolve —
                    # logged once per type (not per channel) so a
                    # systemic gap would still be visible, without
                    # treating "no content" as an error on its own.
                    _unhandled_panel_types_warned.add(panel_data.get("__typename"))
                    logger.info(
                        f"ℹ️ Panel type '{panel_data.get('__typename')}' returned no "
                        "title/description/linkURL — either empty or this field set "
                        "doesn't apply to it."
                    )

        # Merge (not overwrite) so the unrecognized-platform Social Links
        # entries seeded directly into result["social_links"] above (e.g.
        # "wishlist") survive alongside the pattern-classified links
        # pulled from urls (bio, panels, and recognized-platform Social
        # Links) — an earlier version of this assignment replaced the
        # dict outright and would have silently dropped them.
        classified = _extract_social_links(urls, self_username=username)
        for label, url in classified.items():
            result["social_links"].setdefault(label, url)

        if had_gql_errors and not result["bio"] and not result["social_links"]:
            # Every operation failed and nothing was recovered — don't
            # cache this as a confirmed-empty result (that would hide a
            # transient/systemic failure behind the normal 6h TTL and make
            # it look identical to "this channel genuinely has nothing
            # set" for the rest of that window).
            result["_failed"] = True
            return result

        # Location/timezone: best-effort, extracted from the same bio
        # text already fetched above. Twitch has no structured location
        # field on a user, so this only ever recovers what the streamer
        # has self-reported in their bio (see _extract_location) — left
        # as None when nothing recognizable is found, rather than guessing.
        location = _extract_location(result["bio"])
        if location:
            result["location"] = location
            result["timezone"] = _guess_timezone(location)

        # Age: same best-effort, self-reported-only approach as location
        # above — checked in bio first, then panel text, so a bio-level
        # mention takes precedence over one buried in a panel.
        result["age"] = _extract_age(result["bio"], *panel_texts)

    except Exception as e:
        logger.warning(f"⚠️ Social/panel lookup error for {username}: {e}")
        result["_failed"] = True
        return result

    _store_cache(social_cache, username, {"data": result, "timestamp": time.time()}, SOCIAL_CACHE_TIME)
    return result
