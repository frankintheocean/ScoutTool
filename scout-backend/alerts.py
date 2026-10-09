import asyncio
import hashlib
import json
import os
import smtplib
from email.message import EmailMessage

import database as db
import twitch_api
from logger import logger
from notifier import push_alert_notification

# ==========================
# NOTIFICATION / ALERT RULES
# ==========================
# Two rule kinds, both stored in alert_rules (see database.py):
#   - streamer_live: fires when a *tracked* streamer's status flips
#     Offline -> Live. Hooked into tracker.py right next to the existing
#     live-notification call, so it reuses the same transition detection
#     instead of polling separately.
#   - discover_match: fires when a live Discover search (same filters
#     /api/discover already accepts) returns at least one result. Polled
#     by a background loop in main.py's lifespan, independent of the
#     streamer tracker's interval since Discover searches hit different
#     Twitch endpoints.
#
# Delivery channels: 'browser' (pushes onto the existing SSE live-feed via
# notifier.py, no new frontend transport needed), 'webhook' (POSTs a JSON
# payload, reusing twitch_api's shared aiohttp session), and 'email' (via
# SMTP, configured through env vars — kept optional so most installs never
# need to touch it).

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)


def channels_of(rule):
    return {c.strip() for c in (rule["channels"] or "").split(",") if c.strip()}


async def _deliver_webhook(rule, payload):
    url = rule["webhook_url"]
    if not url:
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "webhook", payload.get("summary", ""), ok=False, error="No webhook URL configured")
        return
    try:
        session = await twitch_api.get_session()
        async with session.post(url, json=payload, timeout=10) as resp:
            ok = resp.status < 400
            await asyncio.to_thread(db.add_alert_delivery, 
                rule["id"], "webhook", payload.get("summary", ""),
                ok=ok, error=None if ok else f"HTTP {resp.status}",
            )
    except Exception as e:
        logger.error(f"Webhook delivery failed for rule {rule['id']}: {e}")
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "webhook", payload.get("summary", ""), ok=False, error=str(e))


def _deliver_email_sync(rule, payload):
    """Runs in a worker thread (smtplib is blocking) — see _deliver_email."""
    msg = EmailMessage()
    msg["Subject"] = f"ScoutBot alert: {payload.get('summary', '')}"
    msg["From"] = SMTP_FROM or "scoutbot@localhost"
    msg["To"] = rule["email"]
    msg.set_content(payload.get("body", payload.get("summary", "")))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        smtp.starttls()
        if SMTP_USER and SMTP_PASSWORD:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


async def _deliver_email(rule, payload):
    if not rule["email"]:
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "email", payload.get("summary", ""), ok=False, error="No email address configured")
        return
    if not SMTP_HOST:
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "email", payload.get("summary", ""), ok=False, error="SMTP not configured (set SMTP_HOST in .env)")
        return
    import asyncio
    try:
        await asyncio.to_thread(_deliver_email_sync, rule, payload)
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "email", payload.get("summary", ""), ok=True)
    except Exception as e:
        logger.error(f"Email delivery failed for rule {rule['id']}: {e}")
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "email", payload.get("summary", ""), ok=False, error=str(e))


async def _deliver_browser(rule, payload):
    try:
        await push_alert_notification(payload)
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "browser", payload.get("summary", ""), ok=True)
    except Exception as e:
        logger.error(f"Browser push failed for rule {rule['id']}: {e}")
        await asyncio.to_thread(db.add_alert_delivery, rule["id"], "browser", payload.get("summary", ""), ok=False, error=str(e))


async def fire_rule(rule, payload):
    """Delivers `payload` across every channel this rule is configured
    for, and stamps last_triggered. Each channel's failure is logged
    independently so one broken webhook doesn't hide a working email
    channel (or vice versa)."""
    for channel in channels_of(rule):
        if channel == "webhook":
            await _deliver_webhook(rule, payload)
        elif channel == "email":
            await _deliver_email(rule, payload)
        elif channel == "browser":
            await _deliver_browser(rule, payload)
        else:
            logger.warning(f"Unknown alert channel '{channel}' on rule {rule['id']}")

    await asyncio.to_thread(db.mark_alert_triggered, rule["id"])


