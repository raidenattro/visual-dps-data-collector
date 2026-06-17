/** COCO-17 骨架连线（与 visual-dps 一致） */
const COCO_LINES = [
  [15, 13], [13, 11], [16, 14], [14, 12], [11, 12], [5, 11], [6, 12], [5, 6],
  [5, 7], [6, 8], [7, 9], [8, 10], [1, 2], [0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6],
];
const SCORE_MIN = 0.3;

const $ = (sel) => document.querySelector(sel);
const tabs = document.querySelectorAll(".tab");
const panels = { collect: $("#panel-collect"), annotate: $("#panel-annotate"), playback: $("#panel-playback") };

let poseData = null;
let annotationBoxes = [];
let annotationSize = null;
let frameByTime = [];
let frameCache = new Map();
/** 已拉取的 Parquet 分块 "from-to"，避免播放时重复请求 */
const loadedChunkKeys = new Set();
const prefetchPromises = new Map();
let renderGeneration = 0;
let lastRenderedFrameIdx = -1;
/** 播放循环中已绘制的骨架帧号，避免 RAF 在相邻帧边界来回切换 */
let tickPoseFrameIdx = -1;
let currentRecordId = null;
let playbackEvents = [];
/** 事件列表来自回放实时重算（非采集落盘） */
let playbackEventsFromRealtime = false;
let activeEventKey = null;
/** 人工标为真的事件键（eventRowKey） */
const verifiedTrueKeys = new Set();
let eventReviewSaveTimer = null;
let eventReviewSaveSeq = 0;
/** 标真并下一条后，上一条优先回到此事件（已标真事件不在「未标真」队列中） */
let reviewBackKey = null;
let currentEventReviewStatus = "not_started";
const FRAME_CHUNK_SIZE = 120;
const COLLISION_CFG_STORAGE_KEY = "datacollect_collision_cfg";
let rafId = null;
let playbackId = null;
let playbackVideoObjectUrl = null;

/** object-fit: contain 布局（与 visual-dps previewLayout 一致） */
function computeContainLayout(containerW, containerH, frameW, frameH) {
  const cw = Math.max(1, containerW);
  const ch = Math.max(1, containerH);
  const fw = Math.max(1, frameW || cw);
  const fh = Math.max(1, frameH || ch);
  const scale = Math.min(cw / fw, ch / fh);
  const drawW = fw * scale;
  const drawH = fh * scale;
  return {
    offsetX: (cw - drawW) / 2,
    offsetY: (ch - drawH) / 2,
    drawW,
    drawH,
    frameW: fw,
    frameH: fh,
  };
}

/** 推理坐标 → 容器内显示坐标（对齐视频 letterbox 区域） */
function mapInferToDisplay(x, y, inferW, inferH, layout) {
  const iw = Math.max(1, inferW);
  const ih = Math.max(1, inferH);
  return [
    layout.offsetX + (Number(x) * layout.drawW) / iw,
    layout.offsetY + (Number(y) * layout.drawH) / ih,
  ];
}

function getVideoFrameSize() {
  const vw = videoEl.videoWidth;
  const vh = videoEl.videoHeight;
  if (vw > 0 && vh > 0) return { frameW: vw, frameH: vh };
  const f0 = frameByTime[0];
  return {
    frameW: poseData?.infer_width || f0?.w || 640,
    frameH: poseData?.infer_height || f0?.h || 480,
  };
}

function chunkRangeForFrame(frameIdx) {
  const idx = Math.max(1, Number(frameIdx) || 1);
  const start = Math.floor((idx - 1) / FRAME_CHUNK_SIZE) * FRAME_CHUNK_SIZE + 1;
  return { from: start, to: start + FRAME_CHUNK_SIZE - 1 };
}

function resetFrameFetchState() {
  frameCache.clear();
  loadedChunkKeys.clear();
  prefetchPromises.clear();
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  renderGeneration++;
}

async function prefetchFrameChunk(from, to) {
  if (!currentRecordId || (poseData?.schema || 1) < 2) return;
  const lo = Math.max(1, from);
  const hi = Math.max(lo, to);
  const key = `${lo}-${hi}`;
  if (loadedChunkKeys.has(key)) return;
  if (prefetchPromises.has(key)) return prefetchPromises.get(key);

  const promise = (async () => {
    const res = await fetch(
      `${recordApiUrl(currentRecordId, "/frames")}?from_frame=${lo}&to_frame=${hi}`
    );
    if (!res.ok) return;
    const body = await res.json();
    (body.frames || []).forEach((fr) => {
      if (fr?.frame_idx != null) frameCache.set(fr.frame_idx, fr);
    });
    loadedChunkKeys.add(key);
  })().finally(() => {
    prefetchPromises.delete(key);
  });

  prefetchPromises.set(key, promise);
  return promise;
}

function prefetchNextChunkIfNeeded(frameIdx) {
  const idx = Number(frameIdx) || 0;
  if (!idx || !currentRecordId) return;
  const { to } = chunkRangeForFrame(idx);
  const nextFrom = to + 1;
  const total = Number(poseData?.frame_count) || 0;
  if (nextFrom > total) return;
  const nextTo = Math.min(nextFrom + FRAME_CHUNK_SIZE - 1, total);
  void prefetchFrameChunk(nextFrom, nextTo);
}

async function ensureFrame(frameIdx) {
  if (frameIdx == null) return null;
  if (frameCache.has(frameIdx)) return frameCache.get(frameIdx);
  if ((poseData?.schema || 1) >= 2 && currentRecordId) {
    const { from, to } = chunkRangeForFrame(frameIdx);
    await prefetchFrameChunk(from, to);
    return frameCache.get(frameIdx) || null;
  }
  return null;
}

async function ensureFrameChunkLoaded(frameIdx) {
  if (frameIdx == null) return;
  if (frameCache.has(frameIdx)) {
    prefetchNextChunkIfNeeded(frameIdx);
    return;
  }
  const { from, to } = chunkRangeForFrame(frameIdx);
  await prefetchFrameChunk(from, to);
  prefetchNextChunkIfNeeded(frameIdx);
}

function getDisplayLayout() {
  const wrap = stageWrap || document.querySelector(".stage-wrap");
  if (!wrap) return computeContainLayout(640, 480, 640, 480);
  const rect = wrap.getBoundingClientRect();
  const { frameW, frameH } = getVideoFrameSize();
  return computeContainLayout(rect.width, rect.height, frameW, frameH);
}

// --- 回放临时视频清理（仅服务端临时目录；本地 blob 用 revoke） ---
async function cleanupPlaybackVideo() {
  if (playbackId) {
    try {
      await fetch(`/api/playback/video/${playbackId}`, { method: "DELETE" });
    } catch {
      /* ignore */
    }
    playbackId = null;
  }
  if (playbackVideoObjectUrl) {
    URL.revokeObjectURL(playbackVideoObjectUrl);
    playbackVideoObjectUrl = null;
  }
}

// --- 回放页：列表选中一条记录后加载 ---
let selectedPlaybackRecord = null;

function updatePlaybackLoadButton() {
  const btn = document.getElementById("playback-load-record");
  if (btn) btn.disabled = !selectedPlaybackRecord?.recordId;
}

function selectPlaybackRecordItem(li) {
  if (!li?.dataset?.recordId) return;
  selectedPlaybackRecord = {
    recordId: li.dataset.recordId,
    displayName: li.dataset.displayName || li.dataset.recordId,
    poseFile: li.dataset.poseFile || "",
    hasVideo: li.dataset.hasVideo === "1",
  };
  document.querySelectorAll("#session-list .record-item").forEach((el) => {
    el.classList.toggle("record-item-selected", el === li);
  });
  updatePlaybackLoadButton();
}

function highlightPlaybackRecordInList(recordId) {
  if (!recordId) return;
  const li = document.querySelector(`#session-list .record-item[data-record-id="${CSS.escape(recordId)}"]`);
  if (li) selectPlaybackRecordItem(li);
}

function getPlaybackRecordSelection() {
  return selectedPlaybackRecord;
}

async function startPlaybackFromSelectedRecord() {
  const sel = getPlaybackRecordSelection();
  if (!sel?.recordId) {
    setPlaybackInfo("❌ 请先在下方列表点击选择一条记录");
    return;
  }
  await openRecordReplay(sel.recordId, sel.displayName, sel.poseFile, sel.hasVideo);
}

// --- 标签页 ---
/** 切离回放页：仅暂停，保留视频源、事件列表与画布叠加 */
function suspendPlaybackOnTabLeave() {
  stopPlayback();
}

/** 回到回放页：恢复导出链接、事件 UI 与当前帧叠加 */
function restorePlaybackPanelUi() {
  if (!poseData && !currentRecordId) return;
  const exportLink = $("#playback-export-xlsx");
  if (exportLink && currentRecordId) {
    exportLink.href = recordApiUrl(currentRecordId, "/export.xlsx");
    exportLink.download = `${currentRecordId}_skeleton.xlsx`;
    exportLink.classList.remove("hidden");
  }
  renderEventJumpList();
  renderEventMarkers();
  redrawCurrentFrame();
  if (currentRecordId && !videoEl.getAttribute("src")) {
    void loadSavedRecordVideo(currentRecordId);
  }
}

tabs.forEach((btn) => {
  btn.addEventListener("click", () => {
    const leavingPlayback = panels.playback.classList.contains("active") && btn.dataset.tab !== "playback";
    if (leavingPlayback) {
      suspendPlaybackOnTabLeave();
    }
    tabs.forEach((b) => b.classList.toggle("active", b === btn));
    Object.values(panels).forEach((p) => p.classList.remove("active"));
    panels[btn.dataset.tab].classList.add("active");
    if (btn.dataset.tab === "collect") {
      void loadInferenceConfigDefaults();
    }
    if (btn.dataset.tab === "annotate") {
      if (typeof window.initAnnotatePanel === "function") window.initAnnotatePanel();
    }
    if (btn.dataset.tab === "playback") {
      loadRecords();
      restorePlaybackPanelUi();
    }
  });
});

$("#collect-fps")?.addEventListener("input", () => {
  $("#collect-fps").dataset.userTouched = "1";
});
$("#collect-alarm-min")?.addEventListener("change", () => {
  saveCollisionConfigToStorage(readCollisionConfigFromForm());
  resetPlaybackCollisionTracker();
});
$("#collect-alarm-cooldown")?.addEventListener("change", () => {
  saveCollisionConfigToStorage(readCollisionConfigFromForm());
  resetPlaybackCollisionTracker();
});

function readCollisionConfigFromForm() {
  const min = Math.max(1, parseInt($("#collect-alarm-min")?.value, 10) || 3);
  const cd = Math.max(1, parseInt($("#collect-alarm-cooldown")?.value, 10) || 6);
  return { alarm_min_consecutive_frames: min, alarm_cooldown_frames: cd };
}

function saveCollisionConfigToStorage(cfg) {
  try {
    localStorage.setItem(COLLISION_CFG_STORAGE_KEY, JSON.stringify(cfg));
  } catch {
    /* ignore */
  }
}

