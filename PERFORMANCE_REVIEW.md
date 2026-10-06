# ScoutTool v4.3: identity and BetterBanned verification

Added persistent numeric Twitch IDs, current usernames, observed rename history, and separately labelled manual older names. Existing local username keys retain notes, ratings, sessions, and alerts. IDs are kept as strings to preserve large values; saved identities cannot silently attach to another account. Tracker lookups batch 100 IDs per upstream request, and unchanged identities avoid rebuilding their search index. Expected identity state and database generations protect newer checks from older in-flight responses.

Added an on-demand BetterBanned activity module and a current-username profile link. The parser contract is based on the user's verified screenshot: two total bans, four ban/unban records with reasons/durations, and nine picture-change records. The screenshot is a test fixture, never seeded into user data. Fetched and copied snapshots are separately labelled, persist through restart/backup/import, and remain visible on provider errors. A newer snapshot or confirmed rename prevents older requests writing their results. Requests use fixed validated provider URLs, normal TLS validation, two-fetch concurrency, deadlines, bounded response bytes, and existing in-flight coalescing. There are no new dependencies.

Validation: 86 backend tests and 33 Chromium scenarios passed. These include a 5,000-saved-ID fixture resolved in 50 upstream requests, the existing 10,000-streamer roster, copied activity filtering, precise IDs above JavaScript's integer limit, retained notes, updated Twitch/BetterBanned links, duplicate-action guards, provider HTTP errors/challenges/oversized responses/timeouts, legacy migrations, and snapshot/identity races. Both Linux PyInstaller targets built, and their packaged frontend assets match source byte for byte. Frozen runtime checks cover startup, legacy migration, activity/manual-name persistence, backup integrity, restart, and shutdown.

Limits: BetterBanned returned HTTP 403 browser verification in this environment; live retrieval and its real HTML markup cannot be verified here. The copied-page-text fallback is available and labelled unverified. A third-party snapshot may contain only part of a channel's history. Twitch supplies no pre-tracking username history; manual entries cannot establish it independently. Native Windows/WebView2 and installer execution require Windows and were not tested here. Existing v4.2 and earlier evidence follows.

# ScoutTool v4.2: discovery and layout verification

Focused changes address the reported header/toolbar sizing and zero-viewer title/tag overflow, unify discovery into independent tabs, add removable filter chips, and reduce duplicated provider work. No dependencies, scoring rules, database schema, or discovery matching rules were changed.

- Zero-viewer cards use roster typography, spacing, borders, and grid sizing while retaining stream thumbnails. Long titles, unbroken tags, and Unicode tags stay within cards.
- Header height adapts to wrapping; toolbar controls have consistent heights. Secondary actions remain available through Tools, including keyboard focus restoration after dialogs close.
- CSS zoom now adjusts app/dialog viewport limits, keeping larger-text dialogs inside the window.
- Clear filters is scoped to its tab, updates remembered zero-viewer filters, and cancels pending roster search debounces.
- Identical in-flight searches share one task. Cancelling one caller does not cancel another caller's search. Failed requests are not cached, invalidation prevents old tasks repopulating the cache, and shutdown cancels and joins pending provider work before closing sessions.
- Raw Twitch stream pages and search user batches reuse the existing bounded, 30-second cache. Local tracking/location/blacklist data is rechecked for each response. Cursor result pages and completed random zero-viewer samples are not cached.
- Fixtures verify 20 simultaneous identical searches invoke the provider once and two different local filter combinations fetch their common Twitch stream page once.

Validation: 67 backend tests; 30 Chromium scenarios including the 10,000-streamer roster, persistence, autosave, drag/drop, previews, cancellation, errors, filter independence, Tools, and 600/900/1360px windows at smaller/normal/larger text sizes. Build and frozen runtime checks use Linux; Windows/WebView2 and the Windows installer still require verification on Windows. Fixture tests cannot establish live Twitch quota availability. The desktop PyInstaller target built successfully; every packaged frontend asset was compared byte for byte with source. The frozen backend passed legacy migration, public zero-viewer discovery, backup/integrity, settings persistence across restart, and shutdown twice.

The v4.1 review below is retained as historical evidence.

# ScoutTool v4.1: performance review and zero-viewer discovery

Reviewed the uploaded `ScoutTool.zip` against its 39-file v4.0 baseline, commit `1c3c65ef744029197d505ceca5ea5933e216ad43`. It matches that baseline byte for byte. Changes are focused on confirmed defects, behavior-preserving optimizations, and the subsequently requested discovery module. Existing scoring weights, search filters, data retention, dependencies, and the normal Twitch Discover workflow are preserved.

