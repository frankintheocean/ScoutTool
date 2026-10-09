<!-- Detailed instructions preserved from the previous root README. -->

# ScoutBot Standalone Windows App

ScoutBot is a tool designed to help you find new Twitch streamers to watch, packaged as a standalone Windows desktop application. The existing Scout frontend, FastAPI backend, Twitch tracking/discovery features, alerts, watchlists, VOD/clip downloader, settings, import/export behavior, changelog, local persistence, and native WebView2 presentation are retained.

## Zero-viewer discovery

Open **Discover** in the header or left rail, then choose **Twitch Discover** or **Zero-viewer discovery**. The tabs retain separate filters and results. Zero-viewer discovery uses the roster's filter-and-card layout with stream thumbnails. Enter terms such as `mario speedrun` and choose **All terms** or **Any term**. Search phrases match games and tags, not stream titles. A blank phrase finds any zero-viewer stream. **Search / Refresh** draws another sample; duplicate and blacklisted channels are removed. Enable **Remember my filters** to restore the phrase and matching mode in this browser on future launches; unchecking it removes those saved filters.

Active roster and discovery filters appear as removable chips. **Clear filters** affects the current tab; clearing zero-viewer filters also updates remembered filters. Switching discovery tabs cancels pending searches without clearing completed results. Secondary header actions (Settings, Watchlists, Alerts, Raid Map, re-scraping, and What's new) are in **Tools**.

Zero-viewer discovery needs internet access to `https://nobody.live/stream`, but no Twitch credentials to search. Results reflect the provider's latest scan, so viewer counts and live status can change. Tracking a result uses ScoutBot's existing Twitch connection. The module reports provider outages/timeouts and lets you retry.

Twitch Discover shares identical in-flight searches and reuses raw stream pages and user lookups for up to 30 seconds across overlapping filter combinations. This reduces API use while preserving search matching and pagination. Tracking/location/blacklist status is refreshed from the local database on every response. Completed zero-viewer samples are not cached; each refresh can draw a new sample. These optimizations reduce quota use but cannot eliminate Twitch's API limits.

## Twitch identity and username history

Streamer details now show the current username and saved numeric Twitch ID. Tracking resolves saved IDs rather than relying on old usernames; confirmed renames retain the same local notes, ratings, sessions, and alerts. **Check username** requests a fresh Twitch lookup. **Save ID** verifies an entered numeric ID with Twitch before saving it; an already saved ID cannot be replaced with a different account. These lookups need your configured Twitch connection. An unavailable account does not prove a rename or ban, and ScoutBot does not guess a replacement account.

Twitch does not supply historical usernames. Known names from before tracking can be added manually and are labelled unverified, with an unknown change date. Confirmed changes record when ScoutBot observed them, rather than claiming the exact rename time.

## BetterBanned activity and bans

In streamer details, **Open user in BetterBanned** opens the current channel's `betterbanned.com/en/streamer/` page in a separate browser tab. **Refresh from BetterBanned** requests its public page on demand; it does not run for every channel in the background. The section displays the supplied total-ban count, dated recent activity, and a **Ban history** filter for bans and unbans, including any supplied reasons and durations. The visible section may cover only part of the channel's history; a missing total is shown as unknown.

BetterBanned can require browser verification or block automated access. If refresh fails, the saved snapshot remains available. Open the channel page, copy **Total Bans** plus the **Recent Activity** heading and dated rows, then use **Save copied BetterBanned page text**. Copied snapshots are explicitly labelled unverified. Fetched data is attributed to BetterBanned, a third-party source, rather than Twitch. Snapshots persist across restarts and database backup/import. Activity saved under an earlier username remains labelled with that name until refreshed. No browser verification is bypassed.

## Import an existing ScoutBot setup

ScoutBot can import an existing setup from inside the app. Open **Settings → Import**, choose your existing `.env` file and/or `streamers.db`, then click **Import selected files**. The files are checked before they replace the current ScoutBot data.

The Windows installer uses plain-language status messages, a live progress bar, elapsed time, and an estimated time remaining. It also creates a Desktop shortcut and Start Menu entry using the standalone ScoutBot icon. ScoutBot uses its own Windows AppUserModelID and a dedicated radar/target icon so its taskbar identity stays separate from Dashboard.

## Install

1. Obtain a Windows build containing `dist\\ScoutBot.exe`.
2. Double-click `Install ScoutBot.bat`.
3. The installer places ScoutBot under `%LOCALAPPDATA%\\Programs\\ScoutBot`, creates Start Menu and Desktop shortcuts, and creates an `Uninstall ScoutBot.bat` entry.
4. Launch ScoutBot from the shortcut.

The install is per-user and does not require administrator privileges.

## Build the Windows executable

This source archive is build-ready. On Windows 10/11 with Python 3.11+ and the Python launcher available, run `build-windows.bat`. It creates `dist\\ScoutBot.exe` using PyInstaller.

The packaged executable contains the frontend and Python backend. Runtime data is kept separately under `%LOCALAPPDATA%\\ScoutBot\\Data`, so reinstalling or replacing the executable does not erase `.env`, `streamers.db`, WebView2 storage, or other user state.

## Uninstall

Use the Start Menu/Desktop uninstall shortcut or run `Uninstall ScoutBot.bat` from the installed application directory. The uninstaller removes the application and shortcuts but intentionally preserves `%LOCALAPPDATA%\\ScoutBot\\Data` so a reinstall does not destroy the Scout roster or Twitch credentials.

To remove all ScoutBot data as well, delete `%LOCALAPPDATA%\\ScoutBot\\Data` after uninstalling.

## Performance and recovery features

The standalone build is optimized for larger rosters without replacing the existing Scout architecture.

- FTS5-backed search covers usernames, aliases, notes, categories, locations, and tags, with targeted SQLite indexes for common sort/filter paths.
- Search requests cancel stale work and use a short debounce so rapid typing does not queue obsolete requests.
- Search operators include `live:true`, `followers:>10000`, `followers:<500`, `priority:high`, `category:valorant`, `location:nz`, and `tag:fps`.
- Show All uses bounded concurrent pages and virtualized rendering rather than creating thousands of DOM nodes.
- Ctrl+K opens the command palette. Ctrl+F focuses roster search. Ctrl+Shift+F opens Discover.
- Select streamers for side-by-side comparison or batched priority/tag edits.
- Settings → Diagnostics provides database integrity, storage, backup, job, Twitch, and runtime health information.
- Database backups are verified with SQLite integrity checks and retained as rolling recovery points.
- Settings → Import has a validation preview and automatically creates a safety backup before replacing `streamers.db`.
- Existing density controls, saved views, alerts, watchlists, virtual scrolling, themes, and WebView2 persistence remain intact.
- Re-running `install.bat` safely performs an update/repair while preserving user data and creating an installer safety copy of the database.
