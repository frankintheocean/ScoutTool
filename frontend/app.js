const API = "/api";

const state = {
  view: "roster",           // roster | favourites | raid | inactive | archived
  page: 1,
  pageSize: 30,
  total: 0,
  items: [],
  selectedUsername: null,
  sortBy: "followers",
  ascending: false,
  search: "",
  priority: "",
  category: "",
  location: "",
  liveOnly: false,
  minFollowers: null,
  maxFollowers: null,
  tags: "",
  selectedUsernames: new Set(),
  categories: [],
  locations: [],
  openPreviews: new Set(),   // usernames whose live-preview embed is expanded
};

// 📜 Scroll position per roster-backed view, keyed by view name — so
// switching to Dashboard/Discover/a modal and back (or reopening the
// detail panel, which re-renders the grid via loadView()/renderGrid())
// doesn't dump the user back at the top of a long roster. `.main` is
// the actual scrolling element (see .main { overflow-y: auto }).
const _scrollPositions = {};

function saveMainScrollPosition() {
  const main = document.querySelector(".main");
  if (main && state.view) _scrollPositions[state.view] = main.scrollTop;
}

function restoreMainScrollPosition() {
  const main = document.querySelector(".main");
  const pos = _scrollPositions[state.view];
  if (main && pos != null) main.scrollTop = pos;
}

// Persist roster sort independently of the iframe URL so leaving/re-entering
// Scout restores the user's chosen sort.
const SORT_STATE_KEY = "scout-roster-sort";
function loadPersistedSortState() {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_STATE_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.sortBy === "string" && saved.sortBy) state.sortBy = saved.sortBy;
      if (typeof saved.ascending === "boolean") state.ascending = saved.ascending;
    }
  } catch (_) {}
}
function persistSortState() {
  localStorage.setItem(SORT_STATE_KEY, JSON.stringify({ sortBy: state.sortBy, ascending: state.ascending }));
}
loadPersistedSortState();

// ===================== URL STATE (filters/sort/page) =====================
// Mirrors the roster's filter/sort/search/page into the URL query string
// (via history.replaceState — no extra history entries per keystroke) so
// a link to a filtered view is shareable and reload preserves it. A
// single popstate handler restores state on back/forward. Kept as a thin
// read/write pair rather than a router: it only touches `state`'s roster
// fields and never drives rendering directly, so it can't race with the
// existing loadView() call sites.
const URL_STATE_KEYS = {
  search: "q",
  priority: "priority",
  category: "category",
  location: "location",
  liveOnly: "live",
  minFollowers: "min_followers",
  maxFollowers: "max_followers",
  tags: "tags",
  sortBy: "sort",
  ascending: "asc",
  page: "page",
  view: "view",
};

function syncStateToUrl() {
  // 🧩 Dashboard has no filters of its own — it ignores state.search/
  // priority/category/location/liveOnly/sortBy/ascending/page entirely
  // (see loadView()'s early return for state.view === "dashboard").
  // Those roster fields are deliberately left untouched here rather
  // than cleared when view=dashboard: switching *back* to roster should
  // restore whatever filters were active before, and the URL should
  // still reflect them (e.g. reloading a `?view=dashboard&q=foo` link
  // keeps `q` ready for when the user returns to the roster view,
  // even though it plays no part while the dashboard is showing).
  const params = new URLSearchParams();
  for (const [stateKey, paramKey] of Object.entries(URL_STATE_KEYS)) {
    const value = state[stateKey];
    const isDefault =
      (stateKey === "page" && value === 1) ||
      (stateKey === "sortBy" && value === "followers") ||
      (stateKey === "ascending" && value === false) ||
      (stateKey === "liveOnly" && value === false) ||
      (stateKey === "view" && value === "roster") ||
      value === "" || value === null || value === undefined;
    if (!isDefault) params.set(paramKey, String(value));
  }
  const qsStr = params.toString();
  const url = qsStr ? `${location.pathname}?${qsStr}` : location.pathname;
  history.replaceState(null, "", url);
}

function readStateFromUrl() {
  const params = new URLSearchParams(location.search);
  if (params.has("q")) state.search = params.get("q");
  if (params.has("priority")) state.priority = params.get("priority");
  if (params.has("category")) state.category = params.get("category");
  if (params.has("location")) state.location = params.get("location");
  if (params.has("live")) state.liveOnly = params.get("live") === "true";
  if (params.has("min_followers")) state.minFollowers = Number(params.get("min_followers")) || null;
  if (params.has("max_followers")) state.maxFollowers = Number(params.get("max_followers")) || null;
  if (params.has("tags")) state.tags = params.get("tags") || "";
  if (params.has("sort")) state.sortBy = params.get("sort");
  if (params.has("asc")) state.ascending = params.get("asc") === "true";
  if (params.has("page")) state.page = Number(params.get("page")) || 1;
  if (params.has("view")) state.view = params.get("view");
}

// Pushes the DOM controls (rail inputs, rail-item active class, view
// title) to match whatever's currently in `state` — used after restoring
// from the URL, since applyPreset() already has an equivalent for the
// preset-apply path.
function syncControlsFromState() {
  document.getElementById("searchInput").value = state.search;
  document.getElementById("filterPriority").value = state.priority;
  document.getElementById("filterCategory").value = state.category;
  document.getElementById("filterLocation").value = state.location;
  document.getElementById("filterLiveOnly").checked = state.liveOnly;
  document.getElementById("sortBy").value = state.sortBy;
  document.getElementById("btnSortDir").textContent = state.ascending ? "↑ Ascending" : "↓ Descending";

  document.querySelectorAll(".rail-item").forEach(b => b.classList.remove("active"));
  const activeBtn = document.querySelector(`.rail-item[data-view="${state.view}"]`);
  if (activeBtn) {
    activeBtn.classList.add("active");
    document.getElementById("viewTitle").textContent = activeBtn.textContent;
  }
}

window.addEventListener("popstate", () => {
  readStateFromUrl();
  syncControlsFromState();
  loadView();
});

// ===================== FETCH HELPERS =====================

// 🔗 Client-side de-duplication for GET requests: if the same GET path
// fires twice in quick succession (e.g. two components both call
// /api/categories independently around the same time), the second
// caller shares the first's in-flight promise instead of firing a
// second network request. Keyed on the exact path+query string, and
// only applied to GETs (no body/method override) — a mutating request
// always fires on its own. The cache entry is cleared once the request
// settles (success or failure) so it never serves a stale promise to a
// later, genuinely-new request for the same path.
const _inflightGetRequests = new Map();
const _pendingPutRequests = new Map();
const _pendingMutations = new Set();

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const isPlainGet = method === "GET" && !options.body && !options.signal;

  if (isPlainGet && _inflightGetRequests.has(path)) {
    return _inflightGetRequests.get(path);
  }

  const priorWrite = method === "PUT" ? _pendingPutRequests.get(path) : null;
  const promise = (async () => {
    if (priorWrite) await priorWrite.catch(() => {});
    const res = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let body = null;
    try { body = await res.json(); } catch (e) { /* no body */ }
    if (!res.ok) {
      const detail = (body && body.detail) || res.statusText;
      // Surface Twitch 429s distinctly instead of a bare "request failed" —
      // the backend now returns a real 429 (with Retry-After) once its own
      // short retry is exhausted, rather than blocking silently.
      if (res.status === 429) {
        const err = new Error(detail);
        err.rateLimited = true;
        err.retryAfter = res.headers.get("Retry-After");
        throw err;
      }
      const err = new Error(detail);
      err.status = res.status;
      err.details = body?.error?.details || body?.detail || null;
      throw err;
    }
    if (method !== "GET") { _statsCache = null; _statsCacheAt = 0; ++_statsGeneration; }
    return body;
  })();

  if (method !== "GET") {
    _pendingMutations.add(promise);
    const cleanupMutation = () => _pendingMutations.delete(promise);
    promise.then(cleanupMutation, cleanupMutation);
  }
  if (method === "PUT") {
    _pendingPutRequests.set(path, promise);
    const cleanupWrite = () => { if (_pendingPutRequests.get(path) === promise) _pendingPutRequests.delete(path); };
    promise.then(cleanupWrite, cleanupWrite);
  }
  if (isPlainGet) {
    _inflightGetRequests.set(path, promise);
    const cleanup = () => {
      // Only clear if this promise is still the one on record — a newer
      // call for the same path (started after this one settled) will
      // already have overwritten the entry with its own promise.
      if (_inflightGetRequests.get(path) === promise) _inflightGetRequests.delete(path);
    };
    promise.then(cleanup, cleanup);
  }

  return promise;
}

// 🏁 Race-condition guard: if the person changes Discover filters (or hits
// Search) again before an in-flight request finishes, the earlier request
// is aborted so its (now-stale) response can't land after the newer one
// and clobber the results shown on screen. `signal` is optional — passed
// through to fetch() when provided.
async function apiAbortable(path, options = {}, signal) {
  return api(path, signal ? { ...options, signal } : options);
}

function qs(params) {
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined && v !== false)
  );
  return "?" + new URLSearchParams(clean).toString();
}

// ===================== TOASTS =====================

function toast(message, kind = "") {
  if (kind === "error") {
    try {
      const key="scoutbot_error_log"; const items=JSON.parse(localStorage.getItem(key)||"[]");
      items.unshift({message:String(message),time:new Date().toISOString()});
      localStorage.setItem(key,JSON.stringify(items.slice(0,30)));
    } catch (_) {}
  }
  const stack = document.getElementById("toastStack");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " toast-" + kind : "");
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// 🔙 Undo toast for destructive actions (archive/blacklist/delete). The
// actual mutating request is deferred (not fired immediately) — a short
// grace period runs first, shown as a countdown-style toast with an
// Undo button. If the person clicks Undo, `onUndo` runs instead and the
// deferred action never fires; otherwise `commit` runs once the toast
// expires. This sidesteps needing any backend "restore" endpoint (some
// of these actions, like DELETE /streamers/{username}, are hard deletes
// with no undo on the server) since nothing has actually happened yet
// while the toast is showing.
const UNDO_TOAST_MS = 6000;
const _pendingUndos = new Set();

function undoToast(message, commit, onUndo) {
  const stack = document.getElementById("toastStack");
  const el = document.createElement("div");
  el.className = "toast toast-undo";

  const label = document.createElement("span");
  label.textContent = message;

  const undoBtn = document.createElement("button");
  undoBtn.className = "toast-undo-btn";
  undoBtn.textContent = "Undo";

  el.appendChild(label);
  el.appendChild(undoBtn);
  stack.appendChild(el);

  let settled = false;
  const timer = setTimeout(async () => {
    if (settled) return;
    settled = true;
    _pendingUndos.delete(record);
    el.remove();
    try {
      await commit();
    } catch (e) {
      toast(e.message || "Action failed", "error");
      loadView();
    }
  }, UNDO_TOAST_MS);

  const record = { cancel: () => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    el.remove();
    _pendingUndos.delete(record);
  }};
  _pendingUndos.add(record);
  undoBtn.addEventListener("click", () => {
    if (settled) return;
    record.cancel();
    if (onUndo) onUndo();
  });
}

// ===================== DATA LOADING =====================

// 📊 Tiny in-memory cache for /stats — loadStats() (topbar counts,
// called from many places: SSE events, after track/untrack/favourite,
// etc.) and the Dashboard's Overview widget both want the same numbers.
// Previously each fetched /stats independently, so switching to
// Dashboard right after an SSE-driven loadStats() refetched the same
// data a moment later. A short TTL (rather than an unbounded cache)
// keeps this safe if a stats-changing action ever fires without going
// through loadStats() — worst case it's stale for a few seconds, not
// forever.
let _statsCache = null;
let _statsCacheAt = 0;
let _statsGeneration = 0;
const STATS_CACHE_TTL_MS = 5000;

async function fetchStatsCached() {
  const now = Date.now();
  if (_statsCache && (now - _statsCacheAt) < STATS_CACHE_TTL_MS) return _statsCache;
  const generation = _statsGeneration;
  const stats = await api("/stats");
  if (generation !== _statsGeneration) return fetchStatsCached();
  _statsCache = stats;
  _statsCacheAt = now;
  return stats;
}

async function loadStats() {
  try {
    const stats = await fetchStatsCached();
    document.getElementById("statTotal").textContent = stats.total.toLocaleString();
    document.getElementById("statLive").textContent = stats.live.toLocaleString();
    document.getElementById("statFollowers").textContent = formatCompact(stats.followers);
  } catch (e) { /* non-fatal */ }
}

// Shared dropdown-population helper for filter <select> elements backed
// by a `/api/<endpoint>` list of {<labelKey>, amount} items — used by
// loadCategories()/loadLocations() below, which differ only in which
// endpoint/elements/label they target. Preserves the previously-selected
// value on each select after repopulating (so an in-flight filter
// choice survives a background refresh), and mirrors the same option
// list across every element id passed in (e.g. the roster filter and
// Discover's filter share one list).
async function populateFilterSelect(endpoint, elementIds, labelKey, placeholder) {
  const sels = elementIds.map(id => document.getElementById(id)).filter(Boolean);
  const optionsHtml = (items) =>
    `<option value="">${placeholder}</option>` +
    items.map(item => `<option value="${escapeAttr(item[labelKey] || "")}">${escapeHtml(item[labelKey] || "Unknown")} (${item.amount})</option>`).join("");

  try {
    const data = await api(endpoint);
    const html = optionsHtml(data.items);
    for (const sel of sels) {
      const current = sel.value;
      sel.innerHTML = html;
      sel.value = current;
    }
    return data.items;
  } catch (e) {
    // Non-fatal — callers decide what (if anything) to surface on failure.
    return null;
  }
}

async function loadCategories() {
  const items = await populateFilterSelect("/categories", ["filterCategory"], "category", "Any category");
  if (items) state.categories = items;
}

// Populates the roster's location filter dropdown, same pattern as
// loadCategories() above — grouped by each tracked streamer's effective
// location (manual, falling back to scraped; see /api/locations).
// Discover's location filter (client-side, only meaningful against
// already-tracked results — see currentDiscoverFilters/sortedDiscoverItems)
// shares the same option list, so both element ids are populated together.
async function loadLocations() {
  const errEl = document.getElementById("filterLocationError");
  const discoverErrEl = document.getElementById("discoverLocationFilterError");
  const items = await populateFilterSelect(
    "/locations",
    ["filterLocation", "discoverLocationFilter"],
    "location",
    "Any location"
  );
  if (items) {
    state.locations = items;
    if (errEl) errEl.style.display = "none";
    if (discoverErrEl) discoverErrEl.style.display = "none";
  } else {
    // Load failure — the dropdown just keeps whatever options it already
    // had — but silently leaving it at "Any location" with nothing else
    // is indistinguishable from a roster with no locations on file yet.
    // Surface a small inline indicator so a load failure is visible.
    if (errEl) errEl.style.display = "inline";
    if (discoverErrEl) discoverErrEl.style.display = "inline";
  }
}

// Suggestions for the manual location editor's <datalist> — reuses
// state.locations (already populated by loadLocations() for the
// roster/Discover filter dropdowns) instead of a separate fetch, so
// previously-used spellings ("Auckland, NZ") are offered as one-click
// suggestions the same way POPULAR_DISCOVER_CATEGORIES suggests
// categories. A <datalist> never restricts the input — typing anything
// else still works exactly as before, this just nudges toward
// consistency with what's already on file.
function locationDatalistOptionsHtml() {
  if (!state.locations || !state.locations.length) return "";
  return state.locations
    .map(item => `<option value="${escapeAttr(item.location || "")}"></option>`)
    .join("");
}

// Skeleton placeholder cards shown while a view's data is in flight,
// instead of leaving the grid blank until the response lands. Count
// mirrors the current page size so the grid doesn't visibly reflow once
// real cards replace them.
// Shimmer placeholder for a Dashboard widget body, shown while its data
// is still loading — replaces the earlier plain "Loading…" text with
// something that mirrors the eventual row-based content (an avatar +
// two lines of varying width, repeated), same shimmer treatment as the
// roster grid's skeleton cards above.
function dashboardWidgetSkeletonHtml() {
  const row = `
    <div class="dashboard-widget-skeleton-row">
      <div class="skeleton-block dashboard-widget-skeleton-avatar"></div>
      <div class="skeleton-block dashboard-widget-skeleton-line"></div>
    </div>`;
  return `<div class="dashboard-widget-skeleton">${row.repeat(3)}</div>`;
}

function renderSkeletons(count) {
  const grid = document.getElementById("cardGrid");
  teardownVirtualGrid(grid);
  const empty = document.getElementById("emptyState");
  empty.style.display = "none";
  const card = `
    <div class="skeleton-card">
      <div class="skeleton-top">
        <div class="skeleton-block skeleton-avatar"></div>
        <div class="skeleton-lines">
          <div class="skeleton-block skeleton-line w-60"></div>
          <div class="skeleton-block skeleton-line w-40"></div>
        </div>
      </div>
      <div class="skeleton-stats">
        <div class="skeleton-block skeleton-stat"></div>
        <div class="skeleton-block skeleton-stat"></div>
        <div class="skeleton-block skeleton-stat"></div>
      </div>
    </div>`;
  grid.innerHTML = card.repeat(Math.max(count, 1));
}

// 🏁 Race-condition guard: loadView() can be triggered from many places in
// quick succession — rapid pagination/filter changes, and especially an
// SSE event firing mid-request (see connectEventStream's `if (state.view
// === "roster") loadView();`). Without a guard, a slower/older request
// could resolve after a newer one and overwrite state.items/state.total
// with stale data. Mirrors the generation-token pattern already used for
// Discover's request cancellation, minus the AbortController since these
// requests are cheap GETs that don't need to be cut short — just ignored
// if superseded.
let _loadViewToken = 0;
let _showAllController = null;

async function loadView() {
  syncStateToUrl();
  const token = ++_loadViewToken;
  ++_dashboardRenderToken;
  if (_rosterController) _rosterController.abort();
  _showAllController?.abort();
  _showAllController = null;
  teardownVirtualGrid(document.getElementById("cardGrid"));

  // 🧩 Dashboard is a fixed layout of widgets, not a paginated/sorted list
  // of streamers — it skips the roster fetch entirely and renders from
  // whatever data each widget needs (see renderDashboard()).
  if (state.view === "dashboard") {
    setDashboardVisible(true);
    await renderDashboard();
    return;
  }
  setDashboardVisible(false);

  // Restoring scroll only matters when we're re-rendering the same
  // view the user was already scrolled through (e.g. favouriting a
  // streamer from the detail panel re-runs loadView() for the roster
  // list behind it) or returning to a view visited earlier this
  // session — a genuine page/filter/sort change should still land at
  // the top like before, so only page 1 with no explicit re-entry is
  // treated as "fresh".
  const restoreScrollAfter = _scrollPositions[state.view] != null;

  renderSkeletons(Math.min(state.pageSize, 12));

  _rosterController = new AbortController();
  const rosterSignal = _rosterController.signal;

  try {
    let data;

    if (state.view === "favourites") {
      data = await api("/favourites");
      data = paginateClientSide(data.items);
    } else if (state.view === "raid") {
      data = await api("/raid/candidates" + qs({ limit: 20 }));
      data = paginateClientSide(data.items);
    } else if (state.view === "inactive") {
      data = await api("/inactive" + qs({ days: 30 }));
      data = paginateClientSide(data.items);
    } else if (state.view === "archived") {
      data = await api("/archived" + qs({ limit: state.virtualScroll ? 500 : state.pageSize, offset: state.virtualScroll ? 0 : (state.page - 1) * state.pageSize }), { signal: rosterSignal });
      if (state.virtualScroll) {
        for (let offset = data.items.length; offset < data.total; offset += 500) {
          const page = await api("/archived" + qs({ limit: 500, offset }), { signal: rosterSignal });
          if (!page.items.length) break;
          data.items.push(...page.items);
        }
      }
    } else {
      const op = parseRosterSearch(state.search);
      const effectivePriority = op.parsed.priority || state.priority;
      const effectiveCategory = op.parsed.category || state.category;
      const effectiveLocation = op.parsed.location || state.location;
      const effectiveLive = op.parsed.liveOnly == null ? state.liveOnly : op.parsed.liveOnly;
      const effectiveMinFollowers = op.parsed.minFollowers ?? state.minFollowers;
      const effectiveMaxFollowers = op.parsed.maxFollowers ?? state.maxFollowers;
      const effectiveTags = [...new Set([...(state.tags || "").split(",").map(x=>x.trim()).filter(Boolean), ...op.parsed.tags])].join(",");
      const hasFilters = op.text || effectivePriority || effectiveCategory || effectiveLocation || effectiveLive || effectiveMinFollowers != null || effectiveMaxFollowers != null || effectiveTags;
      const endpoint = hasFilters ? "/streamers/search" : "/streamers";
      const params = {
        ...(hasFilters ? {
          q: op.text, priority: effectivePriority, category: effectiveCategory, location: effectiveLocation,
          live_only: effectiveLive, min_followers: effectiveMinFollowers, max_followers: effectiveMaxFollowers, tags: effectiveTags,
        } : {}), sort_by: state.sortBy, ascending: state.ascending,
      };
      if (state.virtualScroll) {
        _showAllController = new AbortController();
        data = await loadAllStreamerPages(endpoint, params, _showAllController.signal);
      } else {
        data = await api(endpoint + qs({ ...params, page: state.page, page_size: state.pageSize }), { signal: rosterSignal });
      }
    }

    if (token !== _loadViewToken) return; // superseded by a newer loadView() — ignore stale response

    // 📌 Pinned streamers float to the top of whatever this page's sort
    // produced, regardless of which sort field/direction is active.
    state.items = applyPinOrder(data.items);
    state.total = data.total ?? data.items.length;

    renderGrid();
    renderPagination();
    if (restoreScrollAfter) restoreMainScrollPosition();
  } catch (e) {
    if (token !== _loadViewToken) return;
    if (e?.name === "AbortError") return;
    document.getElementById("cardGrid").innerHTML = "";
    toast("Failed to load streamers: " + (e?.message || "Unknown error"), "error");
  }
}

function paginateClientSide(items) {
  state.total = items.length;
  if (state.virtualScroll) return { items, total: items.length };
  const start = (state.page - 1) * state.pageSize;
  return { items: items.slice(start, start + state.pageSize), total: items.length };
}

// ===================== PINNING =====================
// 📌 Manual pinning: lets specific streamers sit at the top of the roster
// grid no matter the active sort. Kept client-side (localStorage) rather
// than a new DB column/endpoint — it's a per-device display preference,
// not shared data, and needs no backend change to add.

const PIN_KEY = "scoutbot_pinned";

