/** 回放记录列表与打开记录 */
/** 当前查看的机位目录（null = 一级机位列表） */
let playbackSelectedCameraSlug = null;
/** 用户主动返回一级机位列表时置 true，避免播放中记录导致自动下钻 */
let playbackCameraListPinned = false;
let playbackRecordsCache = [];
/** 回放列表当前筛选的模型数据层（rtmpose-t / rtmpose-s / rtmpose-m） */
let playbackPoseTier = "rtmpose-t";
/** 回放标注来源：tier=当前模型层目录，annotation / annotation2=基准标注目录 */
let playbackAnnotationSource = "tier";
/** 已知标签（来自 /api/tags） */
let playbackKnownTags = [];
/** 当前筛选下的全量机位摘要 */
let playbackCameraSummaries = [];
let playbackCameraSummaryMeta = { totalCameras: 0, totalRecords: 0 };
/** 按“模型层 + 机位 + 筛选”缓存的记录列表 */
const playbackRecordsByTier = new Map();
/** 按“模型层 + 机位 + 筛选”维护分页状态 */
const playbackTierLoadState = new Map();
/** 按“模型层 + 筛选”缓存一级机位摘要 */
const playbackCameraSummariesCache = new Map();
/** 同查询并发 load 去重 */
const playbackRecordsLoadInflight = new Map();
/** 一级机位列表滚动位置 */
const playbackCameraScrollPositions = new Map();
let playbackRecordsRequestGeneration = 0;
let playbackRecordsAbortController = null;

const RECORD_LIST_PAGE_SIZE = 200;
const POSE_MODEL_TIERS = new Set(["rtmpose-t", "rtmpose-s", "rtmpose-m"]);

function cameraSlugFromRecordId(recordId) {
  const parts = String(recordId || "")
    .split("/")
    .filter(Boolean);
  if (parts.length >= 3 && POSE_MODEL_TIERS.has(parts[0])) return parts[1];
  if (parts.length >= 2) return parts[0];
  return null;
}

function recordGroupKey(s) {
  const slug = s.camera_slug || cameraSlugFromRecordId(s.record_id);
  if (slug === "_ungrouped") return s.camera_label || "未分组";
  return slug || s.camera_label || "未分类";
}

function cameraSlugForRecordId(recordId) {
  if (!recordId) return null;
  const item = playbackRecordsCache.find((s) => s.record_id === recordId);
  if (item) return recordGroupKey(item);
  return cameraSlugFromRecordId(recordId);
}

function focusPlaybackCameraForRecord(recordId) {
  playbackCameraListPinned = false;
  const slug = cameraSlugForRecordId(recordId);
  if (slug) playbackSelectedCameraSlug = slug;
}

function recordItemEsc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;");
}

function recordSearchBlob(s) {
  const name = s.display_name || s.record_id || "";
  const review = s.event_review_label || reviewStatusLabel(s.event_review_status);
  const tags = Array.isArray(s.tags) ? s.tags.join(" ") : "";
  return `${name} ${s.record_id || ""} ${s.video_stem || ""} ${s.camera_label || ""} ${s.camera_slug || ""} ${review} ${tags}`.toLowerCase();
}

function parseTagFilterQuery() {
  return String($("#playback-tag-filter")?.value || "")
    .split(/[,，]/)
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

function playbackReviewFilterQuery() {
  const status = String($("#playback-review-status-filter")?.value || "all").trim().toLowerCase();
  return status === "all" ? "" : status;
}

function playbackVerifiedFilterQuery() {
  const mode = String($("#playback-verified-filter")?.value || "all").trim().toLowerCase();
  if (mode === "yes") return "yes";
  if (mode === "no") return "no";
  return "";
}

function playbackRecordSearchQuery() {
  return String($("#playback-record-filter")?.value || "").trim();
}

function playbackFilterParams() {
  return {
    q: playbackRecordSearchQuery(),
    tags: parseTagFilterQuery().join(","),
    reviewStatus: playbackReviewFilterQuery(),
    hasVerified: playbackVerifiedFilterQuery(),
  };
}

function playbackFilterSignature() {
  const params = playbackFilterParams();
  return JSON.stringify(params);
}

function appendPlaybackFilterQuery(searchParams) {
  const filters = playbackFilterParams();
  if (filters.q) searchParams.set("q", filters.q);
  if (filters.tags) searchParams.set("tags", filters.tags);
  if (filters.reviewStatus) searchParams.set("review_status", filters.reviewStatus);
  if (filters.hasVerified) searchParams.set("has_verified", filters.hasVerified);
  return searchParams;
}

function cameraSummaryCacheKey(tier = playbackPoseTier) {
  return `${String(tier || "rtmpose-t").trim()}|${playbackFilterSignature()}`;
}

function cameraRecordCacheKey(
  tier = playbackPoseTier,
  cameraSlug = playbackSelectedCameraSlug
) {
  return `${String(tier || "rtmpose-t").trim()}|${String(cameraSlug || "").trim()}|${playbackFilterSignature()}`;
}

function renderRecordTags(s) {
  const esc = recordItemEsc;
  const tags = Array.isArray(s.tags) ? s.tags : [];
  return tags
    .map(
      (tag) =>
        `<span class="record-tag" data-record-id="${esc(s.record_id)}" data-tag="${esc(tag)}">
          <span class="record-tag-text">${esc(tag)}</span>
          <button type="button" class="record-tag-remove" title="移除标签" aria-label="移除标签 ${esc(tag)}">×</button>
        </span>`
    )
    .join("");
}

function patchRecordTagsInCache(recordId, tags) {
  let changed = false;
  playbackRecordsCache = playbackRecordsCache.map((item) => {
    if (item.record_id !== recordId) return item;
    changed = true;
    return { ...item, tags: [...tags] };
  });
  return changed;
}

async function fetchKnownTags() {
  try {
    const res = await fetch("/api/tags");
    if (!res.ok) return;
    const data = await res.json();
    playbackKnownTags = Array.isArray(data.tags) ? data.tags : [];
    refreshTagSuggestions();
  } catch {
    /* 标签索引不可用时忽略 */
  }
}

function refreshTagSuggestions() {
  const list = $("#playback-tag-suggestions");
  if (!list) return;
  list.innerHTML = playbackKnownTags
    .map((item) => `<option value="${recordItemEsc(item.name || "")}"></option>`)
    .join("");
}

async function patchRecordTags(recordId, { add = [], remove = [] } = {}) {
  const res = await fetch(recordApiUrl(recordId, "/tags"), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ add, remove }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "标签更新失败");
  }
  const data = await res.json();
  const tags = Array.isArray(data.tags) ? data.tags : [];
  patchRecordTagsInCache(recordId, tags);
  commitPlaybackRecordsCacheToTier();
  playbackCameraSummariesCache.clear();
  await fetchKnownTags();
  if (parseTagFilterQuery().length) {
    const key = cameraRecordCacheKey();
    playbackRecordsByTier.delete(key);
    playbackTierLoadState.delete(key);
    await loadRecords({ quiet: true });
  } else {
    renderPlaybackRecordsList(playbackRecordsCache);
  }
  return tags;
}

function getTierLoadState(
  tier = playbackPoseTier,
  cameraSlug = playbackSelectedCameraSlug
) {
  const key = cameraRecordCacheKey(tier, cameraSlug);
  if (!playbackTierLoadState.has(key)) {
    playbackTierLoadState.set(key, {
      items: [],
      nextOffset: 0,
      hasMore: true,
      loadingMore: false,
      total: 0,
    });
  }
  return playbackTierLoadState.get(key);
}

