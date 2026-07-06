/** 碰撞沙盒测试页：可视化标注 + 机组记录列表 + 调参重算 */

let sandboxInitialized = false;
let sandboxSessions = [];
let sandboxActiveSessionId = null;
let sandboxRecordsCache = [];
let sandboxSelectedCameraSlug = null;
let sandboxCameraListPinned = false;
let sandboxSelectedRecordId = null;
const sandboxRecordsByTier = new Map();
const sandboxTierLoadState = new Map();

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function sandboxStatusEl() {
  return $("#sandbox-status");
}

function setSandboxStatus(msg, kind = "") {
  const el = sandboxStatusEl();
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("error", kind === "error");
  el.classList.toggle("hidden", !msg);
}

function getSandboxPoseTier() {
  return $("#sandbox-pose-tier")?.value || "rtmpose-m";
}

function getSandboxTierLoadState(tier) {
  const key = String(tier || "rtmpose-m").trim();
  if (!sandboxTierLoadState.has(key)) {
    sandboxTierLoadState.set(key, {
      items: [],
      nextOffset: 0,
      hasMore: true,
      loadingMore: false,
    });
  }
  return sandboxTierLoadState.get(key);
}

function parseSandboxTagFilterQuery() {
  return String($("#sandbox-tag-filter")?.value || "")
    .split(/[,，]/)
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

function sandboxReviewFilterQuery() {
  const status = String($("#sandbox-review-status-filter")?.value || "all").trim().toLowerCase();
  return status === "all" ? "" : status;
}

function sandboxVerifiedFilterQuery() {
  const mode = String($("#sandbox-verified-filter")?.value || "all").trim().toLowerCase();
  if (mode === "yes") return "yes";
  if (mode === "no") return "no";
  return "";
}

function filterSandboxRecords(items) {
  const filterQ = String($("#sandbox-record-filter")?.value || "")
    .trim()
    .toLowerCase();
  const tagFilter = parseSandboxTagFilterQuery();
  const reviewStatus = $("#sandbox-review-status-filter")?.value || "all";
  const verifiedMode = $("#sandbox-verified-filter")?.value || "all";
  return (items || []).filter((s) => {
    if (filterQ && !recordSearchBlob(s).includes(filterQ)) return false;
    if (!recordHasAllTags(s, tagFilter)) return false;
    if (!recordMatchesReviewStatus(s, reviewStatus)) return false;
    if (!recordMatchesVerifiedMode(s, verifiedMode)) return false;
    return true;
  });
}

function renderSandboxRecordItem(s) {
  const name = s.display_name || s.record_id;
  const esc = recordItemEsc;
  const reviewPill = renderReviewPill(s.event_review_status || "not_started", s.event_review_label);
  const tags = (Array.isArray(s.tags) ? s.tags : [])
    .map((t) => {
      const label = typeof t === "string" ? t : tagSuggestionName(t);
      if (!label) return "";
      return `<span class="record-tag readonly-tag">${esc(label)}</span>`;
    })
    .filter(Boolean)
    .join("");
  const selected = s.record_id === sandboxSelectedRecordId ? " selected" : "";
  return `
    <li class="record-item record-item-compact sandbox-record-item${selected}" data-record-id="${esc(s.record_id)}" role="button" tabindex="0">
      <div class="record-main record-main-compact">
        ${reviewPill}
        <strong class="record-name" title="${esc(name)}">${esc(name)}</strong>
        ${tags ? `<span class="record-tags-inline">${tags}</span>` : ""}
      </div>
    </li>`;
}

function sandboxRecordsCountSuffix(tier) {
  const state = getSandboxTierLoadState(tier);
  if (!state?.hasMore) return "";
  return "（已加载部分，可加载更多）";
}

function renderSandboxLoadMoreFooter(tier) {
  const state = getSandboxTierLoadState(tier);
  if (!state?.hasMore) return "";
  if (state.loadingMore) {
    return `<p class="hint playback-records-load-more">加载更多…</p>`;
  }
  return `<p class="playback-records-load-more"><button type="button" class="link-btn sandbox-load-more-btn">加载更多记录…</button></p>`;
}

function bindSandboxRecordListEvents(listEl) {
  listEl.querySelector(".sandbox-load-more-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    void loadMoreSandboxRecords();
  });
  listEl.querySelectorAll(".record-back-cameras").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      sandboxSelectedCameraSlug = null;
      sandboxCameraListPinned = true;
      renderSandboxRecordsList();
    });
  });
  listEl.querySelectorAll(".camera-group-item").forEach((li) => {
    const open = () => {
      sandboxSelectedCameraSlug = li.dataset.cameraSlug || null;
      sandboxCameraListPinned = false;
      renderSandboxRecordsList();
    };
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
  });
  listEl.querySelectorAll(".sandbox-record-item").forEach((li) => {
    const pick = () => selectSandboxRecordItem(li.dataset.recordId || "");
    li.addEventListener("click", pick);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        pick();
      }
    });
  });
}

