/** 标注 ROI 准确率评估（review 范本 vs 告警检测） */

function acc$(sel) {
  return document.querySelector(sel);
}

function pct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escAttr(s) {
  return escHtml(s).replace(/'/g, "&#39;");
}

function setAccuracyStatus(html, isError = false) {
  const el = acc$("#accuracy-status");
  if (!el) return;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
  else el.classList.remove("error");
  el.innerHTML = html;
}

async function readApiError(res) {
  try {
    const body = await res.json();
    const d = body.detail;
    if (Array.isArray(d)) return d.map((x) => x.msg || x).join("; ");
    return d || res.statusText;
  } catch {
    return res.statusText;
  }
}

/** 最近一次评估结果（供筛选与跳转回放） */
let lastAccuracyClips = [];
let lastAccuracyMeta = { pose_tier: "", camera_slug: "", camera_label: "" };

function getAccuracyEvalMode() {
  return acc$("#accuracy-eval-mode")?.value || "evaluate_only";
}

function parseAccuracyTagFilterQuery() {
  return String(acc$("#accuracy-tag-filter")?.value || "")
    .split(/[,，]/)
    .map((t) => t.trim())
    .filter(Boolean);
}

function readAccuracyRequestBody() {
  const poseTier = acc$("#accuracy-pose-tier")?.value || "rtmpose-m";
  const camera = acc$("#accuracy-camera")?.value || "";
  const tags = parseAccuracyTagFilterQuery();
  const collision =
    typeof window.readAccuracyCollisionConfigFromForm === "function"
      ? window.readAccuracyCollisionConfigFromForm()
      : { alarm_min_consecutive_frames: 3, alarm_cooldown_frames: 6 };
  const body = {
    pose_tier: poseTier,
    camera,
    alarm_min_consecutive_frames: collision.alarm_min_consecutive_frames,
    alarm_cooldown_frames: collision.alarm_cooldown_frames,
  };
  if (tags.length) body.tags = tags.join(",");
  return body;
}

function updateAccuracyContextHint(ctx) {
  const el = acc$("#accuracy-context-hint");
  if (!el || !ctx) {
    el?.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  const tagFilter = Array.isArray(ctx.tag_filter) ? ctx.tag_filter : [];
  const tagPart = tagFilter.length
    ? ` · 标签 <code>${escHtml(tagFilter.join(", "))}</code>（须同时命中）可评估 <strong>${ctx.tag_eligible_clip_count ?? 0}</strong> 片`
    : "";
  el.innerHTML = `机位 <strong>${escHtml(ctx.camera_label)}</strong>（<code>${escHtml(ctx.camera_slug)}</code>）· <strong>已复核</strong>分片 <strong>${ctx.clip_count}</strong> 个 · 模型层 <code>${escHtml(ctx.pose_tier)}</code> 可匹配记录 <strong>${ctx.matched_record_count}</strong> 个${tagPart}`;
}

async function loadAccuracyTagSuggestions() {
  const list = acc$("#accuracy-tag-suggestions");
  if (!list) return;
  try {
    const res = await fetch("/api/tags");
    if (!res.ok) return;
    const data = await res.json();
    const tags = Array.isArray(data.tags) ? data.tags : [];
    list.innerHTML = tags
      .map((item) => `<option value="${escAttr(item.name || "")}"></option>`)
      .join("");
  } catch {
    /* 忽略 */
  }
}

async function loadAccuracyCameras() {
  const sel = acc$("#accuracy-camera");
  if (!sel) return;
  try {
    const res = await fetch("/api/accuracy/cameras");
    if (!res.ok) throw new Error(await readApiError(res));
    const body = await res.json();
    const cameras = Array.isArray(body.cameras) ? body.cameras : [];
    sel.innerHTML = '<option value="">— 请选择机位 —</option>';
    cameras.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.camera_label;
      opt.textContent = `${c.camera_label}（已复核 ${c.clip_count} 片）`;
      sel.appendChild(opt);
    });
    if (!cameras.length) {
      sel.innerHTML = '<option value="">— 无 review 范本数据 —</option>';
    }
  } catch (err) {
    sel.innerHTML = '<option value="">— 加载失败 —</option>';
    setAccuracyStatus(`❌ 机位列表加载失败：${escHtml(err.message)}`, true);
  }
}