function loadCollisionConfigFromStorage() {
  try {
    const raw = localStorage.getItem(COLLISION_CFG_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return null;
    return {
      alarm_min_consecutive_frames: Math.max(
        1,
        parseInt(data.alarm_min_consecutive_frames, 10) || 3
      ),
      alarm_cooldown_frames: Math.max(1, parseInt(data.alarm_cooldown_frames, 10) || 6),
    };
  } catch {
    return null;
  }
}

function applyCollisionConfigToForm(cfg) {
  if (!cfg) return;
  const minEl = $("#collect-alarm-min");
  const cdEl = $("#collect-alarm-cooldown");
  if (minEl && cfg.alarm_min_consecutive_frames != null) {
    minEl.value = String(cfg.alarm_min_consecutive_frames);
  }
  if (cdEl && cfg.alarm_cooldown_frames != null) {
    cdEl.value = String(cfg.alarm_cooldown_frames);
  }
}

/** 采集落盘用 manifest；回放实时补算用表单/本地缓存 */
function getEffectiveCollisionConfig() {
  const stored = poseData?.collision;
  if (stored && typeof stored === "object" && stored.enabled) {
    return {
      alarm_min_consecutive_frames:
        stored.alarm_min_consecutive_frames ?? 3,
      alarm_cooldown_frames: stored.alarm_cooldown_frames ?? 6,
    };
  }
  return loadCollisionConfigFromStorage() || readCollisionConfigFromForm();
}

async function loadInferenceConfigDefaults() {
  let serverCfg = null;
  try {
    const res = await fetch("/api/config/inference");
    if (res.ok) serverCfg = await res.json();
  } catch {
    /* ignore */
  }
  const stored = loadCollisionConfigFromStorage();
  applyCollisionConfigToForm({
    alarm_min_consecutive_frames:
      stored?.alarm_min_consecutive_frames ??
      serverCfg?.alarm_min_consecutive_frames ??
      3,
    alarm_cooldown_frames:
      stored?.alarm_cooldown_frames ?? serverCfg?.alarm_cooldown_frames ?? 6,
  });
  if (serverCfg?.frame_rate != null && $("#collect-fps") && !$("#collect-fps").dataset.userTouched) {
    $("#collect-fps").value = String(serverCfg.frame_rate);
  }
}

// --- 采集 ---
const collectForm = $("#collect-form");
const collectStatus = $("#collect-status");
const collectResult = $("#collect-result");
const collectBtn = $("#collect-btn");
const collectBatchBtn = $("#collect-batch-btn");
const collectAnnotationStatus = $("#collect-annotation-status");
const collectCameraStatus = $("#collect-camera-status");

const VIDEO_EXT_RE = /\.(mp4|webm|mov|avi|mkv|m4v)$/i;

function isCollectBatchMode() {
  return document.querySelector('input[name="collect-mode"]:checked')?.value === "batch";
}

function setCollectModeUi() {
  const batch = isCollectBatchMode();
  $("#collect-file-wrap")?.classList.toggle("hidden", batch);
  $("#collect-folder-wrap")?.classList.toggle("hidden", !batch);
  collectBtn?.classList.toggle("hidden", batch);
  collectBatchBtn?.classList.toggle("hidden", !batch);
  if (!batch) {
    $("#collect-folder-summary")?.classList.add("hidden");
  }
}

function filterVideoFilesFromList(fileList) {
  return Array.from(fileList || [])
    .filter((f) => VIDEO_EXT_RE.test(f.name || ""))
    .sort((a, b) =>
      String(a.webkitRelativePath || a.name || "").localeCompare(
        String(b.webkitRelativePath || b.name || ""),
        undefined,
        { numeric: true }
      )
    );
}

function folderNameFromFileList(files) {
  const f = files[0];
  if (!f) return "";
  const rel = String(f.webkitRelativePath || f.name || "");
  const parts = rel.split(/[/\\]/).filter(Boolean);
  return parts.length >= 2 ? parts[0] : "";
}

/** 机位子目录 record_id（如 2-1-3/foo_rtmpose_t）按路径段编码，避免 %2F 导致 404 */
function encodeRecordIdPath(recordId) {
  return String(recordId || "")
    .replace(/\\/g, "/")
    .split("/")
    .filter((p) => p.length > 0)
    .map(encodeURIComponent)
    .join("/");
}

function recordApiUrl(recordId, suffix = "") {
  const base = `/api/records/${encodeRecordIdPath(recordId)}`;
  return suffix ? `${base}${suffix.startsWith("/") ? suffix : `/${suffix}`}` : base;
}

function setCollectCameraStatus(html, className = "") {
  if (!collectCameraStatus) return;
  collectCameraStatus.classList.remove("hidden", "is-loading", "is-ok", "is-error");
  if (!html) {
    collectCameraStatus.classList.add("hidden");
    collectCameraStatus.innerHTML = "";
    return;
  }
  if (className) collectCameraStatus.classList.add(className);
  collectCameraStatus.innerHTML = html;
}

function escapeHtmlAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;");
}

/** 机位目录名与下拉项粗匹配（如文件夹 1-6组-2 对应选项 1-6组-2） */
function normalizeCameraMatchKey(s) {
  return String(s || "")
    .trim()
    .replace(/－|—/g, "-")
    .replace(/\s+/g, "")
    .replace(/组/g, "-")
    .replace(/-+/g, "-")
    .toLowerCase();
}

function getCollectCameraLabel() {
  return $("#collect-camera-label")?.value?.trim() || "";
}

function setCollectCameraLabel(value) {
  const sel = $("#collect-camera-label");
  if (!sel) return;
  const v = String(value || "").trim();
  if (!v) {
    sel.value = "";
    return;
  }
  for (const opt of sel.options) {
    if (opt.value === v) {
      sel.value = v;
      return;
    }
  }
  const key = normalizeCameraMatchKey(v);
  for (const opt of sel.options) {
    if (opt.value && normalizeCameraMatchKey(opt.value) === key) {
      sel.value = opt.value;
      return;
    }
  }
}

async function loadReflectionCameras() {
  const sel = $("#collect-camera-label");
  if (!sel) return;
  try {
    const res = await fetch("/api/reflection/cameras");
    if (!res.ok) return;
    const body = await res.json();
    if (!Array.isArray(body.cameras)) return;
    const opts = body.cameras
      .map((c) => `<option value="${escapeHtmlAttr(c)}">${escapeHtmlAttr(c)}</option>`)
      .join("");
    const prev = sel.value;
    sel.innerHTML = `<option value="">— 请选择机位 —</option>${opts}`;
    if (prev) setCollectCameraLabel(prev);
  } catch {
    /* ignore */
  }
}

async function lookupCollectCameraLabel(label) {
  const cam = String(label || "").trim();
  if (!cam) {
    setCollectCameraStatus("");
    return { ok: false, empty: true };
  }
  setCollectCameraStatus("正在校验机位标识…", "is-loading");
  try {
    const res = await fetch(`/api/reflection/lookup?camera=${encodeURIComponent(cam)}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = body.detail || body.message || res.statusText;
      setCollectCameraStatus(`❌ ${msg}`, "is-error");
      return { ok: false, message: msg };
    }
    const files = (body.json_files_display || body.json_files || []).join(", ");
    setCollectCameraStatus(`✅ ${body.message || `将装配 ${files}`}`, "is-ok");
    return { ok: true, camera: body.camera_label || cam, body };
  } catch (err) {
    setCollectCameraStatus(`❌ ${err.message || err}`, "is-error");
    return { ok: false, message: err.message || String(err) };
  }
}

function videoStemFromFilename(name) {
  const base = String(name || "").trim();
  if (!base) return "";
  const dot = base.lastIndexOf(".");
  return (dot > 0 ? base.slice(0, dot) : base).trim();
}

function switchToTab(tabId) {
  const btn = document.querySelector(`.tab[data-tab="${tabId}"]`);
  if (btn) btn.click();
}

function openAnnotateForVideoStem(stem) {
  const s = String(stem || "").trim();
  switchToTab("annotate");
  const stemEl = document.querySelector("#annotate-stem");
  if (stemEl && s) stemEl.value = s;
  if (typeof window.initAnnotatePanel === "function") window.initAnnotatePanel();
}

async function resolveCollectAnnotationForBatch() {
  const camera = getCollectCameraLabel();
  if (!camera) return { ok: false, message: "请选择机位标识（reflection.json 中的 camera）" };
  const lookup = await lookupCollectCameraLabel(camera);
  if (!lookup.ok) return { ok: false, message: lookup.message || "机位标识无效" };
  return { ok: true, source: "camera", camera: lookup.camera || camera };
}

async function resolveCollectAnnotationSource(file, annFile) {
  if (isCollectBatchMode()) return resolveCollectAnnotationForBatch();
  if (annFile) return { ok: true, source: "upload" };
  const camera = getCollectCameraLabel();
  if (camera) {
    const lookup = await lookupCollectCameraLabel(camera);
    if (lookup.ok) return { ok: true, source: "camera", camera: lookup.camera };
    if (!lookup.empty) {
      return { ok: false, message: lookup.message || "机位标识无效" };
    }
  }
  const stem = videoStemFromFilename(file?.name);
  if (!stem) return { ok: false, stem: "", message: "无法从视频文件名解析主名" };
  try {
    const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(stem)}`);
    if (res.ok) return { ok: true, source: "stored", stem };
  } catch {
    /* ignore */
  }
  return {
    ok: false,
    stem,
    message: `请填写机位标识、上传标注 JSON，或到「标注」页按视频主名「${stem}」保存后再采集`,
  };
}