function commitPlaybackRecordsCacheToTier(
  tier = playbackPoseTier,
  cameraSlug = playbackSelectedCameraSlug
) {
  if (!cameraSlug) return;
  const key = cameraRecordCacheKey(tier, cameraSlug);
  playbackRecordsByTier.set(key, [...playbackRecordsCache]);
  const state = playbackTierLoadState.get(key);
  if (state) state.items = playbackRecordsCache;
}

function resetTierLoadState(tier) {
  const prefix = `${String(tier || "").trim()}|`;
  if (prefix === "|") return;
  for (const key of [...playbackTierLoadState.keys()]) {
    if (key.startsWith(prefix)) playbackTierLoadState.delete(key);
  }
  for (const key of [...playbackRecordsByTier.keys()]) {
    if (key.startsWith(prefix)) playbackRecordsByTier.delete(key);
  }
  for (const key of [...playbackCameraSummariesCache.keys()]) {
    if (key.startsWith(prefix)) playbackCameraSummariesCache.delete(key);
  }
}

function invalidatePlaybackTierCache(tier = "") {
  if (tier) resetTierLoadState(tier);
  else {
    playbackTierLoadState.clear();
    playbackRecordsByTier.clear();
    playbackCameraSummariesCache.clear();
  }
}

function poseTierFromRecordId(recordId) {
  const parts = String(recordId || "")
    .split("/")
    .filter(Boolean);
  if (parts.length >= 1 && POSE_MODEL_TIERS.has(parts[0])) return parts[0];
  return "";
}

/** 采集/删除等变更后：失效缓存；若正在回放页且为当前层则静默刷新 */
function notifyPlaybackRecordsChanged(tier = "") {
  if (tier) resetTierLoadState(tier);
  else invalidatePlaybackTierCache();
  if (!panels?.playback?.classList.contains("active")) return;
  const active = playbackPoseTier || "rtmpose-t";
  if (!tier || tier === active) {
    void loadRecords({ quiet: true, force: true });
  }
}

let activeRecordTagPicker = null;

function closeRecordTagPicker() {
  if (!activeRecordTagPicker) return;
  activeRecordTagPicker._anchorWrap?.classList.remove("record-tags-inline-picker-open");
  activeRecordTagPicker.remove();
  activeRecordTagPicker = null;
}

function recordTagsForId(recordId) {
  const item = playbackRecordsCache.find((s) => s.record_id === recordId);
  return Array.isArray(item?.tags) ? item.tags : [];
}

async function applyTagToRecord(anchorBtn, recordId, tagName) {
  const name = String(tagName || "").trim();
  if (!name) return;
  closeRecordTagPicker();
  anchorBtn.disabled = true;
  try {
    await patchRecordTags(recordId, { add: [name] });
  } catch (err) {
    window.alert(`添加标签失败：${err.message}`);
  } finally {
    anchorBtn.disabled = false;
  }
}

async function openRecordTagPicker(anchorBtn, recordId) {
  closeRecordTagPicker();
  if (!playbackKnownTags.length) await fetchKnownTags();

  const existing = new Set(recordTagsForId(recordId).map((t) => String(t).toLowerCase()));
  const choices = playbackKnownTags
    .map((item) => String(item.name || "").trim())
    .filter((name) => name && !existing.has(name.toLowerCase()));

  const esc = recordItemEsc;
  const picker = document.createElement("div");
  picker.className = "record-tag-picker";
  picker.dataset.recordId = recordId;
  picker.setAttribute("role", "dialog");
  picker.innerHTML = `
    <div class="record-tag-picker-head">选择已有标签</div>
    <div class="record-tag-picker-list">
      ${
        choices.length
          ? choices
              .map(
                (name) =>
                  `<button type="button" class="record-tag-choice" data-tag="${esc(name)}">${esc(name)}</button>`
              )
              .join("")
          : `<p class="hint record-tag-picker-empty">暂无可选标签，可在下方新建</p>`
      }
    </div>
    <div class="record-tag-picker-new">
      <input type="text" class="record-tag-new-input" placeholder="新建标签名" maxlength="64" autocomplete="off" />
      <button type="button" class="record-tag-new-btn">新建</button>
    </div>
  `;

  picker.addEventListener("click", (e) => e.stopPropagation());
  picker.querySelectorAll(".record-tag-choice").forEach((choiceBtn) => {
    choiceBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      void applyTagToRecord(anchorBtn, recordId, choiceBtn.dataset.tag);
    });
  });

  const newInput = picker.querySelector(".record-tag-new-input");
  const newBtn = picker.querySelector(".record-tag-new-btn");
  const submitNew = () => {
    const name = newInput?.value?.trim();
    if (!name) return;
    void applyTagToRecord(anchorBtn, recordId, name);
  };
  newBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    submitNew();
  });
  newInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      submitNew();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeRecordTagPicker();
    }
  });

  const wrap = anchorBtn.closest(".record-tags-inline");
  if (wrap) {
    wrap.classList.add("record-tags-inline-picker-open");
    wrap.appendChild(picker);
  } else {
    anchorBtn.insertAdjacentElement("afterend", picker);
  }
  activeRecordTagPicker = picker;
  picker._anchorWrap = wrap || null;

  setTimeout(() => newInput?.focus(), 0);

  const onDocClick = (e) => {
    if (picker.contains(e.target) || anchorBtn.contains(e.target)) return;
    closeRecordTagPicker();
    document.removeEventListener("click", onDocClick, true);
  };
  setTimeout(() => document.addEventListener("click", onDocClick, true), 0);
}

function reviewStatusLabel(status) {
  if (status === "completed") return "已复核";
  if (status === "no_collision") return "无碰撞";
  if (status === "in_progress") return "复核中";
  return "未复核";
}

function reviewStatusClass(status) {
  if (status === "completed" || status === "no_collision") return "review-completed";
  if (status === "in_progress") return "review-in-progress";
  return "review-not-started";
}

function isReviewTerminalStatus(status) {
  return status === "completed" || status === "no_collision";
}

function renderReviewPill(status, label = "") {
  const st = status || "not_started";
  const text = label || reviewStatusLabel(st);
  return `<span class="record-review-pill ${reviewStatusClass(st)}" title="人工事件复核状态">${text}</span>`;
}

/** 本地即时更新单条/机位分组的复核状态，避免等慢接口返回 */
function patchPlaybackRecordReviewStatus(recordId, status, label = "") {
  if (!recordId) return;
  const st = status || "not_started";
  const labelText = label || reviewStatusLabel(st);
  let changed = false;
  playbackRecordsCache = playbackRecordsCache.map((item) => {
    if (item.record_id !== recordId) return item;
    changed = true;
    return {
      ...item,
      event_review_status: st,
      event_review_label: labelText,
    };
  });
  if (changed) {
    commitPlaybackRecordsCacheToTier();
    playbackCameraSummariesCache.delete(cameraSummaryCacheKey());
    if (playbackReviewFilterQuery() || playbackVerifiedFilterQuery()) {
      const key = cameraRecordCacheKey();
      playbackRecordsByTier.delete(key);
      playbackTierLoadState.delete(key);
      void loadRecords({ quiet: true });
    } else {
      renderPlaybackRecordsList(playbackRecordsCache);
    }
  }
}