async def check_streamer_live_rules(username, data):
    """Called by tracker.py right after it detects an Offline -> Live
    transition for a tracked streamer. Only rules targeting this exact
    username are evaluated — cheap enough to run on every transition."""
    rules = [r for r in await asyncio.to_thread(db.get_alert_rules, enabled_only=True) if r["kind"] == "streamer_live" and r["target"].lower() == username.lower()]
    if not rules:
        return

    payload = {
        "type": "streamer_live",
        "username": username,
        "summary": f"{data.get('display_name', username)} just went live",
        "body": f"{data.get('display_name', username)} is now live in {data.get('category', 'Unknown')} with {data.get('live_viewers', 0)} viewers.",
        "category": data.get("category", "Unknown"),
        "live_viewers": data.get("live_viewers", 0),
        "url": f"https://twitch.tv/{data.get('username', username)}",
    }

    for rule in rules:
        try:
            await fire_rule(rule, payload)
        except Exception as e:
            logger.error(f"Alert rule {rule['id']} failed: {e}")


def _hash_results(results):
    """Stable fingerprint of a Discover result set (by username), so a
    repeat poll that returns the exact same matches doesn't re-fire the
    alert every cycle — only a genuinely new/changed match set does."""
    usernames = sorted(r.get("username", "") for r in results)
    return hashlib.sha256(",".join(usernames).encode("utf-8")).hexdigest()


async def check_discover_match_rules():
    """Polled periodically (see main.py's lifespan) — re-runs each enabled
    discover_match rule's saved filters against live Twitch search and
    fires if the match set is non-empty and has changed since last time."""
    rules = [r for r in await asyncio.to_thread(db.get_alert_rules, enabled_only=True) if r["kind"] == "discover_match"]
    if not rules:
        return

    for rule in rules:
        try:
            filters = json.loads(rule["target"])
        except (TypeError, ValueError):
            logger.error(f"Alert rule {rule['id']}: could not parse target filters")
            continue

        try:
            results, _ = await twitch_api.search_streams(
                category=filters.get("category"),
                min_viewers=filters.get("min_viewers"),
                max_viewers=filters.get("max_viewers"),
                broadcaster_type=filters.get("broadcaster_type"),
                language=filters.get("language"),
                tags=filters.get("tags"),
                exclude_tags=filters.get("exclude_tags"),
                created_after=filters.get("created_after"),
                created_before=filters.get("created_before"),
                blacklist=await asyncio.to_thread(db.get_blacklist_set),
                min_followers=filters.get("min_followers"),
                max_followers=filters.get("max_followers"),
                limit=filters.get("limit", 25),
            )
        except Exception as e:
            logger.error(f"Discover alert rule {rule['id']} search failed: {e}")
            continue

        if not results:
            continue

        result_hash = _hash_results(results)
        if result_hash == rule["last_result_hash"]:
            continue  # same match set as last time — don't re-notify

        top = results[0]
        summary = (
            f"Discover match: {len(results)} live streamer(s) for "
            f"'{filters.get('category') or 'any category'}' (top: {top.get('display_name', top.get('username'))})"
        )
        payload = {
            "type": "discover_match",
            "summary": summary,
            "body": summary + "\n\n" + "\n".join(
                f"- {r.get('display_name', r.get('username'))} ({r.get('live_viewers', 0)} viewers)"
                for r in results[:10]
            ),
            "count": len(results),
            "filters": filters,
        }

        try:
            await fire_rule(rule, payload)
        except Exception as e:
            logger.error(f"Alert rule {rule['id']} failed: {e}")
        finally:
            await asyncio.to_thread(db.mark_alert_triggered, rule["id"], result_hash)