async function refreshCollectAnnotationHint() {
  if (!collectAnnotationStatus) return;
  const annFile = $("#collect-annotation")?.files?.[0];
  if (isCollectBatchMode()) {
    collectAnnotationStatus.classList.remove("hidden");
    const check = await resolveCollectAnnotationForBatch();
    if (check.ok) {
      collectAnnotationStatus.innerHTML = `✅ 批处理将使用机位 <strong>${check.camera}</strong> 的 reflection 标注。`;
      return;
    }
    collectAnnotationStatus.innerHTML = `⚠️ ${check.message || "请选择有效机位标识"}`;
    return;
  }
  const file = $("#collect-file")?.files?.[0];
  if (!file) {
    collectAnnotationStatus.classList.add("hidden");
    collectAnnotationStatus.innerHTML = "";
    return;
  }
  collectAnnotationStatus.classList.remove("hidden");
  const check = await resolveCollectAnnotationSource(file, annFile);
  if (check.ok) {
    const via =
      check.source === "camera"
        ? `将使用机位 <strong>${check.camera}</strong> 的 reflection 标注`
        : check.source === "upload"
          ? "将使用本次上传的标注 JSON"
          : `将使用已存标注（<code>annotations/${check.stem}.json</code>）`;
    collectAnnotationStatus.innerHTML = `✅ ${via}，采集时会计算并保存碰撞/告警。`;
    return;
  }
  const stemEsc = String(check.stem || "").replace(/"/g, "&quot;");
  collectAnnotationStatus.innerHTML = `⚠️ ${check.message} <button type="button" class="link-btn collect-goto-annotate" data-stem="${stemEsc}">去标注「${check.stem}」</button>`;
}

collectAnnotationStatus?.addEventListener("click", (e) => {
  const btn = e.target.closest(".collect-goto-annotate");
  if (!btn) return;
  const file = $("#collect-file")?.files?.[0];
  openAnnotateForVideoStem(btn.dataset.stem || videoStemFromFilename(file?.name));
});

document.querySelectorAll('input[name="collect-mode"]').forEach((el) => {
  el.addEventListener("change", () => {
    setCollectModeUi();
    void refreshCollectAnnotationHint();
  });
});
setCollectModeUi();

$("#collect-file")?.addEventListener("change", () => {
  const file = $("#collect-file")?.files?.[0];
  if (typeof window.initCollectVideoPreview === "function") {
    window.initCollectVideoPreview(file || null);
  }
  void refreshCollectAnnotationHint();
});

$("#collect-folder")?.addEventListener("change", () => {
  const files = filterVideoFilesFromList($("#collect-folder")?.files);
  const summary = $("#collect-folder-summary");
  if (!files.length) {
    summary?.classList.add("hidden");
    if (typeof window.initCollectVideoPreview === "function") window.initCollectVideoPreview(null);
    return;
  }
  summary?.classList.remove("hidden");
  summary.textContent = `已选 ${files.length} 个视频（将保存至机位子目录）`;
  const hint = folderNameFromFileList(files);
  if (hint && !getCollectCameraLabel()) {
    setCollectCameraLabel(hint);
    void lookupCollectCameraLabel(getCollectCameraLabel());
  }
  if (typeof window.initCollectVideoPreview === "function") {
    window.initCollectVideoPreview(files[0]);
  }
  void refreshCollectAnnotationHint();
});

$("#collect-camera-label")?.addEventListener("change", () => {
  void lookupCollectCameraLabel(getCollectCameraLabel());
  void refreshCollectAnnotationHint();
});
$("#collect-annotation")?.addEventListener("change", () => {
  void refreshCollectAnnotationHint();
});

function showStatus(html, isError = false) {
  collectStatus.classList.remove("hidden", "error");
  if (isError) collectStatus.classList.add("error");
  collectStatus.innerHTML = html;
}

function hideStatus() {
  collectStatus.classList.add("hidden");
}

/** 将秒数格式化为可读耗时（用于 ETA / 已用时间） */
function formatDurationSec(sec) {
  if (sec == null || Number.isNaN(sec)) return "";
  const n = Number(sec);
  if (n > 0 && n < 1) return "不足 1 秒";
  const s = Math.max(0, Math.round(n));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

function formatProgressPct(pct) {
  const n = Number(pct);
  if (Number.isNaN(n)) return "0%";
  const v = Math.min(100, Math.max(0, n));
  return Number.isInteger(v) ? `${v}%` : `${v.toFixed(1)}%`;
}

async function pollJob(jobId) {
  for (;;) {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(await res.text());
    const job = await res.json();
    let pct = Math.min(100, Math.max(0, Number(job.progress ?? 0)));
    const parts = [];

    if (job.type === "batch") {
      const cur = job.current_index ?? 0;
      const tot = job.total_videos ?? "?";
      const slug = job.camera_slug || job.camera_label || "";
      let line = `批处理 ${cur}/${tot}`;
      if (job.current_video) line += ` · <code>${job.current_video}</code>`;
      if (slug) line += ` · 机位 <code>${slug}</code>`;
      parts.push(`<div class="hint">${line}</div>`);
      const timing = [];
      if (job.elapsed_sec != null) timing.push(`已用 ${formatDurationSec(job.elapsed_sec)}`);
      if (job.status === "running") {
        if (job.eta_sec != null && job.eta_sec > 0) {
          timing.push(`预计剩余 ${formatDurationSec(job.eta_sec)}`);
        } else if (job.current_frame > 0 && job.frame_total > 0) {
          timing.push("预计剩余 计算中…");
        }
      }
      if (timing.length) parts.push(`<div class="hint">${timing.join(" · ")}</div>`);
      if (
        job.current_frame > 0 &&
        job.frame_total > 0 &&
        job.total_videos > 0 &&
        (pct === 0 || pct < 0.5)
      ) {
        const vi = Math.max(0, (job.current_index ?? 1) - 1);
        const inner = job.current_frame / job.frame_total;
        pct = Math.min(99.9, ((vi + inner) / job.total_videos) * 100);
      }
    }

    if (job.current_frame != null && job.frame_total) {
      const fp = Math.round((job.current_frame / job.frame_total) * 100);
      parts.push(
        `<div class="hint">当前视频帧 ${job.current_frame}/${job.frame_total}（约 ${fp}%）</div>`
      );
    }

    const pctLabel = formatProgressPct(pct);
    showStatus(
      `<div>${job.message || job.status}</div>${parts.join("")}
       <div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
         <i style="width:${pct}%"></i>
       </div>
       <div class="hint progress-pct">${pctLabel}</div>`
    );
    if (job.status === "done" || job.status === "error") return job;
    await new Promise((r) => setTimeout(r, 800));
  }
}

function readCollectFormParams() {
  const collisionCfg = readCollisionConfigFromForm();
  saveCollisionConfigToStorage(collisionCfg);
  return {
    backend: $("#collect-backend").value,
    det_variant: $("#collect-det").value,
    width: $("#collect-width").value || "0",
    height: $("#collect-height").value || "0",
    frame_rate: $("#collect-fps").value ?? "0",
    pose_frame_interval: $("#collect-interval").value || "1",
    max_pose_frames: $("#collect-max").value || "0",
    save_video: $("#collect-save-video").checked ? "1" : "0",
    alarm_min_consecutive_frames: String(collisionCfg.alarm_min_consecutive_frames),
    alarm_cooldown_frames: String(collisionCfg.alarm_cooldown_frames),
  };
}

function appendCollectParams(fd, params) {
  fd.append("backend", params.backend);
  fd.append("det_variant", params.det_variant);
  fd.append("width", params.width);
  fd.append("height", params.height);
  fd.append("frame_rate", params.frame_rate);
  fd.append("pose_frame_interval", params.pose_frame_interval);
  fd.append("max_pose_frames", params.max_pose_frames);
  fd.append("save_video", params.save_video);
  fd.append("alarm_min_consecutive_frames", params.alarm_min_consecutive_frames);
  fd.append("alarm_cooldown_frames", params.alarm_cooldown_frames);
}

collectForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (isCollectBatchMode()) return;

  const file = $("#collect-file").files[0];
  if (!file) {
    showStatus("请选择视频文件", true);
    return;
  }

  const annFile = $("#collect-annotation").files[0];
  const annCheck = await resolveCollectAnnotationSource(file, annFile);
  if (!annCheck.ok) {
    showStatus(`❌ ${annCheck.message}`, true);
    void refreshCollectAnnotationHint();
    return;
  }

  const fd = new FormData();
  fd.append("file", file);
  const params = readCollectFormParams();
  appendCollectParams(fd, params);
  if (annFile) fd.append("annotation", annFile);
  const cameraLabel = getCollectCameraLabel();
  if (cameraLabel && annCheck.source === "camera") fd.append("camera_label", cameraLabel);

  collectBtn.disabled = true;
  const savingVideo = $("#collect-save-video").checked;
  const annNote =
    annCheck.source === "camera"
      ? "（机位标注 + 碰撞事件）"
      : annCheck.source === "stored"
        ? "（已存标注 + 碰撞事件）"
        : "（上传标注 + 碰撞事件）";
  showStatus(
    savingVideo
      ? `上传并推理中${annNote}…（将保存 JSON 与配套视频）`
      : `上传并推理中${annNote}…（仅保存 JSON）`
  );

  try {
    const res = await fetch("/api/collect", { method: "POST", body: fd });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);

    const job = await pollJob(body.job_id);
    const rid = job.record_id || body.record_id || job.job_id;
    collectResult.classList.remove("hidden");
    const hasVideo = job.has_video || savingVideo;
    const hasAnn = job.has_annotation || body.has_annotation;
    const annNote = body.annotation_auto
      ? " · 已关联已存标注 · 碰撞已落盘"
      : hasAnn
        ? " · 碰撞已落盘"
        : "";
    collectResult.innerHTML = `
      <p>✅ 已保存至 <code>localdata/json</code>${hasVideo ? " 与 <code>localdata/video</code>" : ""}${annNote}，共 <strong>${job.frame_count ?? "?"}</strong> 帧</p>
      <p><a href="${job.pose_url}" download>下载 JSON</a> · 管理记录与骨架回放请到「回放」页</p>`;
    hideStatus();
    loadRecords();
  } catch (err) {
    showStatus(`❌ ${err.message}`, true);
  } finally {
    collectBtn.disabled = false;
  }
});

collectBatchBtn?.addEventListener("click", async () => {
  const files = filterVideoFilesFromList($("#collect-folder")?.files);
  if (!files.length) {
    showStatus("请选择包含视频的文件夹", true);
    return;
  }
  const annCheck = await resolveCollectAnnotationForBatch();
  if (!annCheck.ok) {
    showStatus(`❌ ${annCheck.message}`, true);
    return;
  }

  const fd = new FormData();
  for (const f of files) {
    fd.append("files", f, f.webkitRelativePath || f.name);
  }
  fd.append("camera_label", getCollectCameraLabel() || annCheck.camera);
  const params = readCollectFormParams();
  appendCollectParams(fd, params);

  collectBatchBtn.disabled = true;
  collectBtn.disabled = true;
  showStatus(`上传并批处理 ${files.length} 个视频…`);

  try {
    const res = await fetch("/api/collect/batch", { method: "POST", body: fd });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);

    const job = await pollJob(body.job_id);
    const slug = body.camera_slug || job.camera_slug || "";
    const okN = job.success_count ?? (job.results || []).length;
    const errN = job.error_count ?? (job.errors || []).length;
    collectResult.classList.remove("hidden");
    collectResult.innerHTML = `
      <p>✅ 批处理完成：成功 <strong>${okN}</strong>，失败 <strong>${errN}</strong></p>
      <p>数据目录：<code>localdata/json/${slug}</code>${params.save_video === "1" ? ` · 视频：<code>localdata/video/${slug}</code>` : ""}</p>
      <p>管理记录与回放请到「回放」页（按机位分组）</p>`;
    hideStatus();
    loadRecords();
  } catch (err) {
    showStatus(`❌ ${err.message}`, true);
  } finally {
    collectBatchBtn.disabled = false;
    collectBtn.disabled = false;
  }
});

const RECORDS_VISIBLE_PER_GROUP = 8;
/** 机位分组展开条数上限（groupKey -> limit） */
const recordGroupVisibleLimits = new Map();
let playbackRecordsCache = [];

function recordItemEsc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;");
}

function recordSearchBlob(s) {
  const name = s.display_name || s.record_id || "";
  const review = s.event_review_label || reviewStatusLabel(s.event_review_status);
  return `${name} ${s.record_id || ""} ${s.video_stem || ""} ${s.camera_label || ""} ${s.camera_slug || ""} ${review}`.toLowerCase();
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
  if (changed) renderPlaybackRecordsList(playbackRecordsCache);
}