function applyEventReviewPatchFromBody(body, recordId = currentRecordId) {
  if (!recordId || !body) return;
  const st =
    body.event_review_status ||
    body.event_review?.status ||
    (body.event_review?.verified_true?.length || body.event_review?.updated_at ? "in_progress" : null);
  if (!st) return;
  patchPlaybackRecordReviewStatus(
    recordId,
    st,
    body.event_review_label || reviewStatusLabel(st)
  );
}

function aggregateReviewStatus(items) {
  const statuses = (items || []).map((s) => s.event_review_status || "not_started");
  if (!statuses.length) return "not_started";
  if (statuses.every((st) => isReviewTerminalStatus(st))) return "completed";
  if (statuses.every((st) => st === "not_started")) return "not_started";
  return "in_progress";
}

function renderRecordItem(s) {
  const name = s.display_name || s.record_id;
  const jsonFile = s.pose_label || s.pose_file || `${s.record_id}/manifest.json`;
  const esc = recordItemEsc;
  const reviewSt = s.event_review_status || "not_started";
  const reviewPill = renderReviewPill(reviewSt, s.event_review_label);
  const badges = [];
  if (s.frame_count != null) badges.push(`${s.frame_count} 帧`);
  if (s.has_video) badges.push("视频");
  const collisionComputed = s.collision_computed ?? s.collision_enabled;
  if (s.has_stored_annotation || collisionComputed) badges.push("标注");
  if (collisionComputed) badges.push("碰撞");
  else badges.push('<span class="record-badge collision-pending" title="采集时未启用碰撞检测，可在标注页补标后重算">碰撞未计算</span>');
  const badgeHtml = badges.map((b) => `<span class="record-badge">${b}</span>`).join("");
  const tagHtml = renderRecordTags(s);
  return `
      <li class="record-item record-item-compact" data-record-id="${esc(s.record_id)}" data-display-name="${esc(name)}" data-pose-file="${esc(jsonFile)}" data-has-video="${s.has_video ? "1" : "0"}" data-search="${esc(recordSearchBlob(s))}">
        <div class="record-main record-main-compact">
          ${reviewPill}
          <strong class="record-name" title="${esc(name)}">${name}</strong>
          <span class="record-meta-inline">${badgeHtml}</span>
          <span class="record-tags-inline">
            ${tagHtml}
            <button type="button" class="record-tag-add-btn" data-record-id="${esc(s.record_id)}" title="添加标签">+ 标签</button>
          </span>
        </div>
        <span class="record-actions record-actions-compact">
          <a href="${recordApiUrl(s.record_id, "/manifest.json")}" download title="${esc(jsonFile)}">JSON</a>
          <a href="${recordApiUrl(s.record_id, "/export.xlsx")}" download title="导出 Excel">XLSX</a>
          <button type="button" class="danger-btn" data-delete="${esc(s.record_id)}" data-name="${esc(name)}">删</button>
        </span>
      </li>`;
}

function renderCameraGroupItem(summary) {
  const key = summary.camera_slug || "_ungrouped";
  const total = Number(summary.record_count || 0);
  const title = summary.camera_label || key;
  const groupReviewPill = renderReviewPill(
    summary.event_review_status,
    summary.event_review_label
  );
  const esc = recordItemEsc;
  return `
    <li class="camera-group-item" data-camera-slug="${esc(key)}" role="button" tabindex="0">
      <div class="camera-group-main">
        <span class="camera-group-label">机位 ${esc(title)}</span>
        <span class="camera-group-meta">
          ${groupReviewPill}
          <code>${esc(key)}</code> · ${total} 条
        </span>
      </div>
      <span class="camera-group-chevron" aria-hidden="true">›</span>
    </li>`;
}

function renderRecordsLoadMoreFooter() {
  if (!playbackSelectedCameraSlug) return "";
  const state = getTierLoadState(playbackPoseTier, playbackSelectedCameraSlug);
  const loaded = state.items.length;
  const total = Number(state.total || loaded);
  const progress = `<span class="hint playback-records-page-progress">已加载 ${loaded} / ${total} 条</span>`;
  if (!state.hasMore) return `<p class="playback-records-load-more">${progress}</p>`;
  if (state.loadingMore) {
    return `<p class="playback-records-load-more">${progress}<br><span class="hint">加载更多…</span></p>`;
  }
  // 哨兵滚进视野就自动续拉；按钮保留给不支持 IntersectionObserver 与键盘操作的场景。
  return `<p class="playback-records-load-more"><span class="playback-records-sentinel" aria-hidden="true"></span>${progress}<br><button type="button" class="link-btn playback-load-more-btn">加载更多记录…</button></p>`;
}

/**
 * 记录列表滚到底部自动加载下一页，避免上百条记录要反复点「加载更多」。
 * 列表每次重渲染都会换掉哨兵节点，因此这里先断开旧 observer 再重新挂。
 */
let recordsAutoLoadObserver = null;

function observeRecordsAutoLoad(list) {
  recordsAutoLoadObserver?.disconnect();
  recordsAutoLoadObserver = null;
  if (typeof IntersectionObserver !== "function") return;
  const sentinel = list?.querySelector(".playback-records-sentinel");
  if (!sentinel) return;
  recordsAutoLoadObserver = new IntersectionObserver(
    (entries) => {
      // loadMoreRecords 内部已按 hasMore / loadingMore 去重，这里不必再加锁。
      if (entries.some((entry) => entry.isIntersecting)) void loadMoreRecords();
    },
    { root: list, rootMargin: "240px 0px" }
  );
  recordsAutoLoadObserver.observe(sentinel);
}

function bindRecordListEvents(list) {
  list.querySelector(".playback-load-more-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    void loadMoreRecords();
  });
  observeRecordsAutoLoad(list);
  list.querySelectorAll(".record-back-cameras").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      playbackSelectedCameraSlug = null;
      playbackCameraListPinned = true;
      playbackRecordsCache = [];
      if (playbackCameraSummariesCache.has(cameraSummaryCacheKey())) {
        const cached = playbackCameraSummariesCache.get(cameraSummaryCacheKey());
        playbackCameraSummaries = cached.items;
        playbackCameraSummaryMeta = cached.meta;
        renderPlaybackRecordsList([]);
      } else {
        await loadRecords({ quiet: false });
      }
    });
  });
  list.querySelectorAll(".camera-group-item").forEach((li) => {
    const open = async () => {
      const slug = li.dataset.cameraSlug;
      if (!slug) return;
      playbackCameraScrollPositions.set(cameraSummaryCacheKey(), list.scrollTop);
      playbackSelectedCameraSlug = slug;
      playbackCameraListPinned = false;
      playbackRecordsCache =
        playbackRecordsByTier.get(cameraRecordCacheKey(playbackPoseTier, slug)) || [];
      await loadRecords({ quiet: Boolean(playbackRecordsCache.length) });
    };
    li.addEventListener("click", () => void open());
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        void open();
      }
    });
  });
  list.querySelectorAll(".record-item").forEach((li) => {
    li.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      if (
        typeof isPlaybackRecordOpening === "function" &&
        isPlaybackRecordOpening()
      ) {
        return;
      }
      selectPlaybackRecordItem(li);
      startPlaybackFromSelectedRecord().catch((err) =>
        setPlaybackInfo(`❌ ${err.message}`)
      );
    });
  });
  const keepId = selectedPlaybackRecord?.recordId || currentRecordId || "";
  if (keepId) highlightPlaybackRecordInList(keepId);
  else updatePlaybackLoadButton();
  list.querySelectorAll(".record-tag-remove").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const pill = btn.closest(".record-tag");
      const rid = pill?.dataset.recordId;
      const tag = pill?.dataset.tag;
      if (!rid || !tag) return;
      btn.disabled = true;
      try {
        await patchRecordTags(rid, { remove: [tag] });
      } catch (err) {
        window.alert(`移除标签失败：${err.message}`);
        btn.disabled = false;
      }
    });
  });
  list.querySelectorAll(".record-tag-add-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const rid = btn.dataset.recordId;
      if (!rid) return;
      if (activeRecordTagPicker?.dataset.recordId === rid) {
        closeRecordTagPicker();
        return;
      }
      await openRecordTagPicker(btn, rid);
    });
  });
  list.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const rid = btn.dataset.delete;
      const name = btn.dataset.name || rid;
      if (
        !window.confirm(
          `确定删除记录「${name}」？\n\n将删除骨架数据、meta 与配套视频。\nannotations/ 目录下的标注文件不会删除。`
        )
      ) {
        return;
      }
      btn.disabled = true;
      try {
        const res = await fetch(recordApiUrl(rid), { method: "DELETE" });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || res.statusText || "删除失败");
        }
        if (currentRecordId === rid) {
          await prepareEventReviewRecordSwitch({ force: true });
          finishPlaybackSession();
          currentRecordId = null;
        }
        if (selectedPlaybackRecord?.recordId === rid) {
          selectedPlaybackRecord = null;
          updatePlaybackLoadButton();
        }
        invalidatePlaybackTierCache();
        await loadRecords({ force: true });
      } catch (err) {
        window.alert(`删除失败：${err.message}`);
        btn.disabled = false;
      }
    });
  });
}