## Implemented module

**Zero-viewer streams** in the left rail opens an independent module. Search phrases match games and tags with **All terms** or **Any term**. Spaces/commas separate terms; matching is case-insensitive substring matching. Blank searches draw a random sample. **Remember my filters** saves only this module’s phrase and matching mode in browser/WebView2 storage; unchecking removes them.

The async client in `scout-backend/nobody_discovery.py` calls the public `https://nobody.live/stream` index. Its contract was verified against [the public source](https://github.com/jkingsman/Nobody.live/tree/d099772b96b04daeb9b843ce092a9e0796ff0062), especially `app.py`, `db_utils.py`, and the English-language scanner. ScoutTool does not run another crawler or PostgreSQL service. Search sends no Twitch credentials. Tracking still uses ScoutTool’s existing Twitch connection.

Client safeguards: a separately owned, proxy-aware HTTP session closed after each request; at most two active provider fetches, a 15-second total deadline including queue wait, a 12-second network timeout, at most 1 MiB response data and 65 provider records, no redirects, validation of usernames/counts, duplicate removal, current blacklist filtering (including changes while a fetch is in flight), and rechecking game/tag matches. API responses are not cached. Provider counts are snapshots, not guarantees that a channel is still live with exactly zero viewers when opened. Errors are retryable and do not erase saved settings. Closing/superseding a search prevents stale UI updates; database replacement resets stale tracking results.

## Findings fixed

| Area | Confirmed problem | Change |
|---|---|---|
| Twitch caches | Cache size divisible by 128 caused a complete scan on every overwrite; updating a full cache evicted an unrelated entry. | Sweep once per 128 writes; evict only for new keys. TTL and capacity are unchanged. |
| Discover cache | Updating an existing key at capacity evicted another search result. | Keep all existing entries during updates. |
| Bulk scraping | A semaphore limited active requests but still allocated one waiting coroutine/task per streamer. | Eight workers share an iterator; capture rows and generation together; stop obsolete network work and join cancelled workers. Return counts are unchanged. |
| Raid candidates | Sorting all scored rows required O(N log N) ranking and O(N) extra ranking storage for a small result limit. | Stable top-K selection uses O(N log K) time and O(K) selection storage. Equal-score order and legacy negative/None limit slicing are preserved. All rows are still scored; history query/input storage costs remain. |
| Virtual scrolling | Every scroll rebuilt offsets and linearly searched from the first row; reattaching retained cards reloaded live-preview iframes and lost editor focus. | Reuse offsets until geometry changes, binary-search row boundaries, and update only changed DOM membership/order; ignore obsolete virtual renders after switching to Dashboard. Offsets still cost O(rows) to rebuild when measurements change. |
| Twitch retries | Backoff/token refresh held responses open; malformed rate-limit reset headers failed; simultaneous 401s could discard another request’s fresh token. | Release the response before retry work, validate reset values, preserve already-refreshed tokens, and report a final-attempt 429. The existing three-request/one-rate-limit-retry budgets remain. |
| Downloads | Retention began at job creation, so long downloads lost results immediately after finishing. Six-significant-digit start formatting collided for different trims; huge integer times raised 500. | Start retention at completion/failure, use round-trip float labels for distinct trim files, and return 400 for numeric overflow. |
| Discover pagination | The pending flag was set during rendering rather than when Load more actually started. | Set it before the request and clear it in the owning completion path. |

## Measurements

These are single-run local microbenchmarks of scheduling/cache/selection overhead, not live Twitch latency or whole-process memory. Inputs were allocated before tracing; SQLite, network, real scoring and successful scrape writes are excluded. The 100,000-row fixtures use a deterministic score stub and asynchronously failing social fetches. Compare the exact results in `tests/performance-results.json`.

| Fixture | Before | After |
|---|---:|---:|
| 256 overwrites, 49,920-entry cache | 1.60 s / 256 full scans | 0.013 s / 2 full scans |
| Select six candidates from 100,000 pre-scored rows | 0.130 s / 7.99 MB incremental peak | 0.034 s / 2.24 KB incremental peak |
| Schedule 100,000 scrape attempts | 4.02 s / 146.2 MB incremental peak | 0.65 s / 1.62 MB incremental peak |

## Realistic edge cases exercised

- Search: empty/whitespace phrases, spaces/commas, case differences, Unicode game/tag terms, ALL versus ANY, missing/null tags, title-only matches excluded, punctuation treated literally, duplicate channels, blacklisted channels, one-viewer records rejected, invalid usernames and boolean viewer counts rejected.
- Provider: empty results, malformed JSON, wrong root shape, excessive records/response bytes, HTTP redirects/413/429/503/500, timeout while waiting for a slot, cancellation during body reading, response closure, and two-fetch concurrency bounds.
- API: invalid matching mode, phrases over 65 characters, zero/excessive result limits, tracked/untracked enrichment, blacklist propagation, actionable retry headers, and no-store response headers.
- UI: all/any controls, HTML escaping, 900px window controls, remembered filters across reload, forgetting saved filters, corrupt storage, storage quota errors, provider error/retry, rapid superseding searches, modal cancellation, and resetting after database replacement.
- Performance/correctness: cache expiration and updates at capacity, stable score ties and zero/negative/None/oversized limits, 1,000-attempt scrape partial failures, cancellation joining all workers, changed-generation suppression, malformed retry headers, concurrent token refresh, long download completion/expiry, precise trim filenames, and numeric overflow.
- Existing regression suite: SQLite migrations/FTS, import/restore rollback and races, persistence/cleanup, normal/empty/error APIs, 10,000-record paging and bounded DOM, variable-height scrolling, previews, autosaves, comparison, settings/themes, Discover history, dashboard drag/drop, and duplicate VOD actions.

This is a concrete tested inventory, not a claim that every possible failure has been exhaustively enumerated.

## Production recommendations beyond this patch

1. **Keep one application worker/process.** SQLite replacement generations, tracker ownership, notification queues and job dictionaries are process-local. Multiple uvicorn workers would duplicate tracking and break coordinated imports. Shared deployment requires shared job/state coordination and a single tracker owner before scaling out.
2. **Measure long-lived history, not just roster size.** Rolling-average queries apply `datetime(replace(timestamp,...))` and cannot range-seek a normal timestamp index. Viewer history grows without retention. Profile with months of data; consider an indexed validated UTC/epoch field or maintained rolling aggregates. Do not silently delete history or change legacy timestamp interpretation.
3. **Budget SQLite connections and maintenance latency.** Each connection permits an 8 MiB page cache; the registry retains connections created by workers until shutdown/import. The global lock serializes readers with backup/integrity/replacement work. Measure connection count and lock wait times; introduce bounded database access and carefully coordinated maintenance only with Windows replacement-race coverage.
4. **Bound large imports and public admission.** Current database uploads are read completely into memory before staging; stream them into validated temporary files for multi-gigabyte data. Download workers are capped at two but queued distinct jobs remain unbounded. A public server needs explicit request/job admission and upload limits; those product limits were not added to this desktop app silently.
5. **Use byte budgets and monotonic elapsed-time tracking where appropriate.** Cache entry caps do not bound bio/panel payload bytes, and existing TTLs use wall time. Establish payload budgets and migrate expiry bookkeeping with clock-change tests before claiming bounded whole-process memory.
6. **Make builds reproducible and observe real workloads.** Requirements currently use lower bounds. Record/pin tested build inputs for releases; measure endpoint tail latency, SQLite lock waits, RSS, cache bytes, provider failures and tracker duration before increasing concurrency. Avoid adding infrastructure solely to refactor working desktop code.

## Validation and limits

59 backend tests and 25 Chromium browser scenarios passed. Python compilation, pyflakes, JavaScript syntax and Git whitespace checks passed. Linux PyInstaller desktop and headless-backend builds are checked separately; the frozen runtime verifies migrations, bundled resources, persistence, backups and restart/shutdown, including the new module resource/API validation and a live zero-viewer search from the frozen executable.

Live nobody.live access initially returned proxy HTTP 403. After the environment restarted with its current unrestricted policy, the site and public endpoint became reachable. End-to-end ScoutTool requests returned HTTP 200 for all-term, any-term, and blank searches; returned records were unique, had zero indexed viewers, and matched their game/tag terms. A valid all-term search can return no matches. The live check also exposed a direct-connect failure in the shared HTTP client; the isolated module now honors standard proxy settings and closes its own session after every request. Provider availability and current viewer counts are not guaranteed by these point-in-time checks. Live Twitch/media operations and native Windows/WebView2/installer execution remain unverified on this Linux host. The source ZIP includes no Linux ELF masquerading as a Windows executable and no user database/credentials.

The cloud configuration draft includes the new module syntax check and v4.1 startup readiness. Its current network policy is unrestricted and is preserved. Saving a configuration draft does not activate scripts or publish the environment; review/save and publish it in environment settings when needed.
