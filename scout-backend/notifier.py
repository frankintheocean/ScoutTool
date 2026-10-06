import asyncio
import time

from database import get_streamer_metadata
from logger import logger

# ==========================
# IN-APP LIVE NOTIFICATION FEED
# ==========================
# Replaces the Discord embed post from the original bot. The tracker calls
# push_live_notification() when a tracked streamer goes Offline -> Live;
# subscribers (the /api/events SSE stream) receive it in real time, and it's
# also kept in a short ring buffer so a client that connects late can still
# see recent notifications.

_subscribers = set()
_recent = []
_RECENT_LIMIT = 50


def _make_event(username, url, data):
    return {
        "type": "live",
        "username": username,
        "url": url,
        "display_name": data.get("display_name", username),
        "category": data.get("category", "Unknown"),
        "live_viewers": data.get("live_viewers", 0),
        "followers": data.get("followers", 0),
        "profile_image": data.get("profile_image"),
        "timestamp": time.time(),
    }


async def send_live_notification(username, url, data):
    """Called by the tracker when a tracked streamer transitions
    Offline -> Live. Respects each streamer's notify_enabled metadata flag,
    same as the original Discord notifier."""
    try:
        metadata = await asyncio.to_thread(get_streamer_metadata, username)

        if not metadata.get("notify_enabled", True):
            logger.info(f"Notifications muted for {username}, skipping")
            return

        event = _make_event(username, url, data)

        _recent.append(event)
        if len(_recent) > _RECENT_LIMIT:
            del _recent[: len(_recent) - _RECENT_LIMIT]

        for queue in list(_subscribers):
            _enqueue(queue, event)

        logger.info(f"Sent live notification for {username}")

    except Exception as e:
        logger.error(f"Failed to send live notification for {username}: {e}")


async def send_offline_notification(username, data=None):
    """Called by the tracker when a tracked streamer transitions
    Live -> Offline. Unlike send_live_notification, this always fires
    (no notify_enabled gate) since its job is to keep every connected
    browser's roster in sync in real time — without it, a card only
    flips to Offline once some *other* streamer's SSE event happens to
    trigger a reload, so a solo streamer going offline could sit shown
    as Live for up to a full 10-minute tracker cycle."""
    data = data or {}
    event = {
        "type": "offline",
        "username": username,
        "display_name": data.get("display_name", username),
        "category": data.get("category", "Unknown"),
        "timestamp": time.time(),
    }

    _recent.append(event)
    if len(_recent) > _RECENT_LIMIT:
        del _recent[: len(_recent) - _RECENT_LIMIT]

    for queue in list(_subscribers):
        _enqueue(queue, event)


async def push_alert_notification(payload):
    """Browser-push delivery channel for alert_rules (see alerts.py) — put
    onto the same SSE feed used for live-streamer notifications rather
    than opening a second transport. `payload` already carries a `type`
    field ('streamer_live' or 'discover_match') so the frontend can tell
    it apart from the plain 'live' events the tracker sends directly."""
    event = dict(payload)
    event.setdefault("timestamp", time.time())

    _recent.append(event)
    if len(_recent) > _RECENT_LIMIT:
        del _recent[: len(_recent) - _RECENT_LIMIT]

    for queue in list(_subscribers):
        _enqueue(queue, event)


def subscribe():
    queue = asyncio.Queue(maxsize=100)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue):
    _subscribers.discard(queue)


def recent_events(limit=20):
    return _recent[-limit:]


# ==========================
# WEBSOCKET BROADCAST (bidirectional)
# ==========================
# /api/ws in main.py is an alternate transport for the same event feed
# above (subscribe/unsubscribe/recent_events, unchanged) — kept
# additive so the existing SSE stream (/api/events) and every current
# client are untouched. Its extra value over SSE is bidirectionality:
# broadcast_client_event() lets one connected client's message (e.g. a
# "typing" indicator) be relayed to every other connected client, which
# a one-way SSE stream can't do. Not wired into any client-originated
# feature yet — this is the transport a future multi-user feature
# (live typing/collab) would build on. Kept separate from _subscribers
# (asyncio.Queue, used by SSE) since WebSocket connections are pushed
# to directly rather than polled from a queue.
def _enqueue(queue, event):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(event)


_ws_clients = {}


def register_ws(websocket, queue):
    _ws_clients[websocket] = queue


def unregister_ws(websocket):
    _ws_clients.pop(websocket, None)


async def broadcast_client_event(event, sender=None):
    # Each connection has one sender task, so slow clients cannot block
    # other clients or race two simultaneous socket writes.
    for ws, queue in list(_ws_clients.items()):
        if ws is not sender:
            _enqueue(queue, event)
