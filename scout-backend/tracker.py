import asyncio

from database import (
    get_tracking_snapshot,
    get_streamer,
    get_streamer_metadata,
    update_twitch_data,
    update_profile_image,
    update_raid_score,
    create_stream_session,
    end_stream_session,
    add_event,
    get_average_viewers,
    mark_live_notified,
    clear_live_notified,
    was_live_notified,
    set_scraped_social,
    backfill_scraped_platform_links,
)
from twitch_api import get_bulk_streamer_data, get_channel_social
from scoring import calculate_raid_score
from notifier import send_live_notification, send_offline_notification
from alerts import check_streamer_live_rules
from logger import logger
import database as _database

TRACK_INTERVAL = 600  # 10 minutes


async def track_streamers():
    logger.info("Twitch tracker started")

    while True:
        try:
            # Piggybacks on this loop's existing 10-minute cadence rather than
            # adding a separate timer/thread — sweeps out any finished VOD
            # download jobs older than an hour so a session with no further
            # downloads still gets cleaned up (see main.py's
            # prune_stale_vod_jobs / _prune_stale_vod_jobs_locked).
            try:
                import main as _main
                _main.prune_stale_vod_jobs()
            except Exception as e:
                logger.warning(f"VOD job prune error: {e}")

            streamers, generation = await asyncio.to_thread(get_tracking_snapshot)

            if not streamers:
                logger.info("No streamers to track")
                await asyncio.sleep(TRACK_INTERVAL)
                continue

            usernames = [s["username"] for s in streamers if s["username"]]

            data_map = await get_bulk_streamer_data(usernames, expected_generation=generation)

            if not data_map:
                logger.warning("Twitch returned no data")
                await asyncio.sleep(TRACK_INTERVAL)
                continue

            current_cache = {s["username"].lower(): s for s in streamers if s["username"]}

            for username in usernames:
                if generation != _database.database_generation:
                    break
                try:
                    current = current_cache.get(username.lower())
                    if not current:
                        continue

                    data = data_map.get(username.lower())

                    if data is None:
                        logger.warning(f"{username}: Twitch data unavailable")
                        continue

                    old_status = current["live_status"] or "Offline"
                    viewers = data.get("live_viewers", 0)
                    status = data.get("live_status", "Offline")

                    if old_status == "Live" and status == "Offline":
                        peak_viewers = current["peak_viewers"] or 0

                        await asyncio.to_thread(end_stream_session, 
                            username,
                            peak_viewers,
                            await asyncio.to_thread(get_average_viewers, username, _generation=generation),
                            _generation=generation,
                        )

                        await asyncio.to_thread(add_event, username, "Stream ended", _generation=generation)
                        await asyncio.to_thread(clear_live_notified, username, _generation=generation)

                        # Push an SSE event immediately so every connected
                        # browser flips this card to Offline right away,
                        # instead of waiting for some other streamer's
                        # live event to happen to trigger a roster reload
                        # (previously only Offline->Live pushed anything).
                        try:
                            await send_offline_notification(username, data)
                        except Exception as e:
                            logger.error(f"Offline notification error ({username}): {e}")

                    elif old_status != "Live" and status == "Live":
                        await asyncio.to_thread(create_stream_session, username, _generation=generation)
                        await asyncio.to_thread(add_event, username, "Stream started", _generation=generation)

                        if not await asyncio.to_thread(was_live_notified, username, _generation=generation):
                            url = f"https://twitch.tv/{data.get('username', username)}"

                            try:
                                await send_live_notification(username, url, data)
                            except Exception as e:
                                logger.error(f"Notification error ({username}): {e}")

                            try:
                                await check_streamer_live_rules(username, data)
                            except Exception as e:
                                logger.error(f"Alert rule check error ({username}): {e}")

                            await asyncio.to_thread(mark_live_notified, username, _generation=generation)

                    await asyncio.to_thread(update_twitch_data, 
                        username,
                        data.get("category", "Unknown"),
                        data.get("followers", current["followers"]),
                        viewers,
                        status,
                        _generation=generation,
                    )

                    profile_image = data.get("profile_image")

                    if profile_image:
                        current_image = current["profile_image"]

                        if current_image != profile_image:
                            await asyncio.to_thread(update_profile_image, username, profile_image, _generation=generation)

                    try:
                        updated_row = await asyncio.to_thread(get_streamer, username, _generation=generation)

                        if updated_row:
                            new_score = await asyncio.to_thread(calculate_raid_score, updated_row)
                            await asyncio.to_thread(update_raid_score, username, new_score, manual=False, _generation=generation)

                    except Exception as e:
                        logger.error(f"Auto raid score error ({username}): {e}")

                    # Bio/panel social links (see twitch_api.get_channel_social)
                    # rarely change, so this only re-scrapes when missing or
                    # stale (6h, matching twitch_api.SOCIAL_CACHE_TIME) rather
                    # than on every 10-minute tracker tick — keeps this from
                    # adding a GQL call per streamer per tick for data that's
                    # essentially static.
                    try:
                        meta = await asyncio.to_thread(get_streamer_metadata, username, _generation=generation)
                        scraped_at = meta.get("social_scraped_at")
                        is_stale = True
                        if scraped_at:
                            try:
                                from datetime import datetime as _dt
                                age = (_dt.now() - _dt.fromisoformat(scraped_at)).total_seconds()
                                is_stale = age >= 21600
                            except Exception:
                                is_stale = True

                        if is_stale:
                            social = await get_channel_social(username)
                            if not social.get("_failed"):
                                await asyncio.to_thread(set_scraped_social, username, social.get("bio", ""), social.get("social_links", {}), social.get("location"), social.get("timezone"), social.get("age"), _generation=generation)
                                await asyncio.to_thread(backfill_scraped_platform_links, username, social.get("social_links", {}), _generation=generation)

                    except Exception as e:
                        logger.warning(f"⚠️ Social scrape error ({username}): {e}")

                    logger.info(f"{username}: {viewers} viewers")

                except Exception as e:
                    logger.error(f"Tracker error ({username}): {e}")

        except Exception as e:
            logger.error(f"Tracker loop error: {e}")

        await asyncio.sleep(TRACK_INTERVAL)


async def tracker_wrapper():
    while True:
        try:
            await track_streamers()

        except asyncio.CancelledError:
            logger.info("Twitch tracker stopped")
            break

        except Exception as e:
            logger.error(f"Tracker crashed: {e}")
            await asyncio.sleep(60)