function applyEventReviewPatchFromBody(body) {
  if (!currentRecordId || !body) return;
  const st =
    body.event_review_status ||
    body.event_review?.status ||
    (body.event_review?.verified_true?.length || body.event_review?.updated_at ? "in_progress" : null);
  if (!st) return;
  patchPlaybackRecordReviewStatus(
    currentRecordId,
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
  if (s.has_stored_annotation || s.collision_enabled) badges.push("标注");
  if (s.collision_enabled) badges.push("碰撞");
  const badgeHtml = badges.map((b) => `<span class="record-badge">${b}</span>`).join("");
  return `
      <li class="record-item record-item-compact" data-record-id="${esc(s.record_id)}" data-display-name="${esc(name)}" data-pose-file="${esc(jsonFile)}" data-has-video="${s.has_video ? "1" : "0"}" data-search="${esc(recordSearchBlob(s))}">
        <div class="record-main record-main-compact">
          ${reviewPill}
          <strong class="record-name" title="${esc(name)}">${name}</strong>
          <span class="record-meta-inline">${badgeHtml}</span>
        </div>
        <span class="record-actions record-actions-compact">
          <a href="${recordApiUrl(s.record_id, "/manifest.json")}" download title="${esc(jsonFile)}">JSON</a>
          <a href="${recordApiUrl(s.record_id, "/export.xlsx")}" download title="导出 Excel">XLSX</a>
          ${s.has_video ? `<button type="button" data-annotate="${esc(s.record_id)}" data-stem="${esc(s.video_stem || name)}">标注</button>` : ""}
          <button type="button" class="danger-btn" data-delete="${esc(s.record_id)}" data-name="${esc(name)}">删</button>
        </span>
      </li>`;
}

function bindRecordListEvents(list) {
  list.querySelectorAll(".record-show-more").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const key = btn.dataset.groupKey;
      const total = parseInt(btn.dataset.groupTotal || "0", 10);
      if (key && total > 0) recordGroupVisibleLimits.set(key, total);
      renderPlaybackRecordsList(playbackRecordsCache);
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
  list.querySelectorAll("[data-annotate]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (typeof window.openAnnotateForRecord === "function") {
        window.openAnnotateForRecord(btn.dataset.annotate, btn.dataset.stem);
      }
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
          finishPlaybackSession();
          currentRecordId = null;
        }
        if (selectedPlaybackRecord?.recordId === rid) {
          selectedPlaybackRecord = null;
          updatePlaybackLoadButton();
        }
        await loadRecords();
      } catch (err) {
        window.alert(`删除失败：${err.message}`);
        btn.disabled = false;
      }
    });
  });
}

function renderPlaybackRecordsList(items) {
  const list = $("#session-list");
  const countEl = $("#playback-record-count");
  const filterQ = String($("#playback-record-filter")?.value || "")
    .trim()
    .toLowerCase();
  if (!items.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>暂无记录（请先在采集页完成采集）</p>";
    if (countEl) countEl.textContent = "";
    selectedPlaybackRecord = null;
    updatePlaybackLoadButton();
    return;
  }
  const filtered = filterQ
    ? items.filter((s) => recordSearchBlob(s).includes(filterQ))
    : items;
  if (countEl) {
    countEl.textContent = filterQ
      ? `显示 ${filtered.length} / ${items.length} 条`
      : `共 ${items.length} 条`;
  }
  if (!filtered.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>无匹配记录</p>";
    bindRecordListEvents(list);
    return;
  }
  const groups = new Map();
  for (const s of filtered) {
    const key =
      s.camera_slug ||
      s.camera_label ||
      (String(s.record_id || "").includes("/") ? String(s.record_id).split("/")[0] : "") ||
      "未分类";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  const keepId = selectedPlaybackRecord?.recordId || currentRecordId || "";
  const keys = [...groups.keys()].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  list.innerHTML = keys
    .map((key) => {
      const groupItems = groups.get(key);
      const total = groupItems.length;
      const limit = recordGroupVisibleLimits.get(key) ?? Math.min(RECORDS_VISIBLE_PER_GROUP, total);
      const visible = groupItems.slice(0, limit);
      const hidden = total - visible.length;
      const title = groupItems[0]?.camera_label || key;
      const groupReview = aggregateReviewStatus(groupItems);
      const groupReviewPill = renderReviewPill(groupReview);
      const rows = visible.map(renderRecordItem).join("");
      const openGroup =
        keys.length === 1 ||
        groupItems.some((s) => s.record_id === keepId) ||
        key === (keepId.includes("/") ? keepId.split("/")[0] : "");
      return `<details class="record-group" data-camera-slug="${recordItemEsc(key)}"${
        openGroup ? " open" : ""
      }>
          <summary class="record-group-title">
            <span class="record-group-label">机位 ${recordItemEsc(title)}</span>
            <span class="record-group-meta">
              ${groupReviewPill}
              <code>${recordItemEsc(key)}</code> · ${total} 条
            </span>
          </summary>
          <ul class="session-list">${rows}</ul>
          ${
            hidden > 0
              ? `<button type="button" class="record-show-more link-btn" data-group-key="${recordItemEsc(key)}" data-group-total="${total}">展开其余 ${hidden} 条</button>`
              : ""
          }
        </details>`;
    })
    .join("");
  bindRecordListEvents(list);
  if (keepId) highlightPlaybackRecordInList(keepId);
}

async function loadRecords({ quiet = false } = {}) {
  const list = $("#session-list");
  if (!quiet && !playbackRecordsCache.length) {
    list.innerHTML = "<p class='hint playback-records-empty'>加载记录中…</p>";
  }
  try {
    const res = await fetch("/api/records?summary=1");
    const items = await res.json();
    playbackRecordsCache = items;
    renderPlaybackRecordsList(items);
  } catch {
    list.innerHTML = "<p class='hint playback-records-empty'>无法加载列表</p>";
  }
}

function initPlaybackRecordFilter() {
  const input = $("#playback-record-filter");
  if (!input || input.dataset.bound) return;
  input.dataset.bound = "1";
  let t = null;
  input.addEventListener("input", () => {
    if (t) clearTimeout(t);
    t = setTimeout(() => renderPlaybackRecordsList(playbackRecordsCache), 200);
  });
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

// --- 回放 ---
const videoEl = $("#playback-video-el");
const canvas = $("#playback-canvas");
const ctx = canvas.getContext("2d");
const seekBar = $("#seek-bar");
const timeLabel = $("#time-label");
const eventMarkersEl = $("#seek-event-markers");
const eventJumpList = $("#event-jump-list");
const eventFilterSelect = $("#event-filter");
const eventCountLabel = $("#event-count-label");
const eventsPanel = $("#playback-events-panel");
const stageWrap = document.querySelector(".stage-wrap");

/** 舞台尺寸变化时重算 canvas 并强制重绘（避免退出全屏/窗口缩放后骨架卡住） */
function bindStageLayoutWatch() {
  if (!stageWrap || stageWrap.dataset.layoutWatch) return;
  stageWrap.dataset.layoutWatch = "1";

  let layoutTimer = null;
  const onLayoutChange = () => {
    if (layoutTimer) clearTimeout(layoutTimer);
    layoutTimer = setTimeout(() => {
      layoutTimer = null;
      syncCanvasSize();
      redrawCurrentFrame();
    }, 50);
  };

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(onLayoutChange);
    ro.observe(stageWrap);
  }
  window.addEventListener("resize", onLayoutChange);
  document.addEventListener("fullscreenchange", onLayoutChange);
  videoEl.addEventListener("webkitbeginfullscreen", onLayoutChange);
  videoEl.addEventListener("webkitendfullscreen", onLayoutChange);
}

/** 与后端 event_signature 一致 */
function eventRowKey(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0)
    .sort();
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const eventType = String(ev.event_type || "").trim();
  return `${eventType}:${frameIdx}:${tokens.join(",")}`;
}

function eventToReviewPayload(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  return {
    event_type: String(ev.event_type || "").trim(),
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev.source_frame_idx ?? ev.frame_idx, 10) || frameIdx,
    box_tokens: tokens,
  };
}

function buildEventsFromFrames(frames) {
  const events = [];
  (frames || []).forEach((fr) => {
    if (!fr || typeof fr !== "object") return;
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = [...(fr.alarm_collisions || [])].map(String).filter(Boolean);
    const collisions = [...(fr.collisions || [])].map(String).filter(Boolean);
    if (alarms.length) {
      events.push({
        event_type: "alarm",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: alarms,
      });
    }
    const collOnly = collisions.filter((t) => !alarms.includes(t));
    if (collOnly.length) {
      events.push({
        event_type: "collision",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: collOnly,
      });
    }
  });
  events.sort((a, b) => a.timestamp_sec - b.timestamp_sec || a.frame_idx - b.frame_idx);
  return events;
}

function getPlaybackDurationSec() {
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    return videoEl.duration;
  }
  if (frameByTime.length) {
    const last = frameByTime[frameByTime.length - 1];
    const tail = last?.t || 0;
    const fps = poseData?.fps || 15;
    return Math.max(tail + 1 / fps, tail);
  }
  return 0;
}

function formatEventTokens(tokens) {
  const list = (tokens || []).filter(Boolean);
  if (!list.length) return "—";
  if (list.length <= 2) return list.join(", ");
  return `${list.slice(0, 2).join(", ")} +${list.length - 2}`;
}

function isEventVerified(ev) {
  if (!ev) return false;
  const key = eventRowKey(ev);
  if (verifiedTrueKeys.has(key)) return true;
  if (ev.verified_true) {
    verifiedTrueKeys.add(key);
    return true;
  }
  return false;
}

function countVerifiedEvents() {
  return playbackEvents.filter((e) => isEventVerified(e)).length;
}