async function refreshAccuracyContext() {
  const poseTier = acc$("#accuracy-pose-tier")?.value || "rtmpose-m";
  const camera = acc$("#accuracy-camera")?.value || "";
  if (!camera) {
    acc$("#accuracy-context-hint")?.classList.add("hidden");
    return;
  }
  try {
    const qs = new URLSearchParams({ pose_tier: poseTier, camera });
    const tags = parseAccuracyTagFilterQuery();
    if (tags.length) qs.set("tags", tags.join(","));
    const res = await fetch(`/api/accuracy/context?${qs}`);
    if (!res.ok) throw new Error(await readApiError(res));
    updateAccuracyContextHint(await res.json());
  } catch (err) {
    setAccuracyStatus(`❌ ${escHtml(err.message)}`, true);
  }
}

function renderAccuracyRecomputeSummary(recompute) {
  if (!recompute) return "";
  const ok = recompute.recomputed_count ?? 0;
  const err = recompute.error_count ?? 0;
  const tagSkipped = recompute.tag_skipped ?? 0;
  const params = `alarm_min=${recompute.alarm_min_consecutive_frames ?? "—"}, cooldown=${recompute.alarm_cooldown_frames ?? "—"}`;
  const tagHint =
    Array.isArray(recompute.tag_filter) && recompute.tag_filter.length
      ? ` · 标签未命中跳过 <strong>${tagSkipped}</strong> 条`
      : "";
  return `<p class="hint accuracy-recompute-hint">碰撞重算：成功 <strong>${ok}</strong> 条，失败 <strong>${err}</strong> 条${tagHint}（${escHtml(params)}）· ${escHtml(recompute.note || "")}</p>`;
}

function renderAccuracySummary(result) {
  const el = acc$("#accuracy-summary");
  if (!el) return;
  const s = result.summary || {};
  const recomputeBlock = renderAccuracyRecomputeSummary(result.recompute);
  const tagFilter = Array.isArray(s.tag_filter) ? s.tag_filter : [];
  const tagHint = tagFilter.length
    ? `<p class="hint accuracy-tag-hint">记录标签筛选：<code>${escHtml(tagFilter.join(", "))}</code>（须同时命中）· 标签未命中排除 <strong>${s.tag_filtered ?? 0}</strong> 片</p>`
    : "";
  const isUpload = result.source === "upload";
  const uploadHint = isUpload
    ? `<p class="hint accuracy-upload-hint">规则：is_picking=true 为碰撞告警，货框匹配 rule_alarm_collisions / rule_collisions（box_id 兼容）</p>`
    : "";
  const headingLabel = isUpload
    ? "上传推测评估"
    : `${escHtml(result.camera_label || "")} · ${escHtml(result.pose_tier || "")}`;
  if (!s.evaluated && !recomputeBlock && !tagHint && !uploadHint) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `
    ${recomputeBlock}
    ${tagHint}
    ${uploadHint}
    <h2 class="accuracy-summary-heading">汇总 · ${headingLabel}</h2>
    <div class="accuracy-metrics-grid">
      <div class="accuracy-metric"><span class="accuracy-metric-label">评估分片</span><span class="accuracy-metric-value">${s.evaluated ?? 0} / ${s.clip_count ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">范本取货段</span><span class="accuracy-metric-value">${s.gt_segments ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">检出成功</span><span class="accuracy-metric-value accuracy-ok">${s.detected ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">漏报</span><span class="accuracy-metric-value accuracy-warn">${s.missed ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">误报</span><span class="accuracy-metric-value accuracy-bad">${s.false_alarms ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">召回率</span><span class="accuracy-metric-value">${pct(s.recall)}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">漏报率</span><span class="accuracy-metric-value">${pct(s.miss_rate)}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">精确率（代理）</span><span class="accuracy-metric-value">${pct(s.precision_proxy)}</span></div>
    </div>
    <p class="hint accuracy-rules-hint">精确率（代理）= 检出成功 / (检出成功 + 误报)。跳过 ${s.skipped ?? 0} 片，失败 ${s.errors ?? 0} 片。</p>
  `;
}