function loadPinned() {
  try {
    const raw = JSON.parse(localStorage.getItem(PIN_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (e) {
    return new Set();
  }
}

function savePinned(set) {
  localStorage.setItem(PIN_KEY, JSON.stringify([...set]));
}

let pinnedUsernames = loadPinned();

function isPinned(username) {
  return pinnedUsernames.has(username);
}

// ===================== TIMEZONE DISPLAY MODE =====================
// Per-card toggle between an absolute local time and a relative offset
// (see relativeTimeInTimezone) for the 📍 location/timezone line. Same
// client-side-only reasoning as pinning above — a per-device display
// preference, not shared data — so it reuses the identical
// localStorage-backed Set pattern rather than a new backend field.

const RELATIVE_TZ_KEY = "scoutbot_relative_tz";

function loadRelativeTz() {
  try {
    const raw = JSON.parse(localStorage.getItem(RELATIVE_TZ_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (e) {
    return new Set();
  }
}

function saveRelativeTz(set) {
  localStorage.setItem(RELATIVE_TZ_KEY, JSON.stringify([...set]));
}

let relativeTzUsernames = loadRelativeTz();

function isRelativeTz(username) {
  return relativeTzUsernames.has(username);
}

function toggleRelativeTz(username) {
  if (relativeTzUsernames.has(username)) {
    relativeTzUsernames.delete(username);
  } else {
    relativeTzUsernames.add(username);
  }
  saveRelativeTz(relativeTzUsernames);
}

// Renders the collapsible 📍 location/timezone badge shared by the
// roster card and detail panel. `sourceIcon` distinguishes a manual
// location (✏️) from a scraped one (🌐) so it's clear which value is
// authoritative at a glance (see effective_location in serializers.py).
// The <details> badge starts collapsed; expanding it reveals the
// relative/absolute toggle switch alongside the time label.
function locationBadgeHtml(username, locationText, tz, sourceIcon) {
  if (!locationText) return "";
  const relative = isRelativeTz(username);
  const timeLabel = tz ? (relative ? relativeTimeInTimezone(tz) : currentTimeInTimezone(tz)) : "";
  return `
    <details class="tz-badge" data-tz-badge="${escapeAttr(username)}">
      <summary>${sourceIcon} ${escapeHtml(locationText)}${tz ? ` &middot; ${escapeHtml(timeLabel)}` : ""}</summary>
      ${tz ? `
      <label class="tz-badge-toggle" title="Show relative offset instead of local time">
        <input type="checkbox" data-tz-toggle="${escapeAttr(username)}" ${relative ? "checked" : ""}>
        <span>Relative time</span>
      </label>` : ""}
    </details>`;
}

// Location is always shown at "comfortable" density (existing behaviour).
// At "compact" density it becomes a toggleable column, controlled by the
// "Show location" checkbox next to the density switch — see
// syncDensityControls()/currentPrefs.showLocationCompact.
function shouldShowLocationColumn() {
  if ((currentPrefs.density || "comfortable") !== "compact") return true;
  return currentPrefs.showLocationCompact !== false;
}

function togglePinned(username) {
  if (pinnedUsernames.has(username)) {
    pinnedUsernames.delete(username);
  } else {
    pinnedUsernames.add(username);
  }
  savePinned(pinnedUsernames);
}

// Reorders items so pinned streamers come first (in the order they were
// pinned), with everyone else left in whatever order the active sort
// already produced. Only meaningful within the current page's items —
// pinning doesn't change what's fetched, just how this page is arranged.
function applyPinOrder(items) {
  if (!pinnedUsernames.size) return items;
  const pinned = [];
  const rest = [];
  for (const item of items) {
    (isPinned(item.username) ? pinned : rest).push(item);
  }
  return pinned.concat(rest);
}

// ===================== RENDERING =====================

function formatCompact(n) {
  n = n || 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

// Formats the current time in a given IANA timezone (e.g. from a
// streamer's scraped location) using the browser's built-in Intl API —
// no extra dependency needed. Falls back to just returning the raw
// timezone name if the browser doesn't recognize it (defensive; scraped
// values come from a static lookup table server-side so this should be
// rare in practice).
function currentTimeInTimezone(tz) {
  try {
    const formatted = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    }).format(new Date());
    return `${formatted} local (${tz})`;
  } catch (e) {
    return tz;
  }
}

// Relative counterpart to currentTimeInTimezone: instead of the absolute
// local clock time, shows how far that timezone's current offset is from
// the browser's own (e.g. "+5h" if it's 5 hours ahead of here, "-30m" for
// a half-hour-off zone). Computed by diffing the same moment formatted in
// both timezones via Intl, so it stays correct across DST without a date
// math dependency. Falls back to the raw tz name on failure, same as
// currentTimeInTimezone.
function relativeTimeInTimezone(tz) {
  try {
    const now = new Date();
    const offsetMinutes = (zone) => {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: zone,
        hourCycle: "h23",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      }).formatToParts(now).reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
      const asUTC = Date.UTC(
        Number(parts.year), Number(parts.month) - 1, Number(parts.day),
        Number(parts.hour), Number(parts.minute), Number(parts.second)
      );
      return (asUTC - now.getTime()) / 60000;
    };
    const diffMinutes = Math.round(offsetMinutes(tz) - offsetMinutes(Intl.DateTimeFormat().resolvedOptions().timeZone));
    if (diffMinutes === 0) return `same time (${tz})`;
    const sign = diffMinutes > 0 ? "+" : "-";
    const abs = Math.abs(diffMinutes);
    const hrs = Math.floor(abs / 60);
    const mins = abs % 60;
    const label = hrs && mins ? `${hrs}h${mins}m` : hrs ? `${hrs}h` : `${mins}m`;
    return `${sign}${label} (${tz})`;
  } catch (e) {
    return tz;
  }
}

// Static fallback list of common IANA timezone names for the manual
// location editor's dropdown. Prefers the browser's own
// Intl.supportedValuesOf("timeZone") (broad, always current) when
// available, falling back to this shorter curated list on older
// browsers that don't support it — no extra dependency either way.
const FALLBACK_TIMEZONES = [
  "Pacific/Auckland", "Australia/Sydney", "Australia/Melbourne", "Australia/Perth",
  "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore",
  "Asia/Kolkata", "Asia/Dubai", "Asia/Manila", "Asia/Jakarta", "Asia/Bangkok",
  "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Madrid", "Europe/Paris",
  "Europe/Berlin", "Europe/Amsterdam", "Europe/Rome", "Europe/Warsaw", "Europe/Athens",
  "Europe/Moscow", "Europe/Istanbul", "Africa/Cairo", "Africa/Johannesburg", "Africa/Lagos",
  "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/Mexico_City",
  "America/Bogota", "America/New_York", "America/Chicago", "America/Denver",
  "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
  "UTC",
];

function timezoneList() {
  try {
    if (typeof Intl.supportedValuesOf === "function") {
      return Intl.supportedValuesOf("timeZone");
    }
  } catch (e) { /* fall through to static list */ }
  return FALLBACK_TIMEZONES;
}

// Cached once at module load rather than recomputed on every detail-panel
// render — Intl.supportedValuesOf("timeZone") returns a fixed list for
// the life of the page (it doesn't change at runtime), so re-deriving it
// every time renderDetail() runs is wasted work with no benefit.
const CACHED_TIMEZONE_LIST = timezoneList();

function timezoneOptionsHtml(selected) {
  const zones = [...new Set([...CACHED_TIMEZONE_LIST, "UTC", ...(selected ? [selected] : [])])];
  const options = ['<option value="">No timezone set</option>'];

  // Grouped by IANA region (the part before the first "/", e.g. "Europe"
  // from "Europe/London"; zones with no "/" like "UTC" fall into "Other")
  // so the dropdown is a set of collapsible <optgroup>s instead of one
  // flat list of ~400 entries. Groups are ordered by first appearance in
  // `zones`, which for both Intl.supportedValuesOf("timeZone") and
  // FALLBACK_TIMEZONES is already alphabetical — grouping doesn't change
  // which zones are offered or their underlying values, only how the
  // native <select> presents them.
  const groups = new Map();
  for (const tz of zones) {
    const slash = tz.indexOf("/");
    const region = slash === -1 ? "Other" : tz.slice(0, slash);
    if (!groups.has(region)) groups.set(region, []);
    groups.get(region).push(tz);
  }

  for (const [region, tzList] of groups) {
    options.push(`<optgroup label="${escapeAttr(region)}">`);
    for (const tz of tzList) {
      options.push(`<option value="${escapeAttr(tz)}" ${selected === tz ? "selected" : ""}>${escapeHtml(tz)}</option>`);
    }
    options.push("</optgroup>");
  }

  return options.join("");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

function initials(name) {
  return (name || "?").slice(0, 2).toUpperCase();
}

const OUTREACH_LABELS = {
  active: "Active",
  contacted: "Contacted",
  raided: "Raided",
  declined: "Declined",
};

function outreachLabel(status) {
  return OUTREACH_LABELS[status] || status;
}

// ===================== VIRTUALIZED ROSTER ("Show all") =====================
// Opt-in row-windowed rendering for the "Show all" mode (see
// VIRTUAL_SCROLL_KEY below), used instead of the normal paginated
// full-innerHTML rebuild when a roster/favourites/etc. list is large.
// Only renders the DOM nodes for cards currently within (or just
// outside) the visible scroll viewport, backed by a spacer element sized
// to the full list so native scrollbar behavior/position stays correct.
// Every other renderGrid() call site is unaffected — this only activates
// when state.virtualScroll is on, and falls through to the existing
// full-render path otherwise.
const VIRTUAL_SCROLL_KEY = "scoutbot_virtual_scroll";
const VIRTUAL_ROW_ESTIMATE_PX = 230; // matches comfortable-density card height; compact recalculated from actual DOM below
const VIRTUAL_OVERSCAN_ROWS = 3;

function loadVirtualScrollPref() {
  return localStorage.getItem(VIRTUAL_SCROLL_KEY) === "1";
}

function saveVirtualScrollPref(on) {
  localStorage.setItem(VIRTUAL_SCROLL_KEY, on ? "1" : "0");
}

state.virtualScroll = loadVirtualScrollPref();

let _virtualScrollHandler = null;
let _virtualResizeHandler = null;
let _virtualObserver = null;

function teardownVirtualGrid(grid) {
  const main = document.querySelector(".main");
  if (_virtualScrollHandler && main) main.removeEventListener("scroll", _virtualScrollHandler);
  if (_virtualResizeHandler) window.removeEventListener("resize", _virtualResizeHandler);
  _virtualScrollHandler = null;
  _virtualResizeHandler = null;
  _virtualObserver?.disconnect();
  _virtualObserver = null;
  grid._virtualHeights = null;
  grid._virtualLayout = null;
  grid.classList.remove("card-grid-virtual");
  grid.style.display = "";
  grid.style.position = "";
  grid._virtualNodeByUsername = null;
}

function firstVirtualOffsetAtLeast(offsets, position) {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (offsets[middle] < position) low = middle + 1;
    else high = middle;
  }
  return low;
}

// Render visible CSS-grid rows plus overscan. Offsets use measured maximum
// row heights; unvisited rows use an estimate until they enter the viewport.
function renderVirtualGrid() {
  if (!state.virtualScroll || state.view === "dashboard") return;
  const grid = document.getElementById("cardGrid");
  const main = document.querySelector(".main");
  if (!grid || !main) return;

  grid.classList.add("card-grid-virtual");
  grid.style.position = "relative";

  // Determine columns-per-row from actual rendered width so this tracks
  // the same auto-fill breakpoints the non-virtual grid uses (300px
  // comfortable / 240px compact — see styles.css), rather than
  // duplicating those numbers here.
  const isCompact = document.documentElement.dataset.density === "compact";
  const minCardWidth = isCompact ? 240 : 300;
  const gap = isCompact ? 8 : 14;
  const gridWidth = grid.clientWidth || main.clientWidth || 900;
  const columns = Math.max(1, Math.floor((gridWidth + gap) / (minCardWidth + gap)));
  const rows = Math.ceil(state.items.length / columns);
  if (grid._virtualColumns !== columns) grid._virtualHeights = new Map();
  grid._virtualColumns = columns;
  const heights = grid._virtualHeights || (grid._virtualHeights = new Map());
  let layout = grid._virtualLayout;
  if (!layout || layout.rows !== rows || layout.columns !== columns || layout.gap !== gap || layout.heights !== heights) {
    const offsets = [0];
    for (let row = 0; row < rows; row++) offsets.push(offsets[row] + (heights.get(row) || VIRTUAL_ROW_ESTIMATE_PX) + gap);
    layout = grid._virtualLayout = { rows, columns, gap, heights, offsets };
  }
  const offsets = layout.offsets;
  const totalHeight = offsets[rows];
  const scale = main.getBoundingClientRect().width / main.offsetWidth || 1;
  const gridTop = (grid.getBoundingClientRect().top - main.getBoundingClientRect().top) / scale + main.scrollTop;
  const scrollTop = Math.max(0, main.scrollTop - gridTop);
  const viewportHeight = main.clientHeight;
  let firstRow = rows ? Math.max(0, Math.min(rows - 1, firstVirtualOffsetAtLeast(offsets, scrollTop) - 1)) : 0;
  let lastRow = Math.max(firstRow, Math.min(rows - 1, firstVirtualOffsetAtLeast(offsets, scrollTop + viewportHeight)));
  firstRow = Math.max(0, firstRow - VIRTUAL_OVERSCAN_ROWS);
  lastRow = Math.min(rows - 1, lastRow + VIRTUAL_OVERSCAN_ROWS);

  const startIdx = firstRow * columns;
  const endIdx = Math.min(state.items.length, (lastRow + 1) * columns);
  const visibleItems = state.items.slice(startIdx, endIdx);

  // Reuse existing card nodes across scroll frames instead of tearing
  // down and rebuilding the whole visible window's innerHTML on every
  // frame. Cards already in the DOM for a username that's still visible
  // are moved into their new position (cheap, keeps listeners intact);
  // only newly-scrolled-into-view usernames get a fresh cardHtml() parse
  // + wireCardEvents() call. Cards outside the window are removed below;
  // retained cards stay attached so their editors and previews survive.
  let spacer = grid.querySelector(".card-grid-virtual-spacer");
  let windowEl = grid.querySelector(".card-grid-virtual-window");
  if (!spacer || !windowEl) {
    grid.innerHTML = `
      <div class="card-grid-virtual-spacer" style="position:relative;">
        <div class="card-grid-virtual-window" style="position:absolute;left:0;right:0;"></div>
      </div>`;
    spacer = grid.querySelector(".card-grid-virtual-spacer");
    windowEl = grid.querySelector(".card-grid-virtual-window");
    grid._virtualNodeByUsername = new Map();
  }

  spacer.style.height = `${totalHeight}px`;
  windowEl.style.top = `${offsets[firstRow]}px`;
  windowEl.style.display = "grid";
  windowEl.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;
  windowEl.style.gap = `${gap}px`;

  const nodeByUsername = grid._virtualNodeByUsername || (grid._virtualNodeByUsername = new Map());
  const nextNodes = [];
  const seen = new Set();
  for (const s of visibleItems) {
    seen.add(s.username);
    let node = nodeByUsername.get(s.username);
    if (!node) {
      const wrapper = document.createElement("div");
      wrapper.innerHTML = cardHtml(s);
      node = wrapper.firstElementChild;
      wireCardEvents(wrapper); // node is still inside wrapper here, so this wires it before it's moved out below
      nodeByUsername.set(s.username, node);
    }
    nextNodes.push(node);
  }
  // Drop nodes for usernames no longer in the visible window.
  for (const username of Array.from(nodeByUsername.keys())) {
    if (!seen.has(username)) nodeByUsername.delete(username);
  }
  // Keep retained cards attached. Detaching a card also reloads its iframe
  // and loses focus in an inline editor, even when the node itself is reused.
  const retained = new Set(nextNodes);
  for (const child of Array.from(windowEl.children)) {
    if (!retained.has(child)) child.remove();
  }
  nextNodes.forEach((node, index) => {
    const current = windowEl.children[index] || null;
    if (current !== node) windowEl.insertBefore(node, current);
  });

  let changed = false;
  for (let row = firstRow; row <= lastRow; row++) {
    const nodes = nextNodes.slice((row - firstRow) * columns, (row - firstRow + 1) * columns);
    const measured = Math.max(0, ...nodes.map(node => node.offsetHeight));
    if (measured && heights.get(row) !== measured) { heights.set(row, measured); changed = true; }
  }
  if (changed) {
    grid._virtualLayout = null;
    requestAnimationFrame(() => {
      if (grid.isConnected && state.virtualScroll && grid.classList.contains("card-grid-virtual")) renderVirtualGrid();
    });
  }

  if (!_virtualScrollHandler) {
    let ticking = false;
    _virtualScrollHandler = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => { ticking = false; if (state.virtualScroll) renderVirtualGrid(); });
    };
    main.addEventListener("scroll", _virtualScrollHandler);
  }
  if (!_virtualResizeHandler) {
    _virtualResizeHandler = () => { if (state.virtualScroll) renderVirtualGrid(); };
    window.addEventListener("resize", _virtualResizeHandler);
    _virtualObserver = new ResizeObserver(_virtualResizeHandler);
    _virtualObserver.observe(grid);
  }
}

function setVirtualScroll(on) {
  state.virtualScroll = on;
  saveVirtualScrollPref(on);
  const grid = document.getElementById("cardGrid");
  if (!on && grid) teardownVirtualGrid(grid);
  renderGrid();
}

function renderGrid() {
  if (state.view === "dashboard") return;
  const grid = document.getElementById("cardGrid");
  const empty = document.getElementById("emptyState");

  if (!state.items.length) {
    if (grid) teardownVirtualGrid(grid);
    grid.innerHTML = "";
    empty.style.display = "block";
    renderEmptyState();
    return;
  }
  empty.style.display = "none";

  if (state.virtualScroll && state.items.length > 60) {
    grid._virtualNodeByUsername = new Map();
    renderVirtualGrid();
    return;
  }
  teardownVirtualGrid(grid);

  grid.innerHTML = state.items.map(s => cardHtml(s)).join("");
  wireCardEvents(grid);
}

// Pulled out of renderGrid() so the virtualized path (which only ever
// has a subset of cards in the DOM at once) can wire up the same
// per-card listeners on just its rendered window, instead of duplicating
// this block.
function wireCardEvents(grid) {
  grid.querySelectorAll("[data-select-streamer]").forEach(box => {
    box.addEventListener("click", e => e.stopPropagation());
    box.addEventListener("change", (e) => {
      const username=e.target.dataset.selectStreamer;
      if (e.target.checked) state.selectedUsernames.add(username); else state.selectedUsernames.delete(username);
      updateSelectionToolbar();
    });
  });
  grid.querySelectorAll(".streamer-card").forEach(card => {
    card.addEventListener("click", () => selectStreamer(card.dataset.username));
  });

  grid.querySelectorAll("[data-pin-toggle]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const username = btn.dataset.pinToggle;
      togglePinned(username);
      toast(isPinned(username) ? `📌 Pinned ${username}` : `Unpinned ${username}`, "success");
      state.items = applyPinOrder(state.items);
      renderGrid();
    });
  });

  // Collapsible 📍 badge: stop propagation so opening it doesn't also
  // select the card, and wire the relative/absolute time toggle inside.
  grid.querySelectorAll("[data-tz-badge]").forEach(el => {
    el.addEventListener("click", (e) => e.stopPropagation());
  });
  grid.querySelectorAll("[data-tz-toggle]").forEach(chk => {
    chk.addEventListener("click", (e) => e.stopPropagation());
    chk.addEventListener("change", (e) => {
      e.stopPropagation();
      toggleRelativeTz(chk.dataset.tzToggle);
      renderGrid();
    });
  });

  // Card "✕" — same untrack action/confirm/toast as the detail panel's
  // "Stop tracking" button (see wireInlineEdits' detailRemove handler),
  // just reachable straight from the roster grid without opening a card.
  //
  // NOTE: previously this listener was registered twice (once here, once
  // below) which caused confirm() to fire twice per click — requiring the
  // user to dismiss two dialogs to delete a single card. Kept only the
  // busy-guarded version below.
  grid.querySelectorAll("[data-untrack]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const username = btn.dataset.untrack;
      if (!confirm(`Stop tracking ${username}? This deletes all history and cannot be undone.`)) return;
      if (btn.dataset.busy === "1") return;
      btn.dataset.busy = "1";
      btn.disabled = true;
      // Optimistically remove the card immediately; the actual DELETE is
      // deferred behind the undo toast below (see undoToast()) so
      // clicking Undo can cancel it before anything is sent.
      state.items = state.items.filter(item => item.username !== username);
      renderGrid();
      undoToast(
        `🗑️ Removing ${username}…`,
        async () => {
          await api(`/streamers/${encodeURIComponent(username)}`, { method: "DELETE" });
          if (state.selectedUsername === username) {
            state.selectedUsername = null;
            setDetailPanelEmpty(true);
          }
          loadStats();
        },
        () => {
          loadView();
          toast(`Kept ${username}`, "success");
        }
      );
    });
  });

  grid.querySelectorAll("[data-preview-toggle]").forEach(toggle => {
    toggle.addEventListener("click", () => {
      const username = toggle.dataset.previewToggle;
      if (state.openPreviews.has(username)) {
        closePreview(toggle.closest(".streamer-card"), username);
      } else {
        openPreview(toggle.closest(".streamer-card"), username);
      }
    });
  });

  // Roster refreshes (SSE-driven — see connectEventStream) rebuild this
  // grid's HTML from scratch on every live/offline event, which would
  // otherwise silently drop any preview embed someone had open mid-watch.
  // Re-open anything still tracked in state.openPreviews so a running
  // preview keeps playing (starts fresh, since a mid-stream iframe can't
  // carry its playback position across a DOM rebuild) instead of quietly
  // collapsing back to "▶ Preview stream" on the next background refresh.
  state.openPreviews.forEach(username => {
    const toggle = grid.querySelector(`[data-preview-toggle="${CSS.escape(username)}"]`);
    if (toggle) {
      openPreview(grid, username);
    }
  });

  wireInlineEdits(grid);
}

// Expands a card's live-preview embed and remembers it's open so it
// survives the next renderGrid() (see the SSE-refresh note above).
function openPreview(grid, username) {
  const embedEl = grid.querySelector(`[data-preview-embed="${CSS.escape(username)}"]`);
  const toggle = grid.querySelector(`[data-preview-toggle="${CSS.escape(username)}"]`);
  if (!embedEl || !toggle) return;
  const label = toggle.querySelector("[data-preview-label]");
  embedEl.innerHTML = `<iframe src="${twitchEmbedUrl(username)}" allowfullscreen frameborder="0" scrolling="no" allow="autoplay" title="${escapeAttr(username)} live preview"></iframe>`;
  embedEl.classList.add("open");
  if (label) label.textContent = "✕ Hide preview";
  state.openPreviews.add(username);
  if (state.virtualScroll && grid.isConnected) requestAnimationFrame(renderVirtualGrid);
}

// Collapses a card's live-preview embed and drops the iframe entirely
// (not just hides it) so a muted/autoplaying embed isn't still running
// in the background for every card someone has ever previewed.
function closePreview(grid, username) {
  const embedEl = grid.querySelector(`[data-preview-embed="${CSS.escape(username)}"]`);
  const toggle = grid.querySelector(`[data-preview-toggle="${CSS.escape(username)}"]`);
  state.openPreviews.delete(username);
  if (!embedEl || !toggle) return;
  const label = toggle.querySelector("[data-preview-label]");
  embedEl.classList.remove("open");
  embedEl.innerHTML = "";
  if (label) label.textContent = "▶ Preview stream";
  if (state.virtualScroll && grid.isConnected) requestAnimationFrame(renderVirtualGrid);
}

// Inline editing for Priority/Tags directly on each card, as an
// alternative to opening the detail panel. Kept intentionally minimal:
// same PUT endpoints the detail panel already uses, so no new backend
// surface was needed beyond what /priority and /tags already do.
function wireInlineEdits(container) {
  container.querySelectorAll(".card-inline-edit").forEach(wrap => {
    wrap.addEventListener("click", (e) => e.stopPropagation());
    wrap.addEventListener("mousedown", (e) => e.stopPropagation());

    const username = wrap.dataset.username;
    const prioritySelect = wrap.querySelector("[data-inline-priority]");
    const tagsInput = wrap.querySelector("[data-inline-tags]");

    prioritySelect.addEventListener("change", async (e) => {
      const value = e.target.value;
      const previous = prioritySelect.dataset.lastValue || prioritySelect.dataset.priorityBefore || "";
      const item = state.items.find(i => i.username === username);
      const priorBadgeValue = item ? item.priority : previous;
      // ⚡ Optimistic: update the card's own class instantly and the
      // in-memory item, before the request resolves — rolled back to
      // the prior value only if the PUT fails.
      prioritySelect.className = "card-inline-priority badge-priority-" + value;
      if (item) item.priority = value;
      try {
        await api(`/streamers/${username}/priority`, { method: "PUT", body: JSON.stringify({ priority: value }) });
        toast("✅ Priority updated", "success");
        if (state.selectedUsername === username) selectStreamer(username);
      } catch (err) {
        prioritySelect.value = priorBadgeValue;
        prioritySelect.className = "card-inline-priority badge-priority-" + priorBadgeValue;
        if (item) item.priority = priorBadgeValue;
        toast(err.message, "error");
      }
    });

    let lastSavedTags = tagsInput.value;
    tagsInput.addEventListener("click", (e) => e.stopPropagation());
    tagsInput.addEventListener("blur", async () => {
      const value = tagsInput.value;
      if (value === lastSavedTags) return;
      const item = state.items.find(i => i.username === username);
      const priorTags = item ? item.tags : undefined;
      // ⚡ Optimistic: assume the save succeeds and adopt the new value
      // right away; roll the field and in-memory item back only on error.
      const priorSaved = lastSavedTags;
      lastSavedTags = value;
      if (item) item.tags = value.split(",").map(t => t.trim()).filter(Boolean);
      try {
        await api(`/streamers/${username}/tags`, { method: "PUT", body: JSON.stringify({ tags: value }) });
        toast("✅ Tags saved", "success");
        loadCategories();
        if (state.selectedUsername === username) selectStreamer(username);
      } catch (err) {
        lastSavedTags = priorSaved;
        tagsInput.value = priorSaved;
        if (item) item.tags = priorTags;
        toast(err.message, "error");
      }
    });
    tagsInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") tagsInput.blur();
    });
  });
}

// Compact single-glyph labels for scraped social links (from the
// channel's bio + panels — see backend twitch_api.get_channel_social),
// shown as small icon-links on each roster card. Falls back to the
// platform's first letter for anything not in this table so an unusual
// platform still renders something reasonable instead of nothing.
const SOCIAL_GLYPHS = {
  "Twitter / X": "𝕏",
  "Discord": "💬",
  "YouTube": "▶",
  "Instagram": "📷",
  "TikTok": "🎵",
  "Facebook": "f",
  "Kick": "K",
  "Reddit": "👽",
  "Threads": "@",
  "Bluesky": "🦋",
};

// User-supplied icon overrides, one image URL (or data: URL, for an
// uploaded file) per platform label — lets a person swap in their own
// icon image instead of the built-in text glyph above. Stored locally
// only (not synced anywhere), matching how other per-device look/feel
// settings (PREF_KEY) are handled.
const CUSTOM_ICON_KEY = "scoutbot_custom_social_icons";

function loadCustomIcons() {
  try {
    const icons = JSON.parse(localStorage.getItem(CUSTOM_ICON_KEY) || "{}");
    return icons && typeof icons === "object" && !Array.isArray(icons) ? icons : {};
  } catch (e) {
    return {};
  }
}

function saveCustomIcons(icons) {
  try { localStorage.setItem(CUSTOM_ICON_KEY, JSON.stringify(icons)); }
  catch (e) { toast("Could not save icons: browser storage is full or unavailable.", "error"); }
}

// All platform labels a custom icon can be set for — the ones the
// scraper already recognizes (SOCIAL_GLYPHS) plus the two manually-
// entered link types (Twitter / X and Instagram are already covered by
// SOCIAL_GLYPHS; YouTube/Kick below cover the manual-entry fields that
// aren't otherwise part of a scraped label), plus any settings-defined
// custom platforms (see loadSettingsCustomPlatforms) so a custom
// platform gets its own icon row here too.
const SOCIAL_ICON_LABELS = Object.keys(SOCIAL_GLYPHS);

// Populated from GET /api/social-platforms whenever the Icons/Custom
// platforms tab is opened — kept separate from SOCIAL_ICON_LABELS since
// that list is a fixed built-in set, while this reflects whatever's
// currently saved server-side.
let customPlatformLabels = [];

function renderSettingsIcons() {
  const container = document.getElementById("settingsIconsList");
  if (!container) return;
  const icons = loadCustomIcons();
  const allLabels = [...SOCIAL_ICON_LABELS, ...customPlatformLabels.filter(l => !SOCIAL_ICON_LABELS.includes(l))];

  container.innerHTML = allLabels.map((label) => {
    const current = icons[label] || "";
    const previewSrc = current ? escapeAttr(current) : "";
    const safeId = "icon_" + label.replace(/[^a-z0-9]/gi, "_");
    return `
      <div class="field-group" style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">
        <div style="width:28px;height:28px;flex:none;display:flex;align-items:center;justify-content:center;border-radius:6px;background:var(--bg-tertiary,#222);overflow:hidden;">
          ${current
            ? `<img src="${previewSrc}" alt="" style="width:100%;height:100%;object-fit:cover;">`
            : `<span>${SOCIAL_GLYPHS[label] || label.charAt(0)}</span>`}
        </div>
        <div style="flex:1;">
          <label class="field-label">${escapeHtml(label)}</label>
          <input type="text" class="field-input custom-icon-url-input" data-label="${escapeAttr(label)}" id="${safeId}_url" placeholder="Image URL (leave blank for default)" value="${previewSrc}">
        </div>
        <input type="file" accept="image/*" class="custom-icon-file-input" data-label="${escapeAttr(label)}" id="${safeId}_file" style="width:150px;">
        ${current ? `<button class="btn btn-ghost btn-small custom-icon-clear" data-label="${escapeAttr(label)}" style="width:auto;">Clear</button>` : ""}
      </div>`;
  }).join("");

  container.querySelectorAll(".custom-icon-url-input").forEach((input) => {
    input.addEventListener("change", () => {
      const label = input.dataset.label;
      const value = input.value.trim();
      const current = loadCustomIcons();
      if (value) current[label] = value; else delete current[label];
      saveCustomIcons(current);
      renderSettingsIcons();
      // BUGFIX: loadCategories() only refreshes the category filter
      // dropdown, not the roster cards — so a saved custom icon never
      // showed up on existing cards until something else happened to
      // trigger a full re-render (e.g. reloading the view). renderGrid()
      // is the function that actually redraws the cards (and their
      // socialLinksHtml() icons), so call that instead/also.
      renderGrid();
    });
  });

  container.querySelectorAll(".custom-icon-file-input").forEach((input) => {
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const label = input.dataset.label;
      const reader = new FileReader();
      reader.onload = () => {
        const current = loadCustomIcons();
        current[label] = reader.result;
        saveCustomIcons(current);
        renderSettingsIcons();
        renderGrid();
      };
      reader.readAsDataURL(file);
    });
  });

  container.querySelectorAll(".custom-icon-clear").forEach((btn) => {
    btn.addEventListener("click", () => {
      const label = btn.dataset.label;
      const current = loadCustomIcons();
      delete current[label];
      saveCustomIcons(current);
      renderSettingsIcons();
      renderGrid();
    });
  });
}