function renderPlaybackRecordsList(items) {
  closeRecordTagPicker();
  const list = $("#session-list");
  const countEl = $("#playback-record-count");
  const hasFilter = Boolean(
    playbackRecordSearchQuery() ||
      parseTagFilterQuery().length ||
      playbackReviewFilterQuery() ||
      playbackVerifiedFilterQuery()
  );
  const keepId = selectedPlaybackRecord?.recordId || currentRecordId || "";

  if (!playbackSelectedCameraSlug) {
    const summaries = [...playbackCameraSummaries].sort((a, b) =>
      String(a.camera_slug || "").localeCompare(String(b.camera_slug || ""), undefined, {
        numeric: true,
      })
    );
    if (countEl) {
      const tierLabel = playbackPoseTier || "rtmpose-t";
      countEl.textContent = hasFilter
        ? `${tierLabel} · 匹配 ${playbackCameraSummaryMeta.totalCameras} 个机位 · ${playbackCameraSummaryMeta.totalRecords} 条`
        : `${tierLabel} · ${playbackCameraSummaryMeta.totalCameras} 个机位 · 共 ${playbackCameraSummaryMeta.totalRecords} 条`;
    }
    if (!summaries.length) {
      list.innerHTML = `<p class='hint playback-records-empty'>${
        hasFilter ? "无匹配记录" : "暂无记录（请先在采集页完成采集）"
      }</p>`;
      bindRecordListEvents(list);
      return;
    }
    list.innerHTML = `<ul class="camera-group-list">${summaries
      .map(renderCameraGroupItem)
      .join("")}</ul>`;
    bindRecordListEvents(list);
    const savedScroll = playbackCameraScrollPositions.get(cameraSummaryCacheKey()) || 0;
    requestAnimationFrame(() => {
      if (!playbackSelectedCameraSlug) list.scrollTop = savedScroll;
    });
    return;
  }

  const state = getTierLoadState(playbackPoseTier, playbackSelectedCameraSlug);
  const summary = playbackCameraSummaries.find(
    (item) => item.camera_slug === playbackSelectedCameraSlug
  );
  const title = summary?.camera_label || items[0]?.camera_label || playbackSelectedCameraSlug;
  const groupReviewPill = renderReviewPill(
    summary?.event_review_status || aggregateReviewStatus(items),
    summary?.event_review_label
  );
  const rows = items.map(renderRecordItem).join("");
  if (countEl) {
    countEl.textContent = `机位 ${title} · ${hasFilter ? "匹配 " : ""}${state.total} 条`;
  }
  list.innerHTML = `
    <div class="record-camera-nav">
      <button type="button" class="record-back-cameras link-btn">← 返回机位列表</button>
      <span class="record-camera-nav-title">
        <span class="record-group-label">机位 ${recordItemEsc(title)}</span>
        <span class="record-group-meta">
          ${groupReviewPill}
          <code>${recordItemEsc(playbackSelectedCameraSlug)}</code>
        </span>
      </span>
    </div>
    ${
      rows
        ? `<ul class="session-list">${rows}</ul>${renderRecordsLoadMoreFooter()}`
        : `<p class='hint playback-records-empty'>该机位下${
            hasFilter ? "无匹配记录" : "暂无记录"
          }</p>`
    }`;
  bindRecordListEvents(list);
  if (keepId) highlightPlaybackRecordInList(keepId);
}

function startPlaybackRecordsRequest() {
  playbackRecordsRequestGeneration += 1;
  playbackRecordsAbortController?.abort();
  playbackRecordsAbortController = new AbortController();
  return {
    generation: playbackRecordsRequestGeneration,
    signal: playbackRecordsAbortController.signal,
  };
}

function isPlaybackRecordsRequestCurrent(generation, tier, cameraSlug, filterSignature) {
  return (
    generation === playbackRecordsRequestGeneration &&
    tier === (playbackPoseTier || "rtmpose-t") &&
    cameraSlug === (playbackSelectedCameraSlug || "") &&
    filterSignature === playbackFilterSignature()
  );
}

async function fetchRecordCameraSummaries({
  poseTier = playbackPoseTier,
  sync = false,
  signal,
} = {}) {
  const params = appendPlaybackFilterQuery(
    new URLSearchParams({ pose_tier: String(poseTier || "rtmpose-t").trim() })
  );
  if (sync) params.set("sync", "1");
  const res = await fetch(`/api/record-cameras?${params}`, { signal });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "加载机位列表失败");
  }
  const body = await res.json();
  return {
    items: Array.isArray(body.items) ? body.items : [],
    totalCameras: Number(body.total_cameras || 0),
    totalRecords: Number(body.total_records || 0),
  };
}

/** 拉取当前机位的一页记录；仅 offset=0 可带 sync=1 */
async function fetchRecordSummariesPage({
  poseTier = playbackPoseTier,
  cameraSlug = playbackSelectedCameraSlug,
  offset = 0,
  limit = RECORD_LIST_PAGE_SIZE,
  sync = false,
  signal,
} = {}) {
  const tier = String(poseTier || "rtmpose-t").trim();
  const params = appendPlaybackFilterQuery(
    new URLSearchParams({
      summary: "1",
      page_meta: "1",
      offset: String(offset),
      limit: String(limit),
      pose_tier: tier,
      camera_slug: String(cameraSlug || "").trim(),
    })
  );
  if (offset === 0 && sync) params.set("sync", "1");
  const res = await fetch(`/api/records?${params}`, { signal });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "加载记录失败");
  }
  const body = await res.json();
  return {
    items: Array.isArray(body.items) ? body.items : [],
    total: Number(body.total || 0),
    hasMore: Boolean(body.has_more),
    nextOffset: Number(body.next_offset || 0),
  };
}

