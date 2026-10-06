// Zero-viewer tab keeps its filters and request state independent of Twitch Discover.
(() => {
  "use strict";
  const STORAGE_KEY = "scoutbot_zero_viewer_filters";
  const byId = id => document.getElementById(id);
  const phrase = byId("nobodySearchPhrase");
  const match = byId("nobodySearchMatch");
  const remember = byId("nobodyRememberFilters");
  const submit = byId("nobodyDiscoverSubmit");
  const status = byId("nobodyDiscoverStatus");
  const results = byId("nobodyDiscoverResults");
  let controller = null;

  function filters() {
    return { include: phrase.value.trim(), match: match.value };
  }

  function saveFilters() {
    try {
      if (remember.checked) localStorage.setItem(STORAGE_KEY, JSON.stringify(filters()));
      else localStorage.removeItem(STORAGE_KEY);
      return true;
    } catch {
      status.classList.add("discovery-error");
      status.textContent = "Your browser could not save these filters. Search still works.";
      return false;
    }
  }

  function loadFilters() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw || raw.length > 512) return;
      const saved = JSON.parse(raw);
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;
      if (typeof saved.include !== "string" || saved.include.length > 65 || !["all", "any"].includes(saved.match)) return;
      phrase.value = saved.include;
      match.value = saved.match;
      remember.checked = true;
    } catch { /* Unavailable/corrupt storage must not prevent discovery. */ }
  }

  function cancel() {
    status.classList.remove("discovery-error");
    if (controller) status.textContent = "Search cancelled. Search again when you're ready.";
    controller?.abort();
    controller = null;
    submit.disabled = false;
    results.setAttribute("aria-busy", "false");
  }

  function reset() {
    cancel();
    results.replaceChildren();
    status.textContent = "Database changed. Search again to refresh tracking status.";
  }

  function streamHtml(stream) {
    const username = encodeURIComponent(stream.username);
    const thumbnail = `https://static-cdn.jtvnw.net/previews-ttv/live_user_${username}-320x180.jpg`;
    return `<section class="zero-viewer-card">
      <a href="https://twitch.tv/${username}" target="_blank" rel="noopener">
        <img src="${escapeAttr(thumbnail)}" alt="" width="320" height="180" loading="lazy">
        <strong class="card-username">${escapeHtml(stream.display_name || stream.username)}</strong>
      </a>
      <div class="discover-item-meta">0 viewers &middot; ${escapeHtml(stream.category || "No category")}</div>
      <div class="zero-viewer-title">${escapeHtml(stream.title || "")}</div>
      <div class="zero-viewer-tags">${(stream.tags || []).map(tag => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
      ${trackButtonHtml(stream)}
    </section>`;
  }

  async function search() {
    cancel();
    const request = new AbortController();
    controller = request;
    submit.disabled = true;
    results.replaceChildren();
    results.setAttribute("aria-busy", "true");
    const saved = saveFilters();
    status.textContent = "Finding zero-viewer streams…";
    try {
      const data = await apiAbortable("/discover/zero-viewers" + qs(filters()), {}, request.signal);
      if (controller !== request) return;
      results.innerHTML = data.items.map(streamHtml).join("");
      wireTrackButtons(results);
      status.textContent = data.items.length
        ? `${data.items.length} zero-viewer stream${data.items.length === 1 ? "" : "s"} found. Refresh for another random sample.`
        : "No matching zero-viewer streams found. Try fewer terms, Any term, or a blank search.";
      if (!saved) status.textContent += " Your browser could not save these filters.";
    } catch (error) {
      if (error.name !== "AbortError" && controller === request) {
        status.classList.add("discovery-error");
        status.textContent = error.message;
      }
    } finally {
      if (controller === request) {
        controller = null;
        submit.disabled = false;
        results.setAttribute("aria-busy", "false");
      }
    }
  }

  function renderChips() {
    const entries = [];
    if (phrase.value.trim()) entries.push({label: `Search: ${phrase.value.trim()}`, remove: () => {
      phrase.value = ""; renderChips(); search();
    }});
    if (match.value === "any") entries.push({label: "Any term", remove: () => {
      match.value = "all"; renderChips(); search();
    }});
    renderFilterChips(byId("nobodyFilterChips"), entries, () => {
      phrase.value = ""; match.value = "all"; renderChips(); search();
    });
  }
  for (const field of [phrase, match]) field.addEventListener("input", renderChips);
  byId("nobodyDiscoverForm").addEventListener("submit", event => {
    event.preventDefault();
    search();
  });
  for (const field of [phrase, match, remember]) field.addEventListener("change", saveFilters);
  loadFilters();
  renderChips();
  window.ScoutNobodyDiscovery = { cancel, reset };
})();