function selectSandboxRecordItem(recordId) {
  sandboxSelectedRecordId = String(recordId || "").trim() || null;
  renderSandboxRecordsList();
  const createBtn = $("#sandbox-create");
  if (createBtn) createBtn.disabled = !sandboxSelectedRecordId;
}

function renderSandboxRecordsList() {
  const listEl = $("#sandbox-record-list");
  const countEl = $("#sandbox-record-count");
  if (!listEl) return;
  const items = sandboxRecordsCache;
  const tier = getSandboxPoseTier();
  const filterQ = String($("#sandbox-record-filter")?.value || "").trim();
  const tagFilter = parseSandboxTagFilterQuery();
  const hasFilter = Boolean(
    filterQ || tagFilter.length || sandboxReviewFilterQuery() || sandboxVerifiedFilterQuery()
  );

  if (!items.length) {
    listEl.innerHTML = "<p class='hint playback-records-empty'>暂无记录</p>";
    if (countEl) countEl.textContent = "";
    sandboxSelectedCameraSlug = null;
    return;
  }

  const filtered = filterSandboxRecords(items);
  const groups = buildRecordGroups(filtered);
  const keys = [...groups.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));

  if (sandboxSelectedCameraSlug && !groups.has(sandboxSelectedCameraSlug)) {
    sandboxSelectedCameraSlug = null;
  }
  if (!sandboxSelectedCameraSlug && sandboxSelectedRecordId && !sandboxCameraListPinned) {
    const autoSlug = cameraSlugForRecordId(sandboxSelectedRecordId);
    if (autoSlug && groups.has(autoSlug)) sandboxSelectedCameraSlug = autoSlug;
  }

  if (!filtered.length) {
    listEl.innerHTML = "<p class='hint playback-records-empty'>无匹配记录</p>";
    if (countEl) countEl.textContent = hasFilter ? `0 / ${items.length} 条` : "";
    bindSandboxRecordListEvents(listEl);
    return;
  }

  if (!sandboxSelectedCameraSlug) {
    if (countEl) {
      const moreHint = sandboxRecordsCountSuffix(tier);
      countEl.textContent = hasFilter
        ? `${tier} · ${keys.length} 个机位 · 匹配 ${filtered.length} / ${items.length} 条${moreHint}`
        : `${tier} · ${keys.length} 个机位 · 共 ${items.length} 条${moreHint}`;
    }
    listEl.innerHTML = `<ul class="camera-group-list">${keys
      .map((key) => renderCameraGroupItem(key, groups.get(key)))
      .join("")}</ul>${renderSandboxLoadMoreFooter(tier)}`;
    bindSandboxRecordListEvents(listEl);
    return;
  }

  const groupItems = groups.get(sandboxSelectedCameraSlug) || [];
  const title = groupItems[0]?.camera_label || sandboxSelectedCameraSlug;
  const rows = groupItems.map(renderSandboxRecordItem).join("");
  if (countEl) {
    countEl.textContent = hasFilter
      ? `机位 ${title} · 匹配 ${groupItems.length} 条`
      : `机位 ${title} · ${groupItems.length} 条`;
  }
  listEl.innerHTML = `
    <div class="record-camera-nav">
      <button type="button" class="record-back-cameras link-btn">← 返回机位列表</button>
      <span class="record-camera-nav-title">
        <span class="record-group-label">机位 ${recordItemEsc(title)}</span>
        <code>${recordItemEsc(sandboxSelectedCameraSlug)}</code>
      </span>
    </div>
    ${rows ? `<ul class="session-list">${rows}</ul>${renderSandboxLoadMoreFooter(tier)}` : "<p class='hint'>该机位下无匹配记录</p>"}`;
  bindSandboxRecordListEvents(listEl);
}

