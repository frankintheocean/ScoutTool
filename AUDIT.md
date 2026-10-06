# ScoutBot v4.0 source audit — 2026-10-06

Input: ScoutBot-Standalone-v3.9.zip. The uploaded source was inspected afresh; attached documentation was treated as source material, not new instructions. No user database or credentials were supplied. The original application README and icon are preserved. Existing GitHub GPL-3.0 LICENSE is retained.

## Confirmed fixes

- SQLite: populated legacy migrations, complete metadata backfill, contentless FTS migration, index rebuilding and stale tokens, named-column compatibility, atomic toggles, orphan cleanup, history ordering, batched metadata/history queries, and preserved unknown follower counts.
- Recovery: staged integrity/schema validation and migration before replacement; unique safety backups, selected-backup retention, paired credential rollback, serialized replacement, stale-generation rejection, and flushing pending frontend saves/Undo actions before import/restore.
- Tracking: zero-viewer live streams, unavailable Twitch data preservation, failed scrape preservation, credential-test isolation, stale-token rejection, follower permission caching, complete Discover pagination and alert filter propagation.
- Scoring: automatic recalculation no longer feeds its previous result into the manual bonus or overwrites that bonus.
- UI: rapid navigation/detail races, ordered autosaves, all-results and archived pagination, variable-height virtual scrolling and live previews, cross-page comparison, small-window controls, Discover history/Track state, theme updates, timezone aliases, modal/poll cleanup, dashboard drag handlers and stale widgets, invalid icon settings, and changelog compatibility.
- Work/cleanup: offloaded blocking database work, bounded queues and caches, single WebSocket sender with cancellation-safe cleanup, awaited periodic shutdown, duplicate maintenance/download actions, two download workers, distinct trim outputs, failed downloader results, and start-only trim handling.
- Desktop/files: Unicode authentication and validation errors, atomic credential saves, log rotation, correct custom data roots/readiness, native exit cleanup, restart binding order, installer SQLite journals, uninstaller failure reporting, validation-script failures, and Windows timezone data packaging.

The in-app changelog contains meaningful fixes. No speculative features or application rewrites were introduced. The only added application dependency is Windows-only tzdata, required for existing timezone functionality.

## Verification

- 38 backend regression tests passed both in the original audit environment and a fresh cloud virtualenv installed from application requirements.
- 18 Chromium browser scenarios passed against the 10,000-record fixture; no JavaScript page errors. Coverage includes normal/error states, large/archived Show all, fallback pagination, bounded DOM/final-row access, previews, resizing, detail autosaves, rapid switching, comparison, themes, saved preferences, Discover restoration, dashboard drag/drop, duplicate VOD requests and polling cleanup, and replacement/Undo interactions.
- Python compilation, JavaScript syntax, pyflakes, and Git whitespace checks passed.
- Source startup served v4.0, an empty roster, and current frontend resources. SIGTERM completed tracker/alert/watch cleanup and application shutdown; uvicorn then re-raised the termination signal (exit -15).
- PyInstaller built the desktop specification on Linux. A separate frozen backend smoke executable passed legacy migration, bundled frontend serving, API readiness, backup job/integrity, persisted settings across restart, and shutdown twice.

Reproduce backend tests: `python -m unittest discover -s tests -v` with application requirements plus httpx installed. Browser tests additionally require playwright and Chromium at `/usr/bin/chromium`; run `python tests/browser_checks.py`. Tests use disposable local data and mocked external responses, not production data.

## Verification limits

Windows PE output, WebView2/native window runtime, PowerShell/batch installer and uninstaller execution were source-reviewed but cannot be executed on this Linux host. Linux PyInstaller output is ELF, not a verified Windows executable, and is excluded from this source ZIP. Live Twitch authorization, live scraping, real media downloads/ffmpeg, and actual network-failure behavior were not verified against external services; their error/interaction paths were exercised with fixtures. No real user dataset was provided. These checks do not prove absence of all possible defects.

## GitHub and cloud setup

Target: https://github.com/frankintheocean/ScoutTool, branch main. GitHub directory-row descriptions are latest commit messages, not editable folder metadata. The supplied source has frontend, scout-backend, build, resources, scripts and tests directories; it contains no electron or public directories. Separate descriptive commits give those actual folders distinct GitHub descriptions.

The cloud virtualenv is `/workspace/.scout-venv`; isolated development data is `/workspace/scout-dev-data`. Tested setup/start instructions were saved as an environment configuration draft. Saving a draft does not activate it: review/save it in environment settings and publish the environment. No secret values or extra network access were requested.