function accuracyClipStatusMeta(st) {
  const status = String(st || "").trim().toLowerCase();
  if (status === "ok") {
    return { label: "完成", className: "accuracy-st-ok", filter: "ok" };
  }
  if (status === "skipped") {
    return { label: "跳过", className: "accuracy-st-skip", filter: "skipped" };
  }
  if (status === "excluded") {
    return { label: "排除", className: "accuracy-st-skip", filter: "skipped" };
  }
  if (status === "error") {
    return { label: "失败", className: "accuracy-st-err", filter: "error" };
  }
  return { label: status || "—", className: "accuracy-st-err", filter: "error" };
}

function accuracyClipMatchesFilter(clip, filter) {
  const f = String(filter || "all").trim().toLowerCase();
  if (f === "all" || !f) return true;
  return accuracyClipStatusMeta(clip.status).filter === f;
}

function renderAccuracyRecordCell(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return `<span class="hint">—</span>`;
  const shortId = rid.split("/").pop() || rid;
  return `<button type="button" class="link-btn accuracy-record-link" data-record-id="${escAttr(rid)}" title="${escAttr(rid)}"><code>${escHtml(shortId)}</code></button>`;
}

function renderAccuracyClipActions(c) {
  const rid = String(c.record_id || "").trim();
  if (!rid) return `<span class="hint">无记录</span>`;
  return `<span class="accuracy-clip-actions">
    <button type="button" class="link-btn accuracy-goto-playback" data-record-id="${escAttr(rid)}" data-autoplay="0">查看</button>
    <button type="button" class="link-btn accuracy-goto-playback accuracy-goto-playback--play" data-record-id="${escAttr(rid)}" data-autoplay="1">回放</button>
  </span>`;
}

function renderAccuracyClipRow(c) {
  const rk = c.review_key || "";
  const uploadFile = c.upload_file || "";
  const shortKey = uploadFile || (rk.includes("/") ? rk.split("/").slice(1).join("/") : rk);
  const meta = accuracyClipStatusMeta(c.status);
  const detail = c.error ? escHtml(c.error) : "—";

  if (c.status !== "ok") {
    return `<tr class="accuracy-clip-row accuracy-clip-row--${escAttr(meta.filter)}" data-status="${escAttr(meta.filter)}">
      <td title="${escAttr(rk)}">${escHtml(shortKey.slice(0, 48))}${shortKey.length > 48 ? "…" : ""}</td>
      <td>${renderAccuracyRecordCell(c.record_id)}</td>
      <td colspan="5" class="hint accuracy-clip-detail">${detail}</td>
      <td><span class="${meta.className}">${meta.label}</span></td>
      <td>${renderAccuracyClipActions(c)}</td>
    </tr>`;
  }

  return `<tr class="accuracy-clip-row accuracy-clip-row--ok" data-status="ok">
    <td title="${escAttr(rk)}">${escHtml(shortKey.slice(0, 40))}${shortKey.length > 40 ? "…" : ""}</td>
    <td>${renderAccuracyRecordCell(c.record_id)}</td>
    <td>${c.gt_segments ?? 0}</td>
    <td class="accuracy-ok">${c.detected ?? 0}</td>
    <td class="accuracy-warn">${c.missed ?? 0}</td>
    <td class="accuracy-bad">${c.false_alarms ?? 0}</td>
    <td>${pct(c.recall)}</td>
    <td><span class="${meta.className}">${meta.label}</span></td>
    <td>${renderAccuracyClipActions(c)}</td>
  </tr>`;
}

function updateAccuracyClipsFilterCount(shown, total) {
  const el = acc$("#accuracy-clips-filter-count");
  if (!el) return;
  if (!total) {
    el.textContent = "";
    return;
  }
  el.textContent = shown === total ? `共 ${total} 条` : `显示 ${shown} / ${total} 条`;
}

