# ScoutBot Standalone Windows App

ScoutBot is the Scout module extracted from the Dashboard and packaged as a standalone Windows desktop application. The existing Scout frontend, FastAPI backend, Twitch tracking/discovery features, alerts, watchlists, VOD/clip downloader, settings, import/export behavior, changelog, local persistence, and native WebView2 presentation are retained.

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
