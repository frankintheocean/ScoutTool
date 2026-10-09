from scoring import calculate_raid_score


def streamer_to_dict(row, metadata=None, latest_response=None, fallback_average=None):
    """Converts a sqlite3.Row from the `streamers` table into a plain dict
    for JSON responses. `row` supports column-name access (row_factory is
    sqlite3.Row), so this works regardless of column order.

    `latest_response` is an optional sqlite3.Row from streamer_responses
    (see database.get_latest_response) — included so a card/detail view
    can show the most recent outreach-response event without a second
    round trip; the full history is still fetched separately via
    /api/streamers/{username}/responses."""
    if row is None:
        return None

    d = dict(row)

    # Normalize booleans stored as ints
    d["archived"] = bool(d.get("archived", 0))
    d["notified_live"] = bool(d.get("notified_live", 0))

    d["live_raid_score"] = calculate_raid_score(row, fallback_average)

    if metadata is not None:
        d["favourite"] = metadata.get("favourite", False)
        d["tags"] = metadata.get("tags", [])
        d["priority"] = metadata.get("priority", "Watch")
        d["alias"] = metadata.get("alias", "")
        d["notify_enabled"] = metadata.get("notify_enabled", True)
        d["x_url"] = metadata.get("x_url", "")
        d["instagram_url"] = metadata.get("instagram_url", "")
        d["youtube_url"] = metadata.get("youtube_url", "")
        d["kick_url"] = metadata.get("kick_url", "")
        d["outreach_status"] = metadata.get("outreach_status", "active")
        d["bio"] = metadata.get("bio", "")
        d["scraped_social_links"] = metadata.get("scraped_social_links", {})
        d["scraped_location"] = metadata.get("scraped_location", "")
        d["scraped_timezone"] = metadata.get("scraped_timezone", None)
        d["scraped_age"] = metadata.get("scraped_age", None)
        # Manually-entered — separate from the scraped_* pair above (see
        # database.set_location). effective_location/effective_timezone
        # give the frontend a single field to filter/sort/display on:
        # manual value wins when set, falling back to the scraped one.
        d["location"] = metadata.get("location", "")
        d["timezone"] = metadata.get("timezone", None)
        d["effective_location"] = d["location"] or d["scraped_location"]
        d["effective_timezone"] = d["timezone"] or d["scraped_timezone"]

    if latest_response is not None:
        d["latest_response"] = {
            "response_type": latest_response["response_type"],
            "note": latest_response["note"],
            "timestamp": latest_response["timestamp"],
        }
    else:
        d["latest_response"] = None

    return d


def streamers_to_list(rows, metadata_lookup=None, averages=None):
    metadata_lookup = metadata_lookup or {}
    out = []
    for row in rows:
        username = row["username"]
        out.append(streamer_to_dict(row, metadata_lookup.get(username), fallback_average=None if averages is None else averages.get(username, 0)))
    return out