/** 按 activeEventKey 在完整事件列表中定位（不受筛选影响） */
function getActiveEvent() {
  if (!activeEventKey || !playbackEvents.length) return null;
  return playbackEvents.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

function refreshEventCountLabel() {
  if (!eventCountLabel) return;
  if (!playbackEvents.length) return;
  const alarmN = playbackEvents.filter((e) => e.event_type === "alarm").length;
  const collN = playbackEvents.filter((e) => e.event_type === "collision").length;
  const verifiedN = countVerifiedEvents();
  const list = filteredPlaybackEvents();
  const rtHint = playbackEventsFromRealtime ? " · 回放实时计算" : "";
  const filterHint = list.length !== playbackEvents.length ? ` · 队列 ${list.length}` : "";
  eventCountLabel.textContent = `告警 ${alarmN} · 碰撞 ${collN} · 标真 ${verifiedN}${rtHint}${filterHint}`;
}

function syncVerifiedKeysFromEvents(events, reviewPayload = null) {
  verifiedTrueKeys.clear();
  (events || []).forEach((ev) => {
    if (ev?.verified_true) verifiedTrueKeys.add(eventRowKey(ev));
  });
  const reviewList = reviewPayload?.verified_true;
  if (Array.isArray(reviewList)) {
    for (const item of reviewList) {
      if (!item || typeof item !== "object") continue;
      const key = eventRowKey(item);
      verifiedTrueKeys.add(key);
    }
  }
}

function applyVerifiedFlagsToEvents() {
  playbackEvents.forEach((ev) => {
    ev.verified_true = isEventVerified(ev);
  });
}

function setEventVerified(ev, verified) {
  const key = eventRowKey(ev);
  if (verified) verifiedTrueKeys.add(key);
  else verifiedTrueKeys.delete(key);
  ev.verified_true = !!verified;
  if (!verified && isReviewTerminalStatus(currentEventReviewStatus)) {
    currentEventReviewStatus = "in_progress";
    patchPlaybackRecordReviewStatus(currentRecordId, "in_progress", "复核中");
  }
  refreshEventCountLabel();
}

async function flushSaveEventReview() {
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  await saveEventReviewNow();
}

function setEventReviewSaveStatus(text, kind = "") {
  const el = $("#event-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = `event-save-status hint${kind ? ` is-${kind}` : ""}`;
}

function scheduleSaveEventReview() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("仅已保存记录可写入复核", "error");
    return;
  }
  setEventReviewSaveStatus("保存中…", "pending");
  if (eventReviewSaveTimer) clearTimeout(eventReviewSaveTimer);
  eventReviewSaveTimer = setTimeout(() => void saveEventReviewNow(), 450);
}

function buildVerifiedTruePayload() {
  return playbackEvents.filter((e) => isEventVerified(e)).map((e) => eventToReviewPayload(e));
}

function applyEventReviewResponse(body, seq) {
  if (seq !== eventReviewSaveSeq) return false;
  if (Array.isArray(body.events)) {
    const prevKey = activeEventKey;
    playbackEvents = body.events;
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
    if (prevKey && playbackEvents.some((e) => eventRowKey(e) === prevKey)) {
      activeEventKey = prevKey;
    }
  }
  currentEventReviewStatus =
    body.event_review_status || body.event_review?.status || currentEventReviewStatus || "in_progress";
  applyEventReviewPatchFromBody(body);
  const n = countVerifiedEvents();
  setEventReviewSaveStatus(`已保存 · 标真 ${n} 条`);
  refreshEventCountLabel();
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  return true;
}

/** 单条标真/取消：服务端 toggle，避免全量 verified_true 覆盖误删 */
async function persistEventReviewToggle(ev, wantVerified) {
  if (!currentRecordId || !ev) return false;
  const seq = ++eventReviewSaveSeq;
  setEventReviewSaveStatus(`标真 ${countVerifiedEvents()} 条 · 保存中…`, "pending");
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "toggle",
        event: eventToReviewPayload(ev),
        verified_true: !!wantVerified,
        event_total: playbackEvents.length,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `保存失败 (${res.status})`);
    }
    const body = await res.json();
    return applyEventReviewResponse(body, seq);
  } catch (err) {
    if (seq !== eventReviewSaveSeq) return false;
    setEventReviewSaveStatus(err.message || "保存失败", "error");
    return false;
  }
}

async function saveEventReviewNow() {
  if (!currentRecordId) return;
  const seq = ++eventReviewSaveSeq;
  const verified_true = buildVerifiedTruePayload();
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        verified_true,
        event_total: playbackEvents.length,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `保存失败 (${res.status})`);
    }
    const body = await res.json();
    applyEventReviewResponse(body, seq);
  } catch (err) {
    if (seq !== eventReviewSaveSeq) return;
    setEventReviewSaveStatus(err.message || "保存失败", "error");
  }
}

async function markEventReviewCompleted() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("请从记录列表打开回放后再完成复核", "error");
    return;
  }
  setEventReviewSaveStatus("正在标记已复核…", "pending");
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status: "completed",
        event_total: playbackEvents.length,
        verified_true: buildVerifiedTruePayload(),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `操作失败 (${res.status})`);
    }
    const body = await res.json();
    currentEventReviewStatus = body.event_review_status || "completed";
    patchPlaybackRecordReviewStatus(currentRecordId, "completed", "已复核");
    setEventReviewSaveStatus("已标记为复核完成");
    updateReviewDock();
    await loadRecords({ quiet: true });
  } catch (err) {
    setEventReviewSaveStatus(err.message || "操作失败", "error");
  }
}

function filteredPlaybackEvents() {
  const mode = eventFilterSelect?.value || "all";
  if (mode === "all") return playbackEvents;
  if (mode === "verified") return playbackEvents.filter((e) => isEventVerified(e));
  if (mode === "unreviewed") return playbackEvents.filter((e) => !isEventVerified(e));
  if (mode === "alarm" || mode === "collision") {
    return playbackEvents.filter((e) => e.event_type === mode);
  }
  return playbackEvents;
}

function getActiveFilteredEvent() {
  const list = filteredPlaybackEvents();
  if (!list.length) return null;
  if (!activeEventKey) return list[0];
  return list.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

function getActiveFilteredIndex() {
  const list = filteredPlaybackEvents();
  if (!list.length) return -1;
  const ev = getActiveFilteredEvent();
  if (!ev) return -1;
  return list.findIndex((e) => eventRowKey(e) === eventRowKey(ev));
}

function getActiveGlobalIndex() {
  if (!playbackEvents.length) return -1;
  if (!activeEventKey) return 0;
  const idx = playbackEvents.findIndex((e) => eventRowKey(e) === activeEventKey);
  return idx >= 0 ? idx : 0;
}

function navigateReviewEvent(delta) {
  if (!playbackEvents.length) return;

  const cur = getActiveEvent();
  const curKey = cur ? eventRowKey(cur) : "";

  // 标真并下一条后，上一条优先回到刚标真的事件（该事件已不在「未标真」队列）
  if (delta < 0 && reviewBackKey && curKey && curKey !== reviewBackKey) {
    const backEv = playbackEvents.find((e) => eventRowKey(e) === reviewBackKey);
    if (backEv) {
      void seekToEvent(backEv);
      return;
    }
  }

  let idx = getActiveGlobalIndex();
  if (idx < 0) idx = 0;
  idx = Math.max(0, Math.min(playbackEvents.length - 1, idx + delta));
  reviewBackKey = null;
  void seekToEvent(playbackEvents[idx]);
}

function scrollActiveEventRowIntoView() {
  if (!eventJumpList || !activeEventKey) return;
  const row = eventJumpList.querySelector(`tr[data-event-key="${CSS.escape(activeEventKey)}"]`);
  row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function updateReviewDock() {
  const list = filteredPlaybackEvents();
  const ev = getActiveEvent() ?? getActiveFilteredEvent();
  const evInFilter = ev ? list.some((item) => eventRowKey(item) === eventRowKey(ev)) : false;
  const posEl = $("#event-review-position");
  const badgeEl = $("#event-review-badge");
  const metaEl = $("#event-review-meta");
  const verifiedTag = $("#event-review-verified-tag");
  const summaryEl = $("#event-review-list-summary");
  const verifiedN = countVerifiedEvents();
  refreshEventCountLabel();

  if (summaryEl) {
    const reviewNote =
      currentEventReviewStatus === "completed"
        ? " · 记录已复核"
        : currentEventReviewStatus === "no_collision"
          ? " · 无碰撞"
          : currentEventReviewStatus === "in_progress"
            ? " · 复核中"
            : "";
    summaryEl.textContent = `全部事件列表（${playbackEvents.length} 条，已标真 ${verifiedN}${reviewNote}）`;
  }

  const completeBtn = $("#event-review-complete-btn");
  if (completeBtn) {
    const reviewDone = isReviewTerminalStatus(currentEventReviewStatus);
    completeBtn.disabled = reviewDone || !currentRecordId;
    if (currentEventReviewStatus === "no_collision") {
      completeBtn.textContent = "无碰撞（已复核）";
    } else {
      completeBtn.textContent = reviewDone ? "已复核完成" : "标记复核完成";
    }
  }

  if (!playbackEvents.length) {
    if (posEl) {
      posEl.textContent = isReviewTerminalStatus(currentEventReviewStatus) ? "无碰撞事件" : "无事件";
    }
    if (metaEl) {
      metaEl.textContent = isReviewTerminalStatus(currentEventReviewStatus)
        ? "无需人工复核"
        : "—";
    }
    verifiedTag?.classList.add("hidden");
    return;
  }

  if (!list.length) {
    if (posEl) posEl.textContent = "队列已清空";
    if (metaEl) metaEl.textContent = "当前筛选下无待复核事件";
    verifiedTag?.classList.add("hidden");
    return;
  }

  if (posEl) {
    const globalIdx = playbackEvents.findIndex((item) => eventRowKey(item) === eventRowKey(ev));
    const globalNote =
      globalIdx >= 0 ? ` · 总序 ${globalIdx + 1}/${playbackEvents.length}` : "";
    if (evInFilter) {
      const idx = list.findIndex((item) => eventRowKey(item) === eventRowKey(ev));
      posEl.textContent = `第 ${idx + 1} / ${list.length} 条${list.length !== playbackEvents.length ? `（队列）${globalNote}` : globalNote}`;
    } else {
      posEl.textContent = `已标真 / 不在当前队列${globalNote}`;
    }
  }

  if (!ev) return;
  const typeLabel = ev.event_type === "alarm" ? "告警" : "碰撞";
  if (badgeEl) {
    badgeEl.textContent = typeLabel;
    badgeEl.className = `event-badge ${ev.event_type}`;
  }
  if (metaEl) {
    metaEl.textContent = `${formatTime(ev.timestamp_sec)} · 帧 ${ev.frame_idx} · ${formatEventTokens(ev.box_tokens)}`;
  }
  if (verifiedTag) {
    verifiedTag.classList.toggle("hidden", !isEventVerified(ev));
  }
}

function renderEventReviewTable(list = null) {
  if (!eventJumpList) return;
  const rows = list ?? filteredPlaybackEvents();
  const canSave = !!currentRecordId;

  eventJumpList.innerHTML = rows
    .map((ev) => {
      const key = eventRowKey(ev);
      const typeLabel = ev.event_type === "alarm" ? "告警" : "碰撞";
      const active = key === activeEventKey ? " active" : "";
      const verified = isEventVerified(ev);
      const verifiedCls = verified ? " verified-true" : "";
      const checked = verified ? " checked" : "";
      const disabled = canSave ? "" : " disabled";
      return `<tr class="event-review-row${active}${verifiedCls}" data-event-key="${key}">
        <td class="col-verify"><input type="checkbox" class="event-verify-check" data-event-key="${key}"${checked}${disabled} aria-label="标为真实碰撞" /></td>
        <td class="col-type"><span class="event-badge ${ev.event_type}">${typeLabel}</span></td>
        <td class="col-time">${formatTime(ev.timestamp_sec)}</td>
        <td class="col-frame">${ev.frame_idx}</td>
        <td class="col-tokens" title="${formatEventTokens(ev.box_tokens)}">${formatEventTokens(ev.box_tokens)}</td>
      </tr>`;
    })
    .join("");

  eventJumpList.querySelectorAll(".event-verify-check").forEach((input) => {
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("change", () => {
      void (async () => {
        const key = input.dataset.eventKey;
        const item = playbackEvents.find((row) => eventRowKey(row) === key);
        if (!item || !currentRecordId) return;
        const want = input.checked;
        setEventVerified(item, want);
        updateReviewDock();
        const ok = await persistEventReviewToggle(item, want);
        if (!ok) {
          setEventVerified(item, !want);
          input.checked = !want;
          updateReviewDock();
        }
        renderEventReviewTable();
        renderEventMarkers();
      })();
    });
  });

  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    row.addEventListener("click", () => {
      const key = row.dataset.eventKey;
      const item = playbackEvents.find((rowEv) => eventRowKey(rowEv) === key);
      if (item) void seekToEvent(item);
    });
  });

  scrollActiveEventRowIntoView();
}