async function fetchSandboxRecordsPage({ tier, offset = 0, limit = RECORD_LIST_PAGE_SIZE, sync = false } = {}) {
  const tags = parseSandboxTagFilterQuery();
  const qs = new URLSearchParams({
    summary: "1",
    offset: String(offset),
    limit: String(limit),
    pose_tier: tier,
  });
  if (offset === 0 && sync) qs.set("sync", "1");
  if (tags.length) qs.set("tags", tags.join(","));
  const res = await fetch(`/api/records?${qs.toString()}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "加载记录失败");
  }
  const batch = await res.json();
  return Array.isArray(batch) ? batch : [];
}

async function loadSandboxRecords({ sync = false, append = false } = {}) {
  const tier = getSandboxPoseTier();
  const state = getSandboxTierLoadState(tier);
  if (append && !state.hasMore) return state.items;

  const offset = append ? state.nextOffset : 0;
  if (!append) {
    state.items = [];
    state.nextOffset = 0;
    state.hasMore = true;
    sandboxSelectedCameraSlug = null;
    sandboxCameraListPinned = false;
  }

  const batch = await fetchSandboxRecordsPage({
    tier,
    offset,
    limit: RECORD_LIST_PAGE_SIZE,
    sync: !append && sync,
  });

  if (append) state.items.push(...batch);
  else state.items = batch;
  state.nextOffset = state.items.length;
  state.hasMore = batch.length >= RECORD_LIST_PAGE_SIZE;
  sandboxRecordsByTier.set(tier, [...state.items]);
  sandboxRecordsCache = state.items;
  renderSandboxRecordsList();
  return state.items;
}

async function loadMoreSandboxRecords() {
  const tier = getSandboxPoseTier();
  const state = getSandboxTierLoadState(tier);
  if (!state.hasMore || state.loadingMore) return;
  state.loadingMore = true;
  renderSandboxRecordsList();
  try {
    await loadSandboxRecords({ append: true });
    await fetchKnownTags();
  } finally {
    state.loadingMore = false;
    renderSandboxRecordsList();
  }
}

function scheduleSandboxRecordsFilterRerender() {
  clearTimeout(sandboxFilterTimer);
  sandboxFilterTimer = setTimeout(() => renderSandboxRecordsList(), 200);
}

async function fetchSandboxSessions() {
  const res = await fetch("/api/sandbox/sessions");
  if (!res.ok) throw new Error(`加载沙盒列表失败 (${res.status})`);
  const body = await res.json();
  sandboxSessions = Array.isArray(body.sessions) ? body.sessions : [];
  return sandboxSessions;
}

function renderSandboxSessionList() {
  const listEl = $("#sandbox-session-list");
  if (!listEl) return;
  if (!sandboxSessions.length) {
    listEl.innerHTML = '<p class="hint">暂无沙盒 session，请右侧选记录后「创建沙盒」。</p>';
    return;
  }
  listEl.innerHTML = sandboxSessions
    .map((s) => {
      const active = s.session_id === sandboxActiveSessionId ? " active" : "";
      const rec = escHtml(s.source_record_id || "—");
      const sid = escHtml(s.session_id || "");
      const rc = s.recomputed
        ? `已重算 · ${s.event_count ?? 0} 事件 · ${s.frame_count ?? 0} 帧`
        : "未重算";
      return `<button type="button" class="sandbox-session-item${active}" data-session-id="${sid}">
        <span class="sandbox-session-id">${sid.slice(0, 8)}…</span>
        <span class="sandbox-session-rec">${rec}</span>
        <span class="sandbox-session-meta hint">${escHtml(rc)}</span>
      </button>`;
    })
    .join("");
  listEl.querySelectorAll(".sandbox-session-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      void selectSandboxSession(btn.dataset.sessionId);
    });
  });
}

function readSandboxParamsFromForm() {
  return {
    alarm_min_consecutive_frames: Math.max(
      1,
      parseInt($("#sandbox-alarm-min")?.value, 10) || 3
    ),
    alarm_cooldown_frames: Math.max(
      0,
      parseInt($("#sandbox-alarm-cooldown")?.value, 10) || 0
    ),
    probe_mode: $("#sandbox-probe-mode")?.value === "hand_extended" ? "hand_extended" : "wrist",
    extension_ratio: parseFloat($("#sandbox-extension-ratio")?.value || "0.3") || 0.3,
    pose_frame_interval: Math.max(1, parseInt($("#sandbox-pose-interval")?.value, 10) || 1),
  };
}

function applySandboxParamsToForm(params = {}) {
  if (!params || typeof params !== "object") return;
  const minEl = $("#sandbox-alarm-min");
  const cdEl = $("#sandbox-alarm-cooldown");
  const probeEl = $("#sandbox-probe-mode");
  const extEl = $("#sandbox-extension-ratio");
  const intEl = $("#sandbox-pose-interval");
  if (minEl && params.alarm_min_consecutive_frames != null) {
    minEl.value = String(params.alarm_min_consecutive_frames);
  }
  if (cdEl && params.alarm_cooldown_frames != null) {
    cdEl.value = String(params.alarm_cooldown_frames);
  }
  if (probeEl && params.probe_mode) probeEl.value = params.probe_mode;
  if (extEl && params.extension_ratio != null) extEl.value = String(params.extension_ratio);
  if (intEl && params.pose_frame_interval != null) {
    intEl.value = String(params.pose_frame_interval);
  }
  syncSandboxExtensionRatioDisabled();
}

function syncSandboxExtensionRatioDisabled() {
  const probe = $("#sandbox-probe-mode")?.value;
  const extEl = $("#sandbox-extension-ratio");
  if (extEl) extEl.disabled = probe !== "hand_extended";
}

async function selectSandboxSession(sessionId) {
  sandboxActiveSessionId = String(sessionId || "").trim() || null;
  renderSandboxSessionList();
  const editor = $("#sandbox-annotation-editor");
  if (!sandboxActiveSessionId) {
    if (editor) editor.value = "";
    unmountSandboxVisualAnnotate();
    $("#sandbox-open-playback")?.setAttribute("disabled", "disabled");
    return;
  }
  setSandboxStatus("加载沙盒…");
  try {
    const [metaRes, annRes] = await Promise.all([
      fetch(`/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}`),
      fetch(
        `/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}/annotation.json`
      ),
    ]);
    if (!metaRes.ok) throw new Error("session 不存在");
    const meta = await metaRes.json();
    applySandboxParamsToForm(meta.params || {});
    let ann = null;
    if (annRes.ok) {
      ann = await annRes.json();
      if (editor) editor.value = JSON.stringify(ann, null, 2);
    } else if (editor) {
      editor.value = "";
    }
    const playbackBtn = $("#sandbox-open-playback");
    if (playbackBtn) {
      if (meta.recomputed) playbackBtn.removeAttribute("disabled");
      else playbackBtn.setAttribute("disabled", "disabled");
    }
    if (meta.source_record_id && ann) {
      await loadSandboxVisualAnnotate(meta.source_record_id, ann);
    } else {
      unmountSandboxVisualAnnotate();
    }
    setSandboxStatus(
      `已选 ${sandboxActiveSessionId.slice(0, 8)}… · 源记录 ${meta.source_record_id || "—"}`
    );
  } catch (err) {
    setSandboxStatus(err.message || "加载失败", "error");
  }
}

async function createSandboxSession() {
  const recordId = sandboxSelectedRecordId || "";
  if (!recordId) {
    setSandboxStatus("请先在记录列表中选择一条记录", "error");
    return;
  }
  setSandboxStatus("创建沙盒…");
  try {
    const res = await fetch("/api/sandbox/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record_id: recordId }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `创建失败 (${res.status})`);
    }
    const body = await res.json();
    await fetchSandboxSessions();
    renderSandboxSessionList();
    await selectSandboxSession(body.session_id);
    setSandboxStatus(`已创建沙盒 ${body.session_id}（仅复制 annotation，未改正式数据）`);
  } catch (err) {
    setSandboxStatus(err.message || "创建失败", "error");
  }
}

async function saveSandboxAnnotation() {
  if (!sandboxActiveSessionId) {
    setSandboxStatus("请先选择沙盒 session", "error");
    return;
  }
  let payload;
  try {
    payload = getSandboxAnnotationPayloadForSave();
  } catch (err) {
    setSandboxStatus(err.message || "标注无效", "error");
    return;
  }
  const editor = $("#sandbox-annotation-editor");
  if (editor) editor.value = JSON.stringify(payload, null, 2);
  setSandboxStatus("保存沙盒标注…");
  try {
    const res = await fetch(
      `/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}/annotation.json`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `保存失败 (${res.status})`);
    }
    const body = await res.json();
    setSandboxStatus(`沙盒标注已保存（${body.box_count ?? 0} 个货框），请重算碰撞`);
    $("#sandbox-open-playback")?.setAttribute("disabled", "disabled");
    await fetchSandboxSessions();
    renderSandboxSessionList();
  } catch (err) {
    setSandboxStatus(err.message || "保存失败", "error");
  }
}

async function recomputeSandboxCollisions() {
  if (!sandboxActiveSessionId) {
    setSandboxStatus("请先选择沙盒 session", "error");
    return;
  }
  const params = readSandboxParamsFromForm();
  setSandboxStatus("重算碰撞中（仅写沙盒 temp）…");
  const btn = $("#sandbox-recompute");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(
      `/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}/recompute`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `重算失败 (${res.status})`);
    }
    const body = await res.json();
    await fetchSandboxSessions();
    renderSandboxSessionList();
    $("#sandbox-open-playback")?.removeAttribute("disabled");
    setSandboxStatus(
      `重算完成：${body.event_count ?? 0} 事件 · 告警帧 ${body.alarm_frame_count ?? 0} · 碰撞帧 ${body.collision_frame_count ?? 0}`
    );
  } catch (err) {
    setSandboxStatus(err.message || "重算失败", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function openSandboxPlaybackFromPanel() {
  if (!sandboxActiveSessionId) return;
  const session = sandboxSessions.find((s) => s.session_id === sandboxActiveSessionId);
  const recordId = session?.source_record_id;
  if (!recordId) {
    setSandboxStatus("缺少源 record_id", "error");
    return;
  }
  try {
    const meta = await fetch(
      `/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}`
    ).then((r) => r.json());
    if (!meta.recomputed) {
      setSandboxStatus("请先重算碰撞", "error");
      return;
    }
    tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === "playback"));
    Object.values(panels).forEach((p) => p.classList.remove("active"));
    panels.playback.classList.add("active");
    playbackSandboxSessionId = sandboxActiveSessionId;
    await openRecordReplay(recordId, "", "", true);
  } catch (err) {
    setSandboxStatus(err.message || "打开回放失败", "error");
  }
}

async function deleteActiveSandboxSession() {
  if (!sandboxActiveSessionId) return;
  if (!confirm(`删除沙盒 session ${sandboxActiveSessionId}？`)) return;
  try {
    await fetch(`/api/sandbox/sessions/${encodeURIComponent(sandboxActiveSessionId)}`, {
      method: "DELETE",
    });
    sandboxActiveSessionId = null;
    unmountSandboxVisualAnnotate();
    await fetchSandboxSessions();
    renderSandboxSessionList();
    $("#sandbox-annotation-editor").value = "";
    setSandboxStatus("已删除沙盒 session");
  } catch (err) {
    setSandboxStatus(err.message || "删除失败", "error");
  }
}

async function clearAllSandboxSessions() {
  if (!sandboxSessions.length) {
    setSandboxStatus("暂无沙盒数据");
    return;
  }
  if (!confirm(`清除全部 ${sandboxSessions.length} 个沙盒 session？此操作不可恢复。`)) return;
  try {
    const res = await fetch("/api/sandbox/sessions", { method: "DELETE" });
    if (!res.ok) throw new Error("清除失败");
    const body = await res.json();
    sandboxActiveSessionId = null;
    unmountSandboxVisualAnnotate();
    if (typeof clearPlaybackSandbox === "function") clearPlaybackSandbox();
    await fetchSandboxSessions();
    renderSandboxSessionList();
    $("#sandbox-annotation-editor").value = "";
    setSandboxStatus(`已清除 ${body.removed_count ?? 0} 个沙盒 session`);
  } catch (err) {
    setSandboxStatus(err.message || "清除失败", "error");
  }
}

function bindSandboxPanelEvents() {
  $("#sandbox-create")?.addEventListener("click", () => void createSandboxSession());
  $("#sandbox-save-annotation")?.addEventListener("click", () => void saveSandboxAnnotation());
  $("#sandbox-recompute")?.addEventListener("click", () => void recomputeSandboxCollisions());
  $("#sandbox-open-playback")?.addEventListener("click", () => void openSandboxPlaybackFromPanel());
  $("#sandbox-delete-session")?.addEventListener("click", () => void deleteActiveSandboxSession());
  $("#sandbox-clear-all")?.addEventListener("click", () => void clearAllSandboxSessions());
  $("#sandbox-pose-tier")?.addEventListener("change", () => {
    sandboxSelectedRecordId = null;
    $("#sandbox-create")?.setAttribute("disabled", "disabled");
    void loadSandboxRecords({ sync: true });
  });
  $("#sandbox-probe-mode")?.addEventListener("change", syncSandboxExtensionRatioDisabled);
  $("#sandbox-record-filter")?.addEventListener("input", scheduleSandboxRecordsFilterRerender);
  const bindSandboxFilterSelect = (sel) => {
    sel?.addEventListener("change", scheduleSandboxRecordsFilterRerender);
  };
  bindSandboxFilterSelect($("#sandbox-review-status-filter"));
  bindSandboxFilterSelect($("#sandbox-verified-filter"));
  $("#sandbox-tag-filter")?.addEventListener("input", () => {
    scheduleSandboxRecordsFilterRerender();
    clearTimeout(sandboxFilterTimer);
    sandboxFilterTimer = setTimeout(() => void loadSandboxRecords({ sync: false }), 350);
  });
  $("#sandbox-format-json")?.addEventListener("click", () => {
    const editor = $("#sandbox-annotation-editor");
    if (!editor?.value.trim()) {
      syncSandboxAnnotationEditorFromVisual();
      return;
    }
    try {
      editor.value = JSON.stringify(JSON.parse(editor.value), null, 2);
      setSandboxStatus("JSON 已格式化");
    } catch {
      setSandboxStatus("JSON 格式无效", "error");
    }
  });
  $("#sandbox-sync-json-from-visual")?.addEventListener("click", () => {
    syncSandboxAnnotationEditorFromVisual();
    setSandboxStatus("已从画布同步 JSON");
  });
}

async function initSandboxPanel() {
  if (!sandboxInitialized) {
    bindSandboxPanelEvents();
    if (typeof initSandboxVisualAnnotate === "function") initSandboxVisualAnnotate();
    sandboxInitialized = true;
  }
  syncSandboxExtensionRatioDisabled();
  try {
    await Promise.all([
      loadSandboxRecords({ sync: true }),
      fetchSandboxSessions(),
      fetchKnownTags(),
    ]);
    renderSandboxSessionList();
    if (sandboxActiveSessionId) {
      await selectSandboxSession(sandboxActiveSessionId);
      if (typeof window.fitSandboxCanvasDisplay === "function") {
        requestAnimationFrame(() => window.fitSandboxCanvasDisplay());
      }
    }
  } catch (err) {
    setSandboxStatus(err.message || "初始化失败", "error");
  }
}

window.initSandboxPanel = initSandboxPanel;
