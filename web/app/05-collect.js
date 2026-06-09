/** 采集页：单视频与批处理 */
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

window.switchToTab = switchToTab;
window.openAnnotateForVideoStem = openAnnotateForVideoStem;

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

function isCollectSkeletonOnly() {
  return !!$("#collect-skeleton-only")?.checked;
}

function setCollectSkeletonOnlyUi() {
  const sk = isCollectSkeletonOnly();
  $("#collect-annotation-wrap")?.classList.toggle("hidden", sk);
  $("#collect-annotation-status")?.classList.toggle("hidden", sk);
  document.querySelector("fieldset.collision-config")?.classList.toggle("hidden", sk);
  if (sk && collectAnnotationStatus) {
    collectAnnotationStatus.innerHTML = "";
  }
}

async function refreshCollectAnnotationHint() {
  if (!collectAnnotationStatus) return;
  if (isCollectSkeletonOnly()) {
    collectAnnotationStatus.classList.remove("hidden");
    collectAnnotationStatus.innerHTML = "ℹ️ 仅计算骨架：无需标注 JSON，采集后回放列表将显示「碰撞未计算」。";
    return;
  }
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
$("#collect-skeleton-only")?.addEventListener("change", () => {
  setCollectSkeletonOnlyUi();
  void refreshCollectAnnotationHint();
});
setCollectSkeletonOnlyUi();

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
    skeleton_only: isCollectSkeletonOnly() ? "1" : "0",
    collision_method: collisionCfg.method || "wrist_point",
    collision_params: JSON.stringify(collisionCfg),
    alarm_min_consecutive_frames: String(collisionCfg.alarm_min_consecutive_frames ?? 3),
    alarm_cooldown_frames: String(collisionCfg.alarm_cooldown_frames ?? collisionCfg.cooldown_frames ?? 6),
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
  fd.append("skeleton_only", params.skeleton_only);
  fd.append("collision_method", params.collision_method);
  fd.append("collision_params", params.collision_params);
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
  const skeletonOnly = isCollectSkeletonOnly();
  let annCheck = { ok: true, source: "skeleton_only" };
  if (!skeletonOnly) {
    annCheck = await resolveCollectAnnotationSource(file, annFile);
    if (!annCheck.ok) {
      showStatus(`❌ ${annCheck.message}`, true);
      void refreshCollectAnnotationHint();
      return;
    }
  }

  const fd = new FormData();
  fd.append("file", file);
  const params = readCollectFormParams();
  appendCollectParams(fd, params);
  const cameraLabel = getCollectCameraLabel();
  if (!skeletonOnly) {
    if (annFile) fd.append("annotation", annFile);
    if (cameraLabel && annCheck.source === "camera") fd.append("camera_label", cameraLabel);
  } else if (cameraLabel) {
    fd.append("camera_label", cameraLabel);
  }

  collectBtn.disabled = true;
  const savingVideo = $("#collect-save-video").checked;
  const annNote = skeletonOnly
    ? "（仅骨架）"
    : annCheck.source === "camera"
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
    const annNoteResult = skeletonOnly
      ? " · 碰撞未计算"
      : body.annotation_auto
        ? " · 已关联已存标注 · 碰撞已落盘"
        : job.has_annotation || body.has_annotation
          ? " · 碰撞已落盘"
          : "";
    collectResult.innerHTML = `
      <p>✅ 已保存至 <code>localdata/json</code>${hasVideo ? " 与 <code>localdata/video</code>" : ""}${annNoteResult}，共 <strong>${job.frame_count ?? "?"}</strong> 帧</p>
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
  const skeletonOnly = isCollectSkeletonOnly();
  let batchCamera = getCollectCameraLabel();
  if (!skeletonOnly) {
    const annCheck = await resolveCollectAnnotationForBatch();
    if (!annCheck.ok) {
      showStatus(`❌ ${annCheck.message}`, true);
      return;
    }
    batchCamera = batchCamera || annCheck.camera || "";
  } else {
    if (!batchCamera) {
      showStatus("批处理仅骨架模式仍需选择机位标识（用于子目录）", true);
      return;
    }
  }

  const fd = new FormData();
  for (const f of files) {
    fd.append("files", f, f.webkitRelativePath || f.name);
  }
  fd.append("camera_label", batchCamera);
  const params = readCollectFormParams();
  appendCollectParams(fd, params);

  collectBatchBtn.disabled = true;
  collectBtn.disabled = true;
  showStatus(
    skeletonOnly
      ? `上传并批处理 ${files.length} 个视频（仅骨架）…`
      : `上传并批处理 ${files.length} 个视频…`
  );

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