async function loadRecordsPage(
  tier,
  cameraSlug,
  { sync = false, append = false, generation, signal } = {}
) {
  const key = String(tier || "rtmpose-t").trim();
  const camera = String(cameraSlug || "").trim();
  const state = getTierLoadState(key, camera);
  if (append && !state.hasMore) return state.items;

  const offset = append ? state.nextOffset : 0;
  if (!append) {
    state.items = [];
    state.nextOffset = 0;
    state.hasMore = true;
    state.total = 0;
  }

  const page = await fetchRecordSummariesPage({
    poseTier: key,
    cameraSlug: camera,
    offset,
    limit: RECORD_LIST_PAGE_SIZE,
    sync: !append && sync,
    signal,
  });
  if (
    generation != null &&
    !isPlaybackRecordsRequestCurrent(generation, key, camera, playbackFilterSignature())
  ) {
    return state.items;
  }

  if (append) {
    const seen = new Set(state.items.map((item) => item.record_id));
    state.items.push(...page.items.filter((item) => !seen.has(item.record_id)));
  } else {
    state.items = page.items;
  }
  state.nextOffset = page.nextOffset;
  state.hasMore = page.hasMore;
  state.total = page.total;
  playbackRecordsByTier.set(cameraRecordCacheKey(key, camera), [...state.items]);
  return state.items;
}

async function loadMoreRecords() {
  const tier = playbackPoseTier || "rtmpose-t";
  const camera = playbackSelectedCameraSlug || "";
  if (!camera) return;
  const filterSignature = playbackFilterSignature();
  const state = getTierLoadState(tier, camera);
  if (!state.hasMore || state.loadingMore) return;
  const { generation, signal } = startPlaybackRecordsRequest();
  state.loadingMore = true;
  renderPlaybackRecordsList(playbackRecordsCache);
  try {
    const items = await loadRecordsPage(tier, camera, {
      append: true,
      generation,
      signal,
    });
    if (!isPlaybackRecordsRequestCurrent(generation, tier, camera, filterSignature)) return;
    playbackRecordsCache = items;
    renderPlaybackRecordsList(items);
  } catch (err) {
    if (err?.name === "AbortError") return;
    const msg = err?.message ? `加载更多失败：${err.message}` : "加载更多失败";
    setPlaybackInfo(`❌ ${msg}`);
  } finally {
    state.loadingMore = false;
    if (isPlaybackRecordsRequestCurrent(generation, tier, camera, filterSignature)) {
      renderPlaybackRecordsList(playbackRecordsCache);
    }
  }
}

async function loadRecords({ quiet = false, force = false } = {}) {
  const list = $("#session-list");
  const tier = playbackPoseTier || "rtmpose-t";
  const camera = playbackSelectedCameraSlug || "";
  const filterSignature = playbackFilterSignature();
  const requestKey = camera
    ? cameraRecordCacheKey(tier, camera)
    : cameraSummaryCacheKey(tier);

  if (force) resetTierLoadState(tier);
  if (!force && !camera && playbackCameraSummariesCache.has(requestKey)) {
    const cached = playbackCameraSummariesCache.get(requestKey);
    playbackCameraSummaries = cached.items;
    playbackCameraSummaryMeta = cached.meta;
    playbackRecordsCache = [];
    renderPlaybackRecordsList([]);
    return;
  }
  if (!force && camera && playbackRecordsByTier.has(requestKey)) {
    playbackRecordsCache = playbackRecordsByTier.get(requestKey) || [];
    renderPlaybackRecordsList(playbackRecordsCache);
    return;
  }

  if (!force && playbackRecordsLoadInflight.has(requestKey)) {
    try {
      await playbackRecordsLoadInflight.get(requestKey);
    } catch {
      /* 由首次请求展示错误 */
    }
    return;
  }

  const { generation, signal } = startPlaybackRecordsRequest();
  const run = (async () => {
    if (!quiet && list) {
      list.innerHTML = `<p class='hint playback-records-empty'>${
        camera ? "加载记录中…" : "加载机位列表中…"
      }</p>`;
    }
    if (!camera) {
      const result = await fetchRecordCameraSummaries({
        poseTier: tier,
        sync: Boolean(force),
        signal,
      });
      if (!isPlaybackRecordsRequestCurrent(generation, tier, "", filterSignature)) return [];
      playbackCameraSummaries = result.items;
      playbackCameraSummaryMeta = {
        totalCameras: result.totalCameras,
        totalRecords: result.totalRecords,
      };
      playbackCameraSummariesCache.set(requestKey, {
        items: [...result.items],
        meta: { ...playbackCameraSummaryMeta },
      });
      playbackRecordsCache = [];
      renderPlaybackRecordsList([]);
      await fetchKnownTags();
      return [];
    }

    if (!playbackCameraSummariesCache.has(cameraSummaryCacheKey(tier))) {
      const cameras = await fetchRecordCameraSummaries({
        poseTier: tier,
        sync: Boolean(force),
        signal,
      });
      if (!isPlaybackRecordsRequestCurrent(generation, tier, camera, filterSignature)) return [];
      playbackCameraSummaries = cameras.items;
      playbackCameraSummaryMeta = {
        totalCameras: cameras.totalCameras,
        totalRecords: cameras.totalRecords,
      };
      playbackCameraSummariesCache.set(cameraSummaryCacheKey(tier), {
        items: [...cameras.items],
        meta: { ...playbackCameraSummaryMeta },
      });
    }
    const items = await loadRecordsPage(tier, camera, {
      sync: Boolean(force),
      append: false,
      generation,
      signal,
    });
    if (!isPlaybackRecordsRequestCurrent(generation, tier, camera, filterSignature)) return items;
    playbackRecordsCache = items;
    await fetchKnownTags();
    renderPlaybackRecordsList(items);
    return items;
  })();

  playbackRecordsLoadInflight.set(requestKey, run);
  try {
    await run;
  } catch (err) {
    if (err?.name === "AbortError") return;
    const msg = err?.message ? `无法加载列表：${err.message}` : "无法加载列表";
    if (isPlaybackRecordsRequestCurrent(generation, tier, camera, filterSignature) && list) {
      list.innerHTML = `<p class='hint playback-records-empty'>${msg}</p>`;
    }
    throw err;
  } finally {
    playbackRecordsLoadInflight.delete(requestKey);
  }
}

/** 在目标记录所属机位内分页，直至记录出现 */
async function ensurePlaybackRecordInList(recordId, tier = playbackPoseTier) {
  const rid = String(recordId || "").trim();
  const key = String(tier || playbackPoseTier || "rtmpose-t").trim();
  if (!rid) return false;
  const camera = cameraSlugFromRecordId(rid) || playbackSelectedCameraSlug || "";
  if (!camera) return false;
  playbackSelectedCameraSlug = camera;
  const cacheKey = cameraRecordCacheKey(key, camera);
  const hasRecord = () =>
    (playbackRecordsByTier.get(cacheKey) || []).some((item) => item.record_id === rid);
  if (hasRecord()) return true;
  playbackPoseTier = key;
  await loadRecords({ quiet: true });
  while (getTierLoadState(key, camera).hasMore) {
    await loadMoreRecords();
    if (hasRecord()) return true;
  }
  return hasRecord();
}

/**
 * 准确率诊断跳转：加载回放后 seek 到指定帧并展示评估 overlay。
 */
let pendingPlaybackAccuracyNav = null;

function setPendingPlaybackAccuracyNav(nav) {
  pendingPlaybackAccuracyNav = nav && typeof nav === "object" ? { ...nav } : null;
}

async function applyPendingPlaybackAccuracyNav() {
  const pending = pendingPlaybackAccuracyNav;
  pendingPlaybackAccuracyNav = null;
  if (!pending) return;

  if (pending.accuracyOverlay && typeof setExternalPlaybackAccuracyOverlay === "function") {
    setExternalPlaybackAccuracyOverlay(pending.accuracyOverlay);
  } else if (typeof clearExternalPlaybackAccuracyOverlay === "function") {
    clearExternalPlaybackAccuracyOverlay();
  }

  const seekFrame = parseInt(pending.seekFrameIdx, 10) || 0;
  if (seekFrame > 0 && typeof linkPlaybackToFrame === "function") {
    await linkPlaybackToFrame(seekFrame, { pinEvent: true });
  }

  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
  if (typeof refreshEventCountLabel === "function") refreshEventCountLabel();
}

window.setPendingPlaybackAccuracyNav = setPendingPlaybackAccuracyNav;
window.applyPendingPlaybackAccuracyNav = applyPendingPlaybackAccuracyNav;

/**
 * 从准确率等模块跳转到回放：切换模型层、下钻机位并高亮记录。
 * autoPlay=true 时自动加载并回放；可指定 seekFrameIdx 与 accuracyOverlay。
 */
async function navigateToPlaybackRecord({
  recordId = "",
  poseTier = "",
  cameraSlug = "",
  autoPlay = false,
  seekFrameIdx = null,
  accuracyOverlay = null,
} = {}) {
  const rid = String(recordId || "").trim();
  if (!rid) return false;

  const tier = String(poseTier || poseTierFromRecordId(rid) || "rtmpose-t").trim();
  const slug = String(cameraSlug || cameraSlugFromRecordId(rid) || "").trim();

  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === "playback"));
  Object.values(panels).forEach((p) => p.classList.remove("active"));
  panels.playback.classList.add("active");

  playbackPoseTier = tier;
  const tierSel = $("#playback-pose-tier");
  if (tierSel) tierSel.value = tier;
  const annSrcSel = $("#playback-annotation-source");
  if (annSrcSel) {
    const opt = annSrcSel.querySelector('option[value="tier"]');
    if (opt) opt.textContent = `${tier} 模型标注`;
  }

  playbackSelectedCameraSlug = slug || null;
  playbackCameraListPinned = false;

  await loadRecords({
    quiet: playbackRecordsByTier.has(cameraRecordCacheKey(tier, slug)),
  });
  const found = await ensurePlaybackRecordInList(rid, tier);

  playbackPoseTier = tier;
  if (tierSel) tierSel.value = tier;
  playbackRecordsCache =
    playbackRecordsByTier.get(cameraRecordCacheKey(tier, playbackSelectedCameraSlug)) || [];

  if (slug) playbackSelectedCameraSlug = slug;
  else focusPlaybackCameraForRecord(rid);

  renderPlaybackRecordsList(playbackRecordsCache);
  highlightPlaybackRecordInList(rid);

  const li = document.querySelector(
    `#session-list .record-item[data-record-id="${CSS.escape(rid)}"]`
  );
  li?.scrollIntoView({ block: "nearest", behavior: "smooth" });

  if (typeof restorePlaybackPanelUi === "function") restorePlaybackPanelUi();

  if (seekFrameIdx != null || accuracyOverlay) {
    setPendingPlaybackAccuracyNav({ seekFrameIdx, accuracyOverlay });
  }

  if (autoPlay && found && typeof startPlaybackFromSelectedRecord === "function") {
    await startPlaybackFromSelectedRecord();
    await applyPendingPlaybackAccuracyNav();
  }

  return found;
}

window.navigateToPlaybackRecord = navigateToPlaybackRecord;

function initPlaybackRecordFilter() {
  const input = $("#playback-record-filter");
  const tagInput = $("#playback-tag-filter");
  const tierSel = $("#playback-pose-tier");
  const annSrcSel = $("#playback-annotation-source");

  function syncPlaybackAnnotationSourceOptionLabel() {
    const opt = annSrcSel?.querySelector('option[value="tier"]');
    if (opt) {
      const tier = playbackPoseTier || "rtmpose-t";
      opt.textContent = `${tier} 模型标注`;
    }
  }

  if (annSrcSel && !annSrcSel.dataset.bound) {
    annSrcSel.dataset.bound = "1";
    playbackAnnotationSource = annSrcSel.value || "tier";
    syncPlaybackAnnotationSourceOptionLabel();
    annSrcSel.addEventListener("change", () => {
      playbackAnnotationSource = annSrcSel.value || "tier";
      void onPlaybackAnnotationSourceChanged();
    });
  }

  if (tierSel && !tierSel.dataset.bound) {
    tierSel.dataset.bound = "1";
    playbackPoseTier = tierSel.value || "rtmpose-t";
    syncPlaybackAnnotationSourceOptionLabel();
    tierSel.addEventListener("change", async () => {
      playbackPoseTier = tierSel.value || "rtmpose-t";
      playbackSelectedCameraSlug = null;
      playbackCameraListPinned = false;
      syncPlaybackAnnotationSourceOptionLabel();
      await loadRecords({
        quiet: playbackCameraSummariesCache.has(cameraSummaryCacheKey(playbackPoseTier)),
      });
      if (currentRecordId && playbackAnnotationSource === "tier") {
        const annResult = await applyPlaybackRecordAnnotation(currentRecordId);
        redrawCurrentFrame();
        if (annResult.ok) {
          setPlaybackInfo(`已随模型层切换标注：${annResult.label}（${annotationBoxes.length} 个货框）`);
        }
      }
    });
  }
  const reloadForFilterChange = () => {
    selectedPlaybackRecord = null;
    updatePlaybackLoadButton();
    void loadRecords({ quiet: false });
  };
  if (input && !input.dataset.bound) {
    input.dataset.bound = "1";
    let t = null;
    input.addEventListener("input", () => {
      if (t) clearTimeout(t);
      t = setTimeout(reloadForFilterChange, 250);
    });
  }
  if (tagInput && !tagInput.dataset.bound) {
    tagInput.dataset.bound = "1";
    let t = null;
    tagInput.addEventListener("input", () => {
      if (t) clearTimeout(t);
      t = setTimeout(reloadForFilterChange, 250);
    });
  }
  const reviewSel = $("#playback-review-status-filter");
  const verifiedSel = $("#playback-verified-filter");
  const bindFilterSelect = (sel) => {
    if (!sel || sel.dataset.bound) return;
    sel.dataset.bound = "1";
    sel.addEventListener("change", reloadForFilterChange);
  };
  bindFilterSelect(reviewSel);
  bindFilterSelect(verifiedSel);
}

function playbackAnnotationSourceApiParam() {
  if (playbackAnnotationSource === "annotation") return "annotation";
  if (playbackAnnotationSource === "annotation2") return "annotation2";
  if (playbackAnnotationSource === "master") return "annotation";
  return playbackPoseTier || "rtmpose-t";
}

function playbackAnnotationSourceLabel() {
  if (playbackAnnotationSource === "annotation" || playbackAnnotationSource === "master") {
    return "json/annotations";
  }
  if (playbackAnnotationSource === "annotation2") {
    return "json/annotations2";
  }
  const tier = playbackPoseTier || "rtmpose-t";
  return `${tier} json/${tier}/annotations`;
}

function isPlaybackBaseAnnotationSource(src) {
  return src === "annotation" || src === "annotation2" || src === "master";
}

/** 按所选来源加载记录标注；失败时回退 pose 内嵌 annotation */
async function applyPlaybackRecordAnnotation(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return { ok: false, label: "", fromPose: false };

  const src = playbackAnnotationSourceApiParam();
  const url = `${recordApiUrl(rid, "/annotation.json")}?annotation_source=${encodeURIComponent(src)}`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      syncAnnotationBoxesFromPose();
      let detail = "";
      try {
        const errBody = await res.json();
        detail = errBody.detail || "";
      } catch {
        detail = res.statusText || "";
      }
      return {
        ok: false,
        label: playbackAnnotationSourceLabel(),
        fromPose: annotationBoxes.length > 0,
        error: detail ? String(detail) : `HTTP ${res.status}`,
      };
    }
    const data = await res.json();
    const meta = data._meta && typeof data._meta === "object" ? data._meta : {};
    loadAnnotationBoxesFromData(data);
    let label = playbackAnnotationSourceLabel();
    const hasTierFile = meta.has_tier_file === true;
    if (
      (meta.resolved_from === "annotation" || meta.resolved_from === "master") &&
      src !== "annotation" &&
      src !== "annotation2" &&
      !hasTierFile
    ) {
      label += "（模型目录无文件，已用 annotation 内容）";
    } else if (hasTierFile && !isPlaybackBaseAnnotationSource(src)) {
      label += "（模型层）";
    }
    return { ok: true, label, meta };
  } catch {
    syncAnnotationBoxesFromPose();
    return {
      ok: false,
      label: playbackAnnotationSourceLabel(),
      fromPose: annotationBoxes.length > 0,
    };
  }
}

async function onPlaybackAnnotationSourceChanged() {
  if (!currentRecordId) return;
  const result = await applyPlaybackRecordAnnotation(currentRecordId);
  if (typeof loadPlaybackEvents === "function") {
    await loadPlaybackEvents(currentRecordId);
    if (playbackEvents.length && typeof beginEventReview === "function") {
      await beginEventReview();
    }
  }
  redrawCurrentFrame();
  if (result.ok) {
    setPlaybackInfo(`已切换标注：${result.label}（${annotationBoxes.length} 个货框）`);
  } else if (result.fromPose) {
    const errNote = result.error ? `（${result.error}）` : "";
    setPlaybackInfo(`未找到所选标注${errNote}，使用 pose 内嵌货框（${annotationBoxes.length} 个）`);
  } else {
    const errNote = result.error ? `：${result.error}` : "";
    setPlaybackInfo(`未找到所选标注${errNote}（${result.label}）`);
  }
}

async function loadSavedRecordVideo(recordId, opts = {}) {
  const useOriginal = opts.original === true;
  playbackVideoUsesDerivedPreview = opts.derivedPreview === true && !useOriginal;
  const base = recordApiUrl(recordId, "/video");
  const url = useOriginal ? `${base}?original=1` : base;

  if (playbackVideoObjectUrl) {
    URL.revokeObjectURL(playbackVideoObjectUrl);
    playbackVideoObjectUrl = null;
  }
  videoEl.src = url;
  videoEl.style.display = "block";
  videoEl.load();

  return new Promise((resolve) => {
    const finish = (ok) => {
      videoEl.removeEventListener("loadedmetadata", onReady);
      videoEl.removeEventListener("error", onErr);
      resolve(ok);
    };
    const onReady = () => {
      const ok = videoEl.videoWidth > 0 && videoEl.videoHeight > 0 && !videoEl.error;
      finish(ok);
    };
    const onErr = () => finish(false);
    if (videoEl.readyState >= 1 && videoEl.videoWidth > 0 && videoEl.videoHeight > 0 && !videoEl.error) {
      resolve(true);
      return;
    }
    videoEl.addEventListener("loadedmetadata", onReady);
    videoEl.addEventListener("error", onErr);
  });
}

/** 查询/启动派生预览；缓存未就绪时立即回退原片，不阻塞首次打开。 */
async function prepareAndLoadRecordVideo(recordId, displayName = "") {
  const label = displayName || recordId;
  const statusUrl = recordApiUrl(recordId, "/video/preview/status");
  const startedAt = Date.now();
  let usedOriginal = false;

  const formatWaitSec = () => Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  let usedDerivedPreview = false;
  const result = (loaded) => ({
    loaded: !!loaded,
    usedOriginal,
    usedDerivedPreview: !!usedDerivedPreview && !usedOriginal,
  });

  showStageLoading(`【${label}】正在检查视频…`);
  setPlaybackInfo(`【${label}】正在检查视频…`);

  let body = await fetch(statusUrl).then((r) => (r.ok ? r.json() : null));
  if (!body) {
    hideStageLoading();
    setPlaybackInfo(`【${label}】视频状态查询失败`);
    return result(false);
  }

  if (body.status === "transcoding") {
    usedOriginal = true;
    const pct = Number(body.progress) || 0;
    const srcH = Number(body.source_height) || 0;
    const prevH = Number(body.preview_height) || 480;
    const msg =
      srcH > prevH
        ? `【${label}】后台生成 ${prevH}p 预览（原片 ${srcH}p）${pct}%… 本次直接加载原片`
        : `【${label}】后台准备预览 ${pct}%… 本次直接加载原片`;
    updateStageLoading(msg);
    setPlaybackInfo(msg);
    const loadedOriginal = await loadSavedRecordVideo(recordId, { original: true });
    hideStageLoading();
    return result(loadedOriginal);
  }

  if (body.status === "missing") {
    hideStageLoading();
    return result(false);
  }

  if (body.status === "error") {
    if (body.frame_contract && body.frame_contract.ok === false) {
      const c = body.frame_contract;
      const detail = `视频 ${c.video_frames || 0} 帧 / 数据 ${c.expected_frames || 0} 帧 / 碰撞时间轴 ${c.timeline_frames || 0} 帧`;
      hideStageLoading();
      clearVideoElement();
      setPlaybackInfo(`【${label}】帧数校验失败（${detail}），已阻止回放。`);
      return result(false);
    }
    const errMsg = body.error || body.message || "预览转码失败";
    updateStageLoading(`【${label}】${errMsg}，正在加载原视频…`);
    setPlaybackInfo(`【${label}】${errMsg}，正在加载原视频…`);
    usedOriginal = true;
    const loadedOriginal = await loadSavedRecordVideo(recordId, { original: true });
    hideStageLoading();
    return result(loadedOriginal);
  }

  const waitSec = formatWaitSec();
  const readyMsg =
    body.needs_transcode && Number(body.source_height) > Number(body.preview_height)
      ? `【${label}】预览视频已就绪（${body.preview_height}p），正在加载…`
      : `【${label}】正在加载视频…`;
  updateStageLoading(waitSec > 2 ? `${readyMsg}（总耗时 ${waitSec}s）` : readyMsg);
  setPlaybackInfo(readyMsg);

  // 只有帧数达到 preview_min_frames（默认 10000）才会派生 480p 预览；短片始终原片。
  const derivedPreview = body.cache_path_type === "local_preview" && !body.use_original;
  usedDerivedPreview = derivedPreview;
  let loaded = await loadSavedRecordVideo(recordId, { derivedPreview });
  if (!loaded) {
    updateStageLoading(`【${label}】预览视频无法播放，正在加载原视频…`);
    setPlaybackInfo(`【${label}】预览视频无法播放，正在加载原视频…`);
    usedOriginal = true;
    usedDerivedPreview = false;
    loaded = await loadSavedRecordVideo(recordId, { original: true });
  }
  hideStageLoading();
  return result(loaded);
}

async function startVideoPlayback(hintPrefix = "") {
  try {
    readPlaybackSpeedFromSelect();
    playbackEventLinkExact = false;
    lastEventSyncFrameIdx = -1;
    if (typeof clearPlaybackAuthorityFrameIdx === "function") clearPlaybackAuthorityFrameIdx();
    await videoEl.play();
    if (typeof ensurePlaybackRenderLoop === "function") {
      ensurePlaybackRenderLoop();
    }
    if (hintPrefix) setPlaybackInfo(`${hintPrefix}正在播放…`);
    return true;
  } catch (err) {
    setPlaybackInfo(`${hintPrefix}视频已加载，请点击播放或视频控件（${err.message}）`);
    redrawCurrentFrame();
    return false;
  }
}

async function openRecordReplay(recordId, displayName = "", jsonFileName = "", expectVideo = false) {
  if (!(await prepareEventReviewRecordSwitch())) return;
  if (!pendingPlaybackAccuracyNav && typeof clearExternalPlaybackAccuracyOverlay === "function") {
    clearExternalPlaybackAccuracyOverlay();
  }
  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === "playback"));
  Object.values(panels).forEach((p) => p.classList.remove("active"));
  panels.playback.classList.add("active");
  await cleanupPlaybackVideo();
  clearVideoElement();
  currentRecordId = recordId;
  const recordTier = poseTierFromRecordId(recordId);
  if (recordTier) {
    playbackPoseTier = recordTier;
    const tierSel = $("#playback-pose-tier");
    if (tierSel) tierSel.value = recordTier;
    const annSrcSel = $("#playback-annotation-source");
    const tierOpt = annSrcSel?.querySelector('option[value="tier"]');
    if (tierOpt) tierOpt.textContent = `${recordTier} 模型标注`;
  }
  focusPlaybackCameraForRecord(recordId);
  const recordListKey = cameraRecordCacheKey(
    playbackPoseTier,
    playbackSelectedCameraSlug
  );
  if (!playbackRecordsByTier.has(recordListKey)) {
    await loadRecords({ quiet: true });
  } else {
    playbackRecordsCache = playbackRecordsByTier.get(recordListKey) || [];
  }
  renderPlaybackRecordsList(playbackRecordsCache);
  highlightPlaybackRecordInList(recordId);
  resetFrameFetchState();
  if (typeof setPlaybackEventsLoadingState === "function") {
    setPlaybackEventsLoadingState(true);
  }
  const openGeneration = frameFetchGeneration;
  const manifestUrl = recordApiUrl(recordId, "/manifest.json");
  const poseRes = await fetch(manifestUrl);
  if (!poseRes.ok) {
    const fallbackUrl = recordApiUrl(recordId, "/pose.json");
    const fallback = await fetch(fallbackUrl);
    if (!fallback.ok) {
      throw new Error(
        `无法加载骨架记录（manifest ${poseRes.status} / pose ${fallback.status}）\n${manifestUrl}`
      );
    }
    poseData = await fallback.json();
  } else {
    const ct = poseRes.headers.get("content-type") || "";
    if (!ct.includes("json")) {
      throw new Error(`骨架接口返回非 JSON（${poseRes.status} ${ct}）\n${manifestUrl}`);
    }
    poseData = await poseRes.json();
  }
  const timelinePromise = buildFrameIndex(recordId, { reset: false });
  showPlaybackStageLoading(`【${displayName || recordId}】加载骨架…`);
  const initialFramesPromise = prefetchFrameChunksParallel(1, FRAME_CHUNK_PREFETCH_INITIAL);
  const annotationPromise = applyPlaybackRecordAnnotation(recordId);
  const videoPromise = prepareAndLoadRecordVideo(recordId, displayName || recordId);
  const eventsPromise = annotationPromise.then(() => loadPlaybackEvents(recordId));
  if (typeof loadPlaybackSkeletonFeatures === "function") {
    void loadPlaybackSkeletonFeatures(recordId);
  }
  const [annResult, videoResult] = await Promise.all([
    annotationPromise,
    videoPromise,
    timelinePromise,
    initialFramesPromise,
  ]).then(([annotation, video]) => [annotation, video]);
  if (openGeneration !== frameFetchGeneration || recordId !== currentRecordId) return;
  hidePlaybackStageLoading();
  const annHint = annResult.ok
    ? ` · 标注：${annResult.label}`
    : annResult.fromPose
      ? " · 使用 pose 内嵌标注"
      : "";
  const collisionHint =
    annotationBoxes.length && !collisionPersistedAtCollect()
      ? `${annHint} · 回放时将实时计算碰撞`
      : annHint;
  $("#playback-video").value = "";
  const label = displayName || recordId;
  const jsonFile = jsonFileName || poseData?.pose_file || `${recordId}/manifest.json`;
  const storageHint = (poseData?.schema || 1) >= 2 ? " · Parquet" : "";
  const baseHint = `【${label}】${jsonFile}（${poseData.frame_count ?? 0} 帧${storageHint}）`;

  const videoLoaded = !!videoResult.loaded;
  const usedOriginalVideo = !!videoResult.usedOriginal;
  const usedDerivedPreview = !!videoResult.usedDerivedPreview;
  void eventsPromise.then(async () => {
    if (openGeneration !== frameFetchGeneration || recordId !== currentRecordId) return;
    const hadPendingAccuracyNav = !!pendingPlaybackAccuracyNav;
    if (playbackEvents.length && !hadPendingAccuracyNav && videoEl.paused) {
      await beginEventReview();
    } else if (playbackEvents.length && !videoEl.paused) {
      syncActiveEventFromPlaybackPosition({
        timeSec: videoEl.currentTime,
        frameIdx: getCurrentPlaybackFrameIdx(),
        duringPlayback: true,
        force: true,
        skipRedraw: true,
      });
    }
    await applyPendingPlaybackAccuracyNav();
  }).catch((err) => console.warn("事件与复核状态加载失败", err));
  if (videoLoaded) {
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `${baseHint}${collisionHint} · 已加载配套视频 ${frameW}×${frameH}`;
    if (playbackSkeletonReady) hint += " · 骨架已就绪";
    hint += "。";
    if (usedOriginalVideo) {
      hint += " 预览转码不可用，已使用原片（可能略卡）。";
    } else if (usedDerivedPreview) {
      // 仅 ≥10000 帧才会派生 480p；勿用分辨率宽度误判短片。
      hint += " 已使用 ≥10000 帧派生的 480p 预览；播放中为静态货框，暂停后可查看碰撞高亮。";
    }
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理 ${f0.w}×${f0.h}，将自动对齐。`;
    }
    setPlaybackInfo(hint);
    redrawCurrentFrame();
    if (typeof enablePlaybackSkeletonFeatureFetch === "function") {
      enablePlaybackSkeletonFeatureFetch({ delayMs: 700 });
    }
    await startVideoPlayback("");
    return;
  }

  if (expectVideo) {
    setPlaybackInfo(`${baseHint} · 未找到已保存视频（可能采集时关闭了保存）。可上传替换或仅播放骨骼。`);
  } else {
    setPlaybackInfo(`${baseHint} · 无配套视频，可上传或仅播放骨骼。`);
  }
  redrawCurrentFrame();
  if (typeof enablePlaybackSkeletonFeatureFetch === "function") {
    enablePlaybackSkeletonFeatureFetch({ delayMs: 700 });
  }
  await applyPendingPlaybackAccuracyNav();
}