// Formats the `last_live` ISO timestamp (see database.update_streamer /
// last_live column — updated whenever a poll finds a streamer live) into
// a short display date for the "Last streamed on …" line shown alongside
// the other scraped info (age, location) on the roster card and detail
// panel. Returns "" for a missing/unparseable value so callers can just
// skip rendering the line, same pattern as the scraped_age/location checks.
function formatLastLiveDate(lastLive) {
  if (!lastLive) return "";
  const d = new Date(lastLive);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function socialLinksHtml(socialLinks) {
  if (!socialLinks || !Object.keys(socialLinks).length) return "";
  const customIcons = loadCustomIcons();
  const items = Object.entries(socialLinks).map(([label, url]) => {
    const customIcon = customIcons[label];
    const inner = customIcon
      ? `<img src="${escapeAttr(customIcon)}" alt="${escapeAttr(label)}" style="width:16px;height:16px;object-fit:cover;border-radius:3px;">`
      : (SOCIAL_GLYPHS[label] || label.charAt(0));
    return `<a class="card-social-link" href="${escapeAttr(url)}" target="_blank" rel="noopener" title="${escapeAttr(label)}" onclick="event.stopPropagation()">${inner}</a>`;
  }).join("");
  return `<div class="card-social-links">${items}</div>`;
}

// Pulls a bare @handle out of a profile URL for the compact
// "IG: @handle || Twitter: @handle" line on roster cards — falls back to
// the raw URL (trimmed of protocol) if the shape isn't recognized so a
// pasted-in link still shows something readable instead of nothing.
function handleFromUrl(url) {
  if (!url) return "";
  const match = url.match(/^https?:\/\/(?:www\.)?[^/]+\/@?([^/?#]+)/i);
  return match ? match[1] : url.replace(/^https?:\/\//i, "");
}

// Compact "IG: @user || Twitter: @user" line shown on roster cards for
// the manually-entered Instagram/X links (separate from the scraped
// social icon row above, which covers whatever platforms were found in
// the channel's bio/panels).
function manualSocialLine(s) {
  const parts = [];
  if (s.instagram_url) parts.push(`IG: @${escapeHtml(handleFromUrl(s.instagram_url))}`);
  if (s.x_url) parts.push(`Twitter: @${escapeHtml(handleFromUrl(s.x_url))}`);
  if (!parts.length) return "";
  return `<div class="card-manual-social">${parts.join(" || ")}</div>`;
}

// Twitch's channel embed requires a `parent` param matching the exact
// hostname the page is served from (Twitch checks it server-side); using
// location.hostname here means this works unmodified whether ScoutBot is
// reached via 127.0.0.1, localhost, or a LAN hostname (WEB_HOST). Both
// "localhost" and "127.0.0.1" are included as extra `parent` values (the
// player accepts several) since the two aren't interchangeable to Twitch's
// check — a person can reach the same server through either, or through a
// bookmarked link that predates a WEB_HOST change, so this covers that
// without needing the hostname to match exactly.
function twitchEmbedUrl(username) {
  const parents = new Set([location.hostname || "localhost", "localhost", "127.0.0.1"]);
  const parentParams = [...parents].map(p => `parent=${encodeURIComponent(p)}`).join("&");
  return `https://player.twitch.tv/?channel=${encodeURIComponent(username)}&${parentParams}&muted=true&autoplay=true`;
}

// 📈 Inline sparkline of recent viewer counts on each roster card,
// showing trend rather than just the current number. Backed by the
// existing GET /streamers/{username}/history endpoint (viewer_history —
// no new backend surface needed). Rendered lazily: cardHtml() only
// emits an empty placeholder + data-sparkline-for attribute, and
// hydrateSparklines() (called after each grid render) fetches and fills
// them in, so a full-grid rebuild doesn't fire one history request per
// card up front — only for cards that actually end up on screen.
const _sparklineCache = new Map(); // username -> array of viewer counts, oldest→newest
const SPARKLINE_POINTS = 12;

function sparklineSvg(values) {
  // A 2-point sparkline is just a single straight segment between the
  // two most recent readings — with no shape to it, it reads as an
  // arbitrary diagonal line rather than an actual trend (this is what
  // was being reported as "random lines" on freshly-live cards that
  // only have one or two viewer_history rows so far). Require at least
  // 3 points so the line only appears once there's a real trend to show;
  // a fresh session's slot stays an empty placeholder until then, same
  // as it already does for 0/1 points.
  if (!values || values.length < 3) return "";
  const w = 64, h = 20, pad = 2;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stepX = (w - pad * 2) / (values.length - 1);
  const points = values.map((v, i) => {
    const x = pad + i * stepX;
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const trendUp = values[values.length - 1] >= values[0];
  return `<svg class="card-sparkline ${trendUp ? "trend-up" : "trend-down"}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

async function fetchSparklineData(username) {
  if (_sparklineCache.has(username)) return _sparklineCache.get(username);
  try {
    const data = await api(`/streamers/${encodeURIComponent(username)}/history?limit=${SPARKLINE_POINTS}`);
    const values = (data.viewer_history || [])
      .map(r => r.viewers)
      .filter(v => typeof v === "number")
      .reverse(); // API returns newest-first; sparkline reads left(oldest)->right(newest)
    _sparklineCache.set(username, values);
    return values;
  } catch (e) {
    return null; // best-effort — a missing sparkline just leaves the placeholder empty
  }
}

// Fetches + fills in sparklines for every card currently in `container`
// that doesn't have one yet. Only targets live streamers (recent viewer
// history is only meaningful while a stream has been live), and skips
// any card whose data is already cached, so re-renders (pin toggle,
// preview open, etc.) don't refetch history that's already known.
function hydrateSparklines(container) {
  const placeholders = container.querySelectorAll("[data-sparkline-for]");
  placeholders.forEach(async (el) => {
    const username = el.dataset.sparklineFor;
    const cached = _sparklineCache.get(username);
    if (cached) { el.innerHTML = sparklineSvg(cached); return; }
    const values = await fetchSparklineData(username);
    // Card may have been removed/re-rendered by the time this resolves
    // (grid rebuild, view switch, filter change) — re-query by username
    // rather than trusting the captured `el` is still attached.
    const liveEl = document.querySelector(`[data-sparkline-for="${cssEscape(username)}"]`);
    if (liveEl && values) liveEl.innerHTML = sparklineSvg(values);
  });
}

function cardHtml(s) {
  const isLive = s.live_status === "Live";
  const selected = s.username === state.selectedUsername;
  const avatar = s.profile_image
    ? `<img class="card-avatar" src="${escapeAttr(s.profile_image)}" alt="">`
    : `<div class="card-avatar-fallback">${initials(s.username)}</div>`;

  // Inline priority/tags editing: lets priority be changed and tags be
  // edited right on the card (a "row" in the grid) without opening the
  // detail panel. Clicks/interactions inside .card-inline-edit stop
  // propagation so they don't also trigger selectStreamer() via the
  // card's own click handler.
  const priorityOptions = ["High", "Medium", "Watch", "Ignore"]
    .map(p => `<option value="${p}" ${s.priority === p ? "selected" : ""}>${p}</option>`)
    .join("");

  const pinned = isPinned(s.username);

  return `
    <div class="streamer-card ${isLive ? "is-live" : ""} ${selected ? "selected" : ""} ${pinned ? "is-pinned" : ""}" data-username="${escapeAttr(s.username)}">
      <div class="pulse-bar"></div>
      <input type="checkbox" class="card-select-box" style="right:76px" data-select-streamer="${escapeAttr(s.username)}" aria-label="Select ${escapeAttr(s.username)}" ${state.selectedUsernames.has(s.username) ? "checked" : ""}>
      <button class="card-pin-btn ${pinned ? "active" : ""}" data-pin-toggle="${escapeAttr(s.username)}" title="${pinned ? "Unpin" : "Pin to top"}" aria-label="${pinned ? "Unpin " + escapeAttr(s.username) : "Pin " + escapeAttr(s.username) + " to top"}" aria-pressed="${pinned}">📌</button>
      <button class="card-untrack-btn" data-untrack="${escapeAttr(s.username)}" title="Stop tracking ${escapeAttr(s.username)}" aria-label="Stop tracking ${escapeAttr(s.username)}">✕</button>
      <div class="card-top">
        ${avatar}
        <div class="card-identity">
          <div class="card-username">
            ${isLive ? '<span class="live-dot"></span>' : ""}
            ${escapeHtml(s.alias || s.username)}
            ${s.favourite ? '<span class="fav-star">★</span>' : ""}
          </div>
          <div class="card-category">${escapeHtml(s.category || "Unknown")}</div>
          ${s.scraped_age ? `<div class="card-category" style="opacity:0.75;">🎂 ${escapeHtml(String(s.scraped_age))}</div>` : ""}
          ${s.effective_location && shouldShowLocationColumn() ? `<div class="card-category" style="opacity:0.75;">${locationBadgeHtml(s.username, s.effective_location, s.effective_timezone, s.location ? "✏️" : "🌐")}</div>` : ""}
          ${s.last_live ? `<div class="card-category" style="opacity:0.75;">📅 Last streamed on ${escapeHtml(formatLastLiveDate(s.last_live))}</div>` : ""}
        </div>
      </div>
      <div class="card-stats">
        <div class="cstat">
          <span class="cstat-val">${formatCompact(s.followers)}</span>
          <span class="cstat-label">Followers</span>
        </div>
        <div class="cstat">
          <span class="cstat-val">${isLive ? formatCompact(s.current_viewers) : "—"}</span>
          <span class="cstat-label">Viewers</span>

        </div>
        <div class="cstat">
          <span class="cstat-val">${s.live_raid_score}</span>
          <span class="cstat-label">Raid score</span>
        </div>
      </div>
      <div class="card-inline-edit" data-username="${escapeAttr(s.username)}">
        <select class="card-inline-priority badge-priority-${s.priority}" data-inline-priority title="Priority">
          ${priorityOptions}
        </select>
        <input type="text" class="card-inline-tags" data-inline-tags placeholder="tags…" value="${escapeAttr((s.tags || []).join(", "))}" title="Tags (comma separated)">
      </div>
      <div class="card-badges">
        ${s.archived ? '<span class="badge badge-score">Archived</span>' : ""}
        ${s.outreach_status && s.outreach_status !== "active" ? `<span class="badge badge-outreach-${escapeAttr(s.outreach_status)}">${escapeHtml(outreachLabel(s.outreach_status))}</span>` : ""}
      </div>
      ${socialLinksHtml(s.scraped_social_links)}
      ${manualSocialLine(s)}
      ${isLive ? `
        <div class="card-preview-toggle" data-preview-toggle="${escapeAttr(s.username)}" onclick="event.stopPropagation()">
          <span data-preview-label>▶ Preview stream</span>
        </div>
        <div class="card-preview-embed" data-preview-embed="${escapeAttr(s.username)}" onclick="event.stopPropagation()"></div>
      ` : ""}
    </div>
  `;
}

// 🗺️ Fills in the roster's empty state with something more useful than a
// flat "nothing here" — when filters are active (search/priority/
// category/live-only), it points at that instead of implying there's
// nothing tracked at all, and offers a one-click way to clear them.
function renderEmptyState() {
  const empty = document.getElementById("emptyState");
  const title = empty.querySelector(".empty-state-title");
  const body = empty.querySelector(".empty-state-body");
  const hasFilters = state.view === "roster" && (state.search || state.priority || state.category || state.location || state.liveOnly || state.minFollowers != null || state.maxFollowers != null || state.tags);

  let existingClear = document.getElementById("emptyStateClear");
  if (existingClear) existingClear.remove();

  if (hasFilters) {
    title.textContent = "🔍 No streamers match these filters";
    body.textContent = "Try loosening a filter — clear the search text, switch priority/category to \"Any\", or turn off Live only.";
    const btn = document.createElement("button");
    btn.id = "emptyStateClear";
    btn.className = "btn btn-ghost btn-small";
    btn.style.width = "auto";
    btn.style.marginTop = "12px";
    btn.textContent = "🧹 Clear filters";
    btn.addEventListener("click", clearRosterFilters);
    empty.appendChild(btn);
  } else if (state.view === "favourites") {
    title.textContent = "⭐ No favourites yet";
    body.textContent = "Star a streamer from their detail panel to pin them here.";
  } else if (state.view === "raid") {
    title.textContent = "🎯 No raid candidates right now";
    body.textContent = "Raid candidates show up once tracked streamers are live with a solid raid score.";
  } else if (state.view === "inactive") {
    title.textContent = "💤 Nothing inactive";
    body.textContent = "Everyone tracked has been live recently — nice and active roster.";
  } else if (state.view === "archived") {
    title.textContent = "🗄️ Nothing archived";
    body.textContent = "Archived streamers show up here once you archive them from their detail panel.";
  } else {
    title.textContent = "👋 No streamers here yet";
    body.textContent = "Track a streamer or run a Discover search to populate this view.";
  }
}

function clearRosterFilters() {
  state.search = "";
  state.priority = "";
  state.category = "";
  state.location = "";
  state.liveOnly = false;
  state.minFollowers = null;
  state.maxFollowers = null;
  state.tags = "";
  state.selectedUsernames.clear();
  state.page = 1;
  document.getElementById("searchInput").value = "";
  document.getElementById("filterPriority").value = "";
  document.getElementById("filterCategory").value = "";
  document.getElementById("filterLocation").value = "";
  document.getElementById("filterLiveOnly").checked = false;
  loadView();
}

function renderPagination() {
  const el = document.getElementById("pagination");
  if (state.virtualScroll) {
    el.innerHTML = `<span>${state.total.toLocaleString()} total &middot; virtual scroll</span>`;
    return;
  }
  const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
  el.innerHTML = `
    <button id="pgPrev" ${state.page <= 1 ? "disabled" : ""}>&larr;</button>
    <span>Page ${state.page} / ${totalPages} &middot; ${state.total} total</span>
    <button id="pgNext" ${state.page >= totalPages ? "disabled" : ""}>&rarr;</button>
  `;
  document.getElementById("pgPrev").onclick = () => { state.page--; loadView(); };
  document.getElementById("pgNext").onclick = () => { state.page++; loadView(); };
}

// ===================== DASHBOARD (custom widgets) =====================
// A configurable home screen: a fixed catalog of small widgets, each
// backed by an endpoint the app already calls elsewhere (stats/
// favourites/raid/recently-viewed), toggled on/off and reordered by
// `dashboardLayout` — kept in localStorage as a simple per-device
// preference, same reasoning as pinning/density above rather than a new
// backend field. Order in the array is display order; membership is
// on/off.

const DASHBOARD_KEY = "scoutbot_dashboard_widgets";
// 🔢 Per-widget config (currently just "how many rows to show") — kept
// as its own localStorage key/map rather than folded into
// DASHBOARD_KEY so a widget's size preference survives being toggled
// off and back on, same reasoning as keeping layout/pin/density as
// separate per-device preferences.
const DASHBOARD_CONFIG_KEY = "scoutbot_dashboard_widget_config";

const DASHBOARD_WIDGETS = {
  overview: { title: "📊 Overview", render: renderOverviewWidget },
  live_now: { title: "🔴 Live Now", render: renderLiveNowWidget, sizable: true, defaultLimit: 6 },
  favourites: { title: "⭐ Favourites", render: renderFavouritesWidget, sizable: true, defaultLimit: 6 },
  raid: { title: "🎯 Raid Candidates", render: renderRaidWidget, sizable: true, defaultLimit: 6 },
  recently_viewed: { title: "🕒 Recently Viewed", render: renderRecentlyViewedWidget, sizable: true, defaultLimit: 6 },
};

// Maps each widget id to how it pulls its slice out of a
// GET /api/dashboard-summary response (see renderDashboard()) — keeps
// that mapping in one place next to the widget catalog rather than
// scattered across each render*Widget() call site.
const DASHBOARD_SUMMARY_SLICE = {
  overview: (summary) => summary.stats,
  live_now: (summary) => summary.live_now.items,
  favourites: (summary) => summary.favourites.items,
  raid: (summary) => summary.raid.items,
  recently_viewed: (summary) => summary.recently_viewed.items,
};

const DASHBOARD_WIDGET_SIZE_OPTIONS = [3, 6, 10];

const DASHBOARD_DEFAULT_LAYOUT = ["overview", "live_now", "favourites", "raid", "recently_viewed"];

function loadDashboardLayout() {
  try {
    const raw = JSON.parse(localStorage.getItem(DASHBOARD_KEY) || "null");
    if (!Array.isArray(raw)) return [...DASHBOARD_DEFAULT_LAYOUT];
    // Drop any stale/unknown ids (e.g. a widget removed in a later
    // version) rather than rendering an empty/broken tile for them.
    return raw.filter(id => DASHBOARD_WIDGETS[id]);
  } catch (e) {
    return [...DASHBOARD_DEFAULT_LAYOUT];
  }
}

function saveDashboardLayout(layout) {
  localStorage.setItem(DASHBOARD_KEY, JSON.stringify(layout));
}

function loadDashboardConfig() {
  try {
    const raw = JSON.parse(localStorage.getItem(DASHBOARD_CONFIG_KEY) || "null");
    return raw && typeof raw === "object" ? raw : {};
  } catch (e) {
    return {};
  }
}

function saveDashboardConfig(config) {
  localStorage.setItem(DASHBOARD_CONFIG_KEY, JSON.stringify(config));
}

function dashboardWidgetLimit(id) {
  const w = DASHBOARD_WIDGETS[id];
  if (!w || !w.sizable) return null;
  const configured = dashboardConfig[id] && dashboardConfig[id].limit;
  return DASHBOARD_WIDGET_SIZE_OPTIONS.includes(configured) ? configured : w.defaultLimit;
}

function setDashboardWidgetLimit(id, limit) {
  if (!DASHBOARD_WIDGET_SIZE_OPTIONS.includes(limit)) return;
  dashboardConfig[id] = { ...(dashboardConfig[id] || {}), limit };
  saveDashboardConfig(dashboardConfig);
}

let dashboardLayout = loadDashboardLayout();
let dashboardConfig = loadDashboardConfig();

// 🗂️ Saved dashboard layouts — named snapshots of {layout, config} so a
// person can switch between e.g. "Scouting mode" and "Overview mode"
// instead of only ever having the one per-device layout above.
// `dashboardLayout`/`dashboardConfig` remain the single "currently
// active" state (unchanged from before — every existing render/toggle/
// reorder/resize path keeps reading and writing those same two
// variables), so saved layouts are purely an extra save/restore layer
// on top rather than a replacement for how the dashboard already works.
const DASHBOARD_SAVED_LAYOUTS_KEY = "scoutbot_dashboard_saved_layouts";
const DASHBOARD_ACTIVE_LAYOUT_NAME_KEY = "scoutbot_dashboard_active_layout_name";

function loadSavedDashboardLayouts() {
  try {
    const raw = JSON.parse(localStorage.getItem(DASHBOARD_SAVED_LAYOUTS_KEY) || "null");
    return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  } catch (e) {
    return {};
  }
}

function saveSavedDashboardLayouts(named) {
  localStorage.setItem(DASHBOARD_SAVED_LAYOUTS_KEY, JSON.stringify(named));
}

function loadActiveDashboardLayoutName() {
  return localStorage.getItem(DASHBOARD_ACTIVE_LAYOUT_NAME_KEY) || "";
}

function saveActiveDashboardLayoutName(name) {
  if (name) localStorage.setItem(DASHBOARD_ACTIVE_LAYOUT_NAME_KEY, name);
  else localStorage.removeItem(DASHBOARD_ACTIVE_LAYOUT_NAME_KEY);
}

let savedDashboardLayouts = loadSavedDashboardLayouts();
let activeDashboardLayoutName = loadActiveDashboardLayoutName();

// Saves the current layout+config as `name` (overwrites if it already
// exists) and marks it active.
function saveDashboardLayoutAs(name) {
  name = (name || "").trim();
  if (!name) return false;
  savedDashboardLayouts[name] = {
    layout: [...dashboardLayout],
    config: JSON.parse(JSON.stringify(dashboardConfig)),
  };
  saveSavedDashboardLayouts(savedDashboardLayouts);
  activeDashboardLayoutName = name;
  saveActiveDashboardLayoutName(name);
  return true;
}

// Switches the active layout+config to a saved one and re-renders the
// dashboard if it's the current view — same load path renderDashboard()
// already uses, just fed a different starting layout/config.
function applyDashboardLayout(name) {
  const saved = savedDashboardLayouts[name];
  if (!saved) return false;
  dashboardLayout = (saved.layout || []).filter(id => DASHBOARD_WIDGETS[id]);
  dashboardConfig = saved.config && typeof saved.config === "object" ? saved.config : {};
  saveDashboardLayout(dashboardLayout);
  saveDashboardConfig(dashboardConfig);
  activeDashboardLayoutName = name;
  saveActiveDashboardLayoutName(name);
  if (state.view === "dashboard") renderDashboard();
  return true;
}

function deleteDashboardLayout(name) {
  delete savedDashboardLayouts[name];
  saveSavedDashboardLayouts(savedDashboardLayouts);
  if (activeDashboardLayoutName === name) {
    activeDashboardLayoutName = "";
    saveActiveDashboardLayoutName("");
  }
}

function setDashboardVisible(show) {
  document.getElementById("dashboardView").style.display = show ? "block" : "none";
  document.getElementById("cardGrid").style.display = show ? "none" : "";
  document.getElementById("emptyState").style.display = "none";
  document.getElementById("rosterListControls").style.display = show ? "none" : "";
}

async function renderOverviewWidget(prefetched) {
  let stats = { total: 0, live: 0, followers: 0 };
  try { stats = prefetched || await fetchStatsCached(); } catch (e) { /* non-fatal */ }
  return `
    <div class="dashboard-stat-row">
      <div class="dashboard-stat"><span class="dashboard-stat-val">${(stats.total || 0).toLocaleString()}</span><span class="dashboard-stat-label">tracked</span></div>
      <div class="dashboard-stat"><span class="dashboard-stat-val">${(stats.live || 0).toLocaleString()}</span><span class="dashboard-stat-label">live now</span></div>
      <div class="dashboard-stat"><span class="dashboard-stat-val">${formatCompact(stats.followers)}</span><span class="dashboard-stat-label">total reach</span></div>
    </div>`;
}

// 🔗 Empty/error states get an actionable button where there's a
// sensible deep-link target, instead of just describing the problem —
// same destination (switch to the roster view, apply a search term)
// the row-click handler in renderDashboard() already uses, just
// reachable without any items to click first.
function dashboardEmptyHtml(message, action) {
  if (!action) return `<div class="dashboard-widget-empty">${escapeHtml(message)}</div>`;
  return `
    <div class="dashboard-widget-empty">
      ${escapeHtml(message)}
      <button type="button" class="btn btn-ghost btn-small dashboard-widget-action" data-dash-action="${escapeAttr(action.type)}" ${action.value ? `data-dash-value="${escapeAttr(action.value)}"` : ""} style="width:auto;margin-top:8px;">${escapeHtml(action.label)}</button>
    </div>`;
}

function dashboardListHtml(items, emptyMessage, emptyAction) {
  if (!items.length) return dashboardEmptyHtml(emptyMessage, emptyAction);
  return `<div class="dashboard-widget-list">${items.map(s => `
    <div class="dashboard-widget-row" data-dash-username="${escapeAttr(s.username)}">
      ${s.live_status === "Live" ? '<span class="live-dot"></span>' : '<span class="dashboard-dot-spacer"></span>'}
      <span class="dashboard-widget-row-name">${escapeHtml(s.alias || s.username)}</span>
      <span class="dashboard-widget-row-meta">${formatCompact(s.followers)} followers</span>
    </div>`).join("")}</div>`;
}

// Each render*Widget() below accepts an optional `prefetched` items array.
// renderDashboard()'s full-layout load fetches every widget's data in one
// /api/dashboard-summary round trip (see below) and passes each widget its
// slice directly, skipping the individual request entirely. The single-
// widget paths that still need their own request — toggling a widget on
// (applyDashboardLayoutToggle) and changing a widget's row-count selector —
// call these with no argument, which falls back to the original standalone
// endpoint exactly as before, so those already-optimized "fetch only what
// changed" paths are untouched.
async function renderLiveNowWidget(prefetched) {
  let items = [];
  const limit = dashboardWidgetLimit("live_now");
  try {
    if (prefetched) {
      items = prefetched;
    } else {
      const data = await api("/streamers/search" + qs({ live_only: true, sort_by: "current_viewers", ascending: false, page: 1, page_size: limit }));
      items = data.items || [];
    }
  } catch (e) {
    return dashboardEmptyHtml("Couldn't load who's live.", { type: "roster", label: "Go to roster" });
  }
  return dashboardListHtml(items, "Nobody tracked is live right now.", { type: "roster", label: "View roster" });
}

async function renderFavouritesWidget(prefetched) {
  let items = [];
  const limit = dashboardWidgetLimit("favourites");
  try {
    if (prefetched) {
      items = prefetched;
    } else {
      // Capped server-side via ?limit= (see GET /api/favourites) instead
      // of fetching every favourite and slicing client-side — same
      // result for this widget, smaller response on a large favourites
      // list. Other favourites callers (the roster's Favourites view,
      // etc.) still call this with no limit and get everything, as before.
      const data = await api("/favourites" + qs({ limit }));
      items = data.items || [];
    }
  } catch (e) {
    return dashboardEmptyHtml("Couldn't load favourites.", { type: "view", value: "favourites", label: "Go to favourites" });
  }
  return dashboardListHtml(items, "Star a streamer to see them here.", { type: "roster", label: "Favourite a streamer" });
}

async function renderRaidWidget(prefetched) {
  let items = [];
  const limit = dashboardWidgetLimit("raid");
  try {
    if (prefetched) {
      items = prefetched;
    } else {
      const data = await api("/raid/candidates" + qs({ limit }));
      items = data.items || [];
    }
  } catch (e) {
    return dashboardEmptyHtml("Couldn't load raid candidates.", { type: "view", value: "raid", label: "Go to raid candidates" });
  }
  return dashboardListHtml(items, "No raid candidates right now.", { type: "view", value: "raid", label: "View raid candidates" });
}

async function renderRecentlyViewedWidget(prefetched) {
  let items = [];
  const limit = dashboardWidgetLimit("recently_viewed");
  try {
    if (prefetched) {
      items = prefetched;
    } else {
      const data = await api("/recently-viewed" + qs({ limit }));
      items = data.items || [];
    }
  } catch (e) {
    return dashboardEmptyHtml("Couldn't load recently viewed.", { type: "roster", label: "Browse roster" });
  }
  return dashboardListHtml(items, "Nothing viewed yet.", { type: "roster", label: "Browse roster" });
}

// 🏁 Race-condition guard: mirrors _loadViewToken for the roster list —
// rapid view switches or repeated widget-toggle saves (each of which
// calls renderDashboard() again) could otherwise let a slower/older
// render's Promise.all resolve after a newer one and write stale widget
// HTML into what's now a different render pass's DOM.
let _dashboardRenderToken = 0;
const _widgetRenderTokens = new Map();

// Shared "leave the dashboard for a roster-backed view" navigation —
// used by both the existing row-click (jump straight to a streamer)
// and the new empty/error-state action buttons (jump to a view with
// no search applied). Kept as one helper so both paths stay in sync.
function goToRosterView(viewName, searchValue) {
  state.search = searchValue || "";
  state.page = 1;
  document.querySelectorAll(".rail-item").forEach(b => b.classList.remove("active"));
  const rosterBtn = document.querySelector(`.rail-item[data-view="${viewName}"]`) || document.querySelector('.rail-item[data-view="roster"]');
  rosterBtn.classList.add("active");
  state.view = rosterBtn.dataset.view;
  document.getElementById("viewTitle").textContent = rosterBtn.textContent;
  const searchInput = document.getElementById("searchInput");
  if (searchInput) searchInput.value = state.search;
  loadView();
}

function dashboardWidgetSizeControlHtml(id) {
  const w = DASHBOARD_WIDGETS[id];
  if (!w || !w.sizable) return "";
  const current = dashboardWidgetLimit(id);
  return `
    <select class="dashboard-widget-size" data-widget-size="${escapeAttr(id)}" title="Entries to show" aria-label="${escapeAttr(w.title)} — entries to show">
      ${DASHBOARD_WIDGET_SIZE_OPTIONS.map(n => `<option value="${n}" ${n === current ? "selected" : ""}>${n}</option>`).join("")}
    </select>`;
}

async function renderDashboard() {
  const grid = document.getElementById("dashboardGrid");
  const empty = document.getElementById("dashboardEmpty");
  const token = ++_dashboardRenderToken;

  if (!dashboardLayout.length) {
    grid.innerHTML = "";
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  // Render tiles with a loading placeholder first, then fill each in as
  // its own data resolves — avoids one slow widget blocking the rest of
  // the dashboard from appearing. Each tile is draggable so the layout
  // order (dashboardLayout) can be reordered by hand, not just toggled.
  grid.innerHTML = dashboardLayout.map(id => `
    <div class="dashboard-widget" data-widget="${escapeAttr(id)}" draggable="true">
      <div class="dashboard-widget-title">
        <span class="dashboard-widget-drag-handle" title="Drag to reorder" aria-hidden="true">⠿</span>
        <span class="dashboard-widget-title-text">${DASHBOARD_WIDGETS[id].title}</span>
        ${dashboardWidgetSizeControlHtml(id)}
      </div>
      <div class="dashboard-widget-body" id="dashWidgetBody-${escapeAttr(id)}">${dashboardWidgetSkeletonHtml()}</div>
    </div>`).join("");

  wireDashboardDragAndDrop(grid);

  grid.querySelectorAll("[data-widget-size]").forEach(sel => {
    sel.addEventListener("click", e => e.stopPropagation());
    sel.addEventListener("change", () => {
      const id = sel.dataset.widgetSize;
      setDashboardWidgetLimit(id, parseInt(sel.value, 10));
      renderSingleDashboardWidget(id, token);
    });
  });

  // 🧩 One combined round trip for the whole layout instead of each
  // widget firing its own request in parallel (stats, live-now,
  // favourites, raid, recently-viewed — previously 4-5 separate fetches
  // every time the dashboard loaded). Each widget's own limit (its
  // configured 3/6/10 size, or the catalog default for non-sizable
  // widgets like Overview) is passed through so the combined response
  // matches exactly what the individual endpoints would have returned.
  // If this fails outright (e.g. offline), fall back to each widget
  // fetching independently — same end result, just without the single-
  // request saving for that one load.
  const widgetSizes = new Map(dashboardLayout.map(id => [id, dashboardWidgetLimit(id)]));
  let summary = null;
  try {
    summary = await api("/dashboard-summary" + qs({
      live_limit: dashboardWidgetLimit("live_now") || 6,
      favourites_limit: dashboardWidgetLimit("favourites") || 6,
      raid_limit: dashboardWidgetLimit("raid") || 6,
      recently_viewed_limit: dashboardWidgetLimit("recently_viewed") || 6,
    }));
  } catch (e) { /* per-widget fetch fallback below */ }

  if (token !== _dashboardRenderToken) return;
  await Promise.all(dashboardLayout.filter(id => dashboardWidgetLimit(id) === widgetSizes.get(id)).map(id => renderSingleDashboardWidget(id, token, summary)));
}

async function renderSingleDashboardWidget(id, token, summary) {
  const generation = (_widgetRenderTokens.get(id) || 0) + 1;
  _widgetRenderTokens.set(id, generation);
  const targetBody = document.getElementById(`dashWidgetBody-${id}`);
  let html;
  try {
    html = await DASHBOARD_WIDGETS[id].render(summary ? DASHBOARD_SUMMARY_SLICE[id](summary) : undefined);
  } catch (e) {
    html = dashboardEmptyHtml("Couldn't load.", { type: "roster", label: "Go to roster" });
  }
  if (token !== _dashboardRenderToken || _widgetRenderTokens.get(id) !== generation) return;
  const body = document.getElementById(`dashWidgetBody-${escapeAttr(id)}`);
  if (!body || body !== targetBody) return;
  body.innerHTML = html;
  body.querySelectorAll("[data-dash-username]").forEach(row => {
    row.addEventListener("click", () => goToRosterView("roster", row.dataset.dashUsername));
  });
  body.querySelectorAll("[data-dash-action]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const type = btn.dataset.dashAction;
      if (type === "view") {
        goToRosterView(btn.dataset.dashValue, "");
      } else {
        goToRosterView("roster", "");
      }
    });
  });
}

// 🧲 Drag-and-drop widget reordering — HTML5 drag events on each
// `.dashboard-widget` tile. Drop position is resolved by comparing the
// dragged tile's index against the tile under the pointer, same
// left-to-right/top-to-bottom order the grid already renders in, then
// the new order is written straight into `dashboardLayout` and saved.
let _dashDragId = null;

function wireDashboardDragAndDrop(grid) {
  const tiles = () => Array.from(grid.querySelectorAll(".dashboard-widget"));

  tiles().forEach(tile => {
    if (tile.dataset.dragWired) return;
    tile.dataset.dragWired = "1";
    tile.addEventListener("dragstart", (e) => {
      _dashDragId = tile.dataset.widget;
      tile.classList.add("dashboard-widget-dragging");
      e.dataTransfer.effectAllowed = "move";
      // Some browsers require setData to enable the drag.
      try { e.dataTransfer.setData("text/plain", _dashDragId); } catch (err) { /* non-fatal */ }
    });

    tile.addEventListener("dragend", () => {
      tile.classList.remove("dashboard-widget-dragging");
      grid.querySelectorAll(".dashboard-widget-drop-target").forEach(t => t.classList.remove("dashboard-widget-drop-target"));
      _dashDragId = null;
    });

    tile.addEventListener("dragover", (e) => {
      if (!_dashDragId || _dashDragId === tile.dataset.widget) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      grid.querySelectorAll(".dashboard-widget-drop-target").forEach(t => t.classList.remove("dashboard-widget-drop-target"));
      tile.classList.add("dashboard-widget-drop-target");
    });

    tile.addEventListener("drop", (e) => {
      e.preventDefault();
      tile.classList.remove("dashboard-widget-drop-target");
      const draggedId = _dashDragId;
      const targetId = tile.dataset.widget;
      _dashDragId = null;
      if (!draggedId || draggedId === targetId) return;

      const from = dashboardLayout.indexOf(draggedId);
      const to = dashboardLayout.indexOf(targetId);
      if (from === -1 || to === -1) return;

      dashboardLayout.splice(from, 1);
      dashboardLayout.splice(to, 0, draggedId);
      saveDashboardLayout(dashboardLayout);
      renderDashboard();
    });
  });
}

// 🎯 Incremental update for a pure on/off toggle — adds/removes exactly
// one tile in place instead of the full renderDashboard() rebuild
// (which tears down and re-fetches every widget). Reordering still
// goes through renderDashboard()/wireDashboardDragAndDrop() since a
// drag changes every tile's position; this path only ever changes
// membership, so already-rendered tiles are left completely untouched
// — no re-fetch, no loading flicker for widgets that didn't change.
function applyDashboardLayoutToggle(id, added) {
  const grid = document.getElementById("dashboardGrid");
  const empty = document.getElementById("dashboardEmpty");
  if (!grid) return;

  if (!dashboardLayout.length) {
    grid.innerHTML = "";
    if (empty) empty.style.display = "block";
    return;
  }
  if (empty) empty.style.display = "none";

  if (!added) {
    const tile = grid.querySelector(`.dashboard-widget[data-widget="${cssEscape(id)}"]`);
    if (tile) tile.remove();
    else renderDashboard(); // fell out of sync somehow — fall back to a full rebuild
    return;
  }

  // Newly-added widget: append a tile in its dashboardLayout position
  // (always the end — toggling on appends, same as the old push()
  // behavior) and fetch only that widget's data.
  const token = _dashboardRenderToken; // not bumped — existing tiles' in-flight fetches (if any) stay valid
  const tileHtml = `
    <div class="dashboard-widget" data-widget="${escapeAttr(id)}" draggable="true">
      <div class="dashboard-widget-title">
        <span class="dashboard-widget-drag-handle" title="Drag to reorder" aria-hidden="true">⠿</span>
        <span class="dashboard-widget-title-text">${DASHBOARD_WIDGETS[id].title}</span>
        ${dashboardWidgetSizeControlHtml(id)}
      </div>
      <div class="dashboard-widget-body" id="dashWidgetBody-${escapeAttr(id)}">${dashboardWidgetSkeletonHtml()}</div>
    </div>`;
  grid.insertAdjacentHTML("beforeend", tileHtml);
  const newTile = grid.querySelector(`.dashboard-widget[data-widget="${cssEscape(id)}"]`);
  if (newTile) {
    wireDashboardDragAndDrop(grid); // re-wire so the new tile participates in drag/drop too
    const sel = newTile.querySelector("[data-widget-size]");
    if (sel) {
      sel.addEventListener("click", e => e.stopPropagation());
      sel.addEventListener("change", () => {
        setDashboardWidgetLimit(id, parseInt(sel.value, 10));
        renderSingleDashboardWidget(id, token);
      });
    }
  }
  renderSingleDashboardWidget(id, token);
}

// Minimal CSS.escape fallback for the data-widget attribute selector
// above — widget ids are our own fixed catalog keys (see
// DASHBOARD_WIDGETS), never user input, but this keeps the selector
// safe without pulling in a dependency for what CSS.escape already
// does natively where available.
function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function renderDashboardCustomizeList() {
  const list = document.getElementById("dashboardWidgetToggleList");
  // Listed in current layout order first (drag order on the dashboard
  // itself), then any not-yet-enabled widgets — so this list reflects
  // the same order reordering produces, instead of always the fixed
  // catalog order.
  const orderedIds = [...dashboardLayout, ...Object.keys(DASHBOARD_WIDGETS).filter(id => !dashboardLayout.includes(id))];

  list.innerHTML = orderedIds.map(id => {
    const w = DASHBOARD_WIDGETS[id];
    return `
    <div class="dashboard-widget-toggle-row">
      <label class="rail-checkbox">
        <input type="checkbox" data-widget-toggle="${escapeAttr(id)}" ${dashboardLayout.includes(id) ? "checked" : ""}>
        <span>${w.title}</span>
      </label>
      ${w.sizable ? `<label class="dashboard-widget-toggle-size-label">Show
        <select data-widget-size="${escapeAttr(id)}" title="Entries to show">
          ${DASHBOARD_WIDGET_SIZE_OPTIONS.map(n => `<option value="${n}" ${n === dashboardWidgetLimit(id) ? "selected" : ""}>${n}</option>`).join("")}
        </select>
      </label>` : ""}
    </div>`;
  }).join("");

  list.querySelectorAll("[data-widget-toggle]").forEach(chk => {
    chk.addEventListener("change", () => {
      const id = chk.dataset.widgetToggle;
      const added = chk.checked;
      if (added) {
        if (!dashboardLayout.includes(id)) dashboardLayout.push(id);
      } else {
        dashboardLayout = dashboardLayout.filter(x => x !== id);
      }
      saveDashboardLayout(dashboardLayout);
      if (state.view === "dashboard") applyDashboardLayoutToggle(id, added);
    });
  });

  list.querySelectorAll("[data-widget-size]").forEach(sel => {
    sel.addEventListener("change", () => {
      setDashboardWidgetLimit(sel.dataset.widgetSize, parseInt(sel.value, 10));
      if (state.view === "dashboard") renderDashboard();
    });
  });
}

// 🗂️ Renders the "Saved layouts" list inside the Customize modal — one
// row per named layout (Use / Delete), plus a name input + Save button
// that snapshots the current layout+config under that name. Kept as a
// separate render function (called alongside renderDashboardCustomizeList,
// not merged into it) so the widget-toggle list logic above stays
// untouched.
function renderDashboardSavedLayoutsList() {
  const list = document.getElementById("dashboardSavedLayoutsList");
  if (!list) return;
  const names = Object.keys(savedDashboardLayouts).sort((a, b) => a.localeCompare(b));

  if (!names.length) {
    list.innerHTML = `<div class="notes-hint">No saved layouts yet — set up your widgets above, then save this arrangement with a name.</div>`;
  } else {
    list.innerHTML = names.map(name => `
      <div class="dashboard-widget-toggle-row" data-saved-layout-row="${escapeAttr(name)}">
        <span>${name === activeDashboardLayoutName ? "✅ " : ""}${escapeHtml(name)}</span>
        <span style="display:flex;gap:6px;">
          <button class="btn btn-ghost btn-small" style="width:auto;" data-saved-layout-use="${escapeAttr(name)}" ${name === activeDashboardLayoutName ? "disabled" : ""}>Use</button>
          <button class="btn btn-ghost btn-small" style="width:auto;" data-saved-layout-delete="${escapeAttr(name)}" title="Delete this saved layout" aria-label="Delete saved layout ${escapeAttr(name)}">🗑️</button>
        </span>
      </div>`).join("");
  }

  list.querySelectorAll("[data-saved-layout-use]").forEach(btn => {
    btn.addEventListener("click", () => {
      applyDashboardLayout(btn.dataset.savedLayoutUse);
      renderDashboardCustomizeList();
      renderDashboardSavedLayoutsList();
      toast(`🗂️ Switched to "${btn.dataset.savedLayoutUse}"`, "success");
    });
  });

  list.querySelectorAll("[data-saved-layout-delete]").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.savedLayoutDelete;
      if (!confirm(`Delete saved layout "${name}"?`)) return;
      deleteDashboardLayout(name);
      renderDashboardSavedLayoutsList();
      toast(`Deleted "${name}"`, "success");
    });
  });
}

document.getElementById("btnCustomizeDashboard").addEventListener("click", () => {
  renderDashboardCustomizeList();
  renderDashboardSavedLayoutsList();
  openModal("dashboardCustomizeModal");
});

const dashboardLayoutSaveBtn = document.getElementById("dashboardLayoutSaveBtn");
if (dashboardLayoutSaveBtn) {
  dashboardLayoutSaveBtn.addEventListener("click", () => {
    const input = document.getElementById("dashboardLayoutNameInput");
    const name = input.value.trim();
    if (!name) { toast("Enter a name for this layout", "error"); return; }
    const existed = !!savedDashboardLayouts[name];
    saveDashboardLayoutAs(name);
    input.value = "";
    renderDashboardSavedLayoutsList();
    toast(existed ? `🗂️ Updated "${name}"` : `🗂️ Saved "${name}"`, "success");
  });
}

const dashboardLayoutNameInput = document.getElementById("dashboardLayoutNameInput");
if (dashboardLayoutNameInput) {
  dashboardLayoutNameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); dashboardLayoutSaveBtn.click(); }
  });
}

// ===================== DETAIL PANEL =====================

// Collapses the detail panel's reserved column (see .layout in
// styles.css) down to nothing when no streamer is selected, instead of
// always holding the space open around the "Select a streamer to see
// details" placeholder. Every place that clears the selection funnels
// through this so the panel and its column width stay in sync. This is
// independent of the manual collapse toggle below (detail-panel-
// manually-collapsed) — either one collapses the same column, and
// selecting a streamer here doesn't override a manual collapse the
// person set on purpose (see setDetailPanelEmpty(false) below — it now
// also lifts a manual collapse, see the fix note there).
function setDetailPanelEmpty(isEmpty) {
  if (isEmpty) { ++_detailToken; _detailFlush?.(); _detailFlush = null; }
  const layout = document.getElementById("layout");
  const empty = document.getElementById("detailEmpty");
  const content = document.getElementById("detailContent");
  if (layout) {
    layout.classList.toggle("detail-panel-collapsed", isEmpty);
    // Bugfix: selecting a streamer (isEmpty === false) used to leave a
    // manually-collapsed panel collapsed (grid-template-columns keeps the
    // detail column at 0px — see .layout.detail-panel-manually-collapsed
    // in styles.css), so the detail content rendered but stayed hidden
    // off-screen and the Watch Stream button was unreachable. Selecting a
    // streamer is an explicit request to see that streamer's detail, so
    // it should always make the panel visible, the same way dragging the
    // resizer open already does (see initDetailResizer's mousedown
    // handler below) — otherwise this was the one place that still
    // silently deferred to the old manual-collapse flag.
    if (isEmpty === false && layout.classList.contains("detail-panel-manually-collapsed")) {
      layout.classList.remove("detail-panel-manually-collapsed");
      localStorage.setItem(DETAIL_COLLAPSED_KEY, "0");
      const collapseBtn = document.getElementById("detailCollapseBtn");
      if (collapseBtn) {
        collapseBtn.textContent = "›";
        collapseBtn.title = "Collapse detail panel";
        collapseBtn.setAttribute("aria-label", "Collapse detail panel");
      }
    }
  }
  if (empty) empty.style.display = isEmpty ? "block" : "none";
  if (content) content.style.display = isEmpty ? "none" : "block";
}

// Resizable/collapsible detail panel column. Previously the third grid
// column was purely a binary 340px/0px swap driven by whether a streamer
// was selected (setDetailPanelEmpty above, unchanged and still handling
// that case) — this adds a genuine drag-to-resize handle plus a manual
// collapse/expand toggle independent of selection state, so the panel can
// be narrowed, widened, or tucked away on purpose and stays that way
// (persisted per device) regardless of what's selected.
const DETAIL_WIDTH_KEY = "scoutbot-detail-panel-width";
const DETAIL_COLLAPSED_KEY = "scoutbot-detail-panel-manually-collapsed";
const DETAIL_WIDTH_MIN = 260;
const DETAIL_WIDTH_MAX = 640;
const DETAIL_WIDTH_DEFAULT = 340;

function clampDetailWidth(px) {
  return Math.min(DETAIL_WIDTH_MAX, Math.max(DETAIL_WIDTH_MIN, Math.round(px)));
}

function applyDetailWidth(px) {
  const layout = document.getElementById("layout");
  if (layout) layout.style.setProperty("--detail-w", `${clampDetailWidth(px)}px`);
}

function initDetailResizer() {
  const layout = document.getElementById("layout");
  const resizer = document.getElementById("detailResizer");
  const collapseBtn = document.getElementById("detailCollapseBtn");
  if (!layout || !resizer) return;

  const savedWidth = Number(localStorage.getItem(DETAIL_WIDTH_KEY));
  applyDetailWidth(Number.isFinite(savedWidth) && savedWidth > 0 ? savedWidth : DETAIL_WIDTH_DEFAULT);

  const manuallyCollapsed = localStorage.getItem(DETAIL_COLLAPSED_KEY) === "1";
  layout.classList.toggle("detail-panel-manually-collapsed", manuallyCollapsed);
  if (collapseBtn) collapseBtn.textContent = manuallyCollapsed ? "‹" : "›";

  function setManuallyCollapsed(collapsed) {
    layout.classList.toggle("detail-panel-manually-collapsed", collapsed);
    localStorage.setItem(DETAIL_COLLAPSED_KEY, collapsed ? "1" : "0");
    if (collapseBtn) {
      collapseBtn.textContent = collapsed ? "‹" : "›";
      collapseBtn.title = collapsed ? "Expand detail panel" : "Collapse detail panel";
      collapseBtn.setAttribute("aria-label", collapsed ? "Expand detail panel" : "Collapse detail panel");
    }
  }

  if (collapseBtn) {
    collapseBtn.onclick = (e) => {
      e.stopPropagation();
      setManuallyCollapsed(!layout.classList.contains("detail-panel-manually-collapsed"));
    };
  }

  let dragging = false;
  let startX = 0;
  let startWidth = DETAIL_WIDTH_DEFAULT;

  resizer.addEventListener("mousedown", (e) => {
    // Dragging from a collapsed state re-expands first, at the last saved
    // width, rather than starting the drag from a 0px panel — matches
    // dragging any resizable pane back open in other apps.
    if (layout.classList.contains("detail-panel-manually-collapsed")) {
      setManuallyCollapsed(false);
    }
    dragging = true;
    startX = e.clientX;
    const current = Number(localStorage.getItem(DETAIL_WIDTH_KEY));
    startWidth = Number.isFinite(current) && current > 0 ? current : DETAIL_WIDTH_DEFAULT;
    layout.classList.add("resizing");
    resizer.classList.add("dragging");
    document.body.style.userSelect = "none";
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    // Panel sits to the right of the resizer, so dragging left (negative
    // delta) widens it and dragging right narrows it.
    applyDetailWidth(startWidth + (startX - e.clientX));
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    layout.classList.remove("resizing");
    resizer.classList.remove("dragging");
    document.body.style.userSelect = "";
    const layoutStyle = getComputedStyle(layout).getPropertyValue("--detail-w").trim();
    const px = parseFloat(layoutStyle);
    if (Number.isFinite(px)) localStorage.setItem(DETAIL_WIDTH_KEY, String(clampDetailWidth(px)));
  });

  // Double-clicking the resizer resets to the default width — a quick way
  // back to the original 340px after dragging, without needing to eyeball it.
  resizer.addEventListener("dblclick", () => {
    if (layout.classList.contains("detail-panel-manually-collapsed")) setManuallyCollapsed(false);
    applyDetailWidth(DETAIL_WIDTH_DEFAULT);
    localStorage.setItem(DETAIL_WIDTH_KEY, String(DETAIL_WIDTH_DEFAULT));
  });
}
initDetailResizer();

let _detailFlush = null;
let _detailToken = 0;
let _curveToken = 0;
async function selectStreamer(username) {
  const token = ++_detailToken;
  if (_detailFlush) await _detailFlush();
  if (token !== _detailToken) return;
  saveMainScrollPosition();
  state.selectedUsername = username;
  renderGrid();

  setDetailPanelEmpty(false);
  const content = document.getElementById("detailContent");
  content.innerHTML = `<div class="detail-empty">Loading…</div>`;

  try {
    const s = await api(`/streamers/${encodeURIComponent(username)}`);
    if (token !== _detailToken || state.selectedUsername !== username) return;
    renderDetail(s);
    // Best-effort — a failed log shouldn't block viewing the streamer.
    api("/recently-viewed", { method: "POST", body: JSON.stringify({ username }) })
      .then(loadRecentlyViewed)
      .catch(() => { /* non-fatal */ });
  } catch (e) {
    if (token === _detailToken) toast("Failed to load streamer: " + e.message, "error");
  }
}

// ===================== RECENTLY VIEWED =====================

function recentlyViewedItemHtml(s) {
  return `
    <div class="preset-item" data-select-username="${escapeAttr(s.username)}">
      <span class="preset-name">${escapeHtml(s.alias || s.username)}</span>
      <span class="preset-name" style="flex:none;color:var(--text-dim);font-size:11px;">${escapeHtml(s.category || "")}</span>
    </div>`;
}

async function loadRecentlyViewed() {
  const list = document.getElementById("recentlyViewedList");
  try {
    const data = await api("/recently-viewed" + qs({ limit: 8 }));
    if (!data.items.length) {
      list.innerHTML = `<div class="preset-empty">Nothing viewed yet</div>`;
      return;
    }
    list.innerHTML = data.items.map(recentlyViewedItemHtml).join("");
    list.querySelectorAll("[data-select-username]").forEach(item => {
      item.addEventListener("click", () => selectStreamer(item.dataset.selectUsername));
    });
  } catch (e) { /* non-fatal */ }
}

document.getElementById("btnRefreshRecentlyViewed").addEventListener("click", loadRecentlyViewed);

// ===================== SUGGESTIONS =====================
// "Similar to your favourites" — ranked by category/tag overlap with the
// person's favourited roster. Empty until at least one favourite exists.

function suggestionItemHtml(s) {
  return `
    <div class="preset-item" data-select-username="${escapeAttr(s.username)}">
      <span class="preset-name">${escapeHtml(s.alias || s.username)}</span>
      <span class="preset-name" style="flex:none;color:var(--text-dim);font-size:11px;">${escapeHtml(s.category || "")}</span>
    </div>`;
}

async function loadSuggestions() {
  const list = document.getElementById("suggestionsList");
  try {
    const data = await api("/suggestions" + qs({ limit: 8 }));
    if (!data.items.length) {
      list.innerHTML = `<div class="preset-empty">Favourite a few streamers to get suggestions</div>`;
      return;
    }
    list.innerHTML = data.items.map(suggestionItemHtml).join("");
    list.querySelectorAll("[data-select-username]").forEach(item => {
      item.addEventListener("click", () => selectStreamer(item.dataset.selectUsername));
    });
  } catch (e) {
    // Was silently swallowed here, which made a real failure (bad
    // response, network hiccup) look identical to the normal "no
    // matches yet" empty state above — nothing on screen ever told you
    // suggestions had actually errored out. Show it instead, so a
    // failure is visibly distinguishable from "you have no favourites
    // that match anyone yet".
    list.innerHTML = `<div class="preset-empty">Couldn't load suggestions — try refreshing</div>`;
  }
}

document.getElementById("btnRefreshSuggestions").addEventListener("click", loadSuggestions);

function renderDetail(s) {
  const content = document.getElementById("detailContent");
  const isLive = s.live_status === "Live";
  const avatar = s.profile_image
    ? `<img class="detail-avatar" src="${escapeAttr(s.profile_image)}" alt="">`
    : `<div class="card-avatar-fallback" style="width:56px;height:56px;font-size:18px;">${initials(s.username)}</div>`;

  const ratingRow = (label, key, value) => `
    <div class="rating-row">
      <span>${label}</span>
      <span class="rating-stars" data-rating-key="${key}">
        ${[1,2,3,4,5].map(i => `<span class="rating-star ${i <= value ? "filled" : ""}" data-star="${i}">★</span>`).join("")}
      </span>
    </div>`;

  const locationLine = s.effective_location
    ? `<div class="detail-cat" style="margin-top:2px;">${locationBadgeHtml(s.username, s.effective_location, s.effective_timezone, s.location ? "✏️" : "🌐")}</div>`
    : "";

  content.innerHTML = `
    <div class="detail-header">
      ${avatar}
      <div style="flex:1;min-width:0;">
        <div class="detail-name">
          ${isLive ? '<span class="live-dot"></span>' : ""}
          ${escapeHtml(s.alias || s.username)}
        </div>
        <div class="detail-cat">${escapeHtml(s.category || "Unknown")} &middot; ${s.live_status}${s.scraped_age ? ` &middot; 🎂 ${s.scraped_age}` : ""}</div>
        ${locationLine}
        ${s.last_live ? `<div class="detail-cat" style="margin-top:2px;">📅 Last streamed on ${escapeHtml(formatLastLiveDate(s.last_live))}</div>` : ""}
      </div>
      <button class="btn btn-ghost btn-icon" id="detailClose" title="Close" aria-label="Close streamer detail">✕</button>
    </div>

    <div class="detail-actions">
      <button class="icon-btn ${s.favourite ? "active" : ""}" id="detailFav">${s.favourite ? "★ Favourited" : "☆ Favourite"}</button>
      <button class="icon-btn ${isPinned(s.username) ? "active" : ""}" id="detailPin">${isPinned(s.username) ? "📌 Pinned to top" : "📌 Pin to top"}</button>
      <button class="icon-btn ${s.notify_enabled ? "" : "active"}" id="detailNotify">${s.notify_enabled ? "🔔 Notify on" : "🔕 Muted"}</button>
      <button class="icon-btn" id="detailRefresh">↻ Refresh</button>
      <button class="icon-btn" id="detailVods">⬇️ VODs & Clips</button>
      <a class="icon-btn" style="text-decoration:none;display:inline-block;" href="${escapeAttr(s.url || `https://twitch.tv/${s.username}`)}" target="_blank" rel="noopener">Open on Twitch ↗</a>
      ${s.x_url ? `<a class="icon-btn" style="text-decoration:none;display:inline-block;" href="${escapeAttr(s.x_url)}" target="_blank" rel="noopener">X ↗</a>` : ""}
      ${s.instagram_url ? `<a class="icon-btn" style="text-decoration:none;display:inline-block;" href="${escapeAttr(s.instagram_url)}" target="_blank" rel="noopener">Instagram ↗</a>` : ""}
      ${s.youtube_url ? `<a class="icon-btn" style="text-decoration:none;display:inline-block;" href="${escapeAttr(s.youtube_url)}" target="_blank" rel="noopener">YouTube ↗</a>` : ""}
      ${s.kick_url ? `<a class="icon-btn" style="text-decoration:none;display:inline-block;" href="${escapeAttr(s.kick_url)}" target="_blank" rel="noopener">Kick ↗</a>` : ""}
    </div>

    <div class="detail-grid">
      <div class="detail-metric"><div class="detail-metric-val">${formatCompact(s.followers)}</div><div class="detail-metric-label">Followers</div></div>
      <div class="detail-metric"><div class="detail-metric-val">${isLive ? formatCompact(s.current_viewers) : "—"}</div><div class="detail-metric-label">Current viewers</div></div>
      <div class="detail-metric"><div class="detail-metric-val">${formatCompact(s.average_viewers)}</div><div class="detail-metric-label">Avg viewers</div></div>
      <div class="detail-metric"><div class="detail-metric-val">${formatCompact(s.peak_viewers)}</div><div class="detail-metric-label">Peak viewers</div></div>
      <div class="detail-metric"><div class="detail-metric-val">${s.live_raid_score}</div><div class="detail-metric-label">Raid score</div></div>
      <div class="detail-metric"><div class="detail-metric-val">${s.last_live ? new Date(s.last_live).toLocaleDateString() : "—"}</div><div class="detail-metric-label">Last live</div></div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title-row">
        <div class="detail-section-title">🕒 Live pattern</div>
      </div>
      <div class="detail-subrow">
        <div class="detail-sublabel">Typical live hours (your local time)</div>
        <div id="detailLiveWindow" class="notes-hint" style="margin-bottom:0;">Loading…</div>
      </div>
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <div class="detail-sublabel">Session replay — one stream's viewer curve</div>
        <select class="priority-select" id="detailSessionPicker" style="margin-bottom:8px;"></select>
        <div id="detailSessionCurve"></div>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Pipeline</div>
      <div class="detail-2col">
        <div>
          <div class="detail-sublabel">Priority</div>
          <select class="priority-select" id="detailPriority">
            ${["High","Medium","Watch","Ignore"].map(p => `<option value="${p}" ${s.priority === p ? "selected" : ""}>${p}</option>`).join("")}
          </select>
        </div>
        <div>
          <div class="detail-sublabel">Outreach</div>
          <select class="priority-select" id="detailOutreach">
            ${["active","contacted","raided","declined"].map(o => `<option value="${o}" ${(s.outreach_status || "active") === o ? "selected" : ""}>${outreachLabel(o)}</option>`).join("")}
          </select>
        </div>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Ratings &amp; response</div>
      ${ratingRow("Community", "community", s.community_rating || 0)}
      ${ratingRow("Content", "content", s.content_rating || 0)}
      ${ratingRow("Raid suitability", "raid", s.raid_rating || 0)}
      <div class="detail-subdivider"></div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">
        <select class="priority-select" id="detailResponseType" style="width:auto;flex:1;min-width:140px;">
          ${["followed_back","replied","reacted","declined","no_response"].map(t => `<option value="${t}">${responseLabel(t)}</option>`).join("")}
        </select>
        <button class="btn btn-primary btn-small" id="detailResponseLog" style="width:auto;">Log</button>
      </div>
      <div id="detailResponseHistory" class="notes-hint" style="margin-bottom:0;">Loading…</div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Tags &amp; links</div>
      <div class="detail-subrow">
        <div id="detailTags">${(s.tags || []).map(t => `<span class="tag-chip">${escapeHtml(t)}</span>`).join("") || '<span class="detail-cat">No tags yet</span>'}</div>
        <input class="detail-textarea" style="min-height:auto;margin-top:8px;" id="detailTagsInput" placeholder="comma, separated, tags" value="${escapeAttr((s.tags || []).join(", "))}">
      </div>
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <input class="field-input" style="margin-bottom:8px;" id="detailXUrl" placeholder="X (Twitter) profile URL" value="${escapeAttr(s.x_url || "")}">
        <input class="field-input" style="margin-bottom:8px;" id="detailInstagramUrl" placeholder="Instagram profile URL" value="${escapeAttr(s.instagram_url || "")}">
        <input class="field-input" style="margin-bottom:8px;" id="detailYoutubeUrl" placeholder="YouTube channel URL" value="${escapeAttr(s.youtube_url || "")}">
        <input class="field-input" id="detailKickUrl" placeholder="Kick channel URL" value="${escapeAttr(s.kick_url || "")}">
      </div>
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <div class="detail-section-title-row">
          <div class="detail-sublabel" style="margin-bottom:0;">Scraped from bio &amp; panels</div>
          <button class="icon-btn" id="detailRescrapeSocial" style="padding:2px 8px;font-size:10.5px;">↻ Re-scrape</button>
        </div>
        ${s.bio ? `<div class="notes-hint" style="margin:6px 0 8px;">${escapeHtml(s.bio)}</div>` : ""}
        <div id="detailScrapedSocial">${socialLinksHtml(s.scraped_social_links) || '<span class="detail-cat">No social links found yet</span>'}</div>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title-row">
        <div class="detail-section-title">Location</div>
        <span class="notes-status" id="detailLocationStatus"></span>
      </div>
      <div class="detail-subrow">
        <div class="detail-sublabel">Manual — overrides the scraped location below when set</div>
        <div class="field-with-clear" style="margin-bottom:8px;">
          <input class="field-input" id="detailLocation" placeholder="e.g. Auckland, NZ" value="${escapeAttr(s.location || "")}" list="detailLocationOptions" autocomplete="off">
          <datalist id="detailLocationOptions">${locationDatalistOptionsHtml()}</datalist>
          <button class="field-clear-btn" id="detailLocationClear" title="Clear location" aria-label="Clear location">✕</button>
        </div>
        <select class="field-input" id="detailTimezone">${timezoneOptionsHtml(s.timezone)}</select>
      </div>
      ${s.scraped_location ? `
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <div class="detail-sublabel" style="margin-bottom:0;">Scraped from bio</div>
        <div class="notes-hint" style="margin-bottom:0;">${locationBadgeHtml(s.username + ":scraped", s.scraped_location, s.scraped_timezone, "🌐")}</div>
      </div>` : ""}
      ${s.scraped_age ? `
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <div class="detail-sublabel" style="margin-bottom:0;">Age (scraped from bio &amp; panels)</div>
        <div class="notes-hint" style="margin-bottom:0;">🎂 ${escapeHtml(String(s.scraped_age))}</div>
      </div>` : ""}
      ${s.last_live ? `
      <div class="detail-subdivider"></div>
      <div class="detail-subrow">
        <div class="detail-sublabel" style="margin-bottom:0;">Last streamed on</div>
        <div class="notes-hint" style="margin-bottom:0;">📅 ${escapeHtml(formatLastLiveDate(s.last_live))}</div>
      </div>` : ""}
    </div>

    <div class="detail-section">
      <div class="detail-section-title-row">
        <div class="detail-section-title">Notes</div>
        <span class="notes-status" id="detailNotesStatus"></span>
      </div>
      <textarea class="detail-textarea" id="detailNotes" placeholder="Scouting notes — content fit, raid history, contact context…">${escapeHtml(s.notes || "")}</textarea>
    </div>

    <div class="danger-zone">
      ${s.archived
        ? `<button class="btn btn-ghost btn-small" id="detailUnarchive">Unarchive</button>`
        : `<button class="btn btn-ghost btn-small" id="detailArchive">Archive</button>`}
      <button class="btn btn-danger btn-small" id="detailRemove" style="margin-top:8px;">Stop tracking</button>
    </div>
  `;

  wireDetailEvents(s);
  loadLiveWindow(s.username);
  loadResponseHistory(s.username);
  loadSessionReplay(s.username);
}

// ===================== RESPONSE TRACKING =====================

const RESPONSE_LABELS = {
  followed_back: "Followed back",
  replied: "Replied",
  reacted: "Reacted",
  declined: "Declined",
  no_response: "No response",
};

function responseLabel(type) {
  return RESPONSE_LABELS[type] || type;
}

async function loadResponseHistory(username) {
  const el = document.getElementById("detailResponseHistory");
  if (!el) return;
  try {
    const data = await api(`/streamers/${encodeURIComponent(username)}/responses?limit=10`);
    if (!data.items.length) {
      el.textContent = "No response events logged yet.";
      return;
    }
    el.innerHTML = data.items.map(r => `
      <div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--line);">
        <span>${escapeHtml(responseLabel(r.response_type))}${r.note ? ` — ${escapeHtml(r.note)}` : ""}</span>
        <span style="white-space:nowrap;color:var(--text-dim);">${new Date(r.timestamp).toLocaleDateString()}</span>
      </div>
    `).join("");
  } catch (e) {
    el.textContent = "Couldn't load response history.";
  }
}

// 🕒 Time-zone aware live windows: pulls this streamer's recent
// stream_sessions (already recorded for the history/session views) and
// buckets each session's start hour to find when they're typically live.
// Session start times are recorded server-side as naive local timestamps
// of the machine running ScoutBot (see database.py's create_stream_session),
// which — for a self-hosted single-user app — is standardly treated as
// this browser's own local time too, so no conversion math is needed:
// the hours are simply formatted using the browser's locale/clock.
// If ScoutBot is ever run on a server in a different time zone than the
// browser viewing it, this label is a reasonable approximation, not a
// guarantee — hence "your local time" rather than a hard promise.
async function loadLiveWindow(username) {
  const el = document.getElementById("detailLiveWindow");
  if (!el) return;
  try {
    const data = await api(`/streamers/${encodeURIComponent(username)}/history?limit=30`);
    const sessions = (data.sessions || []).filter(s => s.started);
    if (!sessions.length) {
      el.textContent = "Not enough session history yet — check back after this streamer has gone live a few times.";
      return;
    }

    // Bucket by hour-of-day (0-23) using each session's local start time.
    const hourCounts = new Array(24).fill(0);
    for (const s of sessions) {
      const d = new Date(s.started);
      if (isNaN(d.getTime())) continue;
      hourCounts[d.getHours()]++;
    }

    // Find the densest contiguous-ish window: the 4-hour block (wrapping
    // past midnight) with the most session starts, as a simple, readable
    // summary rather than listing all 24 buckets.
    let bestStart = 0;
    let bestCount = -1;
    for (let start = 0; start < 24; start++) {
      let count = 0;
      for (let offset = 0; offset < 4; offset++) count += hourCounts[(start + offset) % 24];
      if (count > bestCount) { bestCount = count; bestStart = start; }
    }

    const fmtHour = (h) => {
      const d = new Date();
      d.setHours(h, 0, 0, 0);
      return d.toLocaleTimeString(undefined, { hour: "numeric", minute: undefined });
    };
    const windowEnd = (bestStart + 4) % 24;
    const tzLabel = Intl.DateTimeFormat().resolvedOptions().timeZone || "your local time zone";

    if (bestCount <= 0) {
      el.textContent = `No clear pattern yet from the last ${sessions.length} session(s) — check back after more data comes in.`;
      return;
    }

    el.innerHTML = `Usually goes live around <strong>${fmtHour(bestStart)}–${fmtHour(windowEnd)}</strong> (${escapeHtml(tzLabel)}), based on the last ${sessions.length} tracked session${sessions.length === 1 ? "" : "s"}.`;
  } catch (e) {
    el.textContent = "Couldn't load session history for this streamer.";
  }
}

// 📈 Session replay: lets you pick one past stream session from a dropdown
// and see its actual viewer curve (start → end), instead of only the
// aggregate peak/average shown in the metrics grid above. Reuses the same
// /history endpoint for the session list, then fetches the per-session
// curve lazily (only when a session is selected) from the new
// /sessions/{id}/curve endpoint.
async function loadSessionReplay(username) {
  const picker = document.getElementById("detailSessionPicker");
  const curveEl = document.getElementById("detailSessionCurve");
  if (!picker || !curveEl) return;

  picker.innerHTML = `<option>Loading sessions…</option>`;
  curveEl.innerHTML = "";

  try {
    const data = await api(`/streamers/${encodeURIComponent(username)}/history?limit=30`);
    // sessions come back newest-first (ORDER BY id DESC); ids are needed
    // to fetch each one's curve, and only sessions that actually started
    // are worth listing.
    const sessions = (data.sessions || []).filter(s => s.started && s.id != null);

    if (!sessions.length) {
      picker.innerHTML = `<option>No sessions yet</option>`;
      picker.disabled = true;
      curveEl.innerHTML = `<div class="notes-hint" style="margin-bottom:0;">No recorded sessions yet — check back after this streamer has gone live.</div>`;
      return;
    }

    picker.disabled = false;
    picker.innerHTML = sessions.map(s => {
      const started = new Date(s.started);
      const label = isNaN(started.getTime())
        ? `Session #${s.id}`
        : `${started.toLocaleDateString()} ${started.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}${s.ended ? "" : " (live)"}`;
      return `<option value="${s.id}">${escapeHtml(label)}</option>`;
    }).join("");

    picker.onchange = () => renderSessionCurve(username, picker.value);
    if (picker.isConnected && state.selectedUsername === username) renderSessionCurve(username, picker.value);
  } catch (e) {
    picker.innerHTML = `<option>Couldn't load sessions</option>`;
    picker.disabled = true;
    curveEl.textContent = "Couldn't load session replay for this streamer.";
  }
}

async function renderSessionCurve(username, sessionId) {
  const token = ++_curveToken;
  const curveEl = document.getElementById("detailSessionCurve");
  if (!curveEl || !sessionId) return;
  curveEl.innerHTML = `<div class="notes-hint" style="margin-bottom:0;">Loading curve…</div>`;

  try {
    const data = await api(`/streamers/${encodeURIComponent(username)}/sessions/${sessionId}/curve`);
    if (token !== _curveToken || state.selectedUsername !== username || !curveEl.isConnected) return;
    const points = (data.points || []).filter(p => p.viewers != null);

    if (points.length < 2) {
      curveEl.innerHTML = `<div class="notes-hint" style="margin-bottom:0;">Not enough data points for this session yet.</div>`;
      return;
    }

    const w = 480, h = 120, pad = 6;
    const viewers = points.map(p => p.viewers);
    const min = Math.min(...viewers);
    const max = Math.max(...viewers);
    const range = Math.max(max - min, 1);

    const coords = points.map((p, i) => {
      const x = pad + (i / (points.length - 1)) * (w - pad * 2);
      const y = h - pad - ((p.viewers - min) / range) * (h - pad * 2);
      return [x, y];
    });

    const linePath = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
    const areaPath = `${linePath} L${coords[coords.length - 1][0].toFixed(1)},${h - pad} L${coords[0][0].toFixed(1)},${h - pad} Z`;

    const startLabel = new Date(points[0].timestamp);
    const endLabel = new Date(points[points.length - 1].timestamp);
    const fmtTime = d => isNaN(d.getTime()) ? "—" : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

    curveEl.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block;">
        <path d="${areaPath}" fill="var(--accent-dim)" opacity="0.25" stroke="none"></path>
        <path d="${linePath}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></path>
      </svg>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);margin-top:4px;">
        <span>${escapeHtml(fmtTime(startLabel))} · start</span>
        <span>Peak ${formatCompact(max)} · Low ${formatCompact(min)}</span>
        <span>${escapeHtml(fmtTime(endLabel))} · end</span>
      </div>
    `;
  } catch (e) {
    curveEl.textContent = "Couldn't load this session's viewer curve.";
  }
}

const vodState = { username: null, tab: "vods", selected: null, poll: null, token: 0, listToken: 0, busy: false };
function parseVodTime(value) {
  value = (value || "").trim(); if (!value) return null;
  const parts = value.split(":").map(Number);
  if (parts.some(n => !Number.isFinite(n) || n < 0) || parts.length > 3) throw new Error("Use HH:MM:SS");
  return parts.reduce((total, n) => total * 60 + n, 0);
}
function fmtVodDuration(seconds) {
  if (seconds == null) return ""; const h=Math.floor(seconds/3600), m=Math.floor(seconds%3600/60), sec=Math.floor(seconds%60); return [h,m,sec].map((n,i)=>i===0?String(n):String(n).padStart(2,"0")).join(":");
}
function fmtVodSpeed(bytesPerSec) {
  if (bytesPerSec == null) return "";
  if (bytesPerSec >= 1024 * 1024) return (bytesPerSec / (1024 * 1024)).toFixed(1) + " MB/s";
  if (bytesPerSec >= 1024) return (bytesPerSec / 1024).toFixed(0) + " KB/s";
  return Math.round(bytesPerSec) + " B/s";
}
async function loadVodItems() {
  const token = ++vodState.listToken;
  vodState.selected = null;
  const list=document.getElementById("vodList"), err=document.getElementById("vodError"); if (!vodState.username) return;
  list.innerHTML='<div class="preset-empty">Loading…</div>'; err.textContent=""; document.getElementById("vodDownloadPanel").style.display="none"; document.getElementById("vodDownloadBtn").disabled=true;
  try {
    const endpoint=vodState.tab === "clips" ? "clips" : "vods"; const data=await api(`/streamers/${encodeURIComponent(vodState.username)}/${endpoint}?limit=50`);
    if (token !== vodState.listToken) return;
    if (!data.items?.length) { list.innerHTML='<div class="preset-empty">Nothing found.</div>'; return; }
    // VODs (Twitch Videos API) report duration as an already-formatted
    // string like "1h2m3s"; clips (Twitch Clips API) report it as a raw
    // number of seconds — display each the way it needs, using
    // fmtVodDuration for the numeric clip case instead of dumping the
    // raw number ("34" instead of "0:34").
    list.innerHTML=data.items.map((v,i)=>`<button class="vod-item" data-vod-index="${i}"><img src="${escapeAttr(v.thumbnail_url || '')}" alt="" onerror="this.style.display='none'"><span class="vod-item-main"><strong>${escapeHtml(v.title || 'Untitled')}</strong><small>${escapeHtml(typeof v.duration === 'number' ? fmtVodDuration(v.duration) : (v.duration || ''))} · ${escapeHtml(v.created_at ? new Date(v.created_at).toLocaleDateString() : '')}</small></span><span>⬇️</span></button>`).join('');
    list.querySelectorAll('[data-vod-index]').forEach(btn=>btn.onclick=()=>selectVod(data.items[Number(btn.dataset.vodIndex)]));
  } catch(e) { if (token !== vodState.listToken) return; list.innerHTML=`<div class="preset-empty">Couldn't load videos.</div>`; err.textContent=e.message; }
}
function selectVod(item) { vodState.selected=item; document.getElementById("vodSelectedTitle").textContent=item.title || "Selected video"; document.getElementById("vodDownloadPanel").style.display="block"; document.getElementById("vodDownloadBtn").disabled=vodState.busy; if (!vodState.busy) document.getElementById("vodProgress").textContent=""; }
async function openVodModal(username) { ++vodState.token; clearTimeout(vodState.poll); vodState.poll=null; vodState.busy=false; vodState.username=username; vodState.tab="vods"; vodState.selected=null; document.getElementById("vodModalChannel").textContent=`@${username}`; openModal("vodModal"); await loadVodItems(); }
async function startVodDownload() {
  if (!vodState.selected || vodState.busy) return;
  vodState.busy = true;
  const token = vodState.token;
  const progress = document.getElementById("vodProgress");
  const button = document.getElementById("vodDownloadBtn");
  button.disabled = true;
  try {
    const start = parseVodTime(document.getElementById("vodStart").value);
    const end = parseVodTime(document.getElementById("vodEnd").value);
    const job = await api('/vod-downloads', { method:'POST', body:JSON.stringify({url:vodState.selected.url,start_time:start,end_time:end}) });
    if (token !== vodState.token) return;
    progress.textContent = 'Queued…';
    clearTimeout(vodState.poll);
    const poll = async () => {
      if (token !== vodState.token) return;
      try {
        const current = await api(`/vod-downloads/${job.id}`);
        if (token !== vodState.token) return;
        if (current.status === 'downloading') {
          const bits = [`${current.progress || 0}%`];
          if (current.elapsed != null) bits.push(`${fmtVodDuration(current.elapsed)} elapsed`);
          bits.push(current.eta != null ? `ETA ${fmtVodDuration(current.eta)}` : 'ETA estimating…');
          if (current.speed != null) bits.push(fmtVodSpeed(current.speed));
          progress.textContent = `Downloading… ${bits.join(' · ')}`;
        } else progress.textContent = current.status === 'complete' ? 'Complete ✓' : current.status === 'failed' ? `Failed: ${current.error}` : 'Queued…';
        if (['complete','failed'].includes(current.status)) { vodState.busy = false; button.disabled = !vodState.selected; vodState.poll = null; return; }
        vodState.poll = setTimeout(poll, 1000);
      } catch (e) {
        if (token !== vodState.token) return;
        progress.textContent = `Could not read download progress: ${e.message}`;
        vodState.busy = false; button.disabled = false; vodState.poll = null;
      }
    };
    vodState.poll = setTimeout(poll, 1000);
  } catch (e) {
    if (token !== vodState.token) return;
    progress.textContent = e.message; vodState.busy = false; button.disabled = false;
  }
}

function wireDetailEvents(s) {
  const username = s.username;

  const vodBtn = document.getElementById("detailVods");
  if (vodBtn) vodBtn.onclick = () => openVodModal(username);

  const closeBtn = document.getElementById("detailClose");
  if (closeBtn) {
    closeBtn.onclick = () => {
      state.selectedUsername = null;
      setDetailPanelEmpty(true);
      renderGrid();
    };
  }

  document.getElementById("detailFav").onclick = async () => {
    const favBtn = document.getElementById("detailFav");
    const item = state.items.find(i => i.username === username);
    const priorFav = item ? item.favourite : undefined;
    const priorBtnState = favBtn.classList.contains("active");
    // ⚡ Optimistic: flip the button + card star immediately.
    const optimisticFav = !priorBtnState;
    favBtn.classList.toggle("active", optimisticFav);
    favBtn.textContent = optimisticFav ? "★ Favourited" : "☆ Favourite";
    if (item) item.favourite = optimisticFav;
    renderGrid();
    try {
      const res = await api(`/streamers/${username}/favourite/toggle`, { method: "POST" });
      if (item) item.favourite = res.favourite;
      favBtn.classList.toggle("active", res.favourite);
      favBtn.textContent = res.favourite ? "★ Favourited" : "☆ Favourite";
      toast(res.favourite ? "⭐ Added to favourites" : "Removed from favourites", "success");
      if (state.view === "favourites") loadView(); else renderGrid();
      loadSuggestions();
    } catch (e) {
      // Roll back on failure.
      if (item) item.favourite = priorFav;
      favBtn.classList.toggle("active", priorBtnState);
      favBtn.textContent = priorBtnState ? "★ Favourited" : "☆ Favourite";
      renderGrid();
      toast(e.message, "error");
    }
  };

  document.getElementById("detailPin").onclick = () => {
    togglePinned(username);
    toast(isPinned(username) ? `📌 Pinned ${username} to the top` : `Unpinned ${username}`, "success");
    selectStreamer(username);
    if (state.view === "roster" || state.view === "favourites" || state.view === "raid" || state.view === "inactive") loadView();
  };

  document.getElementById("detailNotify").onclick = async () => {
    try {
      const res = await api(`/streamers/${username}/notify/toggle`, { method: "POST" });
      toast(res.notify_enabled ? "🔔 Live notifications on" : "🔕 Live notifications muted", "success");
      selectStreamer(username);
    } catch (e) { toast(e.message, "error"); }
  };

  document.getElementById("detailRefresh").onclick = async () => {
    try {
      toast("🔄 Refreshing from Twitch…");
      await api(`/streamers/${username}/refresh`, { method: "POST" });
      selectStreamer(username);
      loadView();
      loadStats();
    } catch (e) { toast(e.message, "error"); }
  };

  const rescrapeBtn = document.getElementById("detailRescrapeSocial");
  if (rescrapeBtn) {
    rescrapeBtn.onclick = async () => {
      rescrapeBtn.disabled = true;
      rescrapeBtn.textContent = "Scraping…";
      try {
        await api(`/streamers/${username}/social/scrape`, { method: "POST" });
        toast("✅ Re-scraped bio & panels", "success");
        selectStreamer(username);
        loadView();
        loadLocations();
      } catch (e) {
        toast(e.message, "error");
        rescrapeBtn.disabled = false;
        rescrapeBtn.textContent = "↻ Re-scrape";
      }
    };
  }

  document.getElementById("detailPriority").onchange = async (e) => {
    const select = e.target;
    const value = select.value;
    const item = state.items.find(i => i.username === username);
    const priorValue = item ? item.priority : select.dataset.priorValue;
    if (item) item.priority = value;
    renderGrid();
    try {
      await api(`/streamers/${username}/priority`, { method: "PUT", body: JSON.stringify({ priority: value }) });
      toast("✅ Priority updated", "success");
      loadView();
    } catch (e2) {
      if (item) item.priority = priorValue;
      select.value = priorValue;
      renderGrid();
      toast(e2.message, "error");
    }
  };

  const outreachSelect = document.getElementById("detailOutreach");
  if (outreachSelect) {
    outreachSelect.onchange = async (e) => {
      try {
        await api(`/streamers/${username}/outreach`, { method: "PUT", body: JSON.stringify({ status: e.target.value }) });
        toast(`✅ Marked as ${outreachLabel(e.target.value)}`, "success");
        selectStreamer(username);
        loadView();
      } catch (e2) { toast(e2.message, "error"); }
    };
  }

  const responseLogBtn = document.getElementById("detailResponseLog");
  if (responseLogBtn) {
    responseLogBtn.onclick = async () => {
      const typeSelect = document.getElementById("detailResponseType");
      const response_type = typeSelect.value;
      try {
        await api(`/streamers/${username}/responses`, {
          method: "POST",
          body: JSON.stringify({ response_type }),
        });
        toast(`✅ Logged: ${responseLabel(response_type)}`, "success");
        loadResponseHistory(username);
      } catch (e2) { toast(e2.message, "error"); }
    };
  }

  document.querySelectorAll(".rating-stars").forEach(group => {
    group.querySelectorAll(".rating-star").forEach(star => {
      star.onclick = async () => {
        const key = group.dataset.ratingKey;
        const score = Number(star.dataset.star);
        try {
          await api(`/streamers/${username}/rating`, { method: "PUT", body: JSON.stringify({ category: key, score }) });
          selectStreamer(username);
        } catch (e2) { toast(e2.message, "error"); }
      };
    });
  });

  const tagsInput = document.getElementById("detailTagsInput");
  tagsInput.onblur = async () => {
    const before = tagsInput.value;
    if (before === tagsInput.dataset.lastSaved) return;
    const item = state.items.find(i => i.username === username);
    const priorTags = item ? item.tags : undefined;
    const priorSaved = tagsInput.dataset.lastSaved;
    // ⚡ Optimistic: adopt immediately, roll back only on failure.
    tagsInput.dataset.lastSaved = before;
    if (item) item.tags = before.split(",").map(t => t.trim()).filter(Boolean);
    try {
      await api(`/streamers/${username}/tags`, { method: "PUT", body: JSON.stringify({ tags: before }) });
      toast("✅ Tags saved", "success");
      loadCategories();
    } catch (e2) {
      tagsInput.dataset.lastSaved = priorSaved;
      tagsInput.value = priorSaved;
      if (item) item.tags = priorTags;
      toast(e2.message, "error");
    }
  };
  tagsInput.dataset.lastSaved = tagsInput.value;

  const notes = document.getElementById("detailNotes");
  let notesTimer = null;
  let savedNotes = notes.value;
  const notesStatus = document.getElementById("detailNotesStatus");
  const saveNotesNow = async () => {
    clearTimeout(notesTimer);
    const value = notes.value;
    if (value === savedNotes) return;
    const previous = savedNotes;
    savedNotes = value;
    try {
      await api(`/streamers/${username}/notes`, { method: "PUT", body: JSON.stringify({ notes: value }) });
      if (notesStatus) notesStatus.textContent = "Saved";
    } catch (e) { savedNotes = previous; if (notesStatus) notesStatus.textContent = ""; toast(e.message, "error"); }
  };
  notes.oninput = () => {
    clearTimeout(notesTimer);
    if (notesStatus) notesStatus.textContent = "Saving…";
    notesTimer = setTimeout(saveNotesNow, 600);
  };

  const xUrlInput = document.getElementById("detailXUrl");
  const instagramUrlInput = document.getElementById("detailInstagramUrl");
  const youtubeUrlInput = document.getElementById("detailYoutubeUrl");
  const kickUrlInput = document.getElementById("detailKickUrl");
  let socialTimer = null;
  const socialPayload = () => JSON.stringify({ x_url:xUrlInput.value.trim(), instagram_url:instagramUrlInput.value.trim(), youtube_url:youtubeUrlInput.value.trim(), kick_url:kickUrlInput.value.trim() });
  let savedSocial = socialPayload();
  const saveSocialNow = async () => {
    clearTimeout(socialTimer);
    const body = socialPayload();
    if (body === savedSocial) return;
    const previous = savedSocial; savedSocial = body;
    try { await api(`/streamers/${username}/social`, { method:"PUT", body }); }
    catch(e) { savedSocial = previous; toast(e.message,"error"); }
  };
  const saveSocial = () => { clearTimeout(socialTimer); socialTimer = setTimeout(saveSocialNow, 600); };
  xUrlInput.oninput = saveSocial;
  instagramUrlInput.oninput = saveSocial;
  youtubeUrlInput.oninput = saveSocial;
  kickUrlInput.oninput = saveSocial;

  const locationInput = document.getElementById("detailLocation");
  const timezoneSelect = document.getElementById("detailTimezone");
  const locationClearBtn = document.getElementById("detailLocationClear");
  const locationStatus = document.getElementById("detailLocationStatus");
  let locationTimer = null;
  const locationPayload = () => JSON.stringify({location:locationInput.value.trim(),timezone:timezoneSelect.value});
  let savedLocation = locationPayload();
  const saveLocationNow = async () => {
    clearTimeout(locationTimer);
    const body = locationPayload();
    if (body === savedLocation) return;
    const previous = savedLocation; savedLocation = body;
    try {
      await api(`/streamers/${username}/location`, {method:"PUT",body});
      if (locationStatus) locationStatus.textContent = "Saved";
      loadCategories(); loadLocations(); loadView();
    } catch(e) { savedLocation = previous; if (locationStatus) locationStatus.textContent = ""; toast(e.message,"error"); }
  };
  const saveLocation = () => {
    clearTimeout(locationTimer);
    if (locationStatus) locationStatus.textContent = "Saving…";
    locationTimer = setTimeout(saveLocationNow,600);
  };
  locationInput.oninput = saveLocation;
  timezoneSelect.onchange = saveLocation;

  // ✕ clear — same affordance as the roster card's untrack button, for
  // wiping the manual location field in one click instead of deleting
  // the text by hand. Clears the timezone too (a location with no text
  // shouldn't keep a stale timezone selection) and saves immediately.
  if (locationClearBtn) {
    locationClearBtn.onclick = () => {
      locationInput.value = "";
      timezoneSelect.value = "";
      delete timezoneSelect.dataset.autoSuggested;
      saveLocation();
    };
  }

  // Timezone auto-suggest: as the person types a free-text location,
  // guess its timezone server-side (reusing twitch_api._guess_timezone
  // via GET /api/guess-timezone) and pre-select the dropdown so most
  // people never have to hunt through the full IANA list by hand. Only
  // pre-selects when the dropdown hasn't already been set for this
  // streamer, and never overrides a choice the person made themselves —
  // tracked with a data attribute rather than reusing the raw "was it
  // ever set" state, so picking a timezone manually always wins even if
  // that happens after an earlier auto-suggestion.
  let guessTimer = null;
  timezoneSelect.dataset.autoSuggested = timezoneSelect.value ? "0" : "1";
  timezoneSelect.addEventListener("change", () => {
    timezoneSelect.dataset.autoSuggested = "0";
  });
  locationInput.addEventListener("input", () => {
    clearTimeout(guessTimer);
    const text = locationInput.value.trim();
    if (!text) return;
    guessTimer = setTimeout(async () => {
      if (timezoneSelect.value && timezoneSelect.dataset.autoSuggested !== "1") return;
      try {
        const res = await api(`/guess-timezone?location=${encodeURIComponent(text)}`);
        if (!locationInput.isConnected || locationInput.value.trim() !== text) return;
        if (res.timezone && (!timezoneSelect.value || timezoneSelect.dataset.autoSuggested === "1")) {
          if (![...timezoneSelect.options].some(option => option.value === res.timezone)) timezoneSelect.add(new Option(res.timezone, res.timezone));
          timezoneSelect.value = res.timezone;
          timezoneSelect.dataset.autoSuggested = "1";
          saveLocation();
        }
      } catch (e2) { /* best-effort — leave dropdown as-is on failure */ }
    }, 500);
  });

  _detailFlush = async () => {
    clearTimeout(guessTimer);
    const saves = [saveNotesNow(), saveSocialNow(), saveLocationNow(), tagsInput.onblur()];
    for (const [path, promise] of _pendingPutRequests) {
      if (path.startsWith(`/streamers/${username}/`)) saves.push(promise);
    }
    await Promise.allSettled(saves);
  };

  // Collapsible 📍 badge toggle inside the detail panel (same behavior
  // as the roster card's, see renderGrid).
  document.getElementById("detailContent").querySelectorAll("[data-tz-toggle]").forEach(chk => {
    chk.addEventListener("change", () => {
      toggleRelativeTz(chk.dataset.tzToggle);
      const badge = chk.closest(".tz-badge");
      if (badge) badge.querySelector("summary").textContent = `${s.location ? "✏️" : "🌐"} ${s.effective_location} · ${relativeTzUsernames.has(s.username) ? relativeTimeInTimezone(s.effective_timezone) : currentTimeInTimezone(s.effective_timezone)}`;
      renderGrid();
    });
  });

  const archiveBtn = document.getElementById("detailArchive");
  if (archiveBtn) archiveBtn.onclick = async () => {
    if (archiveBtn.disabled) return;
    archiveBtn.disabled = true;
    undoToast(
      `🗄️ Archiving ${username}…`,
      async () => {
        try { await api(`/streamers/${username}/archive`, { method: "POST" }); }
        finally { archiveBtn.disabled = false; }
        loadView();
        if (state.selectedUsername === username) selectStreamer(username);
      },
      () => { archiveBtn.disabled = false; toast(`Kept ${username} active`, "success"); }
    );
  };

  const unarchiveBtn = document.getElementById("detailUnarchive");
  if (unarchiveBtn) unarchiveBtn.onclick = async () => {
    try {
      await api(`/streamers/${username}/unarchive`, { method: "POST" });
      toast("✅ Unarchived", "success");
      loadView();
      selectStreamer(username);
    } catch (e2) { toast(e2.message, "error"); }
  };

  document.getElementById("detailRemove").onclick = async () => {
    if (!confirm(`Stop tracking ${username}? This deletes all history and cannot be undone.`)) return;
    state.selectedUsername = null;
    setDetailPanelEmpty(true);
    undoToast(
      `🗑️ Removing ${username}…`,
      async () => {
        await api(`/streamers/${username}`, { method: "DELETE" });
        loadView();
        loadStats();
      },
      () => {
        toast(`Kept ${username}`, "success");
        loadView();
      }
    );
  };
}

// ===================== ADD STREAMER MODAL =====================

// ---- Focus trapping ----------------------------------------------------
// Keeps keyboard focus (Tab/Shift+Tab) cycling within whichever modal is
// currently open, and restores focus to whatever triggered it on close —
// standard modal-dialog accessibility behaviour. Centralized here in
// openModal()/closeModal() (the single chokepoint every modal already
// goes through) instead of per-modal, so every existing modal gets it for
// free with no changes to the ~10 call sites.
const FOCUSABLE_SELECTOR = [
  "a[href]", "button:not([disabled])", "textarea:not([disabled])",
  "input:not([disabled])", "select:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

let activeModalId = null;
let lastFocusedBeforeModal = null;

function _modalFocusables(modalEl) {
  return Array.from(modalEl.querySelectorAll(FOCUSABLE_SELECTOR))
    .filter(el => el.offsetParent !== null); // visible only
}

function _trapFocusKeydown(e) {
  if (!activeModalId) return;
  const modalEl = document.getElementById(activeModalId);
  if (!modalEl) return;

  if (e.key === "Escape") {
    e.preventDefault();
    closeModal(activeModalId);
    return;
  }

  if (e.key !== "Tab") return;

  const focusables = _modalFocusables(modalEl);
  if (!focusables.length) return;

  const first = focusables[0];
  const last = focusables[focusables.length - 1];

  if (e.shiftKey) {
    if (document.activeElement === first || !modalEl.contains(document.activeElement)) {
      e.preventDefault();
      last.focus();
    }
  } else {
    if (document.activeElement === last || !modalEl.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    }
  }
}

document.addEventListener("keydown", _trapFocusKeydown);

function openModal(id) {
  const modalEl = document.getElementById(id);
  if (!modalEl) return;
  lastFocusedBeforeModal = document.activeElement;
  modalEl.classList.add("open");
  activeModalId = id;
  // Defer to next tick so just-shown content (display:flex etc.) has
  // laid out and offsetParent checks in _modalFocusables see it. Skipped
  // if a caller (e.g. openAddModal) already moved focus into the modal
  // itself synchronously right after calling this, so that explicit
  // choice of field isn't immediately overridden.
  setTimeout(() => {
    if (activeModalId !== id) return;
    if (modalEl.contains(document.activeElement) && document.activeElement !== modalEl) return;
    const focusables = _modalFocusables(modalEl);
    (focusables[0] || modalEl).focus();
  }, 0);
}

async function prepareDatabaseReplacement() {
  window.ScoutNobodyDiscovery?.reset();
  document.activeElement?.blur();
  if (_detailFlush) await _detailFlush();
  _detailFlush = null;
  state.selectedUsername = null;
  setDetailPanelEmpty(true);
  for (const record of [..._pendingUndos]) record.cancel();
  await Promise.allSettled([..._pendingMutations]);
  ++_loadViewToken; ++_dashboardRenderToken;
  _rosterController?.abort(); _showAllController?.abort();
  state.selectedUsernames.clear(); updateSelectionToolbar();
}

async function refreshAfterDatabaseReplacement() {
  _statsCache = null; ++_statsGeneration;
  await Promise.all([loadStats(), loadCategories(), loadLocations(), loadPresets(), loadRecentlyViewed(), loadSuggestions(), loadView()]);
}

function closeModal(id) {
  const modalEl = document.getElementById(id);
  if (!modalEl) return;
  modalEl.classList.remove("open");
  // The VOD downloader's progress poll (see startVodDownload) has no
  // other close hook of its own — without this it keeps polling
  // /api/vod-downloads/{job_id} every second in the background for the
  // rest of the page's life once a download starts, even after this
  // modal is dismissed.
  if (id === "vodModal") {
    ++vodState.token; ++vodState.listToken;
    clearTimeout(vodState.poll);
    vodState.poll = null;
    vodState.busy = false;
  }
  if (id === "discoverModal") cancelDiscoverRequest();
  if (id === "nobodyDiscoverModal") window.ScoutNobodyDiscovery?.cancel();
  if (activeModalId === id) {
    activeModalId = null;
    // Restore focus to whatever opened the modal, so keyboard users land
    // back where they were instead of at the top of the page.
    if (lastFocusedBeforeModal && document.body.contains(lastFocusedBeforeModal)) {
      lastFocusedBeforeModal.focus();
    }
    lastFocusedBeforeModal = null;
  }
}

document.querySelectorAll("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => closeModal(btn.dataset.close));
});

// Clicking the dimmed backdrop itself (not the modal card) closes it too —
// standard modal behaviour, and gives mouse users a second way out that
// matches Escape for keyboard users.
//
// A plain "click" listener isn't enough: a text-selection drag that starts
// inside the modal card (e.g. copying a stat) and is released over the
// backdrop still fires a click with e.target === backdrop, closing the
// modal even though the user never intended to dismiss it. So we also
// require the mousedown that started this interaction to have landed on
// the backdrop itself — only a true backdrop-to-backdrop click (press and
// release both outside the card) closes the modal.
document.querySelectorAll(".modal-backdrop").forEach(backdrop => {
  let mousedownOnBackdrop = false;
  backdrop.addEventListener("mousedown", (e) => {
    mousedownOnBackdrop = (e.target === backdrop);
  });
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop && mousedownOnBackdrop) closeModal(backdrop.id);
    mousedownOnBackdrop = false;
  });
  // Make the backdrop itself a valid fallback focus target (see openModal's
  // `focusables[0] || modalEl`) without adding it to normal Tab order.
  if (!backdrop.hasAttribute("tabindex")) backdrop.setAttribute("tabindex", "-1");
});

