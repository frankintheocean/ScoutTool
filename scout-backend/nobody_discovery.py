"""Isolated zero-viewer discovery using nobody.live's public stream index.

Contract verified against jkingsman/Nobody.live at
d099772b96b04daeb9b843ce092a9e0796ff0062 (app.py and db_utils.py).
This client sends search terms only, never ScoutTool/Twitch credentials.
"""
import asyncio
import json
import re

import aiohttp


_STREAM_URL = "https://nobody.live/stream"
_MAX_RESPONSE_BYTES = 1024 * 1024
_REQUEST_DEADLINE = 15
_REQUEST_SLOTS = asyncio.Semaphore(2)


class NobodyDiscoveryError(Exception):
    def __init__(self, detail, status_code=502, retry_after=None):
        super().__init__(detail)
        self.status_code = status_code
        self.retry_after = retry_after


def _normalize_stream(raw, terms, match, denied):
    if not isinstance(raw, dict) or type(raw.get("viewer_count")) is not int or raw["viewer_count"] != 0:
        return None
    username = raw.get("user_login") or raw.get("user_name")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", username):
        return None
    username = username.lower()
    if username in denied:
        return None
    tags = raw.get("tags")
    tags = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    category = raw.get("game_name") if isinstance(raw.get("game_name"), str) else ""
    searchable = " ".join([category, *tags]).lower()
    matches = [term in searchable for term in terms]
    if terms and not (all(matches) if match == "all" else any(matches)):
        return None
    return {
        "username": username,
        "display_name": raw.get("user_name") if isinstance(raw.get("user_name"), str) else username,
        "category": category,
        "tags": tags,
        "title": raw.get("title") if isinstance(raw.get("title"), str) else "",
        "started_at": raw.get("started_at") if isinstance(raw.get("started_at"), str) else None,
        "live_viewers": 0,
    }


async def search_zero_viewers(include="", match="all", limit=25, blacklist=()):
    """Return a bounded random sample matching games/tags, with duplicates removed."""
    terms = include.replace(",", " ").lower().split()
    denied = {username.lower() for username in blacklist}
    params = {
        "count": 65, "max_viewers": 0, "include": include,
        # Upstream's ANY query with no terms would otherwise match nothing.
        "search_operator": match if terms else "all",
    }
    try:
        async with asyncio.timeout(_REQUEST_DEADLINE), _REQUEST_SLOTS:
            # This module owns its session and honors standard proxy settings.
            async with aiohttp.ClientSession(
                trust_env=True, timeout=aiohttp.ClientTimeout(total=12),
            ) as session:
                async with session.get(
                    _STREAM_URL, params=params, headers={"Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=12), allow_redirects=False,
                ) as response:
                    if response.status in (429, 503):
                        raise NobodyDiscoveryError("nobody.live is busy. Please try again shortly.", 503, 5)
                    if response.status == 413:
                        raise NobodyDiscoveryError("Search phrase is too long for nobody.live. Please shorten it.", 422)
                    if response.status != 200:
                        raise NobodyDiscoveryError("nobody.live could not complete the search. Please try again.")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise NobodyDiscoveryError("nobody.live returned an oversized response.")
                        body.extend(chunk)
        data = json.loads(body)
    except asyncio.TimeoutError as exc:
        raise NobodyDiscoveryError("nobody.live timed out. Please try again.", 504) from exc
    except (aiohttp.ClientError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise NobodyDiscoveryError("Could not read results from nobody.live. Please try again.") from exc
    if not isinstance(data, list):
        raise NobodyDiscoveryError("nobody.live returned an unexpected response.")
    if len(data) > 65:
        raise NobodyDiscoveryError("nobody.live returned too many stream records.")

    results = []
    seen = set()
    for raw in data:
        stream = _normalize_stream(raw, terms, match, denied)
        if stream is not None and stream["username"] not in seen:
            seen.add(stream["username"])
            results.append(stream)
            if len(results) == limit:
                break
    return results
