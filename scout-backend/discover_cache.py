import hashlib
import json
import threading
import time

# ==========================
# DISCOVER RESULT CACHE
# ==========================
# Short TTL, per-filter-combo cache for /api/discover so repeated
# identical searches (re-opening the modal, a person retrying the same
# filters, a discover_match alert rule polling) don't hammer Twitch with
# duplicate Get Streams calls. Mirrors the pattern already used for
# get_all() in database.py (`_all_cache`) and for user/follower/category
# lookups in twitch_api.py — a small dict guarded by a lock, TTL checked
# on read.
#
# Deliberately NOT cached: paginated (cursor-based) requests. A cursor is
# only valid for one specific underlying page, so caching it would either
# need cursor-aware keys (churns constantly, ~never hits) or risk serving
# a stale/mismatched page — not worth it for what's a "load more" action
# a person only does once per search anyway.

_TTL = 30  # seconds
_cache = {}
_lock = threading.Lock()


def _cache_key(**filters):
    # Sorted, stable JSON so equivalent filter dicts (regardless of arg
    # order) hash the same.
    return hashlib.sha256(
        json.dumps(filters, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def get_cached_discover(**filters):
    key = _cache_key(**filters)
    with _lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["timestamp"]) < _TTL:
            return entry["value"]
    return None


def set_cached_discover(value, **filters):
    key = _cache_key(**filters)
    with _lock:
        now = time.time()
        for old in list(_cache):
            if now - _cache[old]["timestamp"] >= _TTL:
                del _cache[old]
        while len(_cache) >= 100:
            del _cache[next(iter(_cache))]
        _cache[key] = {"value": value, "timestamp": now}


def invalidate_all():
    with _lock:
        _cache.clear()