async function markActiveEventVerified(verified) {
  const ev = getActiveEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先在列表或进度条上选择一条事件", "");
    return;
  }
  if (!verified && !isEventVerified(ev)) {
    setEventReviewSaveStatus("当前选中事件未标真", "");
    return;
  }
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  setEventVerified(ev, verified);
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  const ok = await persistEventReviewToggle(ev, verified);
  if (!ok) {
    setEventVerified(ev, !verified);
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    return;
  }
  if (!verified && eventRowKey(ev) === reviewBackKey) {
    reviewBackKey = null;
  }
}

async function confirmTrueAndNext() {
  const list = filteredPlaybackEvents();
  const ev = getActiveFilteredEvent();
  if (!ev) return;
  const idx = getActiveFilteredIndex();
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  reviewBackKey = eventRowKey(ev);
  setEventVerified(ev, true);
  await persistEventReviewToggle(ev, true);

  const mode = eventFilterSelect?.value || "all";
  if (mode === "unreviewed") {
    const newList = filteredPlaybackEvents();
    if (!newList.length) {
      updateReviewDock();
      renderEventReviewTable();
      renderEventMarkers();
      setEventReviewSaveStatus("未标真事件已全部复核", "");
      return;
    }
    const nextEv = newList[Math.min(idx, newList.length - 1)];
    await seekToEvent(nextEv);
    return;
  }

  const nextEv = list[idx + 1];
  if (nextEv) await seekToEvent(nextEv);
  else {
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
  }
}

async function skipToNextEvent() {
  navigateReviewEvent(1);
}

async function beginEventReview() {
  if (!playbackEvents.length) return;
  const hasUnreviewed = playbackEvents.some((e) => !isEventVerified(e));
  if (hasUnreviewed && eventFilterSelect) {
    eventFilterSelect.value = "unreviewed";
  }
  const first = filteredPlaybackEvents()[0];
  if (first) await seekToEvent(first);
  else updateReviewDock();
}

function renderEventMarkers() {
  if (!eventMarkersEl) return;
  eventMarkersEl.innerHTML = "";
  const dur = getPlaybackDurationSec();
  if (!dur || !playbackEvents.length) return;

  filteredPlaybackEvents().forEach((ev) => {
    const pct = Math.min(100, Math.max(0, (ev.timestamp_sec / dur) * 100));
    const dot = document.createElement("button");
    dot.type = "button";
    const verifiedCls = isEventVerified(ev) ? " verified" : "";
    dot.className = `event-marker ${ev.event_type}${verifiedCls}`;
    dot.style.left = `${pct}%`;
    const verifiedNote = isEventVerified(ev) ? " · 已标真" : "";
    dot.title = `${ev.event_type === "alarm" ? "告警" : "碰撞"} ${formatTime(ev.timestamp_sec)} · ${formatEventTokens(ev.box_tokens)}${verifiedNote}`;
    dot.addEventListener("click", (e) => {
      e.stopPropagation();
      seekToEvent(ev);
    });
    eventMarkersEl.appendChild(dot);
  });
}

function renderEventReviewList() {
  if (!eventsPanel) return;
  const list = filteredPlaybackEvents();
  const verifiedN = countVerifiedEvents();

  if (!playbackEvents.length) {
    eventsPanel.classList.add("hidden");
    if (eventJumpList) eventJumpList.innerHTML = "";
    if (eventCountLabel) {
      const hint = annotationBoxes.length
        ? "无碰撞事件（已按标注实时扫描）"
        : "无事件（需采集时启用碰撞或加载标注）";
      eventCountLabel.textContent = hint;
    }
    setEventReviewSaveStatus("");
    updateReviewDock();
    return;
  }

  eventsPanel.classList.remove("hidden");
  refreshEventCountLabel();

  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
  }

  updateReviewDock();
  if ($("#event-review-list-details")?.open) {
    renderEventReviewTable(list);
  } else if (eventJumpList) {
    eventJumpList.innerHTML = "";
  }
  renderEventMarkers();
}

/** @deprecated 兼容旧调用 */
const renderEventJumpList = renderEventReviewList;

/** 采集时是否已启用碰撞并落盘（有则信任存储字段，含空数组） */
function collisionPersistedAtCollect() {
  return !!(poseData?.collision?.enabled);
}

function frameUsesStoredCollisions(frame) {
  if (!collisionPersistedAtCollect() || !frame) return false;
  return (
    ("collisions" in frame || "alarm_collisions" in frame) &&
    (Array.isArray(frame.collisions) || Array.isArray(frame.alarm_collisions))
  );
}

async function collectAllFramesForPlayback(recordId) {
  if ((poseData?.schema || 1) < 2) {
    if (poseData?.frames?.length) {
      return poseData.frames.filter((f) => f && typeof f === "object");
    }
    return frameByTime.map((e) => e.frame).filter(Boolean);
  }
  const total = Number(poseData?.frame_count) || frameByTime.length;
  if (!recordId || total <= 0) return [];
  for (let from = 1; from <= total; from += FRAME_CHUNK_SIZE) {
    const to = Math.min(from + FRAME_CHUNK_SIZE - 1, total);
    await prefetchFrameChunk(from, to);
  }
  const frames = [];
  for (let i = 1; i <= total; i++) {
    const fr = frameCache.get(i);
    if (fr) frames.push(fr);
  }
  frames.sort((a, b) => (Number(a.frame_idx) || 0) - (Number(b.frame_idx) || 0));
  return frames;
}

/** 无采集碰撞落盘但有标注时，按帧扫描生成事件（方案一：仅回放侧） */
async function buildPlaybackEventsFromRealtime(recordId) {
  if (!annotationBoxes.length || collisionPersistedAtCollect()) return [];
  resetPlaybackCollisionTracker();
  const tracker = getPlaybackCollisionTracker();
  const frames = await collectAllFramesForPlayback(recordId);
  const events = [];
  for (const fr of frames) {
    const inferW = Number(fr.infer_width) || Number(poseData?.infer_width) || 640;
    const inferH = Number(fr.infer_height) || Number(poseData?.infer_height) || 480;
    const computed = tracker.update(fr, inferW, inferH);
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = [...(computed.alarm_collisions || [])].map(String).filter(Boolean);
    const collisions = [...(computed.collisions || [])].map(String).filter(Boolean);
    if (alarms.length) {
      events.push({
        event_type: "alarm",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: alarms,
      });
    }
    const collOnly = collisions.filter((t) => !alarms.includes(t));
    if (collOnly.length) {
      events.push({
        event_type: "collision",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: collOnly,
      });
    }
  }
  events.sort((a, b) => a.timestamp_sec - b.timestamp_sec || a.frame_idx - b.frame_idx);
  return events;
}

async function loadPlaybackEvents(recordId = null) {
  playbackEvents = [];
  playbackEventsFromRealtime = false;
  activeEventKey = null;
  verifiedTrueKeys.clear();
  reviewBackKey = null;
  currentEventReviewStatus = "not_started";
  setEventReviewSaveStatus("");

  if (recordId) {
    try {
      const res = await fetch(recordApiUrl(recordId, "/events"));
      if (res.ok) {
        const body = await res.json();
        playbackEvents = Array.isArray(body.events) ? body.events : [];
        syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
        currentEventReviewStatus =
          body.event_review_status ||
          (body.event_review?.status
            ? body.event_review.status
            : body.count === 0
              ? "no_collision"
              : body.event_review?.verified_true?.length || body.event_review?.updated_at
                ? "in_progress"
                : "not_started");
        if (body.count === 0 && currentRecordId) {
          patchPlaybackRecordReviewStatus(
            currentRecordId,
            currentEventReviewStatus,
            body.event_review_label || reviewStatusLabel(currentEventReviewStatus)
          );
        }
      }
    } catch {
      /* 忽略 */
    }
  } else if (poseData?.frames?.length) {
    playbackEvents = buildEventsFromFrames(poseData.frames);
  }

  const needRealtime =
    !playbackEvents.length && annotationBoxes.length > 0 && !collisionPersistedAtCollect();
  if (needRealtime) {
    playbackEvents = await buildPlaybackEventsFromRealtime(recordId);
    playbackEventsFromRealtime = playbackEvents.length > 0;
    resetPlaybackCollisionTracker();
  }

  applyVerifiedFlagsToEvents();
  renderEventReviewList();
}

async function seekToTimestamp(timeSec, frameIdx = null) {
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  resetPlaybackCollisionTracker();
  const t = Math.max(0, Number(timeSec) || 0);
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    videoEl.currentTime = Math.min(t, videoEl.duration);
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
    await renderAtTime(videoEl.currentTime);
    return;
  }

  let hit = null;
  if (frameIdx != null) {
    hit = frameByTime.find((item) => item.frameIdx === frameIdx) || null;
  }
  if (!hit) hit = findFrameAt(t);
  if (hit) {
    await renderFrameEntry(hit);
    const idx = frameByTime.indexOf(hit);
    if (idx >= 0 && frameByTime.length) {
      seekBar.value = String((idx / frameByTime.length) * 1000);
      timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    } else {
      timeLabel.textContent = formatTime(t);
    }
  }
}

async function seekToEvent(ev, { keepReviewBack = false } = {}) {
  if (!ev) return;
  activeEventKey = eventRowKey(ev);
  if (!keepReviewBack && reviewBackKey && activeEventKey === reviewBackKey) {
    reviewBackKey = null;
  }
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  videoEl.pause();
  await seekToTimestamp(ev.timestamp_sec, ev.frame_idx);
}

function clearPlaybackEvents() {
  playbackEvents = [];
  playbackEventsFromRealtime = false;
  activeEventKey = null;
  verifiedTrueKeys.clear();
  reviewBackKey = null;
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  if (eventMarkersEl) eventMarkersEl.innerHTML = "";
  if (eventJumpList) eventJumpList.innerHTML = "";
  if (eventsPanel) eventsPanel.classList.add("hidden");
  if (eventCountLabel) eventCountLabel.textContent = "—";
  setEventReviewSaveStatus("");
}

function setPlaybackInfo(text) {
  $("#playback-info").textContent = text;
}

