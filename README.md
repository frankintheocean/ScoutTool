# 🔭 ScoutBot

A Windows desktop app for discovering Twitch streamers and organising a roster, watchlists and alerts.

## ✨ Features

- 🔎 Search Twitch discovery or sample zero-viewer streams.
- ⭐ Track channels with notes, ratings, tags and saved views.
- 🪪 Keep verified Twitch IDs and observed username changes together.
- 📋 Compare streamers, manage watchlists and review alerts.
- 🛡️ View attributed BetterBanned activity and saved snapshots.
- 💾 Import an existing setup and use verified database backups.

Zero-viewer search uses nobody.live without Twitch credentials; tracking and Twitch lookups need your configured Twitch connection. Provider data can become stale or unavailable. BetterBanned may require browser verification; the app does not bypass it.

## 📥 Install on Windows

Use a Windows build containing `dist\ScoutBot.exe`, then run **install.bat**. Setup installs per user, creates shortcuts and can update/repair an existing installation.

To build from source on Windows 10/11, install **Python 3.11+** with the Python launcher and run:

```bat
build-windows.bat
install.bat
```

The build bundles the frontend and Python backend. Native presentation uses WebView2.

## 🚀 Get started

Open **Discover**, choose a discovery tab and use **Search / Refresh**. Track channels to add them to your roster. Settings, watchlists, alerts and other actions are under **Tools**.

To bring across an existing setup, open **Settings → Import**, select your `.env` and/or `streamers.db`, review the validation and confirm.

Keyboard shortcuts: **Ctrl+K** command palette, **Ctrl+F** roster search, **Ctrl+Shift+F** Discover.

## 💾 Data & uninstall

App data lives in `%LOCALAPPDATA%\ScoutBot\Data`. Reinstalling or uninstalling preserves your roster, credentials and browser state. Use the installed uninstall shortcut or the repository's `uninstall.bat`. Remove the retained data folder separately if you want to erase personal data.

## 🧪 Checks & help

Existing test records live in [tests/](tests/); this documentation update does not establish new runtime validation.

📚 [Full usage guide](docs/USER_GUIDE.md) · [Audit](AUDIT.md) · [Performance review](PERFORMANCE_REVIEW.md) · [Third-party notices](THIRD_PARTY_NOTICES.md) · [License](LICENSE)