// Recognizes a twitch.tv/<username> link (with or without scheme/query)
// so a pasted URL can be turned straight into a username, matching the
// backend's parse_twitch_username(). Kept in sync deliberately simple.
const TWITCH_URL_RE = /(?:https?:\/\/)?(?:www\.)?twitch\.tv\/([a-zA-Z0-9_]{2,25})(?:[/?#].*)?$/i;

function extractTwitchUsername(text) {
  if (!text) return null;
  text = text.trim();
  const match = TWITCH_URL_RE.exec(text);
  if (match) return match[1].toLowerCase();
  const bare = text.replace(/^@/, "");
  if (/^[a-zA-Z0-9_]{2,25}$/.test(bare)) return bare.toLowerCase();
  return null;
}

function openAddModal(prefill = "") {
  const input = document.getElementById("addUsernameInput");
  input.value = prefill;
  document.getElementById("addError").textContent = "";
  openModal("addModal");
  input.focus();
}

document.getElementById("btnAdd").addEventListener("click", () => openAddModal());

// Quick-add from URL: as the person types/pastes into the field, a full
// twitch.tv link is parsed down to just the username immediately, so
// pasting "https://twitch.tv/shroud?foo=bar" resolves to "shroud" without
// requiring a separate step.
document.getElementById("addUsernameInput").addEventListener("paste", (e) => {
  const pasted = (e.clipboardData || window.clipboardData).getData("text");
  const parsed = extractTwitchUsername(pasted);
  if (parsed) {
    e.preventDefault();
    e.target.value = parsed;
  }
});

document.getElementById("addSubmit").addEventListener("click", async () => {
  const input = document.getElementById("addUsernameInput");
  const errEl = document.getElementById("addError");
  const username = extractTwitchUsername(input.value) || input.value.trim();
  if (!username) { errEl.textContent = "Enter a username or twitch.tv link"; return; }

  errEl.textContent = "";
  try {
    await api("/streamers", { method: "POST", body: JSON.stringify({ username }) });
    toast(`✅ Now tracking ${username}`, "success");
    closeModal("addModal");
    state.page = 1;
    loadView();
    loadStats();
    loadCategories();
  } catch (e) {
    errEl.textContent = e.message;
  }
});

document.getElementById("addUsernameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("addSubmit").click();
});

// Quick-add from anywhere: pasting a twitch.tv link into the page while
// no text field is focused (i.e. not already typing into some other
// input/textarea) opens the add-streamer flow pre-filled with the parsed
// username. This is a convenience shortcut, not a replacement for the
// modal's own paste handling above.
document.addEventListener("paste", (e) => {
  const active = document.activeElement;
  const isTyping = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA");
  if (isTyping) return;

  const pasted = (e.clipboardData || window.clipboardData).getData("text");
  if (!pasted || !/twitch\.tv\//i.test(pasted)) return;

  const parsed = extractTwitchUsername(pasted);
  if (!parsed) return;

  e.preventDefault();
  openAddModal(parsed);
});

// ===================== DISCOVER MODAL =====================

const discoverState = {
  items: [],       // accumulated across pages
  nextCursor: null,
  loadingMore: false,
  // 🏁 Tracks the in-flight search/load-more request so a fresh filter
  // change (or a second click) can cancel it before it resolves — without
  // this, two overlapping /discover requests could race and whichever
  // response arrived last (not the one for the latest filters) would win.
  abortController: null,
  filters: null,
  // Display-only sort applied to the accumulated items — doesn't affect
  // fetch order or the "Load more" cursor, which stays keyed to the
  // server's original (relevance) ordering.
  sort: "relevance",
  // Display-only location filter, same reasoning as `sort` above.
  // Only already-tracked results carry a location (see main.py's
  // /api/discover — live Twitch search results have none of their own),
  // so this only ever narrows down to tracked entries. Deliberately
  // excluded from currentDiscoverFilters()/search history below since
  // it's client-side-only — see the NOTE on currentDiscoverFilters() for
  // what changes if this ever becomes a real server-side filter.
  locationFilter: "",
};

function sortedDiscoverItems() {
  let items = discoverState.items;
  if (discoverState.locationFilter) {
    items = items.filter(r => r.location === discoverState.locationFilter);
  }
  if (discoverState.sort === "followers_desc") return [...items].sort((a, b) => (b.followers || 0) - (a.followers || 0));
  if (discoverState.sort === "followers_asc") return [...items].sort((a, b) => (a.followers || 0) - (b.followers || 0));
  // Location sort ties (identical or empty location) fall back to
  // followers desc, same secondary key already used for the primary
  // followers_desc sort above, so tied entries don't sit in whatever
  // arbitrary order the accumulated items happened to be in.
  if (discoverState.sort === "location") {
    return [...items].sort((a, b) => {
      const cmp = (a.location || "").localeCompare(b.location || "");
      if (cmp !== 0) return cmp;
      return (b.followers || 0) - (a.followers || 0);
    });
  }
  return items;
}

// Merges a newly-fetched page of Discover results into the accumulated
// list, dropping any streamer already present. Twitch's live streams
// list can shift between page fetches (viewers changing pushes channels
// up/down), so consecutive "Load more" pages can legitimately overlap —
// without this, the same streamer could show up twice in the results.
function mergeDiscoverItems(existingItems, newItems) {
  const seen = new Set(existingItems.map(r => r.username));
  const deduped = newItems.filter(r => {
    if (seen.has(r.username)) return false;
    seen.add(r.username);
    return true;
  });
  return existingItems.concat(deduped);
}

// Cancels whatever Discover request is currently in flight, if any.
function cancelDiscoverRequest() {
  discoverState.loadingMore = false;
  if (discoverState.abortController) {
    discoverState.abortController.abort();
    discoverState.abortController = null;
  }
}

// NOTE on discoverState.locationFilter (see its declaration above): it's
// deliberately NOT included in this object. Every field here is sent to
// GET /discover as an actual server-side search parameter and is what
// gets persisted to /discover/history (see the search handler below and
// applyDiscoverFilters/discoverHistorySummary, which mirror this same
// field list). locationFilter never reaches Twitch or the server at
// all — it only re-filters results already on screen client-side (see
// sortedDiscoverItems) — so including it here would misrepresent it as
// part of the search that was actually run. If it's ever promoted to a
// real server-side filter (i.e. /api/discover starts accepting a
// location param), add it to this object, to applyDiscoverFilters(),
// and to discoverHistorySummary() the same way every other field here
// is handled — that's the only change needed for it to start being
// saved/restored with search history like the rest.
function currentDiscoverFilters() {
  return {
    category: document.getElementById("discoverCategory").value,
    min_viewers: document.getElementById("discoverMinViewers").value,
    max_viewers: document.getElementById("discoverMaxViewers").value,
    min_followers: document.getElementById("discoverMinFollowers").value,
    max_followers: document.getElementById("discoverMaxFollowers").value,
    broadcaster_type: document.getElementById("discoverType").value,
    language: document.getElementById("discoverLanguage").value,
    tags: document.getElementById("discoverTags").value,
    exclude_tags: document.getElementById("discoverExcludeTags").value,
    created_after: document.getElementById("discoverCreatedAfter").value,
    created_before: document.getElementById("discoverCreatedBefore").value,
    limit: 25,
  };
}

function trackButtonHtml(r) {
  return r.already_tracked
    ? `<button class="btn btn-danger btn-small" style="width:auto;" data-track="${escapeAttr(r.username)}" data-tracked="1">Untrack</button>`
    : `<button class="btn btn-primary btn-small" style="width:auto;" data-track="${escapeAttr(r.username)}">Track</button>`;
}

function discoverItemHtml(r) {
  const avatar = r.profile_image
    ? `<img class="discover-item-avatar" src="${escapeAttr(r.profile_image)}" alt="">`
    : `<div class="discover-item-avatar-fallback">${initials(r.username)}</div>`;
  const channelUrl = `https://twitch.tv/${encodeURIComponent(r.username)}`;

  return `
    <div class="discover-item" data-discover-username="${escapeAttr(r.username)}">
      ${avatar}
      <div class="discover-item-info">
        <a class="discover-item-name" href="${escapeAttr(channelUrl)}" target="_blank" rel="noopener">${escapeHtml(r.display_name || r.username)}</a>
        <div class="discover-item-meta">${formatCompact(r.live_viewers)} viewers &middot; ${formatCompact(r.followers)} followers &middot; ${escapeHtml(r.category || "")}${r.location ? ` &middot; 📍 ${escapeHtml(r.location)}` : ""}</div>
      </div>
      ${trackButtonHtml(r)}
      <button class="btn btn-ghost btn-small" style="width:auto;" data-blacklist="${escapeAttr(r.username)}" title="Never show this streamer in Discover again" aria-label="Blacklist ${escapeAttr(r.username)} from Discover">🚫 Blacklist</button>
    </div>
  `;
}

// A blacklisted streamer is a hard exclude from Discover (separate from
// priority="Ignore", which only ranks attention and still lets a streamer
// appear here) — see database.add_to_blacklist. Removing the card
// immediately (rather than just disabling the button) reflects that it
// won't come back on the next search either.
function wireBlacklistButton(btn) {
  btn.addEventListener("click", () => {
    const username = btn.dataset.blacklist;
    btn.disabled = true;
    const item = btn.closest("[data-discover-username]");
    if (item) item.style.display = "none";
    const removedItem = discoverState.items.find(r => r.username === username);
    discoverState.items = discoverState.items.filter(r => r.username !== username);
    undoToast(
      `🚫 Blacklisting ${username}…`,
      async () => {
        await api("/blacklist", { method: "POST", body: JSON.stringify({ username }) });
        if (item) item.remove();
      },
      () => {
        if (item) item.style.display = "";
        btn.disabled = false;
        if (removedItem) discoverState.items.push(removedItem);
        toast(`Kept ${username} in Discover`, "success");
      }
    );
  });
}

function wireBlacklistButtons(container) {
  container.querySelectorAll("[data-blacklist]").forEach(wireBlacklistButton);
}

function wireTrackButton(btn) {
  if (btn.dataset.trackWired === "1") return;
  btn.dataset.trackWired = "1";
  btn.addEventListener("click", async () => {
    // 🏁 Race-condition guard: outerHTML below swaps in a brand new button
    // element once the request resolves, but a fast double-click can fire
    // a second click on this same node before that swap happens — disabled
    // alone doesn't always win that race depending on click timing, so an
    // explicit in-flight flag on the button makes the second click a
    // guaranteed no-op instead of firing a second, overlapping request.
    if (btn.dataset.trackBusy === "1") return;
    btn.dataset.trackBusy = "1";
    const username = btn.dataset.track;
    const alreadyTracked = btn.dataset.tracked === "1";
    btn.disabled = true;
    try {
      if (alreadyTracked) {
        await api(`/streamers/${encodeURIComponent(username)}`, { method: "DELETE" });
        toast(`Untracked ${username}`, "");
        btn.outerHTML = `<button class="btn btn-primary btn-small" style="width:auto;" data-track="${escapeAttr(username)}">Track</button>`;
      } else {
        await api("/streamers", { method: "POST", body: JSON.stringify({ username }) });
        toast(`✅ Now tracking ${username}`, "success");
        btn.outerHTML = `<button class="btn btn-danger btn-small" style="width:auto;" data-track="${escapeAttr(username)}" data-tracked="1">Untrack</button>`;
      }
      // The button was just replaced via outerHTML — wire the new node
      // (only this one, not the whole container, so other buttons in the
      // list don't pick up a second click listener). Every matching node
      // is rewired, not just the first, in case the same username appears
      // more than once in the current results (e.g. both Discover and a
      // watchlist check panel open at once).
      document.querySelectorAll(`[data-track="${CSS.escape(username)}"]`).forEach(el => {
        el.dataset.tracked = alreadyTracked ? "0" : "1";
        el.textContent = alreadyTracked ? "Track" : "Untrack";
        el.classList.toggle("btn-danger", !alreadyTracked);
        el.classList.toggle("btn-primary", alreadyTracked);
        wireTrackButton(el);
      });
      for (const item of discoverState.items) if (item.username === username) item.already_tracked = !alreadyTracked;
      loadStats();
    } catch (e) {
      btn.disabled = false;
      btn.dataset.trackBusy = "0";
      toast(e.message, "error");
    }
  });
}

function wireTrackButtons(container) {
  container.querySelectorAll("[data-track]").forEach(wireTrackButton);
}

function renderDiscoverResults(filters) {
  const results = document.getElementById("discoverResults");
  const sortRow = document.getElementById("discoverSortRow");

  if (!discoverState.items.length) {
    if (sortRow) sortRow.style.display = "none";
    const f = filters || currentDiscoverFilters();
    const activeFilters = ["category", "min_viewers", "max_viewers", "min_followers", "max_followers", "broadcaster_type", "language", "tags", "exclude_tags", "created_after", "created_before"]
      .filter(k => f[k] !== "" && f[k] !== null && f[k] !== undefined);
    const hint = activeFilters.length
      ? "🔍 No live streamers matched those filters — try loosening one (a wider viewer/follower range, or clearing the category/language) and search again."
      : "📭 No live streamers found right now — try again in a bit.";
    results.innerHTML = `
      <div class="detail-empty">
        <div>${hint}</div>
        ${activeFilters.length ? `<button class="btn btn-ghost btn-small" id="discoverClearFilters" style="width:auto;margin-top:10px;">🧹 Clear filters</button>` : ""}
      </div>
    `;
    const clearBtn = document.getElementById("discoverClearFilters");
    if (clearBtn) clearBtn.addEventListener("click", () => {
      document.querySelectorAll(".discover-form .field-input, .discover-form select").forEach(el => { el.value = ""; });
      document.getElementById("discoverSubmit").click();
    });
    return;
  }

  if (sortRow) sortRow.style.display = "flex";

  results.innerHTML = sortedDiscoverItems().map(r => discoverItemHtml(r)).join("") +
    (discoverState.nextCursor
      ? `<button class="btn btn-ghost btn-small" id="discoverLoadMore" style="width:100%;margin-top:8px;">Load more</button>`
      : discoverState.items.length ? `<div class="discover-item-meta" style="text-align:center;margin-top:8px;">End of results</div>` : "");

  wireTrackButtons(results);
  wireBlacklistButtons(results);

  const moreBtn = document.getElementById("discoverLoadMore");
  if (moreBtn) moreBtn.addEventListener("click", loadMoreDiscoverResults);
}

async function loadMoreDiscoverResults() {
  if (discoverState.loadingMore || !discoverState.nextCursor) return;

  // 🏁 Cancel any still-running search before starting this one, so a
  // stray earlier response can't overwrite these (newer) results.
  cancelDiscoverRequest();
  const controller = new AbortController();
  discoverState.abortController = controller;
  discoverState.loadingMore = true;

  const moreBtn = document.getElementById("discoverLoadMore");
  if (moreBtn) { moreBtn.disabled = true; moreBtn.textContent = "Loading…"; }

  try {
    const data = await apiAbortable("/discover" + qs({ ...(discoverState.filters || currentDiscoverFilters()), cursor: discoverState.nextCursor }), {}, controller.signal);
    // If a newer request has since taken over the controller slot, this
    // response is stale — drop it instead of appending out-of-order pages.
    if (discoverState.abortController !== controller) return;
    discoverState.items = mergeDiscoverItems(discoverState.items, data.items);
    discoverState.nextCursor = data.next_cursor || null;
    renderDiscoverResults();
  } catch (e) {
    if (e.name === "AbortError") return; // superseded by a newer request — ignore
    if (e.rateLimited) {
      toast(`Twitch rate limit hit — ${e.message}`, "error");
    } else {
      toast(e.message, "error");
    }
  } finally {
    if (discoverState.abortController === controller) {
      discoverState.abortController = null;
      discoverState.loadingMore = false;
      if (moreBtn?.isConnected) { moreBtn.disabled = false; moreBtn.textContent = "Load more"; }
    }
  }
}

// ===================== DISCOVER SEARCH HISTORY =====================
// Recent Discover filter combos, so a search can be rerun from the modal
// instead of re-entering filters. Loaded whenever the modal opens;
// recorded only after a search actually returns (not on every keystroke
// or a failed/rate-limited attempt).

function applyDiscoverFilters(filters) {
  document.getElementById("discoverCategory").value = filters.category || "";
  document.getElementById("discoverMinViewers").value = filters.min_viewers ?? "";
  document.getElementById("discoverMaxViewers").value = filters.max_viewers ?? "";
  document.getElementById("discoverMinFollowers").value = filters.min_followers ?? "";
  document.getElementById("discoverMaxFollowers").value = filters.max_followers ?? "";
  document.getElementById("discoverType").value = filters.broadcaster_type || "";
  document.getElementById("discoverLanguage").value = filters.language || "";
  for (const [key, id] of [["tags","discoverTags"],["exclude_tags","discoverExcludeTags"],["created_after","discoverCreatedAfter"],["created_before","discoverCreatedBefore"]]) {
    const value = filters[key];
    document.getElementById(id).value = Array.isArray(value) ? value.join(",") : (value ?? "");
  }
}

function discoverHistorySummary(filters) {
  const parts = [];
  if (filters.category) parts.push(filters.category);
  if (filters.min_viewers || filters.max_viewers) {
    parts.push(`${filters.min_viewers || "0"}–${filters.max_viewers || "∞"} viewers`);
  }
  if (filters.min_followers || filters.max_followers) {
    parts.push(`${filters.min_followers || "0"}–${filters.max_followers || "∞"} followers`);
  }
  if (filters.broadcaster_type) parts.push(filters.broadcaster_type);
  if (filters.language) parts.push(filters.language);
  return parts.length ? parts.join(" · ") : "Any live streamer";
}

async function loadDiscoverHistory() {
  const wrap = document.getElementById("discoverHistory");
  const list = document.getElementById("discoverHistoryList");
  try {
    const data = await api("/discover/history");
    if (!data.items.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "block";
    list.innerHTML = data.items.map(entry => `
      <div class="preset-item" data-history-id="${entry.id}">
        <span class="preset-name" title="${escapeAttr(discoverHistorySummary(entry.filters))}">${escapeHtml(discoverHistorySummary(entry.filters))}</span>
        <button class="preset-delete" data-history-delete="${entry.id}" title="Remove" aria-label="Remove search: ${escapeAttr(discoverHistorySummary(entry.filters))}">×</button>
      </div>
    `).join("");

    list.querySelectorAll(".preset-item").forEach(item => {
      item.addEventListener("click", (e) => {
        if (e.target.closest("[data-history-delete]")) return;
        const entry = data.items.find(i => String(i.id) === item.dataset.historyId);
        if (!entry) return;
        applyDiscoverFilters(entry.filters);
        document.getElementById("discoverSubmit").click();
      });
    });
    list.querySelectorAll("[data-history-delete]").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await api(`/discover/history/${btn.dataset.historyDelete}`, { method: "DELETE" });
          loadDiscoverHistory();
        } catch (err) { toast(err.message, "error"); }
      });
    });
  } catch (e) { wrap.style.display = "none"; /* non-fatal */ }
}