function clearVideoElement() {
  stopPlayback();
  clearPlaybackEvents();
  videoEl.pause();
  videoEl.removeAttribute("src");
  videoEl.load();
  videoEl.style.display = "block";
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function boxCollisionToken(box) {
  const id = String(box.box_id ?? box.id ?? "").trim();
  if (!id) return "";
  return `Box_${id}`;
}

/** 射线法：点是否在多边形内（推理坐标系） */
function pointInPolygon(point, polygon) {
  if (!Array.isArray(polygon) || polygon.length < 3) return false;
  const x = Number(point[0]);
  const y = Number(point[1]);
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = Number(polygon[i][0]);
    const yi = Number(polygon[i][1]);
    const xj = Number(polygon[j][0]);
    const yj = Number(polygon[j][1]);
    const intersect = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-12) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

/** 回放时实时碰撞（与 event_engine/collision 逻辑一致：手腕 9/10，score>0.3） */
class PlaybackCollisionTracker {
  constructor(minConsecutive = 3, cooldownFrames = 6) {
    this.minConsecutive = Math.max(1, minConsecutive);
    this.cooldownFrames = Math.max(1, cooldownFrames);
    this.consecutiveHits = new Map();
    this.lastAlarmFrame = new Map();
    this.boxCacheKey = "";
    this.boxCache = [];
  }

  reset() {
    this.consecutiveHits.clear();
    this.lastAlarmFrame.clear();
    this.boxCacheKey = "";
    this.boxCache = [];
  }

  getBoxesInInferSpace(inferW, inferH) {
    const key = `${inferW}x${inferH}:${annotationBoxes.length}:${annotationSize?.width || 0}x${annotationSize?.height || 0}`;
    if (key === this.boxCacheKey) return this.boxCache;
    const pl = window.previewLayout;
    if (!pl?.resolvePolygonFramePoints) {
      this.boxCache = [];
      this.boxCacheKey = key;
      return this.boxCache;
    }
    const { frameW, frameH } = getVideoFrameSize();
    const annSize = getEffectiveAnnotationSize();
    const f0 = frameByTime[0];
    const boxesAlreadyInfer =
      f0 &&
      annSize?.width === f0.w &&
      annSize?.height === f0.h &&
      annotationBoxes.every((b) => {
        let mx = 0;
        let my = 0;
        (b.video_polygon || []).forEach((pt) => {
          mx = Math.max(mx, Number(pt[0]) || 0);
          my = Math.max(my, Number(pt[1]) || 0);
        });
        return mx <= inferW * 1.05 && my <= inferH * 1.05;
      });

    this.boxCache = annotationBoxes
      .map((box) => {
        const poly = box.video_polygon;
        if (!Array.isArray(poly) || poly.length < 3) return null;
        let inferPts;
        if (boxesAlreadyInfer) {
          inferPts = poly.map((pt) => [Number(pt[0]), Number(pt[1])]);
        } else {
          const framePts = pl.resolvePolygonFramePoints(
            poly,
            box.video_polygon_norm,
            annSize,
            frameW,
            frameH
          );
          if (framePts.length < 3) return null;
          inferPts = framePts.map(([x, y]) => [
            (x * inferW) / Math.max(1, frameW),
            (y * inferH) / Math.max(1, frameH),
          ]);
        }
        const token = boxCollisionToken(box);
        return token ? { token, inferPts } : null;
      })
      .filter(Boolean);
    this.boxCacheKey = key;
    return this.boxCache;
  }

  update(frame, inferW, inferH) {
    const boxes = this.getBoxesInInferSpace(inferW, inferH);
    if (!boxes.length) return { collisions: [], alarm_collisions: [] };

    const frameIdx = Number(frame?.frame_idx ?? frame?.source_frame_idx ?? 0);
    const active = new Set();

    (frame?.persons || []).forEach((person) => {
      const kpts = person?.keypoints || [];
      for (const idx of [9, 10]) {
        const kp = kpts[idx];
        if (!kp || kp.length < 3 || Number(kp[2]) <= 0.3) continue;
        const wx = Number(kp[0]);
        const wy = Number(kp[1]);
        for (const { token, inferPts } of boxes) {
          if (pointInPolygon([wx, wy], inferPts)) {
            active.add(token);
            break;
          }
        }
      }
    });

    for (const token of this.consecutiveHits.keys()) {
      if (!active.has(token)) this.consecutiveHits.set(token, 0);
    }

    const alarms = [];
    active.forEach((token) => {
      const next = (this.consecutiveHits.get(token) || 0) + 1;
      this.consecutiveHits.set(token, next);
      const last = this.lastAlarmFrame.get(token) ?? -1e9;
      if (next >= this.minConsecutive && frameIdx - last >= this.cooldownFrames) {
        alarms.push(token);
        this.lastAlarmFrame.set(token, frameIdx);
      }
    });

    return {
      collisions: [...active],
      alarm_collisions: alarms,
    };
  }
}

let playbackCollisionTracker = null;

function resetPlaybackCollisionTracker() {
  playbackCollisionTracker = null;
}

function getPlaybackCollisionTracker() {
  if (!playbackCollisionTracker) {
    const cfg = getEffectiveCollisionConfig();
    playbackCollisionTracker = new PlaybackCollisionTracker(
      cfg.alarm_min_consecutive_frames,
      cfg.alarm_cooldown_frames
    );
  }
  return playbackCollisionTracker;
}

function getFrameCollisionSets(frame, inferW, inferH) {
  if (frameUsesStoredCollisions(frame)) {
    return {
      collisionSet: new Set(frame.collisions || []),
      alarmSet: new Set(frame.alarm_collisions || []),
    };
  }
  if (!annotationBoxes.length) {
    return { collisionSet: new Set(), alarmSet: new Set() };
  }
  const computed = getPlaybackCollisionTracker().update(frame, inferW, inferH);
  return {
    collisionSet: new Set(computed.collisions),
    alarmSet: new Set(computed.alarm_collisions),
  };
}

function getEffectiveAnnotationSize() {
  const { frameW, frameH } = getVideoFrameSize();
  let size = annotationSize;
  if (!size?.width || !size?.height) {
    if (frameByTime.length) {
      size = { width: frameByTime[0].w, height: frameByTime[0].h };
    } else {
      size = { width: frameW, height: frameH };
    }
  }
  // 旧版 pose JSON：boxes 已缩放到推理分辨率，但 annotation_size 仍是原始标注尺寸
  if (frameByTime.length && annotationBoxes.length && size?.width && size?.height) {
    const f0 = frameByTime[0];
    let maxX = 0;
    let maxY = 0;
    annotationBoxes.forEach((box) => {
      (box.video_polygon || []).forEach((pt) => {
        maxX = Math.max(maxX, Number(pt[0]) || 0);
        maxY = Math.max(maxY, Number(pt[1]) || 0);
      });
    });
    if (
      maxX <= f0.w * 1.05 &&
      maxY <= f0.h * 1.05 &&
      (size.width > f0.w * 1.15 || size.height > f0.h * 1.15)
    ) {
      return { width: f0.w, height: f0.h };
    }
  }
  return size;
}

function syncAnnotationBoxesFromPose() {
  const ann = poseData?.annotation;
  annotationBoxes = Array.isArray(ann?.boxes) ? ann.boxes : [];
  annotationSize = ann?.annotation_size || null;
}

function loadAnnotationBoxesFromData(data) {
  if (Array.isArray(data?.annotation?.boxes)) {
    annotationBoxes = data.annotation.boxes;
    annotationSize = data.annotation.annotation_size || data.annotation_size || null;
    resetPlaybackCollisionTracker();
    return;
  }
  annotationSize = data?.annotation_size || null;
  if (Array.isArray(data?.boxes)) {
    annotationBoxes = data.boxes;
    resetPlaybackCollisionTracker();
    return;
  }
  if (Array.isArray(data?.shelves)) {
    annotationBoxes = [];
    data.shelves.forEach((shelf) => {
      const code = String(shelf?.shelf_code || "").trim();
      (shelf?.boxes || []).forEach((b) => {
        annotationBoxes.push({ ...b, shelf_code: b.shelf_code || code });
      });
    });
    resetPlaybackCollisionTracker();
    return;
  }
  annotationBoxes = [];
  resetPlaybackCollisionTracker();
}

async function loadAnnotationBoxesFromFile(file) {
  const data = JSON.parse(await file.text());
  loadAnnotationBoxesFromData(data);
}

function buildFrameIndex(recordId = null) {
  frameByTime = [];
  resetFrameFetchState();
  resetPlaybackCollisionTracker();
  syncAnnotationBoxesFromPose();
  if (!poseData) return Promise.resolve();

  if ((poseData.schema || 1) >= 2 && recordId) {
    return fetch(recordApiUrl(recordId, "/timeline"))
      .then((res) => (res.ok ? res.json() : { timeline: [] }))
      .then((body) => {
        const inferW = poseData.infer_width || 640;
        const inferH = poseData.infer_height || 480;
        (body.timeline || []).forEach((row) => {
          frameByTime.push({
            t: row.timestamp_sec ?? 0,
            frameIdx: row.frame_idx,
            w: row.infer_width || inferW,
            h: row.infer_height || inferH,
          });
        });
        frameByTime.sort((a, b) => a.t - b.t);
      });
  }

  if (!poseData?.frames?.length) return Promise.resolve();
  poseData.frames.forEach((f) => {
    frameByTime.push({
      t: f.timestamp_sec ?? 0,
      frameIdx: f.frame_idx,
      frame: f,
      w: f.infer_width || 640,
      h: f.infer_height || 480,
    });
    if (f.frame_idx != null) frameCache.set(f.frame_idx, f);
  });
  frameByTime.sort((a, b) => a.t - b.t);
  return Promise.resolve();
}

function findFrameAt(timeSec) {
  if (!frameByTime.length) return null;
  let best = frameByTime[0];
  for (const item of frameByTime) {
    if (item.t <= timeSec) best = item;
    else break;
  }
  return best;
}

function syncCanvasSize() {
  const wrap = stageWrap || document.querySelector(".stage-wrap");
  if (!wrap) return { cw: 1, ch: 1 };
  const rect = wrap.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, Math.floor(rect.width));
  const cssH = Math.max(1, Math.floor(rect.height));
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { cw: cssW, ch: cssH };
}

function drawAnnotationBoxes(frame, inferW, inferH, collisionSets = null) {
  if (!annotationBoxes.length) return;
  const pl = window.previewLayout;
  if (!pl?.resolvePolygonFramePoints || !pl?.mapPointToDisplay) return;

  const { frameW, frameH } = getVideoFrameSize();
  const layout = getDisplayLayout();
  const annSize = getEffectiveAnnotationSize();
  const { collisionSet, alarmSet } =
    collisionSets || getFrameCollisionSets(frame, inferW, inferH);

  annotationBoxes.forEach((box) => {
    const poly = box.video_polygon;
    if (!Array.isArray(poly) || poly.length < 3) return;
    const framePts = pl.resolvePolygonFramePoints(
      poly,
      box.video_polygon_norm,
      annSize,
      frameW,
      frameH
    );
    if (framePts.length < 3) return;
    const token = boxCollisionToken(box);
    const isAlarm = alarmSet.has(token);
    const isHit = collisionSet.has(token);
    ctx.strokeStyle = isAlarm ? "rgba(255, 71, 87, 0.95)" : isHit ? "rgba(255, 209, 102, 0.95)" : "rgba(0, 255, 0, 0.35)";
    ctx.lineWidth = isAlarm || isHit ? 2.5 : 1.5;
    ctx.beginPath();
    framePts.forEach(([x, y], i) => {
      const [dx, dy] = pl.mapPointToDisplay(x, y, layout);
      if (i === 0) ctx.moveTo(dx, dy);
      else ctx.lineTo(dx, dy);
    });
    ctx.closePath();
    ctx.stroke();
  });
}

