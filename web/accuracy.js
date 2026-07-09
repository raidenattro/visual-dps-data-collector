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
let lastEvalId = "";
let lastEvalSource = "";
const lastEvalClipByRecordId = new Map();
let selectedDiagnosticsRecordId = "";
const ACCURACY_LAST_EVAL_KEY = "accuracy:lastEvalId";
let evalRunHistoryRows = [];

function poseTierFromRecordIdForAccuracy(recordId) {
  const rid = String(recordId || "").trim();
  const parts = rid.split("/").filter(Boolean);
  if (parts.length >= 3 && /^rtmpose-/i.test(parts[0])) return parts[0];
  return lastAccuracyMeta.pose_tier || "rtmpose-m";
}

function cameraSlugFromRecordIdForAccuracy(recordId) {
  const rid = String(recordId || "").trim();
  const parts = rid.split("/").filter(Boolean);
  if (parts.length >= 3) return parts[1];
  return lastAccuracyMeta.camera_slug || "";
}

function rememberEvalResult(result) {
  lastEvalId = String(result?.eval_id || result?.summary?.eval_id || "").trim();
  lastEvalSource = String(result?.source || "").trim();
  lastEvalClipByRecordId.clear();
  const clips = Array.isArray(result?.clips) ? result.clips : [];
  clips.forEach((clip) => {
    const rid = String(clip?.record_id || "").trim();
    if (rid) lastEvalClipByRecordId.set(rid, clip);
  });
  if (lastEvalId) {
    try {
      localStorage.setItem(ACCURACY_LAST_EVAL_KEY, lastEvalId);
    } catch {
      /* 忽略 */
    }
    syncEvalHistorySelection(lastEvalId);
  }
}

function formatEvalRunTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
    return d.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return String(iso).slice(0, 16);
  }
}

function formatEvalRunSourceShort(run) {
  const params = run?.params || {};
  const source = String(run?.source || run?.eval_mode || "").trim();
  if (source === "upload") {
    const label = String(params.upload_label || "上传推测").trim();
    const base = label.split(/[/\\]/).filter(Boolean).pop() || label;
    const short = base.length > 28 ? `${base.slice(0, 28)}…` : base;
    return `上传·${short}`;
  }
  const pose = params.pose_tier || "—";
  const cam = params.camera_slug || params.camera_label || "—";
  return `${pose}·${cam}`;
}

function formatEvalRunHistoryLabel(run) {
  const evalId = String(run?.eval_id || "").trim();
  const created = formatEvalRunTime(run?.created_at);
  const recall = pct(run?.recall);
  const evaluated = run?.evaluated ?? run?.clip_count ?? "—";
  const src = formatEvalRunSourceShort(run);
  const shortId = evalId.length > 18 ? `${evalId.slice(0, 18)}…` : evalId;
  return `${created} · ${src} · 召回 ${recall} · ${evaluated}片 · ${shortId}`;
}

function findEvalHistoryOption(evalId) {
  const sel = acc$("#accuracy-eval-history");
  if (!sel || !evalId) return null;
  return Array.from(sel.options).find((opt) => opt.value === evalId) || null;
}

function syncEvalHistorySelection(evalId) {
  const sel = acc$("#accuracy-eval-history");
  if (!sel || !evalId) return;
  if (!findEvalHistoryOption(evalId)) {
    const row = evalRunHistoryRows.find((r) => r.eval_id === evalId);
    const opt = document.createElement("option");
    opt.value = evalId;
    opt.textContent = row ? formatEvalRunHistoryLabel(row) : `${formatEvalRunTime(new Date().toISOString())} · 当前评估 · ${evalId}`;
    const first = sel.options[1];
    if (first) sel.insertBefore(opt, first);
    else sel.appendChild(opt);
  }
  sel.value = evalId;
}

function applyHistoricalEvalParams(manifest) {
  const params = manifest?.params || {};
  const source = String(manifest?.source || manifest?.eval_mode || "").trim();
  const modeSel = acc$("#accuracy-eval-mode");
  if (modeSel) {
    modeSel.value = source === "upload" ? "upload_evaluate" : "evaluate_only";
  }
  const poseSel = acc$("#accuracy-pose-tier");
  if (poseSel && params.pose_tier) poseSel.value = params.pose_tier;
  const camSel = acc$("#accuracy-camera");
  if (camSel && params.camera_label) {
    const has = Array.from(camSel.options).some((opt) => opt.value === params.camera_label);
    if (has) camSel.value = params.camera_label;
  }
  const tagInput = acc$("#accuracy-tag-filter");
  if (tagInput) {
    const tags = Array.isArray(params.tag_filter) ? params.tag_filter : [];
    tagInput.value = tags.join(", ");
  }
  syncAccuracyCollisionUi();
}

function buildResultFromHistoricalEvalRun(data) {
  const manifest = data?.manifest || {};
  const params = manifest.params || {};
  const summary = data?.summary || {};
  return {
    eval_id: data?.eval_id || manifest.eval_id || summary.eval_id,
    source: manifest.source || manifest.eval_mode || "",
    pose_tier: params.pose_tier || "",
    camera_slug: params.camera_slug || "",
    camera_label: params.camera_label || "",
    upload_label: params.upload_label || "",
    summary,
    clips: Array.isArray(data?.clips) ? data.clips : [],
    rules: manifest.rules || {},
    from_history: true,
  };
}

async function loadEvalRunHistoryList(options = {}) {
  const sel = acc$("#accuracy-eval-history");
  if (!sel) return [];
  const keepValue = options.keepSelection !== false ? sel.value : "";
  try {
    const res = await fetch("/api/accuracy/eval-runs?limit=100");
    if (!res.ok) throw new Error(await readApiError(res));
    const body = await res.json();
    evalRunHistoryRows = Array.isArray(body.runs) ? body.runs : [];
    sel.innerHTML = '<option value="">— 选择历史评估 —</option>';
    evalRunHistoryRows.forEach((run) => {
      const evalId = String(run?.eval_id || "").trim();
      if (!evalId) return;
      const opt = document.createElement("option");
      opt.value = evalId;
      opt.textContent = formatEvalRunHistoryLabel(run);
      sel.appendChild(opt);
    });
    if (keepValue && findEvalHistoryOption(keepValue)) sel.value = keepValue;
    return evalRunHistoryRows;
  } catch (err) {
    if (!options.silent) {
      setAccuracyStatus(`❌ 历史评估列表加载失败：${escHtml(err.message)}`, true);
    }
    return [];
  }
}

async function loadHistoricalEvalRun(evalId, options = {}) {
  const id = String(evalId || "").trim();
  if (!id) {
    setAccuracyStatus("请选择一条历史评估", true);
    return false;
  }
  const silent = Boolean(options.silent);
  if (!silent) setAccuracyStatus("正在加载历史评估…");
  try {
    const res = await fetch(`/api/accuracy/eval-runs/${encodeURIComponent(id)}`);
    if (!res.ok) throw new Error(await readApiError(res));
    const data = await res.json();
    const result = buildResultFromHistoricalEvalRun(data);
    applyHistoricalEvalParams(data.manifest || {});
    lastAccuracyMeta = {
      pose_tier: result.pose_tier || "",
      camera_slug: result.camera_slug || "",
      camera_label: result.camera_label || "",
    };
    const filterSel = acc$("#accuracy-clip-status-filter");
    if (filterSel) filterSel.value = "all";
    renderAccuracySummary(result);
    renderAccuracyClips(result.clips, lastAccuracyMeta);
    rememberEvalResult(result);
    acc$("#accuracy-diagnostics-wrap")?.classList.add("hidden");
    selectedDiagnosticsRecordId = "";
    syncEvalHistorySelection(id);
    if (!result.source || result.source !== "upload") {
      void refreshAccuracyContext();
    } else {
      acc$("#accuracy-context-hint")?.classList.add("hidden");
    }
    const s = result.summary || {};
    const srcLabel = formatEvalRunSourceShort({
      source: result.source,
      eval_mode: data?.manifest?.eval_mode,
      params: data?.manifest?.params,
    });
    if (!silent) {
      setAccuracyStatus(
        `✅ 已加载历史评估：<code>${escHtml(id)}</code> · ${escHtml(srcLabel)} · 召回率 ${pct(s.recall)} · 误报 ${s.false_alarms ?? 0} 次`
      );
    }
    return true;
  } catch (err) {
    if (!silent) setAccuracyStatus(`❌ 加载历史评估失败：${escHtml(err.message)}`, true);
    return false;
  }
}

async function initEvalRunHistory() {
  await loadEvalRunHistoryList({ silent: true });
  let saved = "";
  try {
    saved = String(localStorage.getItem(ACCURACY_LAST_EVAL_KEY) || "").trim();
  } catch {
    saved = "";
  }
  if (!saved || !findEvalHistoryOption(saved)) return;
  const sel = acc$("#accuracy-eval-history");
  if (sel) sel.value = saved;
  const ok = await loadHistoricalEvalRun(saved, { silent: true });
  if (ok && lastEvalId === saved) {
    setAccuracyStatus(
      `✅ 已恢复上次评估：<code>${escHtml(saved)}</code> · 可直接诊断或跳转回放，无需重新执行`
    );
  }
}

async function fetchEvalClipDetail(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return null;
  const cached = lastEvalClipByRecordId.get(rid);
  if (cached?.diagnostics) return cached;
  if (!lastEvalId) return cached || null;
  const res = await fetch(`/api/accuracy/eval-runs/${encodeURIComponent(lastEvalId)}/clips/${encodeURIComponent(rid)}`);
  if (!res.ok) throw new Error(await readApiError(res));
  const clip = await res.json();
  lastEvalClipByRecordId.set(rid, clip);
  return clip;
}

function inferEvalSourceLabel(clip) {
  const overlayLabel = String(clip?.diagnostics?.playback_overlay?.source_label || "").trim();
  if (overlayLabel) return overlayLabel;
  if (lastEvalSource === "upload") return "上传推测 · is_picking";
  if (lastEvalSource === "local_timeline" && lastAccuracyMeta.pose_tier) {
    return `${lastAccuracyMeta.pose_tier} · timeline 告警`;
  }
  const mode = getAccuracyEvalMode();
  if (mode === "upload_evaluate") return "上传推测 · is_picking";
  if (lastAccuracyMeta.pose_tier) return `${lastAccuracyMeta.pose_tier} · timeline 告警`;
  return "准确率评估";
}

function enrichAccuracyPlaybackOverlay(overlay, clip) {
  if (!overlay || typeof overlay !== "object") return overlay;
  const counts = overlay.counts && typeof overlay.counts === "object" ? { ...overlay.counts } : {};
  const diagCounts = clip?.diagnostics?.counts || {};
  if (counts.alarms == null && clip?.alarm_count != null) counts.alarms = clip.alarm_count;
  if (counts.collisions == null && clip?.collision_count != null) counts.collisions = clip.collision_count;
  if (counts.verified == null && clip?.verified_entry_count != null) {
    counts.verified = clip.verified_entry_count;
  }
  if (counts.missed_segments == null) {
    counts.missed_segments = clip?.missed ?? diagCounts.missed_segments;
  }
  if (counts.false_alarms == null) {
    counts.false_alarms = clip?.false_alarms ?? diagCounts.false_alarms;
  }
  return {
    ...overlay,
    source_label: String(overlay.source_label || inferEvalSourceLabel(clip)).trim(),
    counts,
  };
}

function updateAccuracyDiagnosticsCollapseBtn() {
  const btn = acc$("#accuracy-diagnostics-collapse");
  const wrap = acc$("#accuracy-diagnostics-wrap");
  if (!btn || !wrap) return;
  const collapsed = wrap.classList.contains("is-collapsed");
  btn.textContent = collapsed ? "展开" : "收起";
  btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  btn.title = collapsed ? "展开诊断面板" : "收起诊断面板";
}

function toggleAccuracyDiagnosticsCollapse() {
  const wrap = acc$("#accuracy-diagnostics-wrap");
  if (!wrap || wrap.classList.contains("hidden")) return;
  wrap.classList.toggle("is-collapsed");
  updateAccuracyDiagnosticsCollapseBtn();
}

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
  const evalId = String(result.eval_id || s.eval_id || lastEvalId || "").trim();
  const evalHint = evalId
    ? `<p class="hint accuracy-eval-id-hint">${result.from_history ? "历史评估" : "评估已落盘"}：<code>${escHtml(evalId)}</code> · 可在分片行点「诊断」查看漏报/误报并跳转回放</p>`
    : "";
  const uploadDirHint =
    isUpload && result.upload_label
      ? `<p class="hint accuracy-upload-dir-hint">推测来源：<code>${escHtml(result.upload_label)}</code></p>`
      : "";
  const headingLabel = isUpload
    ? "上传推测评估"
    : `${escHtml(result.camera_label || "")} · ${escHtml(result.pose_tier || "")}`;
  if (!s.evaluated && !recomputeBlock && !tagHint && !uploadHint && !evalHint && !uploadDirHint) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `
    ${recomputeBlock}
    ${tagHint}
    ${uploadHint}
    ${uploadDirHint}
    ${evalHint}
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
  const fn = Number(c.missed ?? c.diagnostics_counts?.missed_segments ?? 0);
  const fp = Number(c.false_alarms ?? c.diagnostics_counts?.false_alarms ?? 0);
  const diagDisabled = c.status !== "ok" || (fn <= 0 && fp <= 0);
  return `<span class="accuracy-clip-actions">
    <button type="button" class="link-btn accuracy-open-diagnostics" data-record-id="${escAttr(rid)}" ${diagDisabled ? "disabled" : ""}>诊断</button>
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

function renderAccuracyDiagnosticsList(clip, filter = "all") {
  const listEl = acc$("#accuracy-diagnostics-list");
  if (!listEl || !clip) return;
  const diag = clip.diagnostics || {};
  const f = String(filter || "all").trim().toLowerCase();
  const items = [];
  if (f === "all" || f === "fn") {
    (diag.missed_segments || []).forEach((row) => {
      items.push({ ...row, kind: "fn" });
    });
  }
  if (f === "all" || f === "fp") {
    (diag.false_alarms || []).forEach((row) => {
      items.push({ ...row, kind: "fp" });
    });
  }
  if (!items.length) {
    listEl.innerHTML = `<p class="hint accuracy-diagnostics-empty">当前筛选无诊断项</p>`;
    return;
  }
  items.sort((a, b) => {
    const af = Number(a.seek_frame ?? a.frame_idx ?? a.frame_start) || 0;
    const bf = Number(b.seek_frame ?? b.frame_idx ?? b.frame_start) || 0;
    return af - bf || String(a.kind).localeCompare(String(b.kind));
  });
  listEl.innerHTML = items
    .map((item) => {
      const kind = item.kind === "fp" ? "fp" : "fn";
      const seek = Number(item.seek_frame ?? item.frame_idx ?? item.frame_start) || 0;
      const label = escHtml(item.label || (kind === "fn" ? `漏报 · 帧 ${seek}` : `误报 · 帧 ${seek}`));
      return `<div class="accuracy-diagnostic-item accuracy-diagnostic-item--${kind}" role="listitem">
        <span class="accuracy-diagnostic-kind">${kind === "fn" ? "漏报" : "误报"}</span>
        <span class="accuracy-diagnostic-label">${label}</span>
        <button type="button" class="link-btn accuracy-diagnostic-seek"
          data-record-id="${escAttr(clip.record_id)}"
          data-seek-frame="${seek}"
          data-kind="${escAttr(kind)}">跳转回放</button>
      </div>`;
    })
    .join("");
}

async function openAccuracyDiagnostics(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return;
  selectedDiagnosticsRecordId = rid;
  const wrap = acc$("#accuracy-diagnostics-wrap");
  const titleEl = acc$("#accuracy-diagnostics-title");
  if (!wrap) return;
  wrap.classList.remove("hidden", "is-collapsed");
  updateAccuracyDiagnosticsCollapseBtn();
  if (titleEl) titleEl.textContent = rid.split("/").pop() || rid;
  setAccuracyStatus("正在加载诊断…");
  try {
    const clip = await fetchEvalClipDetail(rid);
    if (!clip?.diagnostics) {
      listElEmpty("无诊断数据（可能评估时未落盘或该分片未成功评估）");
      return;
    }
    const filter = acc$("#accuracy-diagnostics-filter")?.value || "all";
    renderAccuracyDiagnosticsList(clip, filter);
    const fn = (clip.diagnostics.missed_segments || []).length;
    const fp = (clip.diagnostics.false_alarms || []).length;
    setAccuracyStatus(`✅ 诊断：漏报 ${fn} 段，误报 ${fp} 次 · 点击「跳转回放」定位到对应帧`);
  } catch (err) {
    setAccuracyStatus(`❌ 加载诊断失败：${escHtml(err.message)}`, true);
  }

  function listElEmpty(msg) {
    const listEl = acc$("#accuracy-diagnostics-list");
    if (listEl) listEl.innerHTML = `<p class="hint accuracy-diagnostics-empty">${escHtml(msg)}</p>`;
  }
}

async function gotoPlaybackFromAccuracy(recordId, options = {}) {
  const opts = typeof options === "boolean" ? { autoPlay: options } : options || {};
  const rid = String(recordId || "").trim();
  if (!rid) return;
  if (typeof window.navigateToPlaybackRecord !== "function") {
    setAccuracyStatus("❌ 回放模块未加载，请刷新页面后重试", true);
    return;
  }
  const autoPlay = Boolean(opts.autoPlay);
  const seekFrameIdx = opts.seekFrameIdx != null ? Number(opts.seekFrameIdx) : null;
  let accuracyOverlay = opts.accuracyOverlay || null;
  let clipForOverlay = null;
  if (!accuracyOverlay && (seekFrameIdx != null || autoPlay)) {
    try {
      clipForOverlay = await fetchEvalClipDetail(rid);
      accuracyOverlay = clipForOverlay?.diagnostics?.playback_overlay || null;
    } catch {
      /* 忽略，仅跳转记录 */
    }
  } else if (!accuracyOverlay) {
    try {
      clipForOverlay = await fetchEvalClipDetail(rid);
    } catch {
      /* 忽略 */
    }
  }
  if (accuracyOverlay) {
    if (!clipForOverlay) {
      try {
        clipForOverlay = await fetchEvalClipDetail(rid);
      } catch {
        clipForOverlay = null;
      }
    }
    accuracyOverlay = enrichAccuracyPlaybackOverlay(accuracyOverlay, clipForOverlay);
  }
  setAccuracyStatus(autoPlay ? "正在跳转并加载回放…" : "正在跳转到回放列表…");
  try {
    const found = await window.navigateToPlaybackRecord({
      recordId: rid,
      poseTier: opts.poseTier || poseTierFromRecordIdForAccuracy(rid),
      cameraSlug: opts.cameraSlug || cameraSlugFromRecordIdForAccuracy(rid),
      autoPlay: autoPlay || seekFrameIdx != null,
      seekFrameIdx,
      accuracyOverlay,
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

function shouldSkipAccuracyUploadJson(baseName) {
  const name = String(baseName || "").trim().toLowerCase();
  if (!name.endsWith(".json")) return true;
  if (name === "_manifest.json") return true;
  if (name === "_accuracy_eval.json") return true;
  if (name === "accuracy_report.json") return true;
  if (name === "collision_variants_build_summary.json") return true;
  if (name.startsWith("accuracy_report")) return true;
  return false;
}

function countAccuracyUploadJsonFiles(fileList) {
  let n = 0;
  if (!fileList?.length) return 0;
  for (const f of fileList) {
    const base = (f.webkitRelativePath || f.name || "").split("/").pop();
    if (base && !shouldSkipAccuracyUploadJson(base)) n += 1;
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
    if (shouldSkipAccuracyUploadJson(name)) continue;
    form.append("files", f, name);
    jsonCount += 1;
  }
  for (const f of folder || []) {
    const rel = f.webkitRelativePath || f.name;
    if (!rel) continue;
    const base = rel.split("/").pop() || rel;
    if (shouldSkipAccuracyUploadJson(base)) continue;
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
    rememberEvalResult(result);
    acc$("#accuracy-diagnostics-wrap")?.classList.add("hidden");
    void loadEvalRunHistoryList({ silent: true, keepSelection: true });
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
    rememberEvalResult(result);
    acc$("#accuracy-diagnostics-wrap")?.classList.add("hidden");
    void loadEvalRunHistoryList({ silent: true, keepSelection: true });
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
  const firstInit = !accuracyPanelInited;
  if (firstInit) {
    accuracyPanelInited = true;
    loadAccuracyCameras();
    void loadAccuracyTagSuggestions();
    void initEvalRunHistory();
  }
  syncAccuracyCollisionUi();

  if (!firstInit) return;

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
    const diagBtn = e.target.closest(".accuracy-open-diagnostics");
    if (diagBtn?.dataset?.recordId) {
      e.preventDefault();
      void openAccuracyDiagnostics(diagBtn.dataset.recordId);
      return;
    }
    const btn = e.target.closest(".accuracy-goto-playback, .accuracy-record-link");
    if (!btn?.dataset?.recordId) return;
    e.preventDefault();
    const autoPlay = btn.dataset.autoplay === "1";
    void gotoPlaybackFromAccuracy(btn.dataset.recordId, { autoPlay });
  });
  acc$("#accuracy-diagnostics-list")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".accuracy-diagnostic-seek");
    if (!btn?.dataset?.recordId) return;
    e.preventDefault();
    const seek = parseInt(btn.dataset.seekFrame, 10) || 0;
    void gotoPlaybackFromAccuracy(btn.dataset.recordId, {
      autoPlay: true,
      seekFrameIdx: seek,
    });
  });
  acc$("#accuracy-diagnostics-filter")?.addEventListener("change", async () => {
    if (!selectedDiagnosticsRecordId) return;
    const clip = await fetchEvalClipDetail(selectedDiagnosticsRecordId);
    if (clip) renderAccuracyDiagnosticsList(clip, acc$("#accuracy-diagnostics-filter")?.value || "all");
  });
  acc$("#accuracy-diagnostics-collapse")?.addEventListener("click", () => {
    toggleAccuracyDiagnosticsCollapse();
  });
  acc$("#accuracy-run")?.addEventListener("click", () => {
    void runAccuracyJob();
  });
  acc$("#accuracy-eval-history-load")?.addEventListener("click", () => {
    const evalId = acc$("#accuracy-eval-history")?.value || "";
    void loadHistoricalEvalRun(evalId);
  });
  acc$("#accuracy-eval-history")?.addEventListener("change", () => {
    const evalId = acc$("#accuracy-eval-history")?.value || "";
    if (!evalId) return;
    void loadHistoricalEvalRun(evalId);
  });
  acc$("#accuracy-eval-history-refresh")?.addEventListener("click", () => {
    void (async () => {
      setAccuracyStatus("正在刷新历史评估列表…");
      await loadEvalRunHistoryList();
      setAccuracyStatus(`✅ 历史评估列表已刷新（${evalRunHistoryRows.length} 条）`);
    })();
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