document.getElementById("btnClearDiscoverHistory").addEventListener("click", async () => {
  try {
    await api("/discover/history", { method: "DELETE" });
    loadDiscoverHistory();
  } catch (e) { toast(e.message, "error"); }
});

// A fixed, curated list of consistently-popular Twitch categories, offered
// as suggestions in the Category field's <datalist>. This is intentionally
// static rather than fetched — /api/categories only reflects this app's own
// tracked streamers (not Twitch-wide popularity), and hitting Twitch for a
// live "top games" list on every modal-open would be an extra round trip
// for what's meant to be a lightweight convenience. A <datalist> never
// restricts the input either way: typing any other category still works
// exactly as before, this just adds one-click suggestions on top.
const POPULAR_DISCOVER_CATEGORIES = [
  "Just Chatting", "League of Legends", "Grand Theft Auto V", "VALORANT",
  "Counter-Strike", "Minecraft", "Fortnite", "Call of Duty: Warzone",
  "Dota 2", "World of Warcraft", "Apex Legends", "EA Sports FC",
  "Overwatch 2", "Music", "Art", "Poker", "Chess", "Sports",
];

function populateDiscoverCategoryOptions() {
  const list = document.getElementById("discoverCategoryOptions");
  if (!list || list.childElementCount) return; // already populated, static list
  list.innerHTML = POPULAR_DISCOVER_CATEGORIES
    .map(name => `<option value="${escapeAttr(name)}"></option>`)
    .join("");
}

