from database import get_all, get_average_viewers, get_average_viewers_bulk


# Column indices into a `streamers` row (see database.py setup()):
#   0 id, 1 username, 2 url, 3 profile_image, 4 discovered, 5 category,
#   6 followers, 7 average_viewers, 8 current_viewers, 9 live_status,
#   10 community_rating, 11 content_rating, 12 raid_rating, 13 raid_score,
#   14 peak_viewers, 15 notes, 16 last_live, 17 last_updated


# ==========================
# RAID SCORE CALCULATION
# ==========================

def calculate_raid_score(streamer, fallback_average=None):
    """
    Calculates raid suitability score out of 100.

    Factors:
        Ratings:               45 points
        Viewer compatibility:  25 points
        Growth potential:      15 points
        Current momentum:      10 points
        Manual raid score:      5 points
    """

    if not streamer or len(streamer) < 18:
        return 0


    def field(name, index):
        return streamer[name] if hasattr(streamer, "keys") else streamer[index]

    username = field("username", 1)


    followers = field("followers", 6) or 0
    average_viewers = field("average_viewers", 7) or 0
    current_viewers = field("current_viewers", 8) or 0

    community = field("community_rating", 10) or 0
    content = field("content_rating", 11) or 0
    raid = field("raid_rating", 12) or 0

    
    peak_viewers = field("peak_viewers", 14) or 0


    if average_viewers == 0:

        average_viewers = fallback_average if fallback_average is not None else get_average_viewers(username)


    # ==========================
    # COMMUNITY RATINGS
    # ==========================

    rating_score = min(
        (community * 3)
        +
        (content * 3)
        +
        (raid * 3),
        45
    )


    # ==========================
    # VIEWER COMPATIBILITY
    # ==========================

    if average_viewers == 0:

        viewer_score = 5

    elif average_viewers <= 25:

        viewer_score = 25

    elif average_viewers <= 100:

        viewer_score = 20

    elif average_viewers <= 250:

        viewer_score = 10

    elif average_viewers <= 500:

        viewer_score = 5

    else:

        viewer_score = 0



    # ==========================
    # GROWTH POTENTIAL
    # ==========================

    growth_score = 0


    if followers and average_viewers:

        ratio = followers / average_viewers


        if ratio < 50:

            growth_score = 15

        elif ratio < 200:

            growth_score = 10

        elif ratio < 500:

            growth_score = 5



    # ==========================
    # CURRENT MOMENTUM
    # ==========================

    momentum_score = 0


    if current_viewers > 0:

        if peak_viewers:

            viewer_ratio = current_viewers / peak_viewers


            if viewer_ratio >= 0.75:

                momentum_score = 10

            elif viewer_ratio >= 0.5:

                momentum_score = 5

        else:

            momentum_score = 5



    # ==========================
    # MANUAL ADJUSTMENT
    # ==========================
    # The stored raid_score column (index 13) may hold a manually-set
    # score from /scout raidscore. If present, let a fraction of it
    # nudge the freshly-calculated score, per the docstring's promised
    # "Manual raid score: 5 points" factor (previously always 0).

    manual_raid_score = (streamer["manual_raid_score"] if hasattr(streamer, "keys") and "manual_raid_score" in streamer.keys() else field("raid_score", 13)) or 0
    manual_bonus = round((manual_raid_score / 100) * 5)


    # ==========================
    # FINAL SCORE
    # ==========================

    score = (
        rating_score
        +
        viewer_score
        +
        growth_score
        +
        momentum_score
        +
        manual_bonus
    )


    return int(
        min(
            round(score),
            100
        )
    )



# ==========================
# RAID CANDIDATES
# ==========================

def get_raid_candidates(limit=5):

    streamers = get_all()


    averages = get_average_viewers_bulk([s["username"] for s in streamers if not s["average_viewers"]])
    ranked = [
        (
            streamer,
            calculate_raid_score(streamer, averages.get(streamer["username"], 0))
        )

        for streamer in streamers
    ]


    ranked.sort(
        key=lambda x: x[1],
        reverse=True
    )


    return [
        streamer
        for streamer, score in ranked[:limit]
    ]