/** 回放记录列表与打开记录 */
/** 当前查看的机位目录（null = 一级机位列表） */
let playbackSelectedCameraSlug = null;
/** 用户主动返回一级机位列表时置 true，避免播放中记录导致自动下钻 */
let playbackCameraListPinned = false;
let playbackRecordsCache = [];
/** 回放列表当前筛选的模型数据层（rtmpose-t / rtmpose-s / rtmpose-m） */
let playbackPoseTier = "rtmpose-t";
/** 回放标注来源：tier=当前模型层目录，master=母本 json/annotations */
let playbackAnnotationSource = "tier";
/** 已知标签（来自 /api/tags） */
let playbackKnownTags = [];
/** 按模型层缓存的记录列表，切换 tier 时即时展示 */
const playbackRecordsByTier = new Map();
/** 每层分页加载状态：items / nextOffset / hasMore / loadingMore */
const playbackTierLoadState = new Map();
/** 同 tier 并发 load 去重 */
const playbackRecordsLoadInflight = new Map();

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

function buildRecordGroups(items) {
  const groups = new Map();
  for (const s of items) {
    const key = recordGroupKey(s);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  return groups;
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

function recordHasAllTags(s, requiredTags) {
  if (!requiredTags.length) return true;
  const tags = (Array.isArray(s.tags) ? s.tags : []).map((t) => String(t).toLowerCase());
  return requiredTags.every((t) => tags.includes(t));
}

function recordMatchesReviewFilter(s) {
  const status = String($("#playback-review-status-filter")?.value || "all").trim().toLowerCase();
  const st = String(s.event_review_status || "not_started").trim().toLowerCase();
  if (status === "all" || !status) return true;
  if (status === "reviewed") return st === "completed" || st === "no_collision";
  return st === status;
}

function recordMatchesVerifiedFilter(s) {
  const mode = String($("#playback-verified-filter")?.value || "all").trim().toLowerCase();
  const count = Number(s.event_review_verified_count || 0);
  if (mode === "all" || !mode) return true;
  if (mode === "yes") return count > 0;
  if (mode === "no") return count <= 0;
  return true;
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

function filterPlaybackRecords(items) {
  const filterQ = String($("#playback-record-filter")?.value || "")
    .trim()
    .toLowerCase();
  const tagFilter = parseTagFilterQuery();
  return items.filter((s) => {
    if (filterQ && !recordSearchBlob(s).includes(filterQ)) return false;
    if (!recordHasAllTags(s, tagFilter)) return false;
    if (!recordMatchesReviewFilter(s)) return false;
    if (!recordMatchesVerifiedFilter(s)) return false;
    return true;
  });
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
  await fetchKnownTags();
  renderPlaybackRecordsList(playbackRecordsCache);
  return tags;
}

function getTierLoadState(tier) {
  const key = String(tier || "rtmpose-t").trim();
  if (!playbackTierLoadState.has(key)) {
    playbackTierLoadState.set(key, {
      items: [],
      nextOffset: 0,
      hasMore: true,
      loadingMore: false,
    });
  }
  return playbackTierLoadState.get(key);
}

function commitPlaybackRecordsCacheToTier(tier = playbackPoseTier) {
  const key = String(tier || "rtmpose-t").trim();
  playbackRecordsByTier.set(key, [...playbackRecordsCache]);
  const state = playbackTierLoadState.get(key);
  if (state) state.items = playbackRecordsCache;
}

function resetTierLoadState(tier) {
  const key = String(tier || "").trim();
  if (!key) return;
  playbackTierLoadState.delete(key);
  playbackRecordsByTier.delete(key);
}

function invalidatePlaybackTierCache(tier = "") {
  if (tier) resetTierLoadState(tier);
  else {
    playbackTierLoadState.clear();
    playbackRecordsByTier.clear();
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
    renderPlaybackRecordsList(playbackRecordsCache);
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

function renderCameraGroupItem(key, groupItems) {
  const total = groupItems.length;
  const title = groupItems[0]?.camera_label || key;
  const groupReview = aggregateReviewStatus(groupItems);
  const groupReviewPill = renderReviewPill(groupReview);
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

function playbackRecordsCountSuffix(tier = playbackPoseTier) {
  const state = playbackTierLoadState.get(String(tier || "rtmpose-t").trim());
  if (!state?.hasMore) return "";
  return "（已加载部分，可加载更多）";
}

function renderRecordsLoadMoreFooter() {
  const tier = playbackPoseTier || "rtmpose-t";
  const state = playbackTierLoadState.get(tier);
  if (!state?.hasMore) return "";
  if (state.loadingMore) {
    return `<p class="hint playback-records-load-more">加载更多…</p>`;
  }
  return `<p class="playback-records-load-more"><button type="button" class="link-btn playback-load-more-btn">加载更多记录…</button></p>`;
}

function bindRecordListEvents(list) {
  list.querySelector(".playback-load-more-btn")?.addEventListener("click", (e) => {
    e.preventDefault();
    void loadMoreRecords();
  });
  list.querySelectorAll(".record-back-cameras").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      playbackSelectedCameraSlug = null;
      playbackCameraListPinned = true;
      renderPlaybackRecordsList(playbackRecordsCache);
    });
  });
  list.querySelectorAll(".camera-group-item").forEach((li) => {
    const open = () => {
      const slug = li.dataset.cameraSlug;
      if (!slug) return;
      playbackSelectedCameraSlug = slug;
      playbackCameraListPinned = false;
      renderPlaybackRecordsList(playbackRecordsCache);
    };
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
  });
  list.querySelectorAll(".record-item").forEach((li) => {
    li.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      selectPlaybackRecordItem(li);
    });
    li.addEventListener("dblclick", (e) => {
      if (e.target.closest("a, button")) return;
      selectPlaybackRecordItem(li);
      startPlaybackFromSelectedRecord().catch((err) => setPlaybackInfo(`❌ ${err.message}`));
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
          await prepareEventReviewRecordSwitch();
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
  const filterQ = String($("#playback-record-filter")?.value || "")
    .trim()
    .toLowerCase();
  const tagFilter = parseTagFilterQuery();
  const hasFilter = Boolean(
    filterQ || tagFilter.length || playbackReviewFilterQuery() || playbackVerifiedFilterQuery()
  );
  if (!items.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>暂无记录（请先在采集页完成采集）</p>";
    if (countEl) countEl.textContent = "";
    playbackSelectedCameraSlug = null;
    playbackCameraListPinned = false;
    selectedPlaybackRecord = null;
    updatePlaybackLoadButton();
    return;
  }
  const filtered = filterPlaybackRecords(items);
  const groups = buildRecordGroups(filtered);
  const keepId = selectedPlaybackRecord?.recordId || currentRecordId || "";
  const keys = [...groups.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));

  if (playbackSelectedCameraSlug && !groups.has(playbackSelectedCameraSlug)) {
    playbackSelectedCameraSlug = null;
  }
  if (!playbackSelectedCameraSlug && keepId && !playbackCameraListPinned) {
    const autoSlug = cameraSlugForRecordId(keepId);
    if (autoSlug && groups.has(autoSlug)) playbackSelectedCameraSlug = autoSlug;
  }

  if (!filtered.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>无匹配记录</p>";
    if (countEl) countEl.textContent = hasFilter ? `0 / ${items.length} 条` : "";
    bindRecordListEvents(list);
    return;
  }

  if (!playbackSelectedCameraSlug) {
    if (countEl) {
      const tierLabel = playbackPoseTier || "rtmpose-t";
      const moreHint = playbackRecordsCountSuffix(tierLabel);
      countEl.textContent = hasFilter
        ? `${tierLabel} · ${keys.length} 个机位 · 匹配 ${filtered.length} / ${items.length} 条${moreHint}`
        : `${tierLabel} · ${keys.length} 个机位 · 共 ${items.length} 条${moreHint}`;
    }
    list.innerHTML = `<ul class="camera-group-list">${keys
      .map((key) => renderCameraGroupItem(key, groups.get(key)))
      .join("")}</ul>${renderRecordsLoadMoreFooter()}`;
    bindRecordListEvents(list);
    return;
  }

  const groupItems = groups.get(playbackSelectedCameraSlug) || [];
  const title = groupItems[0]?.camera_label || playbackSelectedCameraSlug;
  const groupReview = aggregateReviewStatus(groupItems);
  const groupReviewPill = renderReviewPill(groupReview);
  const rows = groupItems.map(renderRecordItem).join("");
  if (countEl) {
    countEl.textContent = hasFilter
      ? `机位 ${title} · 匹配 ${groupItems.length} 条`
      : `机位 ${title} · ${groupItems.length} 条`;
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
        : "<p class='hint playback-records-empty'>该机位下无匹配记录</p>"
    }`;
  bindRecordListEvents(list);
  if (keepId) highlightPlaybackRecordInList(keepId);
}

/** 拉取单页记录；仅 offset=0 可带 sync=1 */
async function fetchRecordSummariesPage({
  poseTier = playbackPoseTier,
  offset = 0,
  limit = RECORD_LIST_PAGE_SIZE,
  sync = false,
} = {}) {
  const tier = String(poseTier || "rtmpose-t").trim();
  const syncQs = offset === 0 && sync ? "&sync=1" : "";
  const res = await fetch(
    `/api/records?summary=1&offset=${offset}&limit=${limit}&pose_tier=${encodeURIComponent(tier)}${syncQs}`
  );
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText || "加载记录失败");
  }
  const batch = await res.json();
  return Array.isArray(batch) ? batch : [];
}

async function loadRecordsPage(tier, { sync = false, append = false } = {}) {
  const key = String(tier || "rtmpose-t").trim();
  const state = getTierLoadState(key);
  if (append && !state.hasMore) return state.items;

  const offset = append ? state.nextOffset : 0;
  if (!append) {
    state.items = [];
    state.nextOffset = 0;
    state.hasMore = true;
  }

  const batch = await fetchRecordSummariesPage({
    poseTier: key,
    offset,
    limit: RECORD_LIST_PAGE_SIZE,
    sync: !append && sync,
  });

  if (append) state.items.push(...batch);
  else state.items = batch;
  state.nextOffset = state.items.length;
  state.hasMore = batch.length >= RECORD_LIST_PAGE_SIZE;
  playbackRecordsByTier.set(key, [...state.items]);
  return state.items;
}

async function loadMoreRecords() {
  const tier = playbackPoseTier || "rtmpose-t";
  const state = getTierLoadState(tier);
  if (!state.hasMore || state.loadingMore) return;
  state.loadingMore = true;
  renderPlaybackRecordsList(playbackRecordsCache);
  try {
    const items = await loadRecordsPage(tier, { append: true });
    playbackRecordsCache = items;
    await fetchKnownTags();
    renderPlaybackRecordsList(items);
  } catch (err) {
    const msg = err?.message ? `加载更多失败：${err.message}` : "加载更多失败";
    setPlaybackInfo(`❌ ${msg}`);
  } finally {
    state.loadingMore = false;
    renderPlaybackRecordsList(playbackRecordsCache);
  }
}

async function loadRecords({ quiet = false, force = false } = {}) {
  const list = $("#session-list");
  const tier = playbackPoseTier || "rtmpose-t";

  if (!force) {
    const cached = playbackRecordsByTier.get(tier);
    if (cached?.length) {
      playbackRecordsCache = cached;
      await fetchKnownTags();
      renderPlaybackRecordsList(playbackRecordsCache);
      return;
    }
  }

  if (playbackRecordsLoadInflight.has(tier)) {
    try {
      await playbackRecordsLoadInflight.get(tier);
    } catch {
      /* 由首次请求展示错误 */
    }
    if ((playbackPoseTier || "rtmpose-t") === tier) {
      playbackRecordsCache = playbackRecordsByTier.get(tier) || [];
      if (playbackRecordsCache.length) renderPlaybackRecordsList(playbackRecordsCache);
    }
    return;
  }

  const run = (async () => {
    if (force) resetTierLoadState(tier);
    if (!quiet && !playbackRecordsByTier.get(tier)?.length) {
      if (list) list.innerHTML = "<p class='hint playback-records-empty'>加载记录中…</p>";
    }
    const items = await loadRecordsPage(tier, { sync: Boolean(force), append: false });
    if ((playbackPoseTier || "rtmpose-t") !== tier) return items;
    playbackRecordsCache = items;
    await fetchKnownTags();
    renderPlaybackRecordsList(items);
    return items;
  })();

  playbackRecordsLoadInflight.set(tier, run);
  try {
    await run;
  } catch (err) {
    const msg = err?.message ? `无法加载列表：${err.message}` : "无法加载列表";
    if ((playbackPoseTier || "rtmpose-t") === tier && list) {
      list.innerHTML = `<p class='hint playback-records-empty'>${msg}</p>`;
    }
    throw err;
  } finally {
    playbackRecordsLoadInflight.delete(tier);
  }
}

/** 分页加载直至目标记录出现在当前模型层列表中 */
async function ensurePlaybackRecordInList(recordId, tier = playbackPoseTier) {
  const rid = String(recordId || "").trim();
  const key = String(tier || playbackPoseTier || "rtmpose-t").trim();
  if (!rid) return false;

  const hasRecord = () => (playbackRecordsByTier.get(key) || []).some((s) => s.record_id === rid);
  if (hasRecord()) return true;

  const savedTier = playbackPoseTier;
  playbackPoseTier = key;
  try {
    while (getTierLoadState(key).hasMore) {
      await loadMoreRecords();
      if (hasRecord()) return true;
    }
    return hasRecord();
  } finally {
    playbackPoseTier = savedTier;
  }
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

  await loadRecords({ quiet: Boolean(playbackRecordsByTier.get(tier)?.length) });
  const found = await ensurePlaybackRecordInList(rid, tier);

  playbackPoseTier = tier;
  if (tierSel) tierSel.value = tier;
  playbackRecordsCache = playbackRecordsByTier.get(tier) || [];

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
    playbackAnnotationSource = annSrcSel.value === "master" ? "master" : "tier";
    syncPlaybackAnnotationSourceOptionLabel();
    annSrcSel.addEventListener("change", () => {
      playbackAnnotationSource = annSrcSel.value === "master" ? "master" : "tier";
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
      await loadRecords({ quiet: playbackRecordsByTier.has(playbackPoseTier) });
      if (currentRecordId && playbackAnnotationSource === "tier") {
        const annResult = await applyPlaybackRecordAnnotation(currentRecordId);
        redrawCurrentFrame();
        if (annResult.ok) {
          setPlaybackInfo(`已随模型层切换标注：${annResult.label}（${annotationBoxes.length} 个货框）`);
        }
      }
    });
  }
  if (input && !input.dataset.bound) {
    input.dataset.bound = "1";
    let t = null;
    input.addEventListener("input", () => {
      if (t) clearTimeout(t);
      t = setTimeout(() => renderPlaybackRecordsList(playbackRecordsCache), 200);
    });
  }
  if (tagInput && !tagInput.dataset.bound) {
    tagInput.dataset.bound = "1";
    let t = null;
    tagInput.addEventListener("input", () => {
      if (t) clearTimeout(t);
      t = setTimeout(() => renderPlaybackRecordsList(playbackRecordsCache), 200);
    });
  }
  const reviewSel = $("#playback-review-status-filter");
  const verifiedSel = $("#playback-verified-filter");
  const bindFilterSelect = (sel) => {
    if (!sel || sel.dataset.bound) return;
    sel.dataset.bound = "1";
    sel.addEventListener("change", () => renderPlaybackRecordsList(playbackRecordsCache));
  };
  bindFilterSelect(reviewSel);
  bindFilterSelect(verifiedSel);
}

function playbackAnnotationSourceApiParam() {
  if (playbackAnnotationSource === "master") return "master";
  return playbackPoseTier || "rtmpose-t";
}

function playbackAnnotationSourceLabel() {
  if (playbackAnnotationSource === "master") {
    return "母本 json/annotations";
  }
  const tier = playbackPoseTier || "rtmpose-t";
  return `${tier} json/${tier}/annotations`;
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
    if (meta.resolved_from === "master" && src !== "master" && !hasTierFile) {
      label += "（模型目录无文件，已用母本内容）";
    } else if (hasTierFile && src !== "master") {
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

/** 等待预览视频转码完成后再加载（带进度与遮罩） */
async function prepareAndLoadRecordVideo(recordId, displayName = "") {
  const label = displayName || recordId;
  const statusUrl = recordApiUrl(recordId, "/video/preview/status");
  const startedAt = Date.now();
  let usedOriginal = false;

  const formatWaitSec = () => Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  const result = (loaded) => ({ loaded: !!loaded, usedOriginal });

  showStageLoading(`【${label}】正在检查视频…`);
  setPlaybackInfo(`【${label}】正在检查视频…`);

  let body = await fetch(statusUrl).then((r) => (r.ok ? r.json() : null));
  if (!body) {
    hideStageLoading();
    setPlaybackInfo(`【${label}】视频状态查询失败`);
    return result(false);
  }

  while (body.status === "transcoding") {
    const pct = Number(body.progress) || 0;
    const srcH = Number(body.source_height) || 0;
    const prevH = Number(body.preview_height) || 480;
    const waitSec = formatWaitSec();
    const msg =
      srcH > prevH
        ? `【${label}】正在生成 ${prevH}p 预览视频（原片 ${srcH}p）${pct}%… 已等待 ${waitSec}s`
        : `【${label}】正在准备视频 ${pct}%… 已等待 ${waitSec}s`;
    updateStageLoading(msg);
    setPlaybackInfo(msg);
    await new Promise((r) => setTimeout(r, 600));
    body = await fetch(statusUrl).then((r) => (r.ok ? r.json() : body));
  }

  if (body.status === "missing") {
    hideStageLoading();
    return result(false);
  }

  if (body.status === "error") {
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

  let loaded = await loadSavedRecordVideo(recordId);
  if (!loaded) {
    updateStageLoading(`【${label}】预览视频无法播放，正在加载原视频…`);
    setPlaybackInfo(`【${label}】预览视频无法播放，正在加载原视频…`);
    usedOriginal = true;
    loaded = await loadSavedRecordVideo(recordId, { original: true });
  }
  hideStageLoading();
  return result(loaded);
}

async function startVideoPlayback(hintPrefix = "") {
  try {
    readPlaybackSpeedFromSelect();
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
  await prepareEventReviewRecordSwitch();
  if (!pendingPlaybackAccuracyNav && typeof clearExternalPlaybackAccuracyOverlay === "function") {
    clearExternalPlaybackAccuracyOverlay();
  }
  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === "playback"));
  Object.values(panels).forEach((p) => p.classList.remove("active"));
  panels.playback.classList.add("active");
  const exportLink = $("#playback-export-xlsx");
  if (exportLink) {
    if (recordId) {
      exportLink.href = recordApiUrl(recordId, "/export.xlsx");
      exportLink.download = `${recordId}_skeleton.xlsx`;
      exportLink.classList.remove("hidden");
    } else {
      exportLink.classList.add("hidden");
    }
  }
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
  if (!playbackRecordsByTier.get(playbackPoseTier)?.length) {
    await loadRecords({ quiet: true });
  } else {
    playbackRecordsCache = playbackRecordsByTier.get(playbackPoseTier) || [];
  }
  focusPlaybackCameraForRecord(recordId);
  renderPlaybackRecordsList(playbackRecordsCache);
  highlightPlaybackRecordInList(recordId);
  resetFrameFetchState();
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
  await buildFrameIndex(recordId);
  showPlaybackStageLoading(`【${displayName || recordId}】加载骨架…`);
  await prefetchAllPlaybackChunksInBackground(recordId, (pct) => {
    const msg = `【${displayName || recordId}】加载骨架 ${pct}%…`;
    setPlaybackInfo(msg);
    if (pct < 100) updatePlaybackStageLoading(msg);
    else hidePlaybackStageLoading();
  });
  const annResult = await applyPlaybackRecordAnnotation(recordId);
  const eventsPromise = loadPlaybackEvents(recordId);
  if (typeof loadPlaybackWristFeatures === "function") {
    void loadPlaybackWristFeatures(recordId);
  }
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

  const videoResult = await prepareAndLoadRecordVideo(recordId, displayName || recordId);
  const videoLoaded = !!videoResult.loaded;
  const usedOriginalVideo = !!videoResult.usedOriginal;
  await eventsPromise;
  const hadPendingAccuracyNav = !!pendingPlaybackAccuracyNav;
  if (playbackEvents.length && !hadPendingAccuracyNav) {
    await beginEventReview();
  }
  if (videoLoaded) {
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `${baseHint}${collisionHint} · 已加载配套视频 ${frameW}×${frameH}`;
    if (playbackSkeletonReady) hint += " · 骨架已就绪";
    hint += "。";
    if (usedOriginalVideo) {
      hint += " 预览转码不可用，已使用原片（可能略卡）。";
    } else if (frameW > 720) {
      hint += " 播放中使用预览分辨率与静态货框，暂停后可查看碰撞高亮。";
    }
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理 ${f0.w}×${f0.h}，将自动对齐。`;
    }
    setPlaybackInfo(hint);
    redrawCurrentFrame();
    if (!playbackEvents.length) {
      await startVideoPlayback("");
    }
    await applyPendingPlaybackAccuracyNav();
    return;
  }

  if (expectVideo) {
    setPlaybackInfo(`${baseHint} · 未找到已保存视频（可能采集时关闭了保存）。可上传替换或仅播放骨骼。`);
  } else {
    setPlaybackInfo(`${baseHint} · 无配套视频，可上传或仅播放骨骼。`);
  }
  redrawCurrentFrame();
  await applyPendingPlaybackAccuracyNav();
}