function drawSkeleton(frame, inferW, inferH, collisionSets = null) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  drawAnnotationBoxes(frame, inferW, inferH, collisionSets);
  if (!frame?.persons?.length) return;

  const layout = getDisplayLayout();

  frame.persons.forEach((person) => {
    const kpts = person.keypoints || [];
    COCO_LINES.forEach(([a, b]) => {
      const pa = kpts[a];
      const pb = kpts[b];
      if (!pa || !pb || pa[2] < SCORE_MIN || pb[2] < SCORE_MIN) return;
      const [x1, y1] = mapInferToDisplay(pa[0], pa[1], inferW, inferH, layout);
      const [x2, y2] = mapInferToDisplay(pb[0], pb[1], inferW, inferH, layout);
      ctx.strokeStyle = "rgba(34, 211, 238, 0.9)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    });
    kpts.forEach((kp, i) => {
      if (!kp || kp[2] < SCORE_MIN) return;
      const [x, y] = mapInferToDisplay(kp[0], kp[1], inferW, inferH, layout);
      ctx.fillStyle = ["#22d3ee", "#a78bfa", "#f472b6"][i % 3];
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fill();
    });
  });
}

function redrawCurrentFrame() {
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  if (videoEl.src && videoEl.readyState >= 1) {
    void renderAtTime(videoEl.currentTime);
  } else if (frameByTime.length) {
    void renderFrameEntry(frameByTime[0]);
  }
}

async function renderFrameEntry(hit, renderGen) {
  if (!hit) return;
  const frame = hit.frame || (await ensureFrame(hit.frameIdx));
  if (renderGen != null && renderGen !== renderGeneration) return;
  if (!frame) return;
  if (hit.frameIdx === lastRenderedFrameIdx) return;
  lastRenderedFrameIdx = hit.frameIdx;
  tickPoseFrameIdx = hit.frameIdx;
  const collisionSets = getFrameCollisionSets(frame, hit.w, hit.h);
  drawSkeleton(frame, hit.w, hit.h, collisionSets);
  const { collisionSet, alarmSet } = collisionSets;
  if (collisionSet.size || alarmSet.size) {
    const c = [...collisionSet].join(", ") || "—";
    const a = [...alarmSet].join(", ") || "—";
    timeLabel.title = `碰撞: ${c} | 报警: ${a}`;
  } else {
    timeLabel.title = annotationBoxes.length ? "无碰撞" : "";
  }
}

async function renderAtTime(timeSec) {
  const gen = ++renderGeneration;
  const hit = findFrameAt(timeSec);
  if (!hit) {
    if (gen !== renderGeneration) return;
    lastRenderedFrameIdx = -1;
    const { cw, ch } = syncCanvasSize();
    ctx.clearRect(0, 0, cw, ch);
    return;
  }
  if ((poseData?.schema || 1) >= 2 && hit.frameIdx != null) {
    await ensureFrameChunkLoaded(hit.frameIdx);
  }
  if (gen !== renderGeneration) return;
  await renderFrameEntry(hit, gen);
}

function tick() {
  if (videoEl.readyState >= 2) {
    const hit = findFrameAt(videoEl.currentTime);
    const nextIdx = hit?.frameIdx ?? -1;
    // 仅当骨架帧变化时触发绘制；tickPoseFrameIdx 在 renderFrameEntry 成功后再更新
    if (nextIdx >= 0 && nextIdx !== tickPoseFrameIdx) {
      void renderAtTime(videoEl.currentTime);
    }
  }
  if (videoEl.duration && Number.isFinite(videoEl.duration)) {
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
  }
  rafId = requestAnimationFrame(tick);
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

let jsonOnlyTimer = null;

function stopPlayback() {
  videoEl.pause();
  clearInterval(jsonOnlyTimer);
  jsonOnlyTimer = null;
  cancelAnimationFrame(rafId);
  rafId = null;
}

function finishPlaybackSession() {
  stopPlayback();
  cleanupPlaybackVideo();
  clearVideoElement();
  setPlaybackInfo("回放已停止。");
}

function startJsonOnlyPlayback() {
  if (!frameByTime.length) return;
  const fps = poseData.fps || 15;
  let idx = 0;
  clearInterval(jsonOnlyTimer);
  videoEl.style.display = "none";

  jsonOnlyTimer = setInterval(async () => {
    if (idx >= frameByTime.length) idx = 0;
    await renderFrameEntry(frameByTime[idx]);
    seekBar.value = String((idx / frameByTime.length) * 1000);
    timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    idx += 1;
  }, 1000 / fps);
}

$("#playback-json").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await cleanupPlaybackVideo();
  clearVideoElement();
  poseData = JSON.parse(await file.text());
  currentRecordId = null;
  await buildFrameIndex();
  await loadPlaybackEvents(null);
  $("#playback-annotation").value = "";
  const f0 = frameByTime[0];
  setPlaybackInfo(
    `已导入 ${file.name}，${poseData.frame_count ?? poseData.frames?.length ?? 0} 帧` +
      (f0 ? `（推理 ${f0.w}×${f0.h}）` : "") +
      "。请上传配套视频后播放。"
  );
  redrawCurrentFrame();
});

$("#playback-annotation").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    await loadAnnotationBoxesFromFile(file);
    await loadPlaybackEvents(currentRecordId);
    if (playbackEvents.length) await beginEventReview();
    const rtNote = playbackEventsFromRealtime ? "，已生成回放事件列表" : "";
    setPlaybackInfo(`已导入标注 ${file.name}，${annotationBoxes.length} 个货框${rtNote}`);
    redrawCurrentFrame();
  } catch (err) {
    setPlaybackInfo(`❌ 标注 JSON 无效: ${err.message}`);
  }
});

$("#playback-video").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await cleanupPlaybackVideo();
  stopPlayback();

  playbackVideoObjectUrl = URL.createObjectURL(file);
  videoEl.src = playbackVideoObjectUrl;
  videoEl.style.display = "block";
  videoEl.load();

  const onMeta = () => {
    videoEl.removeEventListener("loadedmetadata", onMeta);
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `视频 ${frameW}×${frameH} 已加载。`;
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理分辨率为 ${f0.w}×${f0.h}，将按视频画面自动对齐。`;
    }
    hint += " 点击播放。";
    setPlaybackInfo(hint);
    redrawCurrentFrame();
  };
  videoEl.addEventListener("loadedmetadata", onMeta);
});

$("#play-btn").addEventListener("click", async () => {
  if (videoEl.src) {
    videoEl.style.display = "block";
    try {
      await videoEl.play();
    } catch (err) {
      setPlaybackInfo(`播放失败: ${err.message}（可点击视频控件或检查格式）`);
      return;
    }
    cancelAnimationFrame(rafId);
    tickPoseFrameIdx = -1;
    resetPlaybackCollisionTracker();
    tick();
  } else if (poseData) {
    startJsonOnlyPlayback();
  } else {
    setPlaybackInfo("请先导入 JSON");
  }
});

$("#pause-btn").addEventListener("click", () => {
  stopPlayback();
});

$("#end-playback-btn").addEventListener("click", () => {
  finishPlaybackSession();
});

videoEl.addEventListener("ended", () => {
  stopPlayback();
  if (videoEl.src && videoEl.src.startsWith("blob:")) {
    cleanupPlaybackVideo();
    videoEl.removeAttribute("src");
    videoEl.load();
    setPlaybackInfo("播放结束。可重新选择视频。");
  } else {
    setPlaybackInfo("播放结束。可再次点击播放。");
  }
});

videoEl.addEventListener("loadedmetadata", () => {
  syncCanvasSize();
  redrawCurrentFrame();
  renderEventMarkers();
});

eventFilterSelect?.addEventListener("change", () => {
  const first = filteredPlaybackEvents()[0];
  if (first) void seekToEvent(first);
  else renderEventReviewList();
});

function initEventReviewControls() {
  $("#event-prev-btn")?.addEventListener("click", () => navigateReviewEvent(-1));
  $("#event-skip-next-btn")?.addEventListener("click", () => void skipToNextEvent());
  $("#event-mark-true-next-btn")?.addEventListener("click", () => void confirmTrueAndNext());
  $("#event-unmark-btn")?.addEventListener("click", () => void markActiveEventVerified(false));
  $("#event-review-complete-btn")?.addEventListener("click", () => void markEventReviewCompleted());

  $("#event-review-list-details")?.addEventListener("toggle", (e) => {
    if (e.target.open) renderEventReviewTable();
  });

  document.addEventListener("keydown", (e) => {
    if (!panels.playback?.classList.contains("active")) return;
    const tag = (e.target?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || e.target?.isContentEditable) {
      return;
    }
    if (!playbackEvents.length) return;
    if (e.key === "y" || e.key === "Y") {
      e.preventDefault();
      void confirmTrueAndNext();
    } else if (e.key === "n" || e.key === "N" || e.key === "j" || e.key === "J") {
      e.preventDefault();
      void skipToNextEvent();
    } else if (e.key === "u" || e.key === "U") {
      e.preventDefault();
      void markActiveEventVerified(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      navigateReviewEvent(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      navigateReviewEvent(-1);
    }
  });
}

videoEl.addEventListener("seeked", () => {
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  resetPlaybackCollisionTracker();
  void renderAtTime(videoEl.currentTime);
});

window.addEventListener("resize", () => {
  syncCanvasSize();
  redrawCurrentFrame();
});

window.addEventListener("beforeunload", () => {
  cleanupPlaybackVideo();
});

seekBar.addEventListener("input", async () => {
  activeEventKey = null;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  resetPlaybackCollisionTracker();
  if (!videoEl.duration || !Number.isFinite(videoEl.duration)) {
    const idx = Math.floor((seekBar.value / 1000) * frameByTime.length);
    const item = frameByTime[Math.min(idx, frameByTime.length - 1)];
    if (item) await renderFrameEntry(item);
    renderEventJumpList();
    return;
  }
  videoEl.currentTime = (seekBar.value / 1000) * videoEl.duration;
  await renderAtTime(videoEl.currentTime);
  renderEventJumpList();
});

bindStageLayoutWatch();
initEventReviewControls();
loadRecords();
initPlaybackRecordFilter();
void loadInferenceConfigDefaults();
void loadReflectionCameras();
updatePlaybackLoadButton();

$("#playback-load-record")?.addEventListener("click", () => {
  startPlaybackFromSelectedRecord().catch((err) => setPlaybackInfo(`❌ ${err.message}`));
});