document.getElementById("btnDiscover").addEventListener("click", () => {
  cancelDiscoverRequest();
  discoverState.items = [];
  discoverState.nextCursor = null;
  discoverState.sort = "relevance";
  discoverState.locationFilter = "";
  const sortSel = document.getElementById("discoverSort");
  if (sortSel) sortSel.value = "relevance";
  const locationSel = document.getElementById("discoverLocationFilter");
  if (locationSel) locationSel.value = "";
  document.getElementById("discoverResults").innerHTML = "";
  populateDiscoverCategoryOptions();
  loadDiscoverHistory();
  loadLocations();
  openModal("discoverModal");
});

document.getElementById("discoverSort").addEventListener("change", (e) => {
  discoverState.sort = e.target.value;
  renderDiscoverResults();
});

document.getElementById("discoverLocationFilter").addEventListener("change", (e) => {
  discoverState.locationFilter = e.target.value;
  renderDiscoverResults();
});

document.getElementById("discoverSubmit").addEventListener("click", async () => {
  const results = document.getElementById("discoverResults");
  results.innerHTML = `<div class="detail-empty">🔎 Searching Twitch…</div>`;
  discoverState.items = [];
  discoverState.nextCursor = null;

  // 🏁 Cancel any previous search still in flight — rapid filter changes
  // / repeated clicks used to be able to race, with an older (slower)
  // response landing after a newer one and silently overwriting it.
  cancelDiscoverRequest();
  const controller = new AbortController();
  discoverState.abortController = controller;

  const filters = currentDiscoverFilters();
  discoverState.filters = filters;
  try {
    const data = await apiAbortable("/discover" + qs(filters), {}, controller.signal);
    if (discoverState.abortController !== controller) return; // superseded — stale response, ignore
    discoverState.items = mergeDiscoverItems([], data.items);
    discoverState.nextCursor = data.next_cursor || null;
    renderDiscoverResults(filters);

    // Record the search that was actually run (not attempted) so failed/
    // rate-limited searches don't clutter the history list.
    const { limit, ...savedFilters } = filters;
    api("/discover/history", { method: "POST", body: JSON.stringify({ filters: savedFilters }) })
      .then(loadDiscoverHistory)
      .catch(() => { /* non-fatal — history is a convenience, not required */ });
  } catch (e) {
    if (e.name === "AbortError") return; // superseded by a newer search — ignore
    if (discoverState.abortController !== controller) return;
    if (e.rateLimited) {
      results.innerHTML = `<div class="detail-empty">⏳ Twitch rate limit hit — ${escapeHtml(e.message)}</div>`;
      toast(`Twitch rate limit hit — ${e.message}`, "error");
    } else {
      results.innerHTML = `<div class="detail-empty">😕 ${escapeHtml(e.message)}</div>`;
    }
  } finally {
    if (discoverState.abortController === controller) discoverState.abortController = null;
  }
});

// ===================== CATEGORY WATCHLISTS MODAL =====================
// Follows a category (rather than an individual streamer) so new/trending
// channels streaming it can be surfaced without already knowing their
// name. Reuses the same discover-item styling/track-button flow as the
// Discover modal.

async function loadWatchlists() {
  const list = document.getElementById("watchlistList");
  list.innerHTML = `<div class="detail-empty">Loading…</div>`;
  try {
    const data = await api("/watchlists");
    if (!data.items.length) {
      list.innerHTML = `<div class="detail-empty">Not following any categories yet — add one above.</div>`;
      return;
    }
    list.innerHTML = data.items.map(w => `
      <div class="discover-item">
        <div class="discover-item-info">
          <span class="discover-item-name" style="cursor:default;">${escapeHtml(w.category)}</span>
          <div class="discover-item-meta">
            ${w.min_viewers ? `min ${formatCompact(w.min_viewers)} viewers &middot; ` : ""}
            ${w.last_checked ? `last checked ${new Date(w.last_checked).toLocaleString()}` : "never checked"}
          </div>
        </div>
        <button class="btn btn-ghost btn-small" style="width:auto;" data-watch-check="${escapeAttr(w.category)}">Check now</button>
        <button class="btn btn-danger btn-small" style="width:auto;" data-watch-remove="${escapeAttr(w.category)}">Unfollow</button>
      </div>
    `).join("");

    list.querySelectorAll("[data-watch-check]").forEach(btn => {
      btn.addEventListener("click", () => checkCategoryWatch(btn.dataset.watchCheck));
    });
    list.querySelectorAll("[data-watch-remove]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/watchlists/${encodeURIComponent(btn.dataset.watchRemove)}`, { method: "DELETE" });
          toast(`Unfollowed ${btn.dataset.watchRemove}`, "success");
          loadWatchlists();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="detail-empty">Couldn't load watchlists: ${escapeHtml(e.message)}</div>`;
  }
}

async function checkCategoryWatch(category) {
  const results = document.getElementById("watchlistCheckResults");
  results.innerHTML = `<div class="detail-empty">Checking ${escapeHtml(category)}…</div>`;
  try {
    const data = await api(`/watchlists/${encodeURIComponent(category)}/check`, { method: "POST" });
    if (!data.items.length) {
      results.innerHTML = `<div class="detail-empty">📭 No live channels found in ${escapeHtml(category)} right now.</div>`;
      return;
    }
    const header = `<div class="detail-section-title">${escapeHtml(category)} &middot; ${data.new_count} new since last check</div>`;
    results.innerHTML = header + data.items.map(r => `
      <div class="discover-item">
        ${r.profile_image
          ? `<img class="discover-item-avatar" src="${escapeAttr(r.profile_image)}" alt="">`
          : `<div class="discover-item-avatar-fallback">${initials(r.username)}</div>`}
        <div class="discover-item-info">
          <a class="discover-item-name" href="${escapeAttr(`https://twitch.tv/${encodeURIComponent(r.username)}`)}" target="_blank" rel="noopener">
            ${escapeHtml(r.display_name || r.username)} ${r.is_new ? '<span class="badge badge-score">New</span>' : ""}
          </a>
          <div class="discover-item-meta">${formatCompact(r.live_viewers)} viewers &middot; ${formatCompact(r.followers || 0)} followers</div>
        </div>
        ${trackButtonHtml(r)}
      </div>
    `).join("");
    wireTrackButtons(results);
    loadWatchlists();
  } catch (e) {
    if (e.rateLimited) {
      results.innerHTML = `<div class="detail-empty">⏳ Twitch rate limit hit — ${escapeHtml(e.message)}</div>`;
    } else {
      results.innerHTML = `<div class="detail-empty">😕 ${escapeHtml(e.message)}</div>`;
    }
  }
}

// ===================== RAID MAP =====================
// Coarse regional overview of tracked streamers (see GET
// /api/locations/map — regions are derived server-side from each
// streamer's on-file timezone, not a real geocode/mapping dependency).
// Rendered as a clickable grid of region cards; clicking one drills
// into that region's streamer list below the grid, mirroring the
// watchlist-check results pattern above (list stays, drilldown
// replaces/updates a second panel rather than navigating away).
let raidMapItems = [];

function raidMapCardHtml(region) {
  return `
    <button class="raid-map-cell" data-map-region="${escapeAttr(region.region)}">
      <div class="raid-map-cell-region">${escapeHtml(region.region)}</div>
      <div class="raid-map-cell-count">${region.count} tracked</div>
      ${region.live_count ? `<div class="raid-map-cell-live">🔴 ${region.live_count} live</div>` : ""}
    </button>
  `;
}

function renderRaidMapDrilldown(region) {
  const panel = document.getElementById("raidMapDrilldown");
  if (!region) {
    panel.style.display = "none";
    panel.innerHTML = "";
    return;
  }
  const sorted = [...region.streamers].sort((a, b) => (b.live - a.live) || (b.followers - a.followers));
  panel.style.display = "";
  panel.innerHTML = `
    <div class="detail-section-title" style="margin-top:14px;">${escapeHtml(region.region)} &middot; ${region.count} tracked</div>
    ${sorted.map(st => `
      <div class="discover-item">
        ${st.profile_image
          ? `<img class="discover-item-avatar" src="${escapeAttr(st.profile_image)}" alt="">`
          : `<div class="discover-item-avatar-fallback">${initials(st.username)}</div>`}
        <div class="discover-item-info">
          <a class="discover-item-name" href="${escapeAttr(`https://twitch.tv/${encodeURIComponent(st.username)}`)}" target="_blank" rel="noopener">
            ${escapeHtml(st.username)} ${st.live ? '<span class="badge badge-score">Live</span>' : ""}
          </a>
          <div class="discover-item-meta">${escapeHtml(st.location || "—")}${st.timezone ? ` &middot; ${escapeHtml(st.timezone)}` : ""} &middot; ${formatCompact(st.followers)} followers</div>
        </div>
      </div>
    `).join("")}
  `;
}

async function loadRaidMap() {
  const grid = document.getElementById("raidMapGrid");
  const errEl = document.getElementById("raidMapError");
  errEl.style.display = "none";
  renderRaidMapDrilldown(null);
  grid.innerHTML = `<div class="detail-empty">Loading…</div>`;
  try {
    const data = await api("/locations/map");
    raidMapItems = data.items;
    if (!raidMapItems.length) {
      grid.innerHTML = `<div class="detail-empty">📭 No streamers with a location on file yet — set one from a streamer's detail panel.</div>`;
      return;
    }
    grid.innerHTML = raidMapItems.map(raidMapCardHtml).join("");
    grid.querySelectorAll("[data-map-region]").forEach(btn => {
      btn.addEventListener("click", () => {
        grid.querySelectorAll("[data-map-region]").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        const region = raidMapItems.find(r => r.region === btn.dataset.mapRegion);
        renderRaidMapDrilldown(region);
      });
    });
  } catch (e) {
    grid.innerHTML = "";
    errEl.textContent = `Couldn't load the raid map: ${e.message}`;
    errEl.style.display = "";
  }
}

document.getElementById("btnRaidMap").addEventListener("click", () => {
  openModal("raidMapModal");
  loadRaidMap();
});

document.getElementById("btnWatchlists").addEventListener("click", () => {
  document.getElementById("watchlistCheckResults").innerHTML = "";
  document.getElementById("watchlistError").textContent = "";
  openModal("watchlistsModal");
  loadWatchlists();
});

document.getElementById("watchlistAddSubmit").addEventListener("click", async () => {
  const category = document.getElementById("watchlistCategory").value.trim();
  const minViewers = document.getElementById("watchlistMinViewers").value;
  const errEl = document.getElementById("watchlistError");
  errEl.textContent = "";
  if (!category) {
    errEl.textContent = "Category is required";
    return;
  }
  try {
    await api("/watchlists", {
      method: "POST",
      body: JSON.stringify({ category, min_viewers: minViewers ? Number(minViewers) : 0 }),
    });
    toast(`👀 Following ${category}`, "success");
    document.getElementById("watchlistCategory").value = "";
    document.getElementById("watchlistMinViewers").value = "";
    loadWatchlists();
  } catch (e) {
    errEl.textContent = e.message;
  }
});

document.getElementById("watchlistCategory").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("watchlistAddSubmit").click();
});

// ===================== ALERTS MODAL =====================
// Notification/alert rules: "notify me when X streamer goes live" or
// "when a Discover search matches, alert me". Reuses the discover-item
// list styling for the rule list, same as Watchlists above.

const ALERT_CHANNEL_LABELS = { browser: "Browser", webhook: "Webhook", email: "Email" };

function alertKindLabel(rule) {
  if (rule.kind === "streamer_live") return `${rule.target} goes live`;
  const f = rule.target || {};
  const bits = [f.category ? `category: ${f.category}` : "any category"];
  if (f.min_viewers) bits.push(`≥${f.min_viewers} viewers`);
  return `Discover match — ${bits.join(", ")}`;
}

function updateAlertFormVisibility() {
  const kind = document.getElementById("alertKind").value;
  document.getElementById("alertTargetStreamerGroup").style.display = kind === "streamer_live" ? "" : "none";
  document.getElementById("alertTargetCategoryGroup").style.display = kind === "discover_match" ? "" : "none";
  document.getElementById("alertTargetMinViewersGroup").style.display = kind === "discover_match" ? "" : "none";

  document.getElementById("alertWebhookGroup").style.display = document.getElementById("alertChWebhook").checked ? "" : "none";
  document.getElementById("alertEmailGroup").style.display = document.getElementById("alertChEmail").checked ? "" : "none";
}

document.getElementById("alertKind").addEventListener("change", updateAlertFormVisibility);
document.getElementById("alertChWebhook").addEventListener("change", updateAlertFormVisibility);
document.getElementById("alertChEmail").addEventListener("change", updateAlertFormVisibility);

async function loadAlertRules() {
  const list = document.getElementById("alertRuleList");
  list.innerHTML = `<div class="detail-empty">Loading…</div>`;
  try {
    const data = await api("/alerts/rules");
    if (!data.items.length) {
      list.innerHTML = `<div class="detail-empty">No alert rules yet — create one above.</div>`;
      return;
    }
    list.innerHTML = data.items.map(r => `
      <div class="discover-item">
        <div class="discover-item-info">
          <span class="discover-item-name" style="cursor:default;">${escapeHtml(alertKindLabel(r))}</span>
          <div class="discover-item-meta">
            ${r.channels.map(c => escapeHtml(ALERT_CHANNEL_LABELS[c] || c)).join(" + ")}
            ${r.last_triggered ? ` &middot; last fired ${new Date(r.last_triggered).toLocaleString()}` : " &middot; not fired yet"}
            ${r.enabled ? "" : " &middot; <strong>paused</strong>"}
          </div>
        </div>
        <button class="btn btn-ghost btn-small" style="width:auto;" data-alert-toggle="${r.id}">${r.enabled ? "Pause" : "Resume"}</button>
        <button class="btn btn-danger btn-small" style="width:auto;" data-alert-remove="${r.id}">Delete</button>
      </div>
    `).join("");

    list.querySelectorAll("[data-alert-toggle]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/alerts/rules/${btn.dataset.alertToggle}/toggle`, { method: "POST" });
          loadAlertRules();
        } catch (e) { toast(e.message, "error"); }
      });
    });
    list.querySelectorAll("[data-alert-remove]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/alerts/rules/${btn.dataset.alertRemove}`, { method: "DELETE" });
          toast("🗑️ Alert rule deleted", "success");
          loadAlertRules();
        } catch (e) { toast(e.message, "error"); }
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="detail-empty">Couldn't load alert rules: ${escapeHtml(e.message)}</div>`;
  }
}

