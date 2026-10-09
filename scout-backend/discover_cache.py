import asyncio
import hashlib
import json
import threading
import time

# ==========================
# DISCOVER RESULT CACHE
# ==========================
# Raw first-page results, stream pages and user batches share a bounded
# 30-second cache. Distinct local filters can reuse their overlapping
# upstream pages. Tracking/location/blacklist enrichment is never cached.
# Cursor result pages and random zero-viewer samples share only in-flight
# work, so refreshing still draws a new sample and pagination stays intact.

_TTL = 30  # seconds
_cache = {}
_lock = threading.Lock()
_generation = 0
_inflight = {}


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


def set_cached_discover(value, *, expected_generation=None, **filters):
    key = _cache_key(**filters)
    with _lock:
        if expected_generation is not None and expected_generation != _generation:
            return
        now = time.time()
        for old in list(_cache):
            if now - _cache[old]["timestamp"] >= _TTL:
                del _cache[old]
        if key not in _cache:
            while len(_cache) >= 100:
                del _cache[next(iter(_cache))]
        _cache[key] = {"value": value, "timestamp": now}


def invalidate_all():
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()


async def shared_search(factory, *, cache=True, **filters):
    """Coalesce identical in-flight work, including independently cancelled callers.

    Only raw provider data is cached: tracking/location/blacklist enrichment
    belongs to each response. Invalidating during a fetch prevents that fetch
    from repopulating the cache. Cursor results can share work without caching.
    """
    if cache:
        value = get_cached_discover(**filters)
        if value is not None:
            return value
    generation = _generation
    key = (asyncio.get_running_loop(), generation, _cache_key(**filters))
    task = _inflight.get(key)
    if task is None:
        async def fetch():
            try:
                value = await factory()
                if cache and value is not None:
                    set_cached_discover(value, expected_generation=generation, **filters)
                return value
            finally:
                _inflight.pop(key, None)
        task = asyncio.create_task(fetch())
        _inflight[key] = task
        def settled(done):
            # Cancellation can precede fetch's first instruction, skipping finally.
            if _inflight.get(key) is done:
                _inflight.pop(key, None)
            if not done.cancelled():
                done.exception()  # A disconnected sole caller still needs cleanup.
        task.add_done_callback(settled)
    return await asyncio.shield(task)


async def close_pending_searches():
    """Finish request cleanup before closing the shared HTTP session."""
    loop = asyncio.get_running_loop()
    tasks = [task for key, task in list(_inflight.items()) if key[0] is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