function renderAccuracyClips(clips, meta = lastAccuracyMeta) {
  const wrap = acc$("#accuracy-clips-wrap");
  const body = acc$("#accuracy-clips-body");
  if (!wrap || !body) return;

  const list = Array.isArray(clips) ? clips : [];
  lastAccuracyClips = list;
  if (meta) {
    lastAccuracyMeta = {
      pose_tier: meta.pose_tier || lastAccuracyMeta.pose_tier,
      camera_slug: meta.camera_slug || lastAccuracyMeta.camera_slug,
      camera_label: meta.camera_label || lastAccuracyMeta.camera_label,
    };
  }

  if (!list.length) {
    wrap.classList.add("hidden");
    return;
  }

  const filter = acc$("#accuracy-clip-status-filter")?.value || "all";
  const filtered = list.filter((c) => accuracyClipMatchesFilter(c, filter));

  wrap.classList.remove("hidden");
  updateAccuracyClipsFilterCount(filtered.length, list.length);

  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="9" class="hint accuracy-clips-empty-filter">当前筛选无匹配分片</td></tr>`;
    return;
  }

  body.innerHTML = filtered.map(renderAccuracyClipRow).join("");
}

async function gotoPlaybackFromAccuracy(recordId, autoPlay = false) {
  const rid = String(recordId || "").trim();
  if (!rid) return;
  if (typeof window.navigateToPlaybackRecord !== "function") {
    setAccuracyStatus("❌ 回放模块未加载，请刷新页面后重试", true);
    return;
  }
  setAccuracyStatus(autoPlay ? "正在跳转并加载回放…" : "正在跳转到回放列表…");
  try {
    const found = await window.navigateToPlaybackRecord({
      recordId: rid,
      poseTier: lastAccuracyMeta.pose_tier,
      cameraSlug: lastAccuracyMeta.camera_slug,
      autoPlay,
    });
    if (!found) {
      setAccuracyStatus(
        `⚠️ 已在回放页定位模型层，但列表中未找到记录 <code>${escHtml(rid)}</code>（可能尚未加载完，可点「加载更多」）`,
        true
      );
      return;
    }
    setAccuracyStatus(
      autoPlay
        ? `✅ 已打开回放：<code>${escHtml(rid)}</code>`
        : `✅ 已在回放页定位：<code>${escHtml(rid)}</code>（模型 ${escHtml(lastAccuracyMeta.pose_tier || "—")} · 机位 ${escHtml(lastAccuracyMeta.camera_slug || "—")}）`
    );
  } catch (err) {
    setAccuracyStatus(`❌ 跳转失败：${escHtml(err.message)}`, true);
  }
}

function syncAccuracyCollisionUi() {
  const mode = getAccuracyEvalMode();
  const fieldset = acc$(".accuracy-collision-config");
  const uploadPanel = acc$("#accuracy-upload-panel");
  const needsCollision = mode === "recompute_evaluate" || mode === "recompute_only";
  const isUpload = mode === "upload_evaluate";
  fieldset?.classList.toggle("hidden", !needsCollision);
  uploadPanel?.classList.toggle("hidden", !isUpload);
  acc$("#accuracy-camera")?.closest("label")?.classList.toggle("hidden", isUpload);
  acc$("#accuracy-pose-tier")?.closest("label")?.classList.toggle("hidden", isUpload);
  if (isUpload) {
    acc$("#accuracy-context-hint")?.classList.add("hidden");
  }
}

function countAccuracyUploadJsonFiles(fileList) {
  let n = 0;
  if (!fileList?.length) return 0;
  for (const f of fileList) {
    const base = (f.webkitRelativePath || f.name || "").split("/").pop();
    if (base?.toLowerCase().endsWith(".json")) n += 1;
  }
  return n;
}

function updateAccuracyUploadHint() {
  const hint = acc$("#accuracy-upload-hint");
  if (!hint) return;
  const single = acc$("#accuracy-upload-file")?.files;
  const folder = acc$("#accuracy-upload-folder")?.files;
  const singleN = countAccuracyUploadJsonFiles(single);
  const folderN = countAccuracyUploadJsonFiles(folder);
  const total = singleN + folderN;
  if (!total) {
    hint.classList.add("hidden");
    hint.textContent = "";
    return;
  }
  const parts = [];
  if (singleN) parts.push(`${singleN} 个文件`);
  if (folderN) parts.push(`文件夹内 ${folderN} 个 JSON`);
  hint.classList.remove("hidden");
  hint.textContent = `已选：${parts.join("，")}`;
}

function appendAccuracyUploadFilesToForm(form) {
  const single = acc$("#accuracy-upload-file")?.files;
  const folder = acc$("#accuracy-upload-folder")?.files;
  let jsonCount = 0;
  for (const f of single || []) {
    const name = f.name || "";
    if (!name.toLowerCase().endsWith(".json")) continue;
    form.append("files", f, name);
    jsonCount += 1;
  }
  for (const f of folder || []) {
    const rel = f.webkitRelativePath || f.name;
    if (!rel) continue;
    const base = rel.split("/").pop() || rel;
    if (!base.toLowerCase().endsWith(".json")) continue;
    form.append("files", f, rel);
    jsonCount += 1;
  }
  return jsonCount;
}

async function runAccuracyUploadJob() {
  const singleInput = acc$("#accuracy-upload-file");
  const folderInput = acc$("#accuracy-upload-folder");
  const hasSingle = (singleInput?.files?.length || 0) > 0;
  const hasFolder = (folderInput?.files?.length || 0) > 0;
  if (!hasSingle && !hasFolder) {
    setAccuracyStatus("请选择 JSON 文件或文件夹", true);
    return;
  }

  const tags = parseAccuracyTagFilterQuery();
  const form = new FormData();
  const jsonCount = appendAccuracyUploadFilesToForm(form);
  if (!jsonCount) {
    setAccuracyStatus("所选内容无 .json 文件", true);
    return;
  }
  if (tags.length) form.append("tags", tags.join(","));

  setAccuracyStatus(`正在评估 ${jsonCount} 个 JSON…`);
  acc$("#accuracy-summary")?.classList.add("hidden");
  acc$("#accuracy-clips-wrap")?.classList.add("hidden");
  lastAccuracyClips = [];

  try {
    const res = await fetch("/api/accuracy/evaluate-upload", {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw new Error(await readApiError(res));
    const result = await res.json();

    lastAccuracyMeta = {
      pose_tier: "",
      camera_slug: "",
      camera_label: "",
    };

    const filterSel = acc$("#accuracy-clip-status-filter");
    if (filterSel) filterSel.value = "all";

    renderAccuracySummary(result);
    renderAccuracyClips(result.clips, lastAccuracyMeta);
    const s = result.summary || {};
    setAccuracyStatus(
      `✅ 上传评估完成：${s.evaluated ?? 0} 个分片，召回率 ${pct(s.recall)}，误报 ${s.false_alarms ?? 0} 次`
    );
  } catch (err) {
    setAccuracyStatus(`❌ ${escHtml(err.message)}`, true);
  }
}

async function runAccuracyJob() {
  const mode = getAccuracyEvalMode();
  if (mode === "upload_evaluate") {
    await runAccuracyUploadJob();
    return;
  }
  const body = readAccuracyRequestBody();
  if (!body.camera) {
    setAccuracyStatus("请选择机位", true);
    return;
  }

  const endpoint =
    mode === "recompute_evaluate"
      ? "/api/accuracy/recompute-evaluate"
      : mode === "recompute_only"
        ? "/api/accuracy/recompute"
        : "/api/accuracy/evaluate";

  const statusMsg =
    mode === "recompute_evaluate"
      ? "正在重算碰撞并批量评估…"
      : mode === "recompute_only"
        ? "正在批量重算碰撞/告警…"
        : "正在批量评估，请稍候…";

  setAccuracyStatus(statusMsg);
  acc$("#accuracy-summary")?.classList.add("hidden");
  acc$("#accuracy-clips-wrap")?.classList.add("hidden");
  lastAccuracyClips = [];

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readApiError(res));
    const result = await res.json();

    if (mode === "recompute_only") {
      acc$("#accuracy-summary")?.classList.remove("hidden");
      acc$("#accuracy-clips-wrap")?.classList.add("hidden");
      const el = acc$("#accuracy-summary");
      if (el) {
        el.innerHTML = `
          <h2 class="accuracy-summary-heading">碰撞重算完成 · ${escHtml(result.camera_label)} · ${escHtml(result.pose_tier)}</h2>
          ${renderAccuracyRecomputeSummary(result)}
          <p class="hint">已覆盖写回匹配记录的 timeline 碰撞/告警与 manifest.collision（骨架 keypoints 复用未重推理）。</p>
        `;
      }
      setAccuracyStatus(
        `✅ 重算完成：成功 ${result.recomputed_count ?? 0} 条，失败 ${result.error_count ?? 0} 条`
      );
      return;
    }

    lastAccuracyMeta = {
      pose_tier: result.pose_tier || body.pose_tier,
      camera_slug: result.camera_slug || "",
      camera_label: result.camera_label || body.camera,
    };

    const filterSel = acc$("#accuracy-clip-status-filter");
    if (filterSel) filterSel.value = "all";

    renderAccuracySummary(result);
    renderAccuracyClips(result.clips, lastAccuracyMeta);
    const s = result.summary || {};
    const rc = result.recompute;
    const recomputeHint = rc
      ? `；重算 ${rc.recomputed_count ?? 0} 条记录后评估`
      : "";
    setAccuracyStatus(
      `✅ 评估完成：${s.evaluated ?? 0} 个分片，召回率 ${pct(s.recall)}，误报 ${s.false_alarms ?? 0} 次${recomputeHint}`
    );
  } catch (err) {
    setAccuracyStatus(`❌ ${escHtml(err.message)}`, true);
  }
}

let accuracyPanelInited = false;

function initAccuracyPanel() {
  if (accuracyPanelInited) return;
  accuracyPanelInited = true;
  loadAccuracyCameras();
  void loadAccuracyTagSuggestions();
  syncAccuracyCollisionUi();

  if (typeof window.applyAccuracyCollisionConfigToForm === "function") {
    const stored =
      typeof loadCollisionConfigFromStorage === "function"
        ? loadCollisionConfigFromStorage()
        : null;
    if (stored) window.applyAccuracyCollisionConfigToForm(stored);
  }

  acc$("#accuracy-eval-mode")?.addEventListener("change", () => {
    syncAccuracyCollisionUi();
  });
  acc$("#accuracy-pose-tier")?.addEventListener("change", () => {
    void refreshAccuracyContext();
  });
  acc$("#accuracy-camera")?.addEventListener("change", () => {
    void refreshAccuracyContext();
  });
  const tagInput = acc$("#accuracy-tag-filter");
  if (tagInput && !tagInput.dataset.bound) {
    tagInput.dataset.bound = "1";
    let tagTimer = null;
    tagInput.addEventListener("input", () => {
      if (tagTimer) clearTimeout(tagTimer);
      tagTimer = setTimeout(() => void refreshAccuracyContext(), 300);
    });
  }
  acc$("#accuracy-clip-status-filter")?.addEventListener("change", () => {
    renderAccuracyClips(lastAccuracyClips);
  });
  acc$("#accuracy-clips-body")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".accuracy-goto-playback, .accuracy-record-link");
    if (!btn?.dataset?.recordId) return;
    e.preventDefault();
    const autoPlay = btn.dataset.autoplay === "1";
    void gotoPlaybackFromAccuracy(btn.dataset.recordId, autoPlay);
  });
  acc$("#accuracy-run")?.addEventListener("click", () => {
    void runAccuracyJob();
  });
  acc$("#accuracy-upload-file")?.addEventListener("change", () => {
    if (acc$("#accuracy-upload-file")?.files?.length) {
      const folderInput = acc$("#accuracy-upload-folder");
      if (folderInput) folderInput.value = "";
    }
    updateAccuracyUploadHint();
  });
  acc$("#accuracy-upload-folder")?.addEventListener("change", () => {
    if (acc$("#accuracy-upload-folder")?.files?.length) {
      const singleInput = acc$("#accuracy-upload-file");
      if (singleInput) singleInput.value = "";
    }
    updateAccuracyUploadHint();
  });
}

window.initAccuracyPanel = initAccuracyPanel;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAccuracyPanel);
} else {
  initAccuracyPanel();
}