// Bulk re-scrape — hits the same scraper as the per-streamer "↻
// Re-scrape" button in the detail panel (see wireDetailEvents), but for
// every tracked streamer in one request (POST /streamers/social/scrape-
// all) instead of opening each usercard individually.
document.getElementById("btnRescrapeAll").addEventListener("click", async () => {
  const btn = document.getElementById("btnRescrapeAll");
  if (btn.dataset.busy === "1") return;
  btn.dataset.busy = "1";
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "…";
  try {
    toast("🌐 Re-scraping all tracked streamers…");
    const res = await api("/streamers/social/scrape-all", { method: "POST" });
    toast(`✅ Re-scraped ${res.succeeded}/${res.total} streamer${res.total === 1 ? "" : "s"}${res.failed ? ` (${res.failed} failed)` : ""}`, res.failed ? "error" : "success");
    if (state.selectedUsername) selectStreamer(state.selectedUsername);
    loadView();
    loadLocations();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.dataset.busy = "0";
    btn.textContent = original;
  }
});

document.getElementById("btnAlerts").addEventListener("click", () => {
  document.getElementById("alertRuleError").textContent = "";
  updateAlertFormVisibility();
  openModal("alertsModal");
  loadAlertRules();
});

document.getElementById("alertRuleSubmit").addEventListener("click", async () => {
  const errEl = document.getElementById("alertRuleError");
  errEl.textContent = "";

  const kind = document.getElementById("alertKind").value;
  const channels = [];
  if (document.getElementById("alertChBrowser").checked) channels.push("browser");
  if (document.getElementById("alertChWebhook").checked) channels.push("webhook");
  if (document.getElementById("alertChEmail").checked) channels.push("email");

  if (!channels.length) { errEl.textContent = "Pick at least one channel"; return; }

  let target;
  if (kind === "streamer_live") {
    target = document.getElementById("alertTargetStreamer").value.trim();
    if (!target) { errEl.textContent = "Enter a username"; return; }
  } else {
    const category = document.getElementById("alertTargetCategory").value.trim();
    const minViewers = document.getElementById("alertTargetMinViewers").value;
    if (!category) { errEl.textContent = "Enter a category to watch for matches"; return; }
    target = { category, min_viewers: minViewers ? Number(minViewers) : undefined };
  }

  const webhook_url = document.getElementById("alertWebhookUrl").value.trim();
  const email = document.getElementById("alertEmailAddr").value.trim();

  try {
    await api("/alerts/rules", {
      method: "POST",
      body: JSON.stringify({ kind, target, channels, webhook_url, email }),
    });
    toast("🔔 Alert rule created", "success");
    document.getElementById("alertTargetStreamer").value = "";
    document.getElementById("alertTargetCategory").value = "";
    document.getElementById("alertTargetMinViewers").value = "";
    loadAlertRules();
  } catch (e) {
    errEl.textContent = e.message;
  }
});

// ===================== RAIL CONTROLS =====================

document.querySelectorAll(".rail-item").forEach(btn => {
  btn.addEventListener("click", () => {
    saveMainScrollPosition();
    document.querySelectorAll(".rail-item").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.view = btn.dataset.view;
    state.page = 1;
    document.getElementById("viewTitle").textContent = btn.textContent;
    loadView();
  });
});

let searchTimer = null;
let _rosterController = null;

// Search operators stay in one input so the common path remains fast and
// uncluttered. The parser is deliberately tiny and deterministic; unsupported
// text remains normal FTS text.
function parseRosterSearch(raw) {
  const tokens = String(raw || "").match(/(?:[^\s"]*"[^"]*"|\S+)/g) || [];
  const text = [];
  const parsed = { minFollowers:null, maxFollowers:null, priority:null, category:null, location:null, liveOnly:null, tags:[] };
  for (const token of tokens) {
    const m = token.match(/^([a-z_]+):(.*)$/i);
    if (!m) { text.push(token); continue; }
    const key=m[1].toLowerCase(), value=m[2].replace(/^"|"$/g,"").trim();
    if (!value) { text.push(token); continue; }
    if (key === "followers" || key === "follower") {
      const op=value.match(/^(>=|<=|>|<|=)?\s*([0-9]+)$/); if (!op) { text.push(token); continue; }
      const n=Number(op[2]); if (op[1]==="<"||op[1]==="<=") parsed.maxFollowers=(op[1]==="<"?n-1:n); else parsed.minFollowers=(op[1]===">"?n+1:n); if(op[1]==="=") parsed.maxFollowers=n; continue;
    }
    if (key === "live") { parsed.liveOnly=!/^(false|0|no|off)$/i.test(value); continue; }
    if (key === "priority") { parsed.priority=value; continue; }
    if (key === "category") { parsed.category=value; continue; }
    if (key === "location") { parsed.location=value; continue; }
    if (key === "tag" || key === "tags") { parsed.tags.push(value); continue; }
    text.push(token);
  }
  return { text:text.join(" ").trim(), parsed };
}

document.getElementById("searchInput").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.page = 1;
    loadView();
  }, 180);
});

document.getElementById("filterPriority").addEventListener("change", (e) => {
  state.priority = e.target.value;
  state.page = 1;
  loadView();
});

document.getElementById("filterCategory").addEventListener("change", (e) => {
  state.category = e.target.value;
  state.page = 1;
  loadView();
});

document.getElementById("filterLocation").addEventListener("change", (e) => {
  state.location = e.target.value;
  state.page = 1;
  loadView();
});

document.getElementById("filterLiveOnly").addEventListener("change", (e) => {
  state.liveOnly = e.target.checked;
  state.page = 1;
  loadView();
});

document.getElementById("sortBy").addEventListener("change", (e) => {
  state.sortBy = e.target.value;
  persistSortState();
  loadView();
});

document.getElementById("btnSortDir").addEventListener("click", (e) => {
  state.ascending = !state.ascending;
  persistSortState();
  e.target.textContent = state.ascending ? "↑ Ascending" : "↓ Descending";
  loadView();
});

// 📜 "Show all": switches the roster from paginated fetches to bounded
// server-side pages combined client-side, then renders them through the
// virtual scroller. This avoids oversized validation failures and keeps each
// response bounded even when the tracked roster grows substantially.
// Pagination controls stay exactly as-is when this is off.
const SHOW_ALL_PAGE_SIZE = 100;
const SHOW_ALL_MAX_ITEMS = 100_000;
const SHOW_ALL_CONCURRENCY = 4;

async function loadAllStreamerPages(endpoint, params, signal) {
  let effectivePageSize = SHOW_ALL_PAGE_SIZE;
  const requestPage = async (page) => {
    try {
      return await api(
        endpoint + qs({ ...params, page, page_size: effectivePageSize }),
        { signal },
      );
    } catch (error) {
      // Older bundled Scout builds capped page_size below the current
      // contract. A validation response is safe to retry at 50 without
      // changing the visible Show all result or multiplying request volume.
      if (error?.status === 422 && SHOW_ALL_PAGE_SIZE > 50 && !signal?.aborted) {
        effectivePageSize = 50;
        return api(
          endpoint + qs({ ...params, page, page_size: effectivePageSize }),
          { signal },
        );
      }
      throw error;
    }
  };

  const first = await requestPage(1);
  const firstItems = Array.isArray(first.items) ? first.items : [];
  const reportedTotal = Number(first.total);
  const total = Number.isFinite(reportedTotal)
    ? Math.max(0, Math.floor(reportedTotal))
    : firstItems.length;

  if (total <= firstItems.length || firstItems.length === 0) {
    return { items: firstItems, total };
  }

  const boundedTotal = Math.min(total, SHOW_ALL_MAX_ITEMS);
  const pageCount = Math.ceil(boundedTotal / effectivePageSize);
  const items = firstItems.slice(0, boundedTotal);

  for (let offset = 1; offset < pageCount; offset += SHOW_ALL_CONCURRENCY) {
    if (signal?.aborted) throw new DOMException('Show all request was cancelled.', 'AbortError');
    const batch = [];
    for (let page = offset + 1; page <= Math.min(pageCount, offset + SHOW_ALL_CONCURRENCY); page += 1) {
      batch.push(requestPage(page));
    }
    const responses = await Promise.all(batch);
    for (const response of responses) {
      if (Array.isArray(response.items)) items.push(...response.items);
    }
  }

  const unique = new Map();
  for (const item of items) {
    const key = String(item?.username ?? "").trim().toLowerCase();
    if (key) unique.set(key, item);
  }
  return { items: [...unique.values()], total: Math.min(total, SHOW_ALL_MAX_ITEMS) };
}
const btnShowAll = document.getElementById("btnShowAll");
if (btnShowAll) {
  btnShowAll.setAttribute("aria-pressed", state.virtualScroll ? "true" : "false");
  if (state.virtualScroll) btnShowAll.classList.add("active");
  btnShowAll.addEventListener("click", () => {
    if (state.view !== "roster") {
      state.view = "roster";
      state.page = 1;
    }
    const on = !state.virtualScroll;
    setVirtualScroll(on);
    btnShowAll.setAttribute("aria-pressed", on ? "true" : "false");
    btnShowAll.classList.toggle("active", on);
    state.page = 1;
    loadView();
  });
}

// Manually re-fetches whatever's currently shown (All Streamers,
// Favourites, Raid Candidates, etc.) without having to switch views and
// back — loadView() already re-reads every filter/sort/page from `state`,
// so this is just a direct call rather than new logic.
document.getElementById("btnRefreshView").addEventListener("click", () => {
  loadView();
});

// ===================== SAVED FILTER PRESETS =====================
// A "view" is the current roster filter/sort combo (search, priority,
// category, live-only, sort, direction) saved under a name via
// /api/filter-presets, so it can be reapplied later without rebuilding
// it by hand. Only meaningful for the "roster" view's own filters, so
// applying a preset also switches to the "All Streamers" rail item.

function currentFilterState() {
  return {
    search: state.search,
    priority: state.priority,
    category: state.category,
    location: state.location,
    liveOnly: state.liveOnly,
    sortBy: state.sortBy,
    ascending: state.ascending,
    minFollowers: state.minFollowers,
    maxFollowers: state.maxFollowers,
    tags: state.tags,
  };
}

async function loadPresets() {
  const list = document.getElementById("presetList");
  try {
    const data = await api("/filter-presets");
    if (!data.items.length) {
      list.innerHTML = `<div class="preset-empty">No saved views yet</div>`;
      return;
    }
    list.innerHTML = data.items.map(p => `
      <div class="preset-item" data-preset="${escapeAttr(p.name)}">
        <span class="preset-name">${escapeHtml(p.name)}</span>
        <button class="preset-delete" data-preset-delete="${escapeAttr(p.name)}" title="Delete view" aria-label="Delete view: ${escapeAttr(p.name)}">×</button>
      </div>
    `).join("");

    list.querySelectorAll(".preset-item").forEach(item => {
      item.addEventListener("click", (e) => {
        if (e.target.closest("[data-preset-delete]")) return;
        applyPreset(item.dataset.preset, data.items);
      });
    });
    list.querySelectorAll("[data-preset-delete]").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        try {
          await api(`/filter-presets/${encodeURIComponent(btn.dataset.presetDelete)}`, { method: "DELETE" });
          toast("🗑️ View deleted", "success");
          loadPresets();
        } catch (err) { toast(err.message, "error"); }
      });
    });
  } catch (e) { /* non-fatal */ }
}

function applyPreset(name, items) {
  const preset = items.find(p => p.name === name);
  if (!preset) return;
  const f = preset.filters || {};

  state.search = f.search || "";
  state.priority = f.priority || "";
  state.category = f.category || "";
  state.location = f.location || "";
  state.liveOnly = !!f.liveOnly;
  state.minFollowers = f.minFollowers ?? null;
  state.maxFollowers = f.maxFollowers ?? null;
  state.tags = f.tags || "";
  state.sortBy = f.sortBy || "followers";
  state.ascending = !!f.ascending;
  persistSortState();
  state.page = 1;

  document.getElementById("searchInput").value = state.search;
  document.getElementById("filterPriority").value = state.priority;
  document.getElementById("filterCategory").value = state.category;
  document.getElementById("filterLocation").value = state.location;
  document.getElementById("filterLiveOnly").checked = state.liveOnly;
  document.getElementById("sortBy").value = state.sortBy;
  document.getElementById("btnSortDir").textContent = state.ascending ? "↑ Ascending" : "↓ Descending";

  document.querySelectorAll(".rail-item").forEach(b => b.classList.remove("active"));
  const rosterBtn = document.querySelector('.rail-item[data-view="roster"]');
  if (rosterBtn) { rosterBtn.classList.add("active"); document.getElementById("viewTitle").textContent = rosterBtn.textContent; }
  state.view = "roster";

  toast(`✅ Applied view "${name}"`, "success");
  loadView();
}

document.getElementById("btnSavePreset").addEventListener("click", () => {
  document.getElementById("presetNameInput").value = "";
  document.getElementById("presetError").textContent = "";
  openModal("savePresetModal");
  document.getElementById("presetNameInput").focus();
});

document.getElementById("presetSubmit").addEventListener("click", async () => {
  const input = document.getElementById("presetNameInput");
  const errEl = document.getElementById("presetError");
  const name = input.value.trim();
  if (!name) { errEl.textContent = "Enter a name for this view"; return; }

  try {
    await api("/filter-presets", { method: "POST", body: JSON.stringify({ name, filters: currentFilterState() }) });
    toast(`✅ Saved view "${name}"`, "success");
    closeModal("savePresetModal");
    loadPresets();
  } catch (e) { errEl.textContent = e.message; }
});

document.getElementById("presetNameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("presetSubmit").click();
});

// ===================== SELECTION / BULK / COMPARISON =====================
function updateSelectionToolbar() {
  const compare=document.getElementById("btnCompare");
  const bulk=document.getElementById("btnBulkActions");
  const n=state.selectedUsernames.size;
  if (compare) { compare.disabled=n<2 || n>4; compare.textContent=`⚖ Compare${n ? ` (${n})` : ""}`; }
  if (bulk) { bulk.style.display=n ? "inline-flex" : "none"; bulk.textContent=`✏ Bulk actions (${n})`; }
}

async function runBulkAction() {
  const names=[...state.selectedUsernames]; if (!names.length) return;
  const action=prompt("Bulk action:\n1 = Set priority\n2 = Add/replace tags\n3 = Clear selection", "1");
  if (action === "3") { state.selectedUsernames.clear(); updateSelectionToolbar(); renderGrid(); return; }
  try {
    if (action === "1") {
      const priority=prompt("Priority: High, Medium, Watch, or Ignore", "Watch");
      if (!priority) return;
      await api("/streamers/priority/bulk", {method:"PUT", body:JSON.stringify({usernames:names,priority})});
    } else if (action === "2") {
      const tags=prompt("Comma-separated tags to set:", ""); if (tags==null) return;
      await api("/streamers/tags/bulk", {method:"PUT", body:JSON.stringify({usernames:names,tags})});
    } else return;
    toast(`✅ Updated ${names.length} streamer${names.length===1?"":"s"}`,"success");
    state.selectedUsernames.clear(); updateSelectionToolbar(); await loadView();
  } catch(e) { toast(e.message||"Bulk action failed","error"); }
}

document.getElementById("btnBulkActions")?.addEventListener("click", runBulkAction);
document.getElementById("btnCompare")?.addEventListener("click", async () => {
  const names=[...state.selectedUsernames].slice(0,4);
  if (names.length<2) return;
  let rows;
  try { rows = await Promise.all(names.map(n => api(`/streamers/${encodeURIComponent(n)}`))); }
  catch (e) { toast(e.message, "error"); return; }
  const fields=[
    ["Followers",x=>formatCompact(x.followers)], ["Current viewers",x=>formatCompact(x.current_viewers)],
    ["Average viewers",x=>formatCompact(x.average_viewers)], ["Peak viewers",x=>formatCompact(x.peak_viewers)],
    ["Category",x=>x.category||"—"], ["Location",x=>x.location||"—"], ["Priority",x=>x.priority||"—"],
    ["Raid score",x=>String(x.live_raid_score ?? x.raid_score ?? 0)], ["Outreach",x=>outreachLabel(x.outreach_status||"active")],
  ];
  document.getElementById("compareBody").innerHTML=`<table class="compare-table"><thead><tr><th>Metric</th>${rows.map(x=>`<th>${escapeHtml(x.display_name||x.username)}</th>`).join("")}</tr></thead><tbody>${fields.map(([label,get])=>`<tr><th>${label}</th>${rows.map(x=>`<td>${escapeHtml(get(x))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  openModal("compareModal");
});
document.getElementById("compareClose")?.addEventListener("click",()=>closeModal("compareModal"));

// ===================== COMMAND PALETTE =====================
const paletteCommands=[
  ["Open roster",()=>{state.view="roster";loadView();}], ["Open Discover",()=>document.getElementById("btnDiscover")?.click()],
  ["Open Settings",()=>document.getElementById("btnSettings")?.click()], ["Refresh current view",()=>loadView()],
  ["Show all streamers",()=>document.getElementById("btnShowAll")?.click()], ["Save current view",()=>document.getElementById("btnSavePreset")?.click()],
  ["Compare selected",()=>document.getElementById("btnCompare")?.click()], ["Run bulk actions",runBulkAction],
];
let paletteIndex=0;
let _paletteController = null;
let _paletteToken = 0;
async function renderPalette(query="") {
  const token=++_paletteToken;
  const q=query.trim().toLowerCase();
  const commands=paletteCommands.filter(x=>!q || x[0].toLowerCase().includes(q));
  const el=document.getElementById("commandPaletteResults");
  const localMatches=state.items.filter(x=>!q || `${x.username} ${x.display_name||""} ${x.category||""}`.toLowerCase().includes(q)).slice(0,8);
  let remoteMatches=localMatches;
  if(q && localMatches.length<8){
    _paletteController?.abort(); _paletteController=new AbortController();
    try { const data=await api(`/streamers/search${qs({q, page:1, page_size:8, sort_by:"followers", ascending:false})}`,{signal:_paletteController.signal}); remoteMatches=Array.isArray(data.items)?data.items:localMatches; }
    catch(e){ if(e?.name!=="AbortError") remoteMatches=localMatches; }
  }
  if(token!==_paletteToken) return;
  const streamerMatches=remoteMatches.slice(0,8).map(x=>[`Open @${x.display_name||x.username}`,()=>selectStreamer(x.username)]);
  const items=[...commands,...streamerMatches]; paletteIndex=Math.max(0,Math.min(paletteIndex,Math.max(items.length-1,0)));
  el.innerHTML=items.length?items.map((x,i)=>`<div class="command-item ${i===paletteIndex?"active":""}" data-command-index="${i}"><span>${escapeHtml(x[0])}</span><small>${i<commands.length?"Command":"Streamer"}</small></div>`).join(""):"<div class='command-item'><span>No matching commands or streamers</span></div>";
  el.querySelectorAll(".command-item[data-command-index]").forEach(node=>node.addEventListener("click",()=>{const i=Number(node.dataset.commandIndex);closePalette();items[i]?.[1]();}));
}

function openPalette(){const p=document.getElementById("commandPalette");p.classList.add("open");p.setAttribute("aria-hidden","false");const i=document.getElementById("commandPaletteInput");i.value="";paletteIndex=0;renderPalette();setTimeout(()=>i.focus(),0);}
function closePalette(){const p=document.getElementById("commandPalette");p.classList.remove("open");p.setAttribute("aria-hidden","true");}
document.getElementById("commandPaletteInput")?.addEventListener("input",e=>{paletteIndex=0;renderPalette(e.target.value);});
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"){e.preventDefault();openPalette();return;}
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="f"&&!e.shiftKey){e.preventDefault();document.getElementById("searchInput")?.focus();return;}
  if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key.toLowerCase()==="f"){e.preventDefault();document.getElementById("btnDiscover")?.click();return;}
  const p=document.getElementById("commandPalette"); if(!p?.classList.contains("open")) return;
  const input=document.getElementById("commandPaletteInput");
  if(e.key==="Escape"){e.preventDefault();closePalette();}
  else if(e.key==="ArrowDown"){e.preventDefault();paletteIndex++;renderPalette(input.value);}
  else if(e.key==="ArrowUp"){e.preventDefault();paletteIndex--;renderPalette(input.value);}
  else if(e.key==="Enter"){e.preventDefault();document.querySelector(`.command-item[data-command-index="${paletteIndex}"]`)?.click();}
});

// ===================== LIVE EVENT FEED (SSE) =====================

const _seenFeedEvents = new Set();
let _feedRefreshTimer = null;
function connectEventStream() {
  const source = new EventSource(API + "/events");
  source.onopen = () => { eventStreamConnected = true; };
  source.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      const key = JSON.stringify([data.type, data.username, data.timestamp, data.summary]);
      if (_seenFeedEvents.has(key)) return;
      _seenFeedEvents.add(key);
      if (_seenFeedEvents.size > 100) _seenFeedEvents.delete(_seenFeedEvents.values().next().value);
      addFeedItem(data);
      _statsCache = null; ++_statsGeneration;
      clearTimeout(_feedRefreshTimer);
      _feedRefreshTimer = setTimeout(() => {
        loadStats();
        if (["roster", "favourites", "raid", "dashboard"].includes(state.view)) loadView();
      }, 150);
    } catch (e) { /* ignore malformed */ }
  };
  source.onerror = () => {
    eventStreamConnected = false;
    source.close();
    setTimeout(connectEventStream, 5000);
  };
}

function addFeedItem(ev) {
  const feed = document.getElementById("liveFeed");
  const emptyMsg = feed.querySelector(".live-feed-empty");
  if (emptyMsg) emptyMsg.remove();

  const el = document.createElement("div");
  el.className = "live-feed-item";

  // Alert-rule pushes (see alerts.py/push_alert_notification) carry a
  // `type` of 'streamer_live' or 'discover_match' and their own summary;
  // the tracker's own direct live notifications have no `type` field and
  // keep the original rendering exactly as before.
  if (ev.type === "discover_match") {
    el.innerHTML = `
      <div class="lf-name">🔔 Discover match</div>
      <div class="lf-meta">${escapeHtml(ev.summary || "")}</div>
    `;
  } else if (ev.type === "streamer_live") {
    el.innerHTML = `
      <div class="lf-name">🔔 ${escapeHtml(ev.summary || "")}</div>
      <div class="lf-meta">${formatCompact(ev.live_viewers)} viewers &middot; ${escapeHtml(ev.category || "")}</div>
    `;
  } else if (ev.type === "offline") {
    // Roster-sync signal (see backend notifier.send_offline_notification) —
    // still worth a feed line, but distinct from a "went live" entry.
    el.innerHTML = `
      <div class="lf-name">⚫ ${escapeHtml(ev.display_name || ev.username)} went offline</div>
      <div class="lf-meta">${escapeHtml(ev.category || "")}</div>
    `;
  } else {
    el.innerHTML = `
      <div class="lf-name">🔴 ${escapeHtml(ev.display_name || ev.username)}</div>
      <div class="lf-meta">${formatCompact(ev.live_viewers)} viewers &middot; ${escapeHtml(ev.category || "")}</div>
    `;
  }
  feed.prepend(el);

  while (feed.children.length > 15) feed.removeChild(feed.lastChild);
}

// ===================== VERSION / CHANGELOG =====================

let changelogCache = null;

// Older releases were plain numeric versions ("1.8.0") and always got a
// "v" prefix when displayed. Now in prerelease, versions already read as
// "prerelease v6.5" and shouldn't get a second "v" stuck on front — this
// only prefixes a bare "v" for versions that don't already start with one
// (checking for a leading "v", or "prerelease v" which is how every
// current version string is formatted).
function versionLabel(v) {
  return /^(v|prerelease v)/i.test(v) ? v : `v${v}`;
}

async function loadVersion() {
  try {
    const data = await api("/version");
    changelogCache = data.changelog || [];
    const versionEl = document.getElementById("topbarVersion");
    if (versionEl) versionEl.textContent = versionLabel(data.version);
  } catch (e) { /* non-critical */ }
}

// Shared by both the topbar "What's new" modal and the Settings >
// Changelog tab: a dropdown to step through versions one at a time
// (latest first, matching changelogCache's ordering from app_version.py),
// paginated a fixed number of versions per page instead of ever showing
// every release stacked in one long scroll. Each caller keeps its own
// page-index ref so the two pickers don't fight over state.
const CHANGELOG_PAGE_SIZE = 5;

function renderChangelogPicker(bodyId, pageState) {
  const body = document.getElementById(bodyId);
  if (!changelogCache || !changelogCache.length) {
    body.innerHTML = `<div class="detail-empty">No changelog available</div>`;
    return;
  }

  const pageCount = Math.max(1, Math.ceil(changelogCache.length / CHANGELOG_PAGE_SIZE));
  pageState.page = Math.min(pageState.page, pageCount - 1);

  body.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
      <select class="rail-select" id="${bodyId}Picker" style="flex:1;"></select>
      ${pageCount > 1 ? `
        <div class="pagination" id="${bodyId}Pagination">
          <button id="${bodyId}Prev" title="Older versions">‹ Prev</button>
          <span id="${bodyId}PageLabel"></span>
          <button id="${bodyId}Next" title="Newer versions">Next ›</button>
        </div>
      ` : ""}
    </div>
    <div id="${bodyId}Entry"></div>
  `;

  const picker = document.getElementById(`${bodyId}Picker`);
  const entryEl = document.getElementById(`${bodyId}Entry`);
  const pageLabel = document.getElementById(`${bodyId}PageLabel`);
  const prevBtn = document.getElementById(`${bodyId}Prev`);
  const nextBtn = document.getElementById(`${bodyId}Next`);

  const renderEntry = () => {
    const entry = changelogCache[Number(picker.value)];
    if (!entry) { entryEl.innerHTML = ""; return; }
    entryEl.innerHTML = `
      <div class="detail-section" style="padding-top:0;">
        <div class="detail-section-title">${escapeHtml(versionLabel(entry.version))}</div>
        <ul style="margin:0;padding-left:18px;font-size:12.5px;color:var(--text-lo);">
          ${(entry.highlights || entry.changes || []).map(h => `<li style="margin-bottom:4px;">${escapeHtml(h)}</li>`).join("")}
        </ul>
      </div>
    `;
  };

  const renderPage = () => {
    const start = pageState.page * CHANGELOG_PAGE_SIZE;
    const pageEntries = changelogCache.slice(start, start + CHANGELOG_PAGE_SIZE);
    picker.innerHTML = pageEntries.map((entry, i) => `<option value="${start + i}">${escapeHtml(versionLabel(entry.version))}</option>`).join("");
    if (pageLabel) pageLabel.textContent = `Page ${pageState.page + 1} of ${pageCount}`;
    if (prevBtn) prevBtn.disabled = pageState.page === 0;
    if (nextBtn) nextBtn.disabled = pageState.page >= pageCount - 1;
    renderEntry();
  };

  picker.addEventListener("change", renderEntry);
  // Bound once per render (these buttons are freshly created above via
  // innerHTML each call), and guarded by the same disabled check used on
  // the button itself — so a click can't advance past the last page and
  // land on a different version's notes when there's only one page.
  if (prevBtn) prevBtn.addEventListener("click", () => { if (pageState.page > 0) { pageState.page--; renderPage(); } });
  if (nextBtn) nextBtn.addEventListener("click", () => { if (pageState.page < pageCount - 1) { pageState.page++; renderPage(); } });

  renderPage();
}

const changelogModalPageState = { page: 0 };

function renderChangelog() {
  changelogModalPageState.page = 0; // always open "What's new" on the latest version
  renderChangelogPicker("changelogBody", changelogModalPageState);
}

document.getElementById("btnChangelog").addEventListener("click", () => {
  renderChangelog();
  openModal("changelogModal");
});

// ===================== TWITCH SETUP =====================

async function checkTwitchSetup() {
  try {
    const status = await api("/settings/twitch");
    // Guest browsing: not being configured no longer forces the setup
    // modal to stay open with no way out — it's shown once as a
    // suggestion, but "Browse without connecting" (or just closing it)
    // lets the person use the rest of the app freely. Only adding a
    // streamer is actually blocked server-side without credentials.
    if (!status.configured && !localStorage.getItem("scoutbot_skipped_setup")) {
      document.getElementById("setupClientId").value = status.client_id || "";
      openModal("setupModal");
    }
  } catch (e) { /* non-critical — the rest of the app still works to browse */ }
}

document.getElementById("setupSkip").addEventListener("click", () => {
  localStorage.setItem("scoutbot_skipped_setup", "1");
  closeModal("setupModal");
  toast("👀 Browsing without a Twitch connection — connect anytime from Settings to add streamers", "");
});

document.getElementById("setupSubmit").addEventListener("click", async () => {
  const idInput = document.getElementById("setupClientId");
  const secretInput = document.getElementById("setupClientSecret");
  const errEl = document.getElementById("setupError");
  const client_id = idInput.value.trim();
  const client_secret = secretInput.value.trim();

  if (!client_id || !client_secret) {
    errEl.textContent = "Both fields are required";
    return;
  }

  errEl.textContent = "";
  const btn = document.getElementById("setupSubmit");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    await api("/settings/twitch", { method: "POST", body: JSON.stringify({ client_id, client_secret }) });
    toast("✅ Connected to Twitch", "success");
    closeModal("setupModal");
    secretInput.value = "";
    loadStats();
    loadCategories();
    loadView();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Save & connect";
  }
});

// ===================== SETTINGS MENU =====================
// General / Accessibility / Themes / Diagnostics / Changelog tabs. Kept
// as one modal with tab-switched panels (matches the existing modal
// pattern) rather than five separate modals.

const PREF_KEY = "scoutbot_prefs";

// density: "comfortable" (default, existing spacing) | "compact" (tighter
// row spacing for scanning a large roster). showLocationCompact: whether
// the 📍 location line renders while density is "compact" — irrelevant
// (and ignored) in "comfortable", where location already always shows.
const PREF_DEFAULTS = { fontSize: "normal", reducedMotion: false, highContrast: false, theme: "dark", density: "comfortable", showLocationCompact: true };

function loadPrefs() {
  try {
    return { ...PREF_DEFAULTS, ...JSON.parse(localStorage.getItem(PREF_KEY) || "{}") };
  } catch (e) {
    return { ...PREF_DEFAULTS };
  }
}

function savePrefs(prefs) {
  localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
}

function applyPrefs(prefs) {
  const root = document.documentElement;
  root.dataset.fontSize = prefs.fontSize;
  root.dataset.reducedMotion = prefs.reducedMotion ? "1" : "0";
  root.dataset.highContrast = prefs.highContrast ? "1" : "0";
  root.dataset.density = prefs.density || "comfortable";

  let theme = prefs.theme;
  if (theme === "system") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  root.dataset.theme = theme;
}

let currentPrefs = loadPrefs();
applyPrefs(currentPrefs);
window.matchMedia?.("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (currentPrefs.theme === "system") applyPrefs(currentPrefs);
});

document.querySelectorAll(".settings-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".settings-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".settings-panel").forEach(p => { p.style.display = "none"; });
    const panel = document.getElementById("panel-" + tab.dataset.tab);
    if (panel) panel.style.display = "block";
    if (tab.dataset.tab === "diagnostics") loadDiagnostics();
    if (tab.dataset.tab === "changelog") renderSettingsChangelog();
    if (tab.dataset.tab === "blacklist") loadSettingsBlacklist();
    if (tab.dataset.tab === "icons") loadSettingsCustomPlatforms();
    if (tab.dataset.tab === "import") refreshSettingsImportStatus();
  });
});

