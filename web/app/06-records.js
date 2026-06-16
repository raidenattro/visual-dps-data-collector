/** 回放记录列表与打开记录 */
/** 当前查看的机位目录（null = 一级机位列表） */
let playbackSelectedCameraSlug = null;
/** 用户主动返回一级机位列表时置 true，避免播放中记录导致自动下钻 */
let playbackCameraListPinned = false;
let playbackRecordsCache = [];
/** 回放列表当前筛选的模型数据层（rtmpose-t / rtmpose-s / rtmpose-m） */
let playbackPoseTier = "rtmpose-t";
/** 已知标签（来自 /api/tags） */
let playbackKnownTags = [];
/** 按模型层缓存的记录列表，切换 tier 时即时展示 */
const playbackRecordsByTier = new Map();

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
  playbackRecordsByTier.set(playbackPoseTier, [...playbackRecordsCache]);
  await fetchKnownTags();
  renderPlaybackRecordsList(playbackRecordsCache);
  return tags;
}

function invalidatePlaybackTierCache(tier = "") {
  if (tier) playbackRecordsByTier.delete(tier);
  else playbackRecordsByTier.clear();
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
    playbackRecordsByTier.set(playbackPoseTier, [...playbackRecordsCache]);
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

function bindRecordListEvents(list) {
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
      countEl.textContent = hasFilter
        ? `${tierLabel} · ${keys.length} 个机位 · 匹配 ${filtered.length} / ${items.length} 条`
        : `${tierLabel} · ${keys.length} 个机位 · 共 ${items.length} 条`;
    }
    list.innerHTML = `<ul class="camera-group-list">${keys
      .map((key) => renderCameraGroupItem(key, groups.get(key)))
      .join("")}</ul>`;
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
        ? `<ul class="session-list">${rows}</ul>`
        : "<p class='hint playback-records-empty'>该机位下无匹配记录</p>"
    }`;
  bindRecordListEvents(list);
  if (keepId) highlightPlaybackRecordInList(keepId);
}

/** 分页拉取全部记录（突破历史 500 条上限） */
async function fetchAllRecordSummaries({ onProgress = null, poseTier = playbackPoseTier, quiet = false } = {}) {
  const pageSize = 500;
  const all = [];
  const tier = String(poseTier || "rtmpose-t").trim();
  for (let offset = 0; ; offset += pageSize) {
    const res = await fetch(
      `/api/records?summary=1&offset=${offset}&limit=${pageSize}&pose_tier=${encodeURIComponent(tier)}`
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText || "加载记录失败");
    }
    const batch = await res.json();
    if (!Array.isArray(batch) || batch.length === 0) break;
    all.push(...batch);
    if (typeof onProgress === "function") onProgress(all.length);
    if (batch.length < pageSize) break;
  }
  return all;
}

async function loadRecords({ quiet = false, force = false } = {}) {
  const list = $("#session-list");
  const tier = playbackPoseTier || "rtmpose-t";
  const cached = playbackRecordsByTier.get(tier);

  if (!force && cached?.length) {
    playbackRecordsCache = cached;
    await fetchKnownTags();
    renderPlaybackRecordsList(playbackRecordsCache);
    void refreshRecordsForTier(tier, { quiet: true });
    return;
  }

  if (!quiet && !playbackRecordsCache.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>加载记录中…</p>";
  }
  try {
    const items = await fetchAllRecordSummaries({
      poseTier: tier,
      onProgress: (n) => {
        if (quiet || playbackRecordsCache.length) return;
        if (list) {
          list.innerHTML = `<p class='hint playback-records-empty'>加载记录中…已获取 ${n} 条</p>`;
        }
      },
    });
    playbackRecordsCache = items;
    playbackRecordsByTier.set(tier, items);
    await fetchKnownTags();
    renderPlaybackRecordsList(items);
    void prefetchOtherPoseTiers(tier);
  } catch (err) {
    const msg = err?.message ? `无法加载列表：${err.message}` : "无法加载列表";
    if (list) list.innerHTML = `<p class='hint playback-records-empty'>${msg}</p>`;
  }
}

async function refreshRecordsForTier(tier, { quiet = true } = {}) {
  try {
    const items = await fetchAllRecordSummaries({ poseTier: tier, quiet });
    playbackRecordsByTier.set(tier, items);
    if ((playbackPoseTier || "rtmpose-t") === tier) {
      playbackRecordsCache = items;
      renderPlaybackRecordsList(items);
    }
  } catch {
    /* 后台刷新失败时保留缓存 */
  }
}

async function prefetchOtherPoseTiers(activeTier) {
  const tiers = ["rtmpose-t", "rtmpose-s", "rtmpose-m"].filter((t) => t !== activeTier);
  for (const tier of tiers) {
    if (playbackRecordsByTier.has(tier)) continue;
    try {
      const items = await fetchAllRecordSummaries({ poseTier: tier, quiet: true });
      playbackRecordsByTier.set(tier, items);
    } catch {
      break;
    }
  }
}

function initPlaybackRecordFilter() {
  const input = $("#playback-record-filter");
  const tagInput = $("#playback-tag-filter");
  const tierSel = $("#playback-pose-tier");
  if (tierSel && !tierSel.dataset.bound) {
    tierSel.dataset.bound = "1";
    playbackPoseTier = tierSel.value || "rtmpose-t";
    tierSel.addEventListener("change", async () => {
      playbackPoseTier = tierSel.value || "rtmpose-t";
      playbackSelectedCameraSlug = null;
      playbackCameraListPinned = false;
      await loadRecords({ quiet: playbackRecordsByTier.has(playbackPoseTier) });
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

async function loadSavedRecordVideo(recordId) {
  const url = recordApiUrl(recordId, "/video");

  if (playbackVideoObjectUrl) {
    URL.revokeObjectURL(playbackVideoObjectUrl);
    playbackVideoObjectUrl = null;
  }
  videoEl.src = url;
  videoEl.style.display = "block";
  videoEl.load();

  return new Promise((resolve) => {
    const onReady = () => {
      videoEl.removeEventListener("loadedmetadata", onReady);
      videoEl.removeEventListener("error", onErr);
      resolve(true);
    };
    const onErr = () => {
      videoEl.removeEventListener("loadedmetadata", onReady);
      videoEl.removeEventListener("error", onErr);
      resolve(false);
    };
    if (videoEl.readyState >= 1) {
      resolve(true);
      return;
    }
    videoEl.addEventListener("loadedmetadata", onReady);
    videoEl.addEventListener("error", onErr);
  });
}

async function startVideoPlayback(hintPrefix = "") {
  try {
    readPlaybackSpeedFromSelect();
    await videoEl.play();
    cancelAnimationFrame(rafId);
    tickPoseFrameIdx = -1;
    resetPlaybackCollisionTracker();
    tick();
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
  await prefetchFrameChunk(1, FRAME_CHUNK_SIZE);
  if (!annotationBoxes.length) {
    try {
      const annRes = await fetch(recordApiUrl(recordId, "/annotation.json"));
      if (annRes.ok) {
        loadAnnotationBoxesFromData(await annRes.json());
      }
    } catch {
      /* 无独立标注文件时忽略 */
    }
  }
  await loadPlaybackEvents(recordId);
  const collisionHint =
    annotationBoxes.length && !collisionPersistedAtCollect()
      ? " · 已加载标注，回放时将实时计算碰撞"
      : "";
  $("#playback-video").value = "";
  const label = displayName || recordId;
  const jsonFile = jsonFileName || poseData?.pose_file || `${recordId}/manifest.json`;
  const storageHint = (poseData?.schema || 1) >= 2 ? " · Parquet" : "";
  const baseHint = `【${label}】${jsonFile}（${poseData.frame_count ?? 0} 帧${storageHint}）`;

  const videoLoaded = await loadSavedRecordVideo(recordId);
  if (playbackEvents.length) {
    await beginEventReview();
  }
  if (videoLoaded) {
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `${baseHint}${collisionHint} · 已加载配套视频 ${frameW}×${frameH}。`;
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理 ${f0.w}×${f0.h}，将自动对齐。`;
    }
    setPlaybackInfo(hint);
    redrawCurrentFrame();
    if (!playbackEvents.length) {
      await startVideoPlayback("");
    }
    return;
  }

  if (expectVideo) {
    setPlaybackInfo(`${baseHint} · 未找到已保存视频（可能采集时关闭了保存）。可上传替换或仅播放骨骼。`);
  } else {
    setPlaybackInfo(`${baseHint} · 无配套视频，可上传或仅播放骨骼。`);
  }
  redrawCurrentFrame();
}
