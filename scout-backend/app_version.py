# Single source of truth for the app version + in-app changelog.
# Bump APP_VERSION and add an entry at the TOP of CHANGELOG whenever a
# release goes out. Read by /api/version and rendered in the frontend's
# "What's new" panel.
#
# Versioning: app versions (current and future) use vX.X — a single
# major.minor number, e.g. v3.5, v4.0 — NOT vX.X.X. This supersedes the
# previous "prerelease vX.5"/"prerelease vX.0" scheme (0.5-per-change,
# oldest release "prerelease v1.0") and the vX.X.0 scheme that followed it.

APP_VERSION = "v4.2"

CHANGELOG = [
    {
        "version": "v4.2",
        "date": "2026-10-06",
        "changes": [
            "Combined Twitch Discover and zero-viewer discovery in one tabbed screen, preserving their independent filters and stream previews.",
            "Fixed oversized stream titles and overflowing tags, and aligned discovery cards with the roster layout.",
            "Fixed header button overflow in smaller windows and full-width sort controls; moved secondary header actions into Tools.",
            "Added removable filter chips and reliable Clear filters for roster and both discovery tabs, including remembered zero-viewer filters.",
            "Reduced duplicate Twitch searches by sharing in-flight work and briefly reusing overlapping stream pages and user lookups.",
            "Refreshed tracking, location, and blacklist status on cached discovery results; kept cancellation, retries, and shutdown cleanup safe.",
        ],
    },
    {
        "version": "v4.1",
        "date": "2026-10-06",
        "changes": [
            "Added a separate zero-viewer discovery module using nobody.live, with game/tag search phrases, all/any matching, and remembered filters.",
            "Reduced bulk-scrape task memory, cache-maintenance stalls, raid-candidate ranking memory, and repeated virtual-scroll work.",
            "Stopped virtual scrolling from reloading retained live previews or losing inline editor focus.",
            "Fixed cache updates evicting unrelated results and made Twitch retries release connections before waiting or refreshing tokens.",
            "Kept long-download results available after completion, preserved precise trim filenames, and handled oversized numeric times correctly.",
            "Fixed rapid Discover Load more actions overlapping requests.",
        ],
    },
    {
        "version": "v4.0",
        "date": "2026-10-06",
        "changes": [
            "Fixed populated legacy database migrations, search-index rebuilding, and stale search results after deletion.",
            "Made imports validate and migrate before replacement, preserve credentials on failure, and reject stale background writes after a restore.",
            "Fixed rapid view/detail selection and ordered autosaves; kept detail actions and navigation reachable in smaller windows.",
            "Fixed Show all fallback pagination, archived paging, variable-height virtual scrolling, and cross-page comparison selection.",
            "Stopped repeated automatic raid-score calculations from overwriting manual bonuses and drifting upward.",
            "Preserved live status for zero-viewer streams and saved follower/social data when Twitch lookups fail.",
            "Fixed Discover pagination losing matches, filter-history restoration, alert filter interactions, and repeated download actions.",
            "Bounded notification and cache growth, moved blocking database work off the event loop, and improved shutdown cleanup.",
            "Fixed Unicode authentication, validation error serialization, saved timezone aliases, system-theme updates, and missing changelog entries.",
            "Preserved installer database journals, bundled Windows timezone data, and fixed native exit and startup handling.",
        ],
    },
    {
        "version": "v3.9",
        "date": "2026-09-30",
        "changes": [
            "🎨 Replaced the standalone app icon with a new ScoutBot radar/target design that is visually distinct from the Dashboard icon.",
            "🪟 Added a dedicated Windows AppUserModelID so ScoutBot keeps its own taskbar identity and does not group with the Dashboard application.",
        ],
    },

    {
        "version": "v3.8",
        "date": "2026-09-30",
        "changes": [
            "🐛 Fixed the Windows standalone import path so .env and streamers.db imports no longer trigger an invalid packaged-process restart.",
            "🪟 Hardened the PyInstaller build with explicit Windows asyncio native imports, including _overlapped.",
            "🛠 Hardened install.bat/install_progress.ps1 path resolution and prevented progress-line wrapping artifacts."
        ],
    },

    {
        "version": "v3.7",
        "highlights": [
            "⚡ Performance: roster search now uses an expanded FTS5 index, targeted SQLite indexes, request cancellation, shorter debounce, and relevance-aware search operators so large rosters stay responsive.",
            "⌨️ New: Ctrl+K command palette, keyboard-first navigation, Ctrl+F search, and Ctrl+Shift+F Discover shortcut.",
            "⚖️ New: select up to four streamers for side-by-side comparison, plus batched bulk priority/tag editing.",
            "💾 New: verified database backups, restore points, automatic pre-import safety backups, import preview/diff validation, and richer diagnostics.",
            "🧵 New: bounded maintenance job tracking keeps backup and recovery work observable without blocking the UI.",
            "🧹 Cleanup: fixed duplicate font-size settings, tightened roster controls, and preserved existing density, virtual scrolling, presets, alerts, watchlists, and theming infrastructure.",
        ],
    },
    {
        "version": "v3.6",
        "highlights": [
            "📦 Improved: the Windows installer now explains each setup step in plain language, shows a live progress bar, elapsed time, and estimated time remaining, and launches ScoutBot automatically when installation finishes.",
            "🎨 New: ScoutBot has its own distinct Windows application icon for the executable, shortcuts, and taskbar instead of reusing the Dashboard icon.",
            "📥 New: Settings → Import lets you bring in an existing .env and/or streamers.db without manually copying files into the installation folder. Imports are validated before they replace the current files, and the standalone app refreshes the imported state without closing the window.",
        ],
    },
    {
        "version": "v3.5",
        "highlights": [
            "🔢 Changed: versioning format — app versions (current and future) now use vX.X, a single major.minor number (e.g. v3.5, v4.0), instead of the previous vX.X.0 three-part-looking scheme. No functional changes in this release.",
        ],
    },
    {
        "version": "v3.4.0",
        "highlights": [
            "🐛 Fixed (root cause): clicking a tracked streamer's card in the roster selected them and loaded their detail content in the sidebar, but if the detail panel had ever been manually collapsed (its own collapse-arrow button, or a collapsed state saved from a previous session) the panel's column stayed pinned to 0px width instead of opening back up — the detail content (including the Watch Stream button) rendered but stayed hidden off-screen, with no visible sign anything had happened. Selecting a streamer now always re-opens the panel, the same way dragging its resizer handle open already did, instead of leaving a stale manual-collapse flag in charge.",
        ],
    },
    {
        "version": "v3.3.5",
        "highlights": [
            "🐛 Fixed (root cause, confirmed via a live repro): Settings → Scout → Import with a valid .env and/or streamers.db reported \"Import failed (500)\" with no further detail. The import endpoint replaces streamers.db on disk while this same process (including the tracker thread) still holds it open — on Windows, moving a file onto one that's still open elsewhere raises an OS-level \"file in use\" error, which isn't a normal API error and so came back as a bare 500 with no message, matching exactly what was reported. This never showed up running from source on Linux/Mac, since POSIX allows that. Every open connection to the old database is now closed right before the file is replaced, so the move succeeds on every platform; any remaining file-in-use error now also surfaces as a proper, readable message instead of a blank 500.",
        ],
    },
    {
        "version": "v3.3.0",
        "highlights": [
            "🐛 Fixed (root cause, confirmed via a live repro with real .env/streamers.db files): Settings → Scout → Import (.env and/or streamers.db) reported success and looked like it worked, but the imported data never actually showed up — a re-opened roster still looked empty, or credentials still looked unset, exactly as if nothing had been imported. Two separate causes, both now fixed:",
            "  1. The import endpoint wrote the uploaded file(s) to disk correctly, then killed its own process and depended entirely on something external relaunching it to actually load them — true under Dashboard/Electron (which does relaunch it), but scout-backend is also documented (README_DASHBOARD.md) as runnable standalone (`python -m uvicorn main:app`, `npm run scout:backend`) for development or independent use, and nothing supervises the process in that case: the backend just went offline with the newly-imported files sitting on disk, unread, exactly matching \"imports successfully but never loads.\" The import endpoint now spawns its own replacement uvicorn process (same host/port the request actually came in on, not just .env's default) before exiting, so a fresh process reliably comes back up and loads the new files whether or not Dashboard/Electron is there to do it — Electron's own existing relaunch-on-exit still fires too and safely no-ops against the one already coming up.",
            "  2. Importing a streamers.db could still open right back up looking empty (or missing recent rows) even once the backend did restart. streamers.db runs in WAL mode (see database.py), which keeps recent writes in a separate `streamers.db-wal` file next to the main one — swapping in an uploaded streamers.db only ever replaced the main file, leaving the *previous* database's leftover `-wal`/`-shm` sidecars in place. SQLite replays whatever WAL sidecar it finds next to a database file on open, with no awareness that the main file underneath it was just swapped for a different database — so the next process to open the newly-imported file replayed the old database's leftover writes back onto it, silently undoing some or all of what was just imported. The old database's `-wal`/`-shm` sidecars are now removed as part of the swap, so the newly-imported file opens clean.",
        ],
    },
    {
        "version": "v3.2.5",
        "highlights": [
            "📅 New: \"Last streamed on\" date. Roster cards (every list view — All Streamers, Favourites, Raid Candidates, etc.) and the expanded streamer detail panel now show 📅 Last streamed on <date>, sourced from the existing `last_live` column (already tracked and already sortable via the roster's Sort dropdown, just not previously surfaced as its own display line). Shown in the same own-line style as the existing scraped age/location info, right alongside them — appears once a streamer has been seen live at least once, left off (not \"—\") until then, same as-found-or-omitted convention as scraped age/location. Also added to the detail panel's \"Scraped from bio & panels\" area for visibility alongside age/location there. The existing \"Last live\" summary tile in the detail panel's metrics grid is unchanged.",
        ],
    },
    {
        "version": "v3.2.4",
        "highlights": [
            "🔍 Verified: roster card age display (🎂) audited end-to-end — confirmed still correct and unregressed since v3.0.5 (own line under category, not gated behind expanding the card). Also hardened: the scraped age value is now HTML-escaped on the roster card, matching how category/location are already escaped, closing a theoretical injection gap if a scraped bio ever contained markup-like text.",
        ],
    },
    {
        "version": "v3.2.3",
        "highlights": [
            "🐛 Fixed (root cause, confirmed via bot.log + Twitch's own published schema): panel scraping — and therefore any age typed only into a panel rather than the bio — still failed after v3.2.2, now with \"Cannot query field 'title' on type 'Panel'\". `Panel` turns out to be a GraphQL *interface*, not a concrete type — verified against Twitch's own schema (Twitch blocks live introspection, so this was checked against a maintained schema mirror rather than guessed): title/description/linkURL only exist on the concrete `DefaultPanel` type that implements it, and can only be requested through an inline fragment (`... on DefaultPanel { ... }`), not as plain fields directly on the interface. Every previous panel-query attempt (v3.2.0 through v3.2.2) tried a different wrong shape without confirming the actual type; this one is checked against the real schema. Panel text — including panel-only ages and panel-sourced social links — now scrapes correctly. Age scraped from bio text alone was unaffected throughout and kept working.",
        ],
    },
    {
        "version": "v3.2.2",
        "highlights": [
            "🐛 Attempted fix: corrected the previous \"Cannot query field 'panel' on type 'Panel'\" error but introduced a new one (\"Cannot query field 'title' on type 'Panel'\") — see v3.2.3 above for the actual root cause and fix; panel scraping remained broken across this version.",
            "🎨 Added: the expanded streamer detail panel now shows the scraped age (🎂) alongside the existing scraped location, in its own row under Location — previously only the roster card showed it, so it never appeared anywhere in the detail view even once found.",
        ],
    },
    {
        "version": "v3.2.1",
        "highlights": [
            "🐛 Fixed (root cause, confirmed via logs): the bio/panel scraper's panel query was completely broken for every tracked streamer, not just ones with a particular panel style — it errored with a GQL-level \"Unknown type 'CustomizationImagePanel'\" on every single request, because the v3.2.0 fix (and the query before it) guessed at inline-fragment type names that don't actually exist in Twitch's schema. That error silently failed the whole panel lookup every time — no age, no panel-sourced social links, no panel content of any kind, for anyone, regardless of panel type. The query no longer names a type at all: title/description/linkURL are now requested as plain fields directly, which works regardless of what Twitch's schema actually calls the concrete panel type. A re-scrape (or Settings → re-scrape all) now genuinely picks up panel content again, including a self-reported age typed into any panel style.",
        ],
    },
    {
        "version": "v3.2.0",
        "highlights": [
            "🐛 Attempted fix: broadened the panel query to (incorrectly) target an additional guessed panel type — see v3.2.1 above for the actual root cause and fix; this version's change didn't resolve the underlying issue.",
        ],
    },
    {
        "version": "v3.1.5",
        "highlights": [
            "🐛 Fixed: self-reported age (🎂) scraped from a streamer's bio/panels was missed entirely for the common self-intro phrasings \"I'm 27\" and \"I'm 27 years old\" (and variants like \"I am 27\", \"Im 27\") — the age scraper only recognized \"Age: 27\" and \"27 years old\"/\"27yo\"/\"27y/o\" style lines, so a bare \"I'm N\" (without the word \"age\" or a trailing \"years old\") never matched at all. Now also recognizes \"I'm/I am/Im N\", \"N-year-old\", \"aged N\", and \"turning/turned N\", still bounded to a plausible age range and still self-reported-only (no guessing).",
        ],
    },
    {
        "version": "v3.1.0",
        "highlights": [
            "⚡ Improved: VOD/clip download speed, further — fragment concurrency raised from 5 to 16, and a chunked-range download mode was added so single-file clip downloads (which don't have multiple fragments to parallelize) get split into concurrent ranged chunks too, instead of only VODs benefiting. If aria2c happens to already be installed and on PATH, it's now used automatically for a further speed boost (its own internal multi-connection-per-fragment splitting) — this is fully optional and the app works exactly as before if it isn't present.",
        ],
    },
    {
        "version": "v3.0.5",
        "highlights": [
            "🐛 Fixed: the self-reported age scraped from a streamer's bio/panels (🎂, added in v2.9.0) almost never showed on the roster card — it was appended to the same category line as the category name, which truncates with an ellipsis once it runs out of room, so any streamer with a category name of normal length silently pushed the age off the end. It's now on its own line, same as location, so it always shows once found.",
            "⬇️ Added: elapsed time and ETA while a VOD/clip download is running, shown alongside the existing percentage in the VOD Downloader's progress line (e.g. \"Downloading… 42% · 0:32 elapsed · ETA 0:45 · 3.2 MB/s\") — sourced straight from yt-dlp's own progress tracking.",
            "🖼️ Fixed: the small VOD preview thumbnail next to each title in the VOD Downloader never actually appeared — Twitch's Videos API returns thumbnail_url as a template with literal '%{width}x%{height}' placeholders that need substituting with real pixel dimensions before the URL is usable; left as-is it was simply broken and hidden. Now resolved to a real image URL, so the preview shows as originally intended.",
            "⚡ Improved: VOD/clip download speed — fragments are now fetched 5 at a time instead of one at a time (yt-dlp's concurrent-fragments option), which meaningfully speeds up downloads for Twitch's HLS-segmented VODs/clips.",
        ],
    },
    {
        "version": "v3.0.0",
        "highlights": [
            "🐛 Fixed: downloading a Clip from the new VOD Downloader (v2.9.5) always failed with \"A Twitch VOD or clip URL is required\", even for a clip picked directly from the Clips tab — Twitch's Clips API returns a clips.twitch.tv URL, not a twitch.tv one, and the download endpoint's URL check only accepted twitch.tv/www.twitch.tv. Full VODs were unaffected. Any twitch.tv subdomain is now accepted.",
            "🐛 Fixed: clip duration in the VOD Downloader's Clips tab showed as a raw number of seconds (e.g. \"34\") instead of a readable time — clips report duration as a plain number, unlike VODs which already come pre-formatted from Twitch; the formatter for this existed but was never actually wired up.",
            "🐛 Fixed: background VOD/clip download jobs were kept in memory forever after finishing, growing without bound over a long-running session — completed/failed jobs older than an hour are now cleared out as new downloads start.",
        ],
    },
    {
        "version": "v2.9.5",
        "highlights": [
            "⬇️ New: Twitch VOD Downloader inside each tracked streamer's detail panel. Browse recent VODs and clips, pick a full video or a time range, and download it directly to your Downloads folder without leaving ScoutBot.",
            "⚡ Added: background download jobs with live progress, so a VOD can keep downloading while you continue scouting elsewhere in the app.",
        ],
    },
    {
        "version": "v2.9.0",
        "highlights": [
            "✨ Added: streamer age scraping — the existing bio/panel scraper (Settings → Re-scrape, and the background scrape that runs when adding a streamer) now also looks for a self-reported age (e.g. \"Age: 27\", \"27 years old\", \"27yo\") in the channel's About-tab bio and profile panels, same best-effort approach as the existing location scrape. When found, it's shown as \"🎂 27\" on both the roster card and the expanded streamer detail panel, next to the category/live status line.",
            "♿ Improved: all three Text size accessibility options (Normal/Large/Larger, Settings → Accessibility) now scale the UI noticeably more than before.",
            "✅ Verified: the per-streamer ↻ Refresh button already correctly flips a streamer's live status to Offline when they've gone offline since the last check — confirmed end-to-end (Twitch lookup → live_status write → roster/detail re-render) rather than only trusting the previous behaviour.",
        ],
    },
    {
        "version": "v2.8.5",
        "highlights": [
            "✨ Added: sort by live status. The Sort dropdown now has a \"Live status\" option to bring currently-live streamers to the top (or bottom, with the direction toggle) — previously live streamers could only be found by scanning the grid or using the Live-only filter, not sorted for alongside other criteria.",
            "ℹ️ Clarified: Twitch API quota (Settings → General) — the existing rate-limit display (remaining/limit and reset countdown, with a ⚠️ warning once quota is low) is unchanged; this is a documentation pass only, see README.",
            "ℹ️ Clarified: \"Show all\" roster mode (the toggle next to Sort) already renders very large rosters with a windowed/virtual-scrolling list rather than one huge DOM tree — unchanged, documentation pass only, see README.",
        ],
    },
    {
        "version": "v2.8.0",
        "highlights": [
            "✨ Added: right-clicking inside any text field (Add Streamer, search, notes, tags, presets, etc.) now shows a small in-app Cut/Copy/Paste menu styled to match the rest of the app, as an addition alongside the browser's own menu. Paste works even when nothing has been typed yet, and Cut/Copy grey themselves out automatically when there's no text selected. Uses the browser's standard clipboard permission model, so on a first paste the browser may ask to allow clipboard access — allow it once and it's remembered.",
            "🎨 Updated: app icon redrawn in a modern, iOS-style rounded-square format with a gradient background using the app's own accent colour.",
        ],
    },
    {
        "version": "v2.7.5",
        "highlights": [
            "🐛 Fixed (packaged Windows build): right-click did nothing in text fields — no Cut/Copy/Paste menu — which meant a Twitch username (or anything else) copied elsewhere couldn't be right-click-pasted in, e.g. into the Add Streamer field. Root cause: pywebview's window defaults text_select to False, which disables both text selection and, as a consequence, the native WebView2 context menu tied to it. text_select is now explicitly enabled on window creation. Ctrl+V and the app's own paste handling (e.g. parsing a pasted twitch.tv link down to just the username) were unaffected either way — only the right-click menu itself was missing. Running from source in a normal browser tab was never affected.",
        ],
    },
    {
        "version": "v2.7.0",
        "highlights": [
            "🐛 Fixed: build-installer.bat now anchors itself to its own directory, writes logs beside the batch file, and prepares bundled pnpm with Windows-safe filesystem handling, so builds no longer depend on the caller's working directory or lose diagnostics.",
            "🐛 Fixed: live roster cards still showed a stray red/green diagonal line under the viewer count (the v2.3.0 inline viewer sparkline) — v2.6.5 tried to fix this by requiring more data points before drawing, but the line was still showing up looking like a random, meaningless mark on live cards. The sparkline is now removed from the card entirely; Followers/Viewers/Raid score stats are unchanged.",
        ],
    },
    {
        "version": "v2.6.5",
        "highlights": [
            "🐛 Fixed: live roster cards could show a stray-looking diagonal line under the viewer count for a streamer that had only just gone live. The inline viewer sparkline (v2.3.0) only needs 2 viewer_history readings to draw a line, but with just 2 points that line is a single straight segment with no real shape — it read as a random/meaningless mark rather than an actual trend. The sparkline now waits for at least 3 readings before drawing, leaving the slot empty until there's a real trend to show (same as it already did for 0-1 points).",
        ],
    },
    {
        "version": "v2.6.0",
        "highlights": [
            "🐛 Fixed: duplicate streamers could show up in a single Discover search again — deepening the page budget for follower-filtered searches (v2.5.5) also made it more likely for the same streamer to be fetched twice within one search, since Twitch's live list can reorder mid-paginate as viewer counts change and a streamer can shift across a page boundary. The existing frontend de-duplication only guards across separate \"Load more\" calls, not within one response, so this went unnoticed by it. Results are now de-duplicated by user ID as pages are gathered, server-side, closing the gap for both a single search and \"Load more\".",
        ],
    },
    {
        "version": "v2.5.5",
        "highlights": [
            "🐛 Fixed: Discover could return zero (or far too few) results with a narrow Min/Max followers range set (e.g. 0-100), even in a big category with obviously-matching live streamers. Follower count can only be filtered *after* candidates are gathered by viewer count, and the gathering step was stopping as soon as it had \"enough\" candidates by that measure alone — with a tight follower range, the follower filter could then reject nearly all of them, leaving few or none, even though a much deeper search would have found plenty. Follower-filtered searches now gather a much larger, deeper candidate pool before giving up.",
        ],
    },
    {
        "version": "v2.5.0",
        "highlights": [
            "🔒 Fixed (security): the new WebSocket live feed (`/api/ws`) was reachable with zero authentication even when WEB_USERNAME/WEB_PASSWORD were set — Basic Auth is applied via HTTP middleware, which never sees WebSocket connections, so this one route silently bypassed the same access control every other endpoint (including the SSE feed it mirrors) already enforces. `/api/ws` now checks credentials itself before accepting the connection.",
        ],
    },
    {
        "version": "v2.4.5",
        "highlights": [
            "🐛 Fixed: category watchlists' automatic check-interval timer compared a UTC clock against a locally-timestamped last-checked value — on any server not running in the UTC timezone, this silently skewed when a watchlist was actually treated as \"due\" by roughly the server's UTC offset. The comparison is now done consistently in local time on both sides.",
            "🐛 Fixed: the new WebSocket live feed (`/api/ws`) could get stuck open on a client that vanished without a clean disconnect (e.g. a dropped connection) — the send side silently gave up while the receive side kept waiting forever, leaking that connection's subscription until the app restarted. A failed send now tears down the whole connection immediately.",
            "⚡ Improved: automatic category-watchlist checks now look up which results are already-tracked streamers in one batched query instead of one query per live channel returned — matters more now that this runs on a recurring background timer instead of only on a manual click.",
        ],
    },
    {
        "version": "v2.4.0",
        "highlights": [
            "📦 New: bulk tag/priority editing — PUT /api/streamers/tags/bulk and PUT /api/streamers/priority/bulk apply one tag list or priority to a whole multi-select at once, mirroring the existing PUT /api/streamers/location/bulk endpoint, to back a bulk-actions selection in the UI instead of one request per streamer.",
            "🔌 New: GET /api/ws — a WebSocket alternative to the existing SSE live feed (/api/events, unchanged and still primary), for future bidirectional real-time features (e.g. live typing/collab if multi-user is ever supported). Replays the same recent-event backlog and streams the same notifier events as SSE; not wired into any client-facing feature yet.",
            "⏱️ Improved: category watchlists now check on their own configurable interval (check_interval_seconds, settable via the new PUT /api/watchlists/{category}/interval) instead of only ever running when someone hits the manual /check endpoint. A background cron loop checks each watchlist as its own interval comes due and pushes new matches onto the live notification feed.",
        ],
    },
    {
        "version": "v2.3.5",
        "highlights": [
            "🔎 New: roster/notes/alias search now runs on a SQLite FTS5 full-text index instead of `LIKE '%...%'` scans — keeps search-as-you-type responsive as the tracked roster grows into the thousands. Falls back automatically to the previous LIKE-based search on the rare SQLite build without FTS5 compiled in; results and matching behavior are otherwise unchanged (still matches on username, alias, and notes).",
            "💀 New: Dashboard widgets now show a shimmer skeleton placeholder (matching the existing roster grid skeleton) while loading, instead of plain \"Loading…\" text.",
            "📡 New: offline, read-only roster viewing — a service worker now caches the app shell plus the roster/favourites/raid-candidates/stats/categories/locations GET endpoints, so the roster stays browsable (last-known data) if Twitch or the network becomes unreachable. A banner appears while offline; nothing can be added/edited/saved until the connection returns, and every write request is untouched — it still always hits the network.",
            "📊 New: structured Twitch API rate-limit telemetry — the backend now tracks the Ratelimit-Limit/Ratelimit-Remaining/Ratelimit-Reset headers Twitch returns on every Helix call (not just on a 429) and exposes the current quota via GET /api/settings/twitch. Settings → General now shows remaining quota and warns before it runs low, instead of only finding out via a 429.",
        ],
    },
    {
        "version": "v2.3.0",
        "highlights": [
            "🗂️ New: Saved Dashboard layouts — save your current widget arrangement under a name (e.g. \"Scouting mode\" vs \"Overview mode\") from the Customize widgets panel, then switch between saved layouts any time. The existing per-device active layout/config is unchanged; saved layouts are just named snapshots on top of it.",
            "📜 New: \"Show all\" roster mode — a toggle next to Sort switches the roster from paginated pages to a single fetch of every matching streamer, rendered with a virtual-scrolling list so browsing thousands of tracked streamers stays smooth instead of only ever showing one page at a time.",
            "↩️ New: Undo toast for destructive actions — archiving, blacklisting, or removing/untracking a streamer now shows a few seconds of \"Undo\" before the change is actually sent, instead of committing immediately.",
            "⚡ New: Favourite, priority, and tag changes now update the card instantly (optimistic UI) instead of waiting on the request to resolve — reverted automatically if the save fails.",
            "🔗 New: Client-side GET de-duplication — if the same GET fires twice in quick succession (e.g. two components both asking for /api/categories around the same time), the second call now shares the first's in-flight request instead of firing a second one.",
            "📈 New: Inline sparkline on live roster cards showing the recent viewer trend, not just the current number — filled in lazily per card from the existing per-streamer history endpoint.",
        ],
    },
    {
        "version": "v2.2.0",
        "highlights": [
            "⚡ New: GET /api/dashboard-summary combines the Dashboard's five widget requests (Overview, Live Now, Favourites, Raid Candidates, Recently Viewed) into one round trip — the full dashboard load now fires a single fetch instead of 4-5 parallel ones. Toggling a single widget on or changing its row-count still uses that widget's own endpoint directly, unchanged, since a one-widget update is still cheaper than refetching everything.",
            "📝 Documented: GET /api/locations and GET /api/categories already accepted limit/offset (added in v1.8.0) but it was only ever called out in this changelog, not in the API reference section — now documented there too.",
        ],
    },
    {
        "version": "v2.1.5",
        "highlights": [
            "⚡ Dashboard widget toggles (Customize widgets) now add/remove a single tile in place instead of rebuilding and re-fetching every widget on the dashboard — turning one widget on or off no longer reloads the ones already showing. Reordering by drag-and-drop still does a full re-layout, since every tile's position changes.",
            "⚡ /api/stats is now cached client-side for a few seconds and shared between the topbar counts (loadStats(), refreshed on live events/track/favourite/etc.) and the Dashboard's Overview widget — switching to Dashboard right after a stats refresh elsewhere reuses that data instead of re-fetching it.",
            "📉 New: GET /api/favourites accepts an optional ?limit= — the Dashboard's Favourites widget now asks for only as many rows as it displays (3/6/10) instead of fetching every favourite and slicing client-side. Every other caller (the roster's Favourites view, etc.) is unaffected — omitting limit still returns everything, as before.",
            "📝 Clarified: added an explicit comment on syncStateToUrl() documenting that Dashboard intentionally ignores roster filter state (search/priority/category/location/live/sort/page) rather than clearing it — so URL state round-trips correctly if you switch back to the roster view.",
            "🔧 Confirmed populateFilterSelect() (shared by loadCategories()/loadLocations()) already covers the categories/locations dropdown-population duplication — no further change needed there.",
        ],
    },
    {
        "version": "v2.1.0",
        "highlights": [
            "🧲 New: Dashboard widgets can now be reordered by drag-and-drop — grab the ⠿ handle on any widget tile and drop it on another to swap positions, instead of layout being fixed to catalog order. Order is saved per device, same as the on/off toggle.",
            "🔢 New: per-widget size. Live Now, Favourites, Raid Candidates, and Recently Viewed each get a small entry-count selector (3/6/10) on the widget itself and in \"Customize widgets\", instead of a fixed 6 rows for every widget on every device.",
            "🔗 Improved: a Dashboard widget with nothing to show (or that failed to load) now offers a button straight to the relevant view — e.g. \"Favourite a streamer\" jumps to the roster, \"View raid candidates\" jumps to Raid — instead of just describing the empty/error state with no next step.",
            "📜 Improved: returning to the roster list — closing the detail panel, favouriting/un-favouriting from it, switching views and back — now restores your scroll position instead of snapping back to the top of the list.",
            "📏 Improved: Compact density now also shrinks card text size (username, category, and stat row), not just padding/avatar size, for genuinely higher information density rather than just tighter whitespace around the same-size text.",
            "🗂️ Change: Settings' Accessibility and Themes tabs are now combined into one \"Display\" tab, cutting Settings from 7 tabs to 6. Same controls, same behavior — just grouped with roster density (which stays on the roster toolbar, called out with a pointer from the new tab) instead of split across two separate tabs.",
        ],
    },
    {
        "version": "v2.0.1",
        "highlights": [
            "🐛 Fixed: rapidly toggling Dashboard widgets on/off (or switching away from Dashboard and back quickly) could let an older, slower widget fetch resolve after a newer one and overwrite it with stale data — Dashboard rendering now uses the same stale-response guard already used by the roster/Discover views.",
            "🐛 Fixed: clicking a streamer in a Dashboard widget fired two separate roster loads back-to-back (one from switching views, one from applying the search) — now a single load.",
        ],
    },
    {
        "version": "v2.0.0",
        "highlights": [
            "🧩 New: Dashboard view! A new 🧩 Dashboard rail item shows a home screen of widgets — Overview stats, Live Now, Favourites, Raid Candidates, and Recently Viewed — each toggleable on/off from a lightweight \"Customize\" panel so it only shows what matters to you. Layout preference is saved per device, same pattern as pinning and theme.",
            "📏 New: roster density toggle. A Compact/Comfortable switch next to Sort now controls row spacing on the roster grid — Compact shrinks card padding/stats for scanning a big roster, Comfortable keeps the existing spacing. Saved per device alongside the other display preferences.",
            "📍 Change: in Compact density, Location becomes a toggleable column — a \"Show location\" checkbox appears next to the density switch (compact mode only) so the 📍 location line can be hidden to save a row when it's not needed, without losing the existing manual/scraped distinction when it's shown.",
        ],
    },
    {
        "version": "v1.9.5",
        "highlights": [
            "🗺️ New: Raid Map! A new 🗺️ Raid Map button groups your tracked streamers into coarse regions (Americas, Europe, Asia, Australia, Pacific, etc.) derived from each streamer's on-file timezone, so you can see at a glance where your roster is concentrated. Click a region to drill into who's there, live status included.",
            "🔧 The manual location editor now offers a datalist of previously-used locations as you type — same one-click-suggestion pattern as Discover's category field, so entries stay consistent (e.g. reusing \"Auckland, NZ\" instead of accumulating near-duplicate spellings).",
            "🐛 Fixed: the manual timezone field accepted any string, not just real IANA timezone names — a typo or garbage value would save fine but then silently fail to format as a local time in the UI (showing the raw string instead). Both the single-streamer and bulk location endpoints now validate against Python's own timezone database before storing, rejecting anything unrecognized with a clear error.",
        ],
    },
    {
        "version": "v1.9.0",
        "highlights": [
            "🔧 Discover's per-result location enrichment (attaching this app's on-file location to already-tracked results) is now one batched `WHERE username IN (...)` lookup instead of a separate `streamer_exists()` + metadata query per result — same output, fewer DB round trips on a page full of tracked streamers.",
            "🔧 New `PUT /api/streamers/location/bulk` endpoint — applies one location/timezone pair to a list of usernames in a single call, for bulk-editing location across multiple tracked streamers at once instead of one request per streamer. Same field semantics as the existing single-streamer endpoint (either field optional, empty string clears, untracked usernames are skipped).",
            "🔧 Location filtering/sorting (roster, search, and the location filter dropdown's counts) now reads from a new stored `resolved_location` column instead of recomputing the manual/scraped fallback expression per row — keeps location queries index-backed as the roster grows, instead of a full table scan.",
            "🔧 The manual location editor's timezone dropdown is now grouped by IANA region (Pacific/, America/, Europe/, …) via `<optgroup>` instead of one flat list of ~400 zones — same options, easier to scan.",
            "🔧 Consolidated the roster/Discover category and location filter dropdown population (`loadCategories()`/`loadLocations()`) behind one shared helper — no behavior change, just less near-duplicate code to keep in sync.",
        ],
    },
    {
        "version": "v1.8.5",
        "highlights": [
            "🐛 Fixed: Discover's \"Location\" sort had no secondary sort key, so entries with identical or blank locations (e.g. every untracked live result, which carries no location at all) kept whatever arbitrary order they happened to be accumulated in. Ties now fall back to followers (high to low), same secondary key already used by the primary followers sort.",
            "✨ New: the manual Location field now shows an inline \"Saving…/Saved\" status next to the section title, matching the existing Notes field — feedback parity with the rest of the detail panel instead of relying on the toast alone.",
            "🔧 Manually-entered locations are now normalized before saving: surrounding/internal whitespace is trimmed and collapsed, and the text is title-cased. A case/whitespace near-duplicate of a location already on file (manual or scraped, any streamer) reuses that existing spelling instead of adding a second, differently-formatted entry for the same place — keeps the location filter dropdown from accumulating things like \"Auckland, NZ\" / \"auckland, nz\" / \"Auckland,  NZ\" as three separate options over time.",
        ],
    },
    {
        "version": "v1.8.0",
        "highlights": [
            "🐛 Fixed: a failed `GET /api/locations` load (e.g. a transient network/server error) was silently swallowed in the frontend, leaving both the roster's and Discover's location filter dropdown looking identical to a roster with no locations on file yet — same class of bug as the earlier Suggested-For-You fix. Both dropdowns now show a small ⚠ next to them on a load failure, cleared automatically on the next successful load.",
            "🔧 `GET /api/categories` and `GET /api/locations` now accept `limit`/`offset` query params (default 200, max 1000) and cap their result sets accordingly — both were previously unbounded `GROUP BY` queries over the full streamers table, fine at small scale but unnecessary load on a very large roster. Defaulted generously enough that this is a no-op for any roster under 200 distinct categories/locations, which is effectively every current install.",
            "🔧 The manual location editor's timezone dropdown (`Intl.supportedValuesOf(\"timeZone\")`) is now resolved once at module load and cached, instead of being recomputed on every detail-panel render — the browser's supported-timezone list is fixed for the life of the page, so re-deriving it per render was wasted work.",
            "📝 Clarified in code (no behavior change): Discover's client-side-only location filter (`discoverState.locationFilter`) is intentionally excluded from `currentDiscoverFilters()`/saved search history, since it only re-filters already-tracked results on screen and never reaches `/api/discover` or Twitch. Documented exactly what to add (and where) if it's ever promoted to a real server-side filter, so that future change persists it the same way every other Discover filter already is.",
        ],
    },
    {
        "version": "v1.7.5",
        "highlights": [
            "✨ New: relative local-time toggle. The 📍 location/timezone line on a streamer card and detail panel is now a collapsible badge — expand it to flip between an absolute local time (\"3:45 PM local\") and a relative one (\"+5h\", \"-8h\") with a small switch, per streamer card. Kept client-side (localStorage, same pattern as 📌 pinning) since it's a per-device display preference, not shared data.",
            "✨ New: ✕ clear button next to the manual Location field in the detail panel's Location section — matches the roster card's existing ✕ (stop tracking) affordance instead of requiring the text to be deleted by hand. Clears both the location text and the timezone dropdown in one click and saves immediately.",
            "✨ New: manual vs. scraped location is now visually distinguished — the manual location line shows a ✏️ icon and the scraped one shows 🌐, instead of both rendering identically with a plain 📍, so which value is authoritative is clear at a glance.",
            "✨ New: timezone auto-suggest while typing a manual location. Typing a free-text location (e.g. \"Lisbon, Portugal\") now auto-guesses and pre-selects the timezone dropdown using the same city/region lookup table already used for scraped bio locations (`GET /api/guess-timezone`, wrapping `twitch_api._guess_timezone` — no new lookup table). Only pre-selects when the dropdown hasn't already been set for that streamer, so it never overwrites a timezone someone picked by hand.",
        ],
    },
    {
        "version": "v1.7.0",
        "highlights": [
            "✨ New: manual location editing. The streamer detail panel now has a Location section — a free-text field for wherever you know a streamer is actually based (not dependent on a bio 📍 pin being found or recognized) plus a timezone dropdown for the local-time display. A manually-entered location always takes precedence over the scraped one for display, filtering, and sorting, but never overwrites or blocks it — the scraped value (when there is one) still shows underneath for reference, and re-scraping a bio never touches the manual fields, same as the existing manual-vs-scraped split for social links.",
            "✨ New: location is now a filter/sort option on the main roster, styled the same as the existing category dropdown — a new 'Any location' dropdown in the left rail filters to streamers sharing a location (manual, falling back to scraped), and 'Location' is now a Sort by option.",
            "✨ New: Discover also gets a location filter/sort. Live Twitch search results don't carry a location of their own (Twitch's stream-search API doesn't expose one, and bio-scraping every result would be far more Twitch calls than results shown), so this applies to already-tracked results shown in Discover, which now carry whatever location this app already has on file for them — untracked live results simply show none. Both the new Location filter dropdown and 'Location' sort option only affect the accumulated results already on screen, the same way the existing followers sort does, and don't change what gets fetched from Twitch.",
            "🔧 New endpoints: `PUT /api/streamers/{username}/location` sets the manual location/timezone; `GET /api/locations` lists tracked locations with counts for the filter dropdowns (mirrors the existing `GET /api/categories`).",
        ],
    },
    {
        "version": "v1.6.2",
        "highlights": [
            "🐛 Fixed: streamer location scraped from a Twitch bio's 📍 pin (e.g. \"📍 Lisbon, Portugal\" or \"📍 Cape Town, South Africa\") wasn't showing on the roster card or detail panel at all for a lot of streamers — not just missing a local time, missing entirely. Root cause: `_extract_location` required the location to also resolve in the built-in city/timezone table before it would return anything, so a perfectly good self-reported 📍 location got thrown away whenever that specific place wasn't one of the ~90 entries in the table. A 📍-marked location is now shown on its own once found in the bio; only the local-time part next to it still depends on that place being recognized in the timezone table (shown when it is, left blank when it isn't, same as before).",
            "🐛 Fixed: a failed Suggested For You request (`/api/suggestions`) was silently swallowed in the frontend, so a real error looked identical to the normal \"favourite a few streamers to get suggestions\" empty state, with nothing on screen to tell them apart. It now shows its own distinct \"couldn't load\" message. Note: the suggestion-matching logic itself (category/tag overlap against your favourites) was tested against several favourites/candidate scenarios and produced correct results in all of them — if suggestions are still empty for you with favourites that do share a category or tag with other tracked streamers, that's now visibly a load failure rather than a silent one, which should help narrow down what's actually happening.",
        ],
    },
    {
        "version": "v1.6.1",
        "highlights": [
            "🐛 Fixed (packaged Windows build, root cause): a custom social icon set via \"Image URL\" (Settings → Icons) could still fail to survive an app restart even after the v1.5.0 WebView2-profile-location fix — most noticeable when testing a freshly-typed URL right before closing the app. Root cause: `webview.start()` defaults to `private_mode=True`, which loads the window in an ephemeral/InPrivate WebView2 context and never persists localStorage to disk at all, regardless of where its profile folder is pinned — the earlier fix corrected *where* that profile would live but didn't stop the app from using a throwaway one. `private_mode` is now explicitly disabled with `storage_path` pinned next to the exe, so custom icons (and any other localStorage-backed setting) reliably persist across restarts.",
        ],
    },
    {
        "version": "v1.6.0",
        "highlights": [
            "🐛 Fixed: the roster card's ✕ (stop tracking) button required clicking through two confirmation dialogs instead of one. Root cause: both the button itself and its click handler were accidentally duplicated in the card markup/rendering code, so a single click fired two independent confirm() prompts. Removed the duplicates — one click now shows one confirmation.",
            "✨ New: 🌐 Re-scrape all button in the top bar re-scrapes bio, social links, and location for every tracked (non-archived) streamer in one request, instead of opening each streamer's card individually and hitting its own Re-scrape button.",
            "✨ New: the expanded streamer detail card now has a ✕ close button next to the streamer's name, so it can be closed directly instead of only by selecting a different streamer card.",
            "✨ New: streamer cards and the detail panel now show a 📍 location + local time when a streamer has a recognizable self-reported location in their Twitch bio (e.g. \"📍 London, UK\" or \"Based in Los Angeles\") — resolved to a timezone via a built-in city/region lookup, shown alongside the current local time there. Picked up automatically on scrape/re-scrape; left blank when nothing recognizable is found in the bio rather than guessing.",
        ],
    },
    {
        "version": "v1.5.1",
        "highlights": [
            "🐛 Fixed: Discover could return zero results for a category-based search (e.g. Just Chatting) even with filters that were verified to work moments earlier — Max followers, default Broadcaster type, or any other combination. Root cause: resolving a category name to its Twitch game_id was a single unretried lookup, unlike every other Twitch call in the app; one transient non-200 response from Twitch (a momentary blip, or a request that raced a token refresh from another in-flight call, e.g. the discover_match alert loop polling in the background) permanently failed that lookup with nothing logged as an error, which looked identical to \"no live streamers match these filters.\" The category lookup now gets one bounded retry before being treated as a genuine miss, matching the retry behavior already used for streams paging and rate limits.",
        ],
    },
    {
        "version": "v1.5.0",
        "highlights": [
            "✨ New: roster cards now have a ✕ button (top-right, on hover) to stop tracking that streamer directly from the grid — same confirm prompt and delete behavior as Settings → Streamer detail → Stop tracking, just without opening the card first.",
            "🐛 Fixed (packaged Windows build): custom social icons (Settings → Icons) reset to their defaults every time the app was restarted. Root cause: they're stored in the desktop window's localStorage, but pywebview's Edge WebView2 backend was picking its own profile folder by default, which wasn't guaranteed to resolve to the same real on-disk path on every launch of a frozen exe — the same root cause class as the v1.4.0 credentials bug, just for the browser profile instead of .env/streamers.db. The WebView2 user-data folder is now pinned to a stable location next to the exe, so localStorage (custom icons and any other browser-side settings) now persists across restarts. Running from source (a normal browser tab) was never affected.",
        ],
    },
    {
        "version": "v1.4.2",
        "highlights": [
            "🐛 Fixed: after adding a custom platform (Settings → Icons → Custom platforms), a streamer scraped *before* that platform existed wouldn't pick up a link/icon for it until the 6h social-scrape cache happened to expire on its own — the add/remove endpoints were only clearing the 30s pattern-recognition cache, not the separate 6h per-streamer scraped-links cache, so the new platform's label never made it into that streamer's scraped_social_links (and therefore never showed an icon) no matter how many times a custom icon image was set for it. Adding or removing a custom platform now also clears the scraped-links cache, so the next profile view re-scrapes with the current pattern set immediately.",
        ],
    },
    {
        "version": "v1.4.1",
        "highlights": [
            "🐛 Fixed: a saved Discover search/alert with Min viewers explicitly set to 0 could get silently stripped down to \"no min viewers filter at all\" when saved to search history or a discover_match alert rule — `0` and `False` were treated the same as a genuinely blank/omitted filter by a `value not in (None, \"\", False)` check, and in Python `0 == False`, so an explicit 0 got dropped instead of kept. Explicit `0`/`False` filter values are now preserved distinctly from omitted ones.",
            "🐛 Fixed: setting a custom social icon (Settings → Icons), including for a custom platform added via Settings → Icons → Custom platforms, saved correctly but didn't visually update existing roster cards — the save handlers were calling `loadCategories()`, which only refreshes the category filter dropdown, not the cards themselves. They now call the actual card-rendering function, so a newly-set (or cleared) custom icon appears on roster cards immediately instead of only after some unrelated action forced a re-render.",
        ],
    },
    {
        "version": "v1.4.0",
        "highlights": [
            "🐛 Fixed (packaged Windows build, root cause): Twitch Client ID/Secret still had to be re-entered on every app restart even after the v1.3.0 installer-location fix. The actual cause was that `.env`/`streamers.db` were located next to the *Python source files' own bundled location* inside the frozen exe (PyInstaller's internal extraction folder), which isn't guaranteed to be the same physical path on every single launch in every environment — so a save could land in one location while the next launch's read came from a different one, silently losing saved credentials (and, in principle, the roster) on restart. Both are now anchored to the actual running .exe's own folder instead, which is always the same real path across restarts, so this can no longer happen regardless of how PyInstaller unpacks things internally. Running from source was never affected.",
            "🐛 Fixed: Discover again returning no results for valid filters — same symptom as the v1.0.4 fix, but this time caused by the credential-persistence bug above: once saved Twitch credentials silently failed to survive a restart, every Twitch API call (including Discover's search) failed with no valid token, which looked identical to \"no streamers matched\". Discover's search logic itself (deep paging for narrow/low-viewer filters, extra candidate headroom for tag/follower/account-age/broadcaster-type filters) is unchanged from v1.0.4 and was verified still intact — this was a credentials issue, not a search-logic regression.",
        ],
    },
    {
        "version": "v1.3.0",
        "highlights": [
            "🐛 Fixed (packaged Windows build): Twitch Client ID/Secret entered in the app had to be re-entered every time the app was closed and reopened, instead of only on first run. The installer previously installed to Program Files, which a normal (non-admin) install/run isn't allowed to write to — saving credentials either silently failed or got redirected by Windows to a hidden per-user shadow copy that the next launch didn't reliably read back from, so the save looked like it worked but didn't stick. The installer now installs to a per-user location that's always writable without admin rights, so a saved Client ID/Secret (and the streamers.db roster) now persists across restarts as expected. Running from source (`uvicorn main:app`) was never affected by this.",
        ],
    },
    {
        "version": "v1.2.0",
        "highlights": [
            "🔗 New: custom social platforms. Settings → Icons → Custom platforms lets you add your own label + URL-matching pattern (e.g. \"Linktree\" / \"linktree.com/\") — matching bio/panel links found on future scrapes are then recognized and labelled just like the built-in platforms, and each custom platform gets its own row in the Social icons list below so you can also set a custom icon for it. Stored server-side (not per-device), and applies to every channel scraped afterward — existing scraped social links aren't retroactively relabelled until the next re-scrape.",
        ],
    },
    {
        "version": "v1.1.0",
        "highlights": [
            "🎨 New: custom social icons. Settings → Icons now lets you set your own icon per social platform (Twitter/X, Instagram, TikTok, YouTube, Discord, Facebook, Kick, Reddit, Threads, Bluesky) — paste an image URL or upload a small image file — instead of the built-in glyph. Applies to the scraped social-icon row shown on roster cards. Stored on this device only; leave a platform blank to keep using its default glyph.",
        ],
    },
    {
        "version": "v1.0.9",
        "highlights": [
            "⚡ Improved: first-time `build.bat` install is faster. Dependencies and PyInstaller were previously installed via two separate `pip install` calls, each paying its own dependency-resolution and already-installed-check overhead; they're now installed together in a single call, plus `--prefer-binary` so pip takes an available prebuilt wheel instead of a slower local sdist build when both exist. Build output/behavior is unchanged — same packages, same versions.",
        ],
    },
    {
        "version": "v1.0.8",
        "highlights": [
            "🐛 Fixed (root cause #3): the row of icon+label links on a channel's About page (e.g. 'instagram', 'tik tok', 'discord', 'wishlist') — Twitch's dedicated Social Links feature (Creator Dashboard → About → Social Links) — wasn't scraped at all. This is a separate, structured field on the channel, entirely distinct from bio text and custom panels, so no amount of text/Markdown scanning could ever have picked it up — the query never asked for it. Now queried directly and merged in alongside bio- and panel-sourced links, including links on platforms outside the app's recognized list (e.g. a wishlist), which are kept under the label Twitch itself gives them instead of being dropped.",
        ],
    },
    {
        "version": "v1.0.7",
        "highlights": [
            "🐛 Fixed (root cause #2): panel links where the clickable text is a label like 'Instagram' or 'Twitter' — not the URL itself — still weren't scraped even after the v1.0.6 request-blocking fix. Twitch panel/bio text is Markdown, and the standard way a 'Social Media' panel links out is `[Instagram](https://instagram.com/you)` — the URL only exists inside the parentheses, never in the visible text. Neither the raw-URL scanner nor the bare-domain scanner could see a URL hidden that way. Panel and bio text is now also scanned for Markdown-style links, so the URL is pulled out regardless of what the link's visible text says.",
        ],
    },
    {
        "version": "v1.0.6",
        "highlights": [
            "🐛 Fixed (root cause): social/panel link scraping — hyperlinks in a channel's About bio and clickable panel images that lead to social accounts (e.g. Instagram, Twitter/X) — wasn't finding links at all, even though the query and panel-parsing logic were correct. Twitch's gql.twitch.tv endpoint (used to read bio/panel data, since it isn't part of the public Helix API) rejects requests that don't look like they came from the twitch.tv web client itself; without a browser User-Agent and an Origin/Referer of twitch.tv, the request was being blocked before it ever reached the query, which made it indistinguishable from 'this channel has no social links set'. The lookup now sends the same headers the real twitch.tv site sends, so the request goes through and previously-added panel/bio parsing (including image panels used as link-out buttons) actually gets a chance to run.",
        ],
    },
    {
        "version": "v1.0.5",
        "highlights": [
            "🐛 Fixed: social links written as plain text with no 'https://' — e.g. a bio or panel that just says 'twitter.com/someuser' instead of the full link — weren't being picked up, even though Twitch's own page renders that text as a clickable hyperlink. Social-link scanning now also checks bio/panel text directly against each recognized platform (Twitter/X, Discord, Instagram, etc.), which already match on the domain itself, so a link hiding in unprefixed text is found the same as a full URL.",
            "🔍 Added: when a channel's profile panel is a type this app doesn't yet read content from (some older/legacy panel styles), that's now logged once so it's visible instead of silently showing up as 'no social links' with no indication why.",
        ],
    },
    {
        "version": "v1.0.4",
        "highlights": [
            "🐛 Fixed: Discover with a Max viewers filter (and no Min viewers set) in a large category — e.g. Just Chatting with Max viewers 10 — could still return zero results even with matching streamers live. Twitch returns live streams sorted by viewer count descending, so finding low-viewer streams means paging deep past every higher-viewer one first; the search was giving up after its normal page budget (1000 streams) before getting that far. This filter combo now searches a much deeper page budget (up to 4000 streams) instead of stopping early.",
        ],
    },
    {
        "version": "v1.0.3",
        "highlights": [
            "🐛 Fixed: Discover with a Broadcaster type filter (e.g. Standard) combined with a narrow viewer range and/or a large category (like Just Chatting) could return zero results even though matching streamers existed. Broadcaster type wasn't counted as a narrowing filter when deciding how many candidate streams to pull in before filtering, so a search for a less-common type in a huge category could exhaust its page budget before finding any match. Broadcaster type now gets the same extra headroom the other filters (tags, followers, account age) already got.",
            "🐛 Fixed: the Discover window's header and bottom buttons (Load more, Close) could be clipped off-screen once results loaded, since the modal had no height limit and just grew past the top and bottom of the window. Modals now cap their height to the window and scroll their contents internally, keeping the header and footer buttons always visible and in place.",
        ],
    },
    {
        "version": "v1.0.2",
        "highlights": [
            "🐛 Fixed: clicking outside a modal (e.g. Discover) while drag-selecting text to copy it would close the window — a text-selection drag that ended over the dimmed backdrop was being treated as a click on it. The backdrop now only closes the modal when both the press and release happened on the backdrop itself, so selecting text anywhere no longer accidentally dismisses the window.",
            "🐛 Fixed: Discover searches using a follower filter (Min/Max followers) always returned zero results. Twitch's follower-count lookup requires a broadcaster/moderator token that this app doesn't have, so every channel's follower count was silently coming back as 0 — which meant any 'min followers' above 0 rejected every live streamer. Follower counts that can't be looked up are now treated as unknown rather than 0, so they no longer get excluded by a follower filter; other filters (category, viewers, broadcaster type, language, tags, account age) were verified unaffected.",
            "🔎 New: the Discover category field now suggests popular categories (Just Chatting, League of Legends, VALORANT, Minecraft, and more) in a dropdown as you click or type — typing any other category to search manually still works exactly as before.",
        ],
    },
    {
        "version": "v1.0.1",
        "highlights": [
            "🎨 New: 10 more themes — Indigo, Chartreuse, Fuchsia, Gold, Cobalt, Plum, Seafoam, Rust, Graphite, and Blossom — each with its own matching text colors, alongside the existing 20 (30 total, plus System).",
            "🔤 Changed: the app now uses SF Pro app-wide for every UI/display/body text role (falls back to the closest equivalent system font on non-Apple platforms, then Inter). Numeric stats (viewers, followers, scores) keep IBM Plex Mono, unchanged.",
        ],
    },
    {
        "version": "v1.0",
        "highlights": [
            "🐛 Fixed: tracked streamers and the saved Twitch Client ID/Secret could reset on every app restart. .env was read from wherever python-dotenv's default upward directory search happened to find one, while saved credentials were written to the current working directory's own .env — a mismatch between those two paths meant a freshly-connected Twitch account (and, depending on setup, the streamers.db location) didn't reliably survive a restart. Both are now pinned to the same fixed location next to the app itself, so saved data persists as expected.",
            "🔢 Version reset back to 1.0 for this release.",
        ],
    },
    {
        "version": "v1.1",
        "highlights": [
            "🐛 Fixed: scraped social links (bio + profile panels) stopped coming back for every channel — the panel/bio lookup used Twitch's persisted-query cache, which had rotated out from under it; Twitch returned a 200 response with no data instead of an error, so the failure was invisible and every channel looked like it had no social links set. The lookup now sends full queries instead of relying on that cache, and a failed lookup no longer gets cached as a false 'nothing set' result.",
            "🐛 Fixed: a profile panel's social links could be missed if they were only in the panel's title text, or if the panel type stored its outbound link somewhere other than the image-link field.",
            "🐛 Fixed: a panel linking to a *different* Twitch channel (e.g. a co-host or raid target) was always dropped — only the channel's own Twitch link is excluded now.",
        ],
    },
    {
        "version": "v1.0 (previous)",
        "highlights": [
            "🐛 Fixed: rapid roster/favourites/raid/inactive/archived list reloads could race — a slower, older request occasionally resolved after a newer one and silently overwrote it with stale data. This was most noticeable right after the live event feed triggered a background reload while you were also paging or changing filters. The list now ignores any response that's been superseded by a newer request, matching the fix already in place for Discover.",
            "🔒 Locked as release version 1.0.",
        ],
    },
    {
        "version": "prerelease v13.0",
        "highlights": [
            "🎥 Improved: YouTube/Kick roster tracking now uses the existing bio/panel scraper — if a channel's Twitch About-tab or profile panels link out to YouTube or Kick, that link auto-fills the streamer's youtube_url/kick_url tracking fields the next time a scrape runs (on add, on manual re-scrape, or the periodic 6h background refresh), instead of requiring manual entry. A field only gets filled while it's still blank — a manually-entered or previously-scraped YouTube/Kick link is never overwritten. x_url/instagram_url remain manual-only, unchanged.",
        ],
    },
    {
        "version": "prerelease v12.5",
        "highlights": [
            "🎥 New: multi-platform presence tracking — a streamer's roster entry can now also hold their YouTube and Kick links (Tags & links section in the detail panel), alongside the existing Twitch/X/Instagram fields, so their presence across platforms lives in one place.",
            "🎨 Improved: All Streamers cards are bigger — larger profile pictures and more padding, with room for a new \"IG: @handle || Twitter: @handle\" line showing the manually-entered Instagram/X links right on the card.",
            "🔄 Changed: the manually-entered social field previously labelled Discord is now Instagram (Settings/detail panel \"Discord invite/profile URL\" field), since the app has never actually scraped Discord links — Instagram is one of the platforms it does recognize. Existing saved links carry over unchanged into the new field.",
        ],
    },
    {
        "version": "prerelease v12.0",
        "highlights": [
            "🐛 Fixed: All Streamers (and every other list view) had no way to manually reload — the only way to see newly-tracked or updated streamers was to switch to a different tab and back. A refresh button next to the sort controls now reloads the current view on demand.",
            "🐛 Fixed: clicking a Discover result's Track button in quick succession could leave it showing \"Track\" even though the streamer was already tracked. The button now ignores repeat clicks while a request is in flight, instead of relying on timing alone.",
            "🎨 Moved: the sort dropdown and ascending/descending button now sit next to the Prev/Next page buttons in the main view's header, instead of the left rail — sorting and paging controls now live in one place.",
            "✏️ Clarified: Discover's Include/Exclude tags fields now say \"comma-separated\" directly in the label, with an example showing multiple tags.",
            "🎨 Improved: Discover result cards are bigger and less cramped — larger avatars, more padding, and a wider modal.",
        ],
    },
    {
        "version": "prerelease v11.5",
        "highlights": [
            "🖥️ New: the packaged Windows build (`ScoutBot.exe`) is now a real native desktop app — it opens in its own window (Edge WebView2) instead of a browser tab, with no address bar or browser chrome. Everything else works exactly as before: live updates, streamer previews, and links to Twitch/X/Discord still open in your default browser. Settings → General → Exit ScoutBot (or just closing the window) still shuts everything down cleanly. Running from source is unaffected — `uvicorn main:app` still opens in a normal browser tab as documented in the README.",
        ],
    },
    {
        "version": "prerelease v11.0",
        "highlights": [
            "🚫 New: Streamer blacklist — a hard exclude, separate from the existing \"Ignore\" priority. Blacklist a streamer from Discover results or Settings → Blacklist and they'll never reappear in Discover again, whether or not they've ever been tracked.",
            "📦 New: ScoutBot can now be built into a standalone Windows .exe (see build/build.bat) with its own app icon — same PyInstaller + Inno Setup pipeline WhiteBoard uses.",
            "♿ Improved: accessibility pass — icon-only buttons (favourite, pin, sort direction, refresh, delete) now have proper labels for screen readers, and every modal now traps keyboard focus (Tab/Shift+Tab cycles inside it, Escape closes it, focus returns to what opened it) instead of leaking focus to the page behind it.",
        ],
    },
    {
        "version": "prerelease v10.0",
        "highlights": [
            "🔍 New: Discover can now filter by account creation date (\"Account created after\"/\"before\"), so results can be narrowed to newer or older Twitch accounts.",
            "🔍 New: Discover now supports Exclude tags alongside the existing (renamed) Include tags field — e.g. exclude 'vtuber' to filter vtuber streamers out of results.",
        ],
    },
    {
        "version": "prerelease v9.5",
        "highlights": [
            "🐛 Fixed: Discover search results could show the same streamer twice — Twitch's live streams list can shift between page fetches (viewer counts changing reorders it), so consecutive \"Load more\" pages occasionally overlapped. Results are now de-duplicated as pages come in.",
            "🐛 Fixed: scraped social links (bio + profile panels) almost never came back — the profile-panels lookup was keyed by the wrong field and silently matched nothing. Panel links (Instagram, YouTube, etc.) now scrape correctly alongside bio links.",
            "🔤 Changed: the scraped social link label for X/Twitter is now \"Twitter / X\" (was \"X (Twitter)\") for consistency.",
            "🎨 Improved: Discover results now lay out as a grid instead of a single scrollable column, so more streamers are visible at once.",
        ],
    },
    {
        "version": "prerelease v9.0",
        "highlights": [
            "🐛 Fixed: a streamer going offline could keep showing as Live on the roster for a while — only \"went live\" events refreshed the roster in real time, so an offline transition sat unreflected until some other streamer's live event happened to trigger a reload. Going offline now pushes its own instant update.",
            "🐛 Fixed: an open live-preview embed on a card could silently vanish (back to a collapsed \"▶ Preview stream\") whenever the roster refreshed in the background from another streamer's live/offline event. Open previews now survive those refreshes.",
            "🔧 Improved: the live-preview embed now also passes \"localhost\" and \"127.0.0.1\" as accepted parent domains, so the preview loads regardless of which of the two the app happens to be reached through.",
        ],
    },
    {
        "version": "prerelease v8.5",
        "highlights": [
            "🔗 New: streamer cards in All Streamers now show social links scraped automatically from the channel's Twitch bio and profile panels (X, Discord, YouTube, Instagram, TikTok, and more) — no manual entry needed. The detail panel shows the scraped bio too, with a manual re-scrape button; this is separate from the existing manual Social links fields, so it never overwrites anything typed in by hand.",
            "📺 New: live streamers in All Streamers now have a Preview stream toggle that drops in a small muted embed right on the card — a quick peek at what's happening before opening the channel.",
            "🎨 New: 10 more themes — Amber, Crimson, Aurora, Coral, Lavender, Mint, Steel, Nord, Solaris, and Grape — each with matching text colors, alongside the existing 10.",
            "🔧 Fixed: the packaged Windows installer could overwrite an already-installed .env (and the Twitch credentials saved in it) when updating to a new version. Updating now leaves an existing .env untouched.",
        ],
    },
    {
        "version": "prerelease v8.0",
        "highlights": [
            "🐛 Fixed: version numbers showed a stray extra \"v\" (like \"vprerelease v7.5\") in the What's new panel, the Settings changelog, and the top bar.",
            "🐛 Fixed: the \"What's new\" panel is now a compact box with a version dropdown, like the one in Settings, instead of one long scrolling wall of text.",
            "🐛 Fixed: the changelog's Next button could jump to the wrong version's notes in some cases — Prev/Next now always stay on the right page.",
            "🔍 New: sort Discover results by follower count (high to low or low to high), right from the results list.",
            "✅ Improved: pressing Track in Discover results now flips the button to a red Untrack button, so it's clear at a glance and you can undo it in one click.",
            "✨ Simplified the wording used throughout this changelog to make it quicker to read.",
        ],
    },
    {
        "version": "prerelease v7.5",
        "highlights": [
            "🔐 New: optional login for the app. Before this, anyone who could reach ScoutBot (once it wasn't just on your own computer) could see and change everything, with no password needed. Set WEB_USERNAME and WEB_PASSWORD in .env to require a username and password; it's off by default for the normal local setup. Settings → General now shows whether login is on, and warns you if the app can be reached from elsewhere without one.",
        ],
    },
    {
        "version": "prerelease v7.0",
        "highlights": [
            "🔢 Changed: the app is now in prerelease, so every change bumps the version by 0.5 (this and all older versions are now labelled \"prerelease vX.X\") until version 1.0 is locked in.",
            "📄 Improved: the Changelog tab in Settings now has Prev/Next buttons so you can page through older versions instead of scrolling one giant list.",
        ],
    },
    {
        "version": "prerelease v6.5",
        "highlights": [
            "🐛 Fixed: the text-size setting (Normal/Large/Larger) in Accessibility didn't actually change anything. It now properly makes text bigger across the whole app.",
            "🎨 New: 10 themes to pick from in Settings → Themes (Dark, Light, Midnight, Slate, Forest, Sunset, Ocean, Rose, Sepia, Mono), plus System to match your OS.",
            "📈 New: Session replay — pick one of a streamer's past streams in their detail panel to see that stream's full viewer count over time, not just the peak and average.",
            "🔽 Changed: the Changelog tab in Settings now uses a dropdown to step through versions one at a time (latest first) instead of one long list.",
            "✨ Every changelog entry now has an emoji, making it quicker to scan.",
        ],
    },
    {
        "version": "prerelease v6.0",
        "highlights": [
            "🕒 New: Discover search history — recent Discover searches are remembered so you can rerun one from the modal instead of re-entering filters every time.",
            "👀 New: Recently viewed — a quick-access list of the streamers you've opened lately, right in the rail.",
            "💡 New: Suggestions — \"similar to your favourites\", ranked by shared category/tags across your favourited roster. Appears wherever you'd browse for new streamers to track.",
            "🧱 Under the hood: every API error now returns a consistent shape (a stable `error.code` alongside the existing message) instead of ad hoc error strings, and new endpoints validate their inputs through a shared layer instead of scattered manual checks.",
        ],
    },
    {
        "version": "prerelease v5.5",
        "highlights": [
            "🔔 New: Alert rules! Get notified when a specific tracked streamer goes live, or when a saved Discover search matches — deliverable via in-app browser push, a webhook (POST to any URL), and/or email (needs SMTP configured in .env). Manage rules from the new Alerts button in the top bar.",
            "🤝 New: Response tracking — log whether a scouted streamer has followed back, replied, reacted, declined, or not responded, right from their detail panel. Builds a timestamped history per streamer instead of a single status, so you can see how outreach played out over time.",
            "⚡ Discover results are now cached for 30s per unique filter combo — reopening the Discover modal or repeating the same search no longer re-hits Twitch every time. \"Load more\" pages always fetch live, since a pagination cursor is only valid once.",
            "🔗 Roster filters/sort/search/page are now reflected in the URL — links to a specific filtered view are shareable, and browser back/forward moves through filter changes instead of leaving the page.",
        ],
    },
    {
        "version": "prerelease v5.0",
        "highlights": [
            "🐛 Fixed: the packaged Windows app (ScoutBot.exe) could crash on launch with \"Unable to configure formatter 'default'\" — uvicorn's own startup logging tried to check whether the (nonexistent, windowed-build) console was a terminal and crashed. The app now uses ScoutBot's own log setup instead, exactly as before.",
            "📋 New: Outreach status! Beyond Archived/Active, streamers can now be marked Contacted, Raided, or Declined from the detail panel — track exactly where each one sits in your raid outreach pipeline.",
            "👀 New: Category watchlists — follow a Twitch category (not just individual streamers) from Discover and get a one-click check for new or trending channels streaming it since you last looked.",
            "💀 Better loading states: the roster, favourites, raid candidates, and other list views now show skeleton placeholder cards while fetching, instead of a blank grid.",
        ],
    },
    {
        "version": "prerelease v4.5",
        "highlights": [
            "🐛 Fixed: rapid Discover filter changes could race — an older, slower search occasionally landed after a newer one and quietly overwrote its results. Discover now cancels any in-flight search the moment a new one starts.",
            "📌 New: pin streamers! Click the pin on a card (or 'Pin to top' in the detail panel) to keep them at the top of the roster no matter what sort is active.",
            "🕒 New: each streamer's detail panel now shows their typical live hours in your local time, worked out from their recent stream sessions.",
            "🔍 Better empty states: Discover and the roster now explain *why* nothing showed up (filters too narrow, nothing tracked yet, etc.) and offer a one-click 'Clear filters' button instead of a blank message.",
            "🪟 Changed: the packaged Windows app (ScoutBot.exe) now launches fully windowed — no more cmd/console window popping up. Close it from Settings → General → Exit ScoutBot, same as before.",
            "✨ Simpler wording + emoji throughout the app and changelog, so it's easier to scan at a glance.",
        ],
    },
    {
        "version": "prerelease v4.0",
        "highlights": [
            "🐛 Fixed Discover returning \"No live streamers matched those filters\" whenever a follower filter narrowed out most of the current page — the extra-candidates headroom (already used for tag filtering) wasn't applied for follower filtering, so a min/max followers search could scan too few streamers to find any matches.",
        ],
    },
    {
        "version": "prerelease v3.5",
        "highlights": [
            "🔍 Discover can now filter by channel follower count (min/max), alongside the existing viewer range.",
            "🖼️ Discover results show each streamer's Twitch profile picture.",
            "🔗 Streamer names in Discover are now clickable links straight to their Twitch channel (opens in a new tab).",
        ],
    },
    {
        "version": "prerelease v3.0",
        "highlights": [
            "⚙️ New Settings menu (gear icon in the top bar) with General, Accessibility, Themes, Diagnostics, and Changelog tabs — Twitch connection, guest mode, exiting the app, font size, reduced motion, light/dark/system theme, and live diagnostics all live there now.",
            "💾 Saved filter presets — save the current search/priority/category/live-only/sort combination as a named view (e.g. \"Live + High priority + VALORANT\") from the rail, then reapply or delete it in one click.",
            "✏️ Inline editing for Priority and Tags directly on each streamer card — no need to open the detail panel just to reprioritise or retag someone.",
            "👤 Guest browsing — the whole app (roster, discover, stats, favourites, raid candidates) can be used without connecting Twitch credentials; only adding a new streamer requires a connection, and now says so clearly instead of failing with a generic error.",
            "📦 Packaged build: the console window now starts minimised (it's just uvicorn's log output — the browser tab is the real UI), and a new Settings > General > Exit ScoutBot button shuts the app down cleanly without having to dig the console back out of the taskbar.",
        ],
    },
    {
        "version": "prerelease v2.5",
        "highlights": [
            "🐛 Fixed the packaged Windows build serving a 404 for every page — the bundled frontend files weren't being found at runtime.",
            "📄 Discover can now page past its first ~100 live streamers — a \"Load more\" button fetches the next page instead of stopping there.",
            "✅ Actions like favouriting, saving tags, and saving a note now show a toast confirmation instead of relying on the UI just changing.",
            "📝 Notes got a clearer label and hint text distinguishing them from Tags — notes are free-text scouting context, tags are filterable labels.",
            "⏳ Twitch rate limits (429s) are now surfaced in the UI with a retry-after message instead of the app silently retrying and appearing to hang.",
        ],
    },
    {
        "version": "prerelease v2.0",
        "highlights": [
            "🔑 Twitch Client ID/Secret can now be entered right in the app on first run — no more hand-editing .env.",
        ],
    },
    {
        "version": "prerelease v1.5",
        "highlights": [
            "🔗 Streamer profiles now show X (Twitter) and Discord links alongside Twitch, when set.",
            "⚡ Quick-add from URL: paste any twitch.tv/<username> link into the Track Streamer field and it's parsed automatically.",
            "📦 Added a packaged Windows installer (PyInstaller + Inno Setup) — see build/ for build.bat.",
            "📰 Added this in-app changelog.",
        ],
    },
    {
        "version": "prerelease v1.0",
        "highlights": [
            "🚀 Initial web release: roster, favourites, raid candidates, discovery, live SSE feed.",
        ],
    },
]