async function refreshSettingsGeneral() {
  const statusEl = document.getElementById("settingsTwitchStatus");
  const rateLimitEl = document.getElementById("settingsTwitchRateLimit");
  try {
    const status = await api("/settings/twitch");
    statusEl.textContent = status.configured
      ? `Connected (Client ID: ${status.client_id})`
      : "Not connected — browsing works, but adding streamers needs a connection";

    // Rate-limit telemetry: read from Twitch's own Ratelimit-* response
    // headers on the most recent Helix call (see
    // twitch_api.get_rate_limit_snapshot). Nulls mean no Twitch call has
    // completed yet this process — show nothing rather than a
    // misleading "0/0". Warn proactively once remaining is low, instead
    // of only finding out via a 429 on the next action.
    if (rateLimitEl) {
      const rl = status.rate_limit;
      if (!rl || rl.remaining == null) {
        rateLimitEl.textContent = "";
      } else {
        const parts = [`Twitch API quota: ${rl.remaining}${rl.limit != null ? ` / ${rl.limit}` : ""} remaining`];
        if (rl.reset_at) {
          const resetIn = Math.max(0, Math.round(rl.reset_at - Date.now() / 1000));
          parts.push(`resets in ${resetIn}s`);
        }
        rateLimitEl.textContent = (rl.low ? "⚠️ " : "") + parts.join(" · ");
        rateLimitEl.classList.toggle("settings-status-warning", !!rl.low);
      }
    }
  } catch (e) {
    statusEl.textContent = "Couldn't check connection status";
    if (rateLimitEl) rateLimitEl.textContent = "";
  }

  const authEl = document.getElementById("settingsAuthStatus");
  if (authEl) {
    try {
      const auth = await api("/settings/auth");
      const localOnly = auth.host === "127.0.0.1" || auth.host === "localhost";
      if (auth.enabled) {
        authEl.innerHTML = `🔒 Basic auth is <strong>on</strong> — a username/password is required to reach this app.`;
      } else if (localOnly) {
        authEl.innerHTML = `Basic auth is off. That's fine while ScoutBot is only reachable on this machine (${escapeHtml(auth.host)}).`;
      } else {
        authEl.innerHTML = `⚠️ Basic auth is <strong>off</strong>, and this app is bound to <code>${escapeHtml(auth.host)}</code> — anyone who can reach this machine has full read/write access. Set <code>WEB_USERNAME</code> / <code>WEB_PASSWORD</code> in <code>.env</code> and restart to require a login (see README).`;
      }
    } catch (e) {
      authEl.textContent = "Couldn't check access control status";
    }
  }
}

document.getElementById("settingsReconnect").addEventListener("click", () => {
  closeModal("settingsModal");
  document.getElementById("setupClientId").value = "";
  openModal("setupModal");
});

async function refreshSettingsImportStatus() {
  const status = document.getElementById("settingsImportStatus");
  if (!status) return;
  try {
    const result = await api("/settings/twitch");
    status.textContent = result.configured
      ? "Current ScoutBot credentials are configured. Importing a .env will replace them."
      : "No Twitch credentials are currently configured."
  } catch (e) {
    status.textContent = "Choose the files you want to import, then click Import selected files.";
  }
}

const settingsImportPreview = document.getElementById("settingsImportPreview");
if (settingsImportPreview) settingsImportPreview.addEventListener("click", async () => {
  const env=document.getElementById("settingsImportEnv")?.files?.[0], dbf=document.getElementById("settingsImportDb")?.files?.[0], status=document.getElementById("settingsImportStatus");
  if(!env&&!dbf){status.textContent="Choose a file to preview first.";return;}
  const form=new FormData(); if(env)form.append("env_file",env,env.name); if(dbf)form.append("db_file",dbf,dbf.name);
  settingsImportPreview.disabled=true; status.textContent="Checking the selected files…";
  try{const r=await fetch(`${API}/maintenance/import-preview`,{method:"POST",body:form});const d=await r.json();if(!r.ok)throw new Error(d.detail||"Preview failed");
    const envText=d.env?`.env: valid · ${d.env.keys.length} settings (${d.env.keys.join(", ")})`:".env: not selected";
    const dbText=d.db?`streamers.db: valid · ${d.db.streamers.toLocaleString()} streamers (current ${d.db.current_streamers.toLocaleString()}, change ${d.db.streamer_count_delta>=0?"+":""}${d.db.streamer_count_delta.toLocaleString()}) · integrity ${d.db.integrity}`:"streamers.db: not selected";
    status.textContent=`🔎 ${envText} · ${dbText}. A safety backup is created automatically before database replacement.`;
  }catch(e){status.textContent="Preview failed: "+(e.message||"");}finally{settingsImportPreview.disabled=false;}
});

const settingsImportSubmit = document.getElementById("settingsImportSubmit");
if (settingsImportSubmit) {
  settingsImportSubmit.addEventListener("click", async () => {
    const envInput = document.getElementById("settingsImportEnv");
    const dbInput = document.getElementById("settingsImportDb");
    const status = document.getElementById("settingsImportStatus");
    const envFile = envInput?.files?.[0];
    const dbFile = dbInput?.files?.[0];

    if (!envFile && !dbFile) {
      status.textContent = "Choose a .env file, a streamers.db file, or both first.";
      return;
    }

    const form = new FormData();
    if (envFile) form.append("env_file", envFile, envFile.name);
    if (dbFile) form.append("db_file", dbFile, dbFile.name);

    settingsImportSubmit.disabled = true;
    status.textContent = "Importing selected files…";
    try {
      if (dbFile) await prepareDatabaseReplacement();
      const response = await fetch(`${API}/system/import`, { method: "POST", body: form });
      let payload = null;
      try { payload = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(payload?.detail || `Import failed (${response.status})`);

      const imported = Array.isArray(payload?.imported) ? payload.imported : [];
      status.textContent = `Import complete: ${imported.map(v => v === "env" ? ".env" : "streamers.db").join(" + ")}. ScoutBot is refreshing now.`;
      if (envInput) envInput.value = "";
      if (dbInput) dbInput.value = "";

      setTimeout(async () => {
        await refreshSettingsGeneral();
        await refreshAfterDatabaseReplacement();
        status.textContent = "Import complete. Your ScoutBot data is now loaded.";
      }, 500);
    } catch (e) {
      status.textContent = e?.message || "Import failed. Nothing was changed.";
    } finally {
      settingsImportSubmit.disabled = false;
    }
  });
}

document.getElementById("settingsExit").addEventListener("click", async () => {
  if (!confirm("Exit ScoutBot? This stops the local server and closes the app.")) return;
  try {
    await api("/system/exit", { method: "POST" });
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:\'Space Grotesk\',sans-serif;color:#8B93A1;">ScoutBot has stopped. You can close this tab.</div>';
  } catch (e) {
    // The server process dies mid-response sometimes, which surfaces as a
    // fetch error even though the shutdown itself succeeded — treat any
    // failure here as "probably shut down" rather than showing an error.
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:\'Space Grotesk\',sans-serif;color:#8B93A1;">ScoutBot has stopped. You can close this tab.</div>';
  }
});

// --- Accessibility tab ---

const fontSizeSel = document.getElementById("settingsFontSize");
const reducedMotionChk = document.getElementById("settingsReducedMotion");
const highContrastChk = document.getElementById("settingsHighContrast");

fontSizeSel.addEventListener("change", () => {
  currentPrefs.fontSize = fontSizeSel.value;
  savePrefs(currentPrefs);
  applyPrefs(currentPrefs);
});
reducedMotionChk.addEventListener("change", () => {
  currentPrefs.reducedMotion = reducedMotionChk.checked;
  savePrefs(currentPrefs);
  applyPrefs(currentPrefs);
});
highContrastChk.addEventListener("change", () => {
  currentPrefs.highContrast = highContrastChk.checked;
  savePrefs(currentPrefs);
  applyPrefs(currentPrefs);
});

// --- Themes tab ---

document.querySelectorAll(".theme-swatch").forEach(btn => {
  btn.addEventListener("click", () => {
    currentPrefs.theme = btn.dataset.theme;
    savePrefs(currentPrefs);
    applyPrefs(currentPrefs);
    document.querySelectorAll(".theme-swatch").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

function syncSettingsFormFromPrefs() {
  fontSizeSel.value = currentPrefs.fontSize;
  reducedMotionChk.checked = currentPrefs.reducedMotion;
  highContrastChk.checked = currentPrefs.highContrast;
  document.querySelectorAll(".theme-swatch").forEach(b => b.classList.toggle("active", b.dataset.theme === currentPrefs.theme));
}

// --- Roster density (compact/comfortable) + compact-mode location column ---

function syncDensityControls() {
  const density = currentPrefs.density || "comfortable";
  document.querySelectorAll(".density-btn").forEach(btn => {
    const active = btn.dataset.density === density;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
  const wrap = document.getElementById("densityLocationToggleWrap");
  const chk = document.getElementById("densityLocationToggle");
  wrap.style.display = density === "compact" ? "" : "none";
  chk.checked = currentPrefs.showLocationCompact !== false;
}

document.querySelectorAll(".density-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    currentPrefs.density = btn.dataset.density;
    savePrefs(currentPrefs);
    applyPrefs(currentPrefs);
    syncDensityControls();
    if (state.view !== "dashboard") renderGrid();
  });
});

document.getElementById("densityLocationToggle").addEventListener("change", (e) => {
  currentPrefs.showLocationCompact = e.target.checked;
  savePrefs(currentPrefs);
  if (state.view !== "dashboard") renderGrid();
});

syncDensityControls();

// --- Blacklist tab ---

function blacklistItemHtml(entry) {
  return `
    <div class="discover-item" data-blacklist-username="${escapeAttr(entry.username)}">
      <div class="discover-item-avatar-fallback">${initials(entry.username)}</div>
      <div class="discover-item-info">
        <a class="discover-item-name" href="${escapeAttr(`https://twitch.tv/${encodeURIComponent(entry.username)}`)}" target="_blank" rel="noopener">${escapeHtml(entry.username)}</a>
        <div class="discover-item-meta">Blacklisted ${entry.blacklisted_at ? escapeHtml(new Date(entry.blacklisted_at).toLocaleString()) : ""}</div>
      </div>
      <button class="btn btn-ghost btn-small" style="width:auto;" data-blacklist-remove="${escapeAttr(entry.username)}" aria-label="Remove ${escapeAttr(entry.username)} from blacklist">Remove</button>
    </div>
  `;
}

async function loadSettingsBlacklist() {
  const list = document.getElementById("settingsBlacklistList");
  if (!list) return;
  list.innerHTML = `<div class="detail-empty">Loading…</div>`;
  try {
    const data = await api("/blacklist");
    if (!data.items.length) {
      list.innerHTML = `<div class="detail-empty">No streamers blacklisted yet.</div>`;
      return;
    }
    list.innerHTML = data.items.map(blacklistItemHtml).join("");
    list.querySelectorAll("[data-blacklist-remove]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const username = btn.dataset.blacklistRemove;
        btn.disabled = true;
        try {
          await api(`/blacklist/${encodeURIComponent(username)}`, { method: "DELETE" });
          toast(`Removed ${username} from blacklist`, "success");
          const item = btn.closest("[data-blacklist-username]");
          if (item) item.remove();
          if (!list.querySelector("[data-blacklist-username]")) {
            list.innerHTML = `<div class="detail-empty">No streamers blacklisted yet.</div>`;
          }
        } catch (e) {
          btn.disabled = false;
          toast(e.message, "error");
        }
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="detail-empty">Couldn't load blacklist: ${escapeHtml(e.message)}</div>`;
  }
}

document.getElementById("blacklistAddSubmit").addEventListener("click", async () => {
  const input = document.getElementById("blacklistAddInput");
  const errorEl = document.getElementById("blacklistError");
  const username = extractTwitchUsername(input.value) || input.value.trim().replace(/^@/, "").toLowerCase();
  errorEl.textContent = "";
  if (!username) {
    errorEl.textContent = "Enter a Twitch username.";
    return;
  }
  try {
    await api("/blacklist", { method: "POST", body: JSON.stringify({ username }) });
    toast(`🚫 ${username} blacklisted`, "success");
    input.value = "";
    loadSettingsBlacklist();
  } catch (e) {
    errorEl.textContent = e.message;
  }
});

document.getElementById("blacklistAddInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("blacklistAddSubmit").click(); }
});

// --- Custom social platforms (Settings > Icons) ---
// Settings-defined platforms (label + URL-matching pattern) merged into
// the backend's SOCIAL_PATTERNS at scrape time (see
// twitch_api.get_effective_social_patterns) so bio/panel links to a
// platform the built-in scraper doesn't recognize still get labelled.
// Mirrors the blacklist tab's add/list/remove pattern above.

function customPlatformItemHtml(entry) {
  return `
    <div class="discover-item" data-platform-label="${escapeAttr(entry.label)}">
      <div class="discover-item-info">
        <div class="discover-item-name">${escapeHtml(entry.label)}</div>
        <div class="discover-item-meta">${escapeHtml(entry.pattern)}</div>
      </div>
      <button class="btn btn-ghost btn-small" style="width:auto;" data-platform-remove="${escapeAttr(entry.label)}" aria-label="Remove custom platform ${escapeAttr(entry.label)}">Remove</button>
    </div>
  `;
}

async function loadSettingsCustomPlatforms() {
  const list = document.getElementById("settingsCustomPlatformsList");
  if (!list) return;
  list.innerHTML = `<div class="detail-empty">Loading…</div>`;
  try {
    const data = await api("/social-platforms");
    customPlatformLabels = data.items.map(entry => entry.label);
    if (!data.items.length) {
      list.innerHTML = `<div class="detail-empty">No custom platforms added yet.</div>`;
    } else {
      list.innerHTML = data.items.map(customPlatformItemHtml).join("");
      list.querySelectorAll("[data-platform-remove]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const label = btn.dataset.platformRemove;
          btn.disabled = true;
          try {
            await api(`/social-platforms/${encodeURIComponent(label)}`, { method: "DELETE" });
            toast(`Removed ${label}`, "success");
            customPlatformLabels = customPlatformLabels.filter(l => l !== label);
            const item = btn.closest("[data-platform-label]");
            if (item) item.remove();
            if (!list.querySelector("[data-platform-label]")) {
              list.innerHTML = `<div class="detail-empty">No custom platforms added yet.</div>`;
            }
            renderSettingsIcons();
          } catch (e) {
            btn.disabled = false;
            toast(e.message, "error");
          }
        });
      });
    }
  } catch (e) {
    list.innerHTML = `<div class="detail-empty">Couldn't load custom platforms: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderSettingsIcons();
}

document.getElementById("customPlatformAddSubmit").addEventListener("click", async () => {
  const labelInput = document.getElementById("customPlatformLabelInput");
  const patternInput = document.getElementById("customPlatformPatternInput");
  const errorEl = document.getElementById("customPlatformError");
  const label = labelInput.value.trim();
  const pattern = patternInput.value.trim();
  errorEl.textContent = "";
  if (!label) {
    errorEl.textContent = "Enter a label.";
    return;
  }
  if (!pattern) {
    errorEl.textContent = "Enter a URL-matching pattern.";
    return;
  }
  try {
    await api("/social-platforms", { method: "POST", body: JSON.stringify({ label, pattern }) });
    toast(`✅ ${label} added`, "success");
    labelInput.value = "";
    patternInput.value = "";
    loadSettingsCustomPlatforms();
  } catch (e) {
    errorEl.textContent = e.message;
  }
});

[document.getElementById("customPlatformLabelInput"), document.getElementById("customPlatformPatternInput")].forEach(input => {
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); document.getElementById("customPlatformAddSubmit").click(); }
  });
});

// --- Diagnostics tab ---

function loadErrorCenter(){const el=document.getElementById("errorCenterList");if(!el)return;try{const items=JSON.parse(localStorage.getItem("scoutbot_error_log")||"[]");el.innerHTML=items.length?items.slice(0,12).map(x=>`<div class="backup-item"><span>🔴 ${escapeHtml(x.message)}</span><small>${escapeHtml(new Date(x.time).toLocaleString())}</small></div>`).join(""):"<div class='notes-hint'>🟢 No recent errors recorded.</div>";}catch(e){el.textContent="Error history unavailable.";}}

async function loadDiagnostics() {
  const body = document.getElementById("diagnosticsBody");
  body.textContent = "Loading…";
  try {
    const [version, stats, twitch, health] = await Promise.all([
      api("/version"), api("/stats"), api("/settings/twitch"), api("/maintenance/diagnostics"),
    ]);
    const db=health.database||{}, backup=health.backup||{};
    body.innerHTML = `
      <div class="diag-row"><span>App version</span><span>${escapeHtml(version.version)}</span></div>
      <div class="diag-row"><span>Twitch connected</span><span>${twitch.configured ? "🟢 Yes" : "⚪ No"}</span></div>
      <div class="diag-row"><span>Streamers tracked</span><span>${Number(stats.total??0).toLocaleString()}</span></div>
      <div class="diag-row"><span>Currently live</span><span>${Number(stats.live??0).toLocaleString()}</span></div>
      <div class="diag-row"><span>Database</span><span>${db.healthy?"🟢 Healthy":"🔴 Problem"} · ${escapeHtml(db.integrity||"")}</span></div>
      <div class="diag-row"><span>Database size</span><span>${formatBytes(db.size||0)} · ${db.tables||0} tables</span></div>
      <div class="diag-row"><span>Backups</span><span>${backup.count||0}${backup.latest?` · latest ${escapeHtml(backup.latest)}`:""}</span></div>
      <div class="diag-row"><span>Background jobs</span><span>${health.jobs_running||0} running</span></div>
      <div class="diag-row"><span>Live event stream</span><span>${eventStreamConnected ? "🟢 Connected" : "🟡 Reconnecting…"}</span></div>
      <div class="diag-row"><span>Browser</span><span>${escapeHtml(navigator.userAgent)}</span></div>`;
    loadBackupList();
    loadDuplicateList();
    loadErrorCenter();
  } catch (e) { body.textContent = "Couldn't load diagnostics: " + e.message; }
}
function formatBytes(n){if(n<1024)return `${n} B`;if(n<1048576)return `${(n/1024).toFixed(1)} KB`;if(n<1073741824)return `${(n/1048576).toFixed(1)} MB`;return `${(n/1073741824).toFixed(1)} GB`;}
async function loadBackupList(){const el=document.getElementById("backupList");if(!el)return;try{const d=await api("/maintenance/backups");el.innerHTML=d.items.length?d.items.map(x=>`<div class="backup-item"><span>💾 ${escapeHtml(x.filename)} · ${formatBytes(x.size)}</span><button class="btn btn-ghost btn-small" data-restore-backup="${escapeAttr(x.filename)}" style="width:auto;">Restore</button></div>`).join(""):"<div class='notes-hint'>No backups yet.</div>";el.querySelectorAll("[data-restore-backup]").forEach(btn=>btn.addEventListener("click",async()=>{if(!confirm(`Restore ${btn.dataset.restoreBackup}? A fresh backup of the current database will be made first.`))return;btn.disabled=true;try{await prepareDatabaseReplacement();await api(`/maintenance/restore/${encodeURIComponent(btn.dataset.restoreBackup)}`,{method:"POST"});toast("♻️ Database restored","success");await refreshAfterDatabaseReplacement();await loadDiagnostics();}catch(e){toast(e.message||"Restore failed","error");}finally{btn.disabled=false;}}));}catch(e){el.textContent="Could not load backups.";}}
async function loadDuplicateList(){const el=document.getElementById("duplicateList");if(!el)return;try{const d=await api("/maintenance/duplicates");el.innerHTML=d.items.length?d.items.map(x=>`<div class="backup-item"><span>⚠️ ${escapeHtml(x.type)}: <strong>${escapeHtml(x.value)}</strong> · ${x.count} records</span><span>${x.usernames.map(escapeHtml).join(", ")}</span></div>`).join(""):"<div class='notes-hint'>🟢 No duplicate alias candidates found.</div>";}catch(e){el.textContent="Could not check duplicates.";}}

document.getElementById("diagnosticsBackup")?.addEventListener("click",async()=>{const btn=document.getElementById("diagnosticsBackup");btn.disabled=true;try{const j=await api("/maintenance/backup",{method:"POST"});let d;do{await new Promise(r=>setTimeout(r,250));d=await api(`/jobs/${j.job_id}`);}while(d.status==="queued"||d.status==="running");if(d.status!=="complete")throw new Error(d.error||"Backup failed");toast("💾 Database backup verified","success");loadDiagnostics();}catch(e){toast(e.message||"Backup failed","error");}finally{btn.disabled=false;}});

document.getElementById("diagnosticsRefresh").addEventListener("click", loadDiagnostics);

// --- Changelog tab (reuses the same cached data as the topbar button) ---
// Rather than stacking every version's notes in one long scroll, the
// Settings tab uses a dropdown to step through versions one at a time —
// changelogCache is already ordered latest-first (see app_version.py), so
// the first <option> is the latest version, matching that ordering.
// As the changelog keeps growing, the dropdown itself is paginated (a
// fixed number of versions per page) with Prev/Next controls, instead of
// ever having to scroll through a single huge <select>.

const settingsChangelogPageState = { page: 0 };

function renderSettingsChangelog() {
  renderChangelogPicker("settingsChangelogBody", settingsChangelogPageState);
}

document.getElementById("btnSettings").addEventListener("click", () => {
  refreshSettingsGeneral();
  syncSettingsFormFromPrefs();
  openModal("settingsModal");
});

// ===================== OFFLINE SUPPORT =====================
// Registers sw.js (see that file for the actual caching strategy) so
// the app shell and a handful of read-only roster endpoints stay
// available when Twitch/the network is unreachable. Registration
// failures (unsupported browser, served over plain http on a
// non-localhost host, etc.) are non-fatal — the app already works
// fully online without a service worker, this is purely additive.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => {
      console.warn("Service worker registration failed:", e);
    });
  });
}

// Lightweight offline/online banner — the service worker keeps the
// roster browsable while offline, but the person should still know
// they're looking at possibly-stale, read-only cached data rather than
// assuming everything (adding streamers, saving notes, etc.) still
// works normally.
function updateOfflineBanner() {
  let banner = document.getElementById("offlineBanner");
  if (navigator.onLine) {
    if (banner) banner.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "offlineBanner";
    banner.className = "offline-banner";
    banner.textContent = "📡 Offline — showing cached roster data. Changes can't be saved until you're back online.";
    document.body.prepend(banner);
  }
}

window.addEventListener("online", updateOfflineBanner);
window.addEventListener("offline", updateOfflineBanner);

// ===================== TEXT FIELD COPY/PASTE MENU =====================
// A small custom right-click menu for text inputs/textareas, offering
// Cut/Copy/Paste in the app's own styling. This is purely additive: the
// browser's native context menu is untouched everywhere else, and
// native shortcuts (Ctrl+C/X/V) keep working in fields exactly as
// before regardless of whether this menu is used. Uses the standard
// async Clipboard API (navigator.clipboard) rather than the deprecated
// document.execCommand.
let _fieldMenuEl = null;
let _fieldMenuTarget = null;

function _closeFieldMenu() {
  if (_fieldMenuEl) {
    _fieldMenuEl.remove();
    _fieldMenuEl = null;
    _fieldMenuTarget = null;
    document.removeEventListener("mousedown", _onFieldMenuOutsideClick, true);
    document.removeEventListener("keydown", _onFieldMenuKeydown, true);
  }
}

function _onFieldMenuOutsideClick(e) {
  if (_fieldMenuEl && !_fieldMenuEl.contains(e.target)) _closeFieldMenu();
}

function _onFieldMenuKeydown(e) {
  if (e.key === "Escape") _closeFieldMenu();
}

async function _fieldMenuAction(action) {
  const field = _fieldMenuTarget;
  if (!field) return;
  const start = field.selectionStart ?? 0;
  const end = field.selectionEnd ?? 0;
  try {
    if (action === "cut" || action === "copy") {
      const selected = field.value.slice(start, end);
      if (!selected) { _closeFieldMenu(); return; }
      await navigator.clipboard.writeText(selected);
      if (action === "cut") {
        field.setRangeText("", start, end, "end");
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } else if (action === "paste") {
      const text = await navigator.clipboard.readText();
      field.setRangeText(text, start, end, "end");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
  } catch (err) {
    // Most likely a browser/OS clipboard-permission denial. Native
    // Ctrl+C/X/V still work regardless, so this is a soft failure.
    toast("Clipboard access was blocked by the browser — try Ctrl+C/X/V instead.", "error");
  }
  field.focus();
  _closeFieldMenu();
}

function _openFieldMenu(field, x, y) {
  _closeFieldMenu();
  const hasSelection = (field.selectionStart ?? 0) !== (field.selectionEnd ?? 0);
  const items = [
    { action: "cut", label: "✂️ Cut", enabled: hasSelection },
    { action: "copy", label: "📋 Copy", enabled: hasSelection },
    { action: "paste", label: "📥 Paste", enabled: true },
  ];
  const menu = document.createElement("div");
  menu.className = "field-context-menu";
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "field-context-menu-item";
    btn.textContent = item.label;
    btn.disabled = !item.enabled;
    btn.addEventListener("click", () => _fieldMenuAction(item.action));
    menu.appendChild(btn);
  }
  document.body.appendChild(menu);

  // Position within viewport bounds.
  const menuRect = menu.getBoundingClientRect();
  const maxX = window.innerWidth - menuRect.width - 8;
  const maxY = window.innerHeight - menuRect.height - 8;
  menu.style.left = Math.max(8, Math.min(x, maxX)) + "px";
  menu.style.top = Math.max(8, Math.min(y, maxY)) + "px";

  _fieldMenuEl = menu;
  _fieldMenuTarget = field;
  document.addEventListener("mousedown", _onFieldMenuOutsideClick, true);
  document.addEventListener("keydown", _onFieldMenuKeydown, true);
}

document.addEventListener("contextmenu", (e) => {
  const el = e.target;
  const isTextField =
    (el.tagName === "TEXTAREA") ||
    (el.tagName === "INPUT" && ["text", "search", "url", "tel", "password"].includes(el.type));
  if (!isTextField || el.disabled || el.readOnly) return;
  // Only intercept when the Clipboard API is actually available (secure
  // context/supported browser); otherwise let the native menu show.
  if (!navigator.clipboard) return;
  e.preventDefault();
  _openFieldMenu(el, e.clientX, e.clientY);
});

// ===================== INIT =====================

let eventStreamConnected = false;

async function init() {
  readStateFromUrl();
  syncControlsFromState();
  updateOfflineBanner();
  await Promise.all([loadStats(), loadCategories(), loadLocations(), loadView(), loadVersion(), checkTwitchSetup(), loadPresets(), loadRecentlyViewed(), loadSuggestions()]);
  connectEventStream();
}

init();


// VOD downloader modal controls.
const vodTabVods = document.getElementById("vodTabVods");
const vodTabClips = document.getElementById("vodTabClips");
if (vodTabVods) vodTabVods.onclick = () => { vodState.tab="vods"; loadVodItems(); };
if (vodTabClips) vodTabClips.onclick = () => { vodState.tab="clips"; loadVodItems(); };
const vodDownloadBtn = document.getElementById("vodDownloadBtn");
if (vodDownloadBtn) vodDownloadBtn.onclick = startVodDownload;
