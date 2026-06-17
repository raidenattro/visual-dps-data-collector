/** 货位标注：按标注来源 + 机位编辑；母本只读，模型层保存至 json/{tier}/annotations */

function getAnnotateAnnotationSource() {
  return (ann$("#annotate-annotation-source")?.value || "rtmpose-t").trim();
}

function isAnnotateMasterSource() {
  return getAnnotateAnnotationSource() === "master";
}

function annotateSourceLabel(src) {
  const s = String(src || "").trim();
  return s === "master" ? "母本" : s;
}

function annotateSaveDirForContext(ctx) {
  if (!ctx) return "json/annotations";
  if (ctx.annotation_save_dir) return ctx.annotation_save_dir;
  const src = ctx.annotation_source || getAnnotateAnnotationSource();
  return src === "master" ? "json/annotations" : `json/${src}/annotations`;
}

function syncAnnotateSaveUi(ctx) {
  const readonly = ctx?.annotation_readonly ?? isAnnotateMasterSource();
  const saveBtn = ann$("#annotate-save");
  if (saveBtn) {
    saveBtn.disabled = readonly;
    saveBtn.title = readonly ? "母本只读，请选择模型层（rtmpose-t/s/m）后保存" : "";
  }
}

const getAnnCanvas = () => document.getElementById("annotate-canvas");
const getAnnCtx = () => {
  const c = getAnnCanvas();
  return c ? c.getContext("2d") : null;
};

let bgImage = new Image();
let gridRows = 4;
let gridCols = 4;
let finalBoxes = [];
let unbindVisualCanvas = null;
let currentAnnotationId = "";
let currentSourceVideo = "";
let frameWidth = 0;
let frameHeight = 0;
let loadedAnnotationSize = null;
let currentShelfCode = "SHELF_1";
let annotateContext = null;
let annotateLoadToken = 0;
let annotateFrameIndex = 0;
let annotateFrameCount = 0;
let annotateVideoFile = "";
let annotateVideoNames = [];
let annotateVideoIndex = 0;

const previewLayoutApi = window.previewLayout || {};
const mapPtsToVideoFrame = previewLayoutApi.mapPointsToVideoFrame;
const isNormPolyValid = previewLayoutApi.isNormPolygonValid;

/** loadShelf 只传 (points, norm)，此处绑定标注分辨率与当前首帧尺寸 */
function createFramePointMapper(annotationSize, frameW, frameH) {
  return (points, normPolygon) =>
    mapPtsToVideoFrame(points, normPolygon, annotationSize, frameW, frameH);
}

function visualMode() {
  return window.AnnotateVisualMode;
}

function ann$(sel) {
  return document.querySelector(sel);
}

function getAnnotateCamera() {
  return (ann$("#annotate-camera")?.value || "").trim();
}

function getAnnotateAnnotationId() {
  return (ann$("#annotate-annotation-id")?.value || currentAnnotationId || "").trim();
}

function setAnnotateStatus(html, isError = false) {
  const el = ann$("#annotate-status");
  if (!el) return;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
  else el.classList.remove("error");
  el.innerHTML = html;
  requestAnimationFrame(() => {
    el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function hideAnnotateStatus() {
  ann$("#annotate-status")?.classList.add("hidden");
}

function formatApiDetail(detail) {
  if (detail == null) return "";
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d === "object" && d?.msg ? d.msg : String(d)))
      .filter(Boolean)
      .join("; ");
  }
  return String(detail);
}

async function readApiErrorDetail(res) {
  try {
    const body = await res.json();
    return formatApiDetail(body.detail) || res.statusText;
  } catch {
    return res.statusText;
  }
}

function notifyAnnotateFailure(message, { alertUser = true } = {}) {
  const msg = String(message || "未知错误").trim() || "未知错误";
  console.error("[annotate]", msg);
  setAnnotateStatus(`❌ ${msg}`, true);
  if (alertUser) {
    window.alert(`标注页加载失败：\n\n${msg}`);
  }
}

function ensureAnnotatePanelVisible() {
  const panel = document.getElementById("panel-annotate");
  if (panel?.classList.contains("active")) return;
  const tab = document.querySelector('.tab[data-tab="annotate"]');
  if (tab) tab.click();
}

function countAnnotationBoxes(data) {
  if (!data) return 0;
  if (Array.isArray(data.boxes)) return data.boxes.length;
  let n = 0;
  (data.shelves || []).forEach((s) => {
    n += Array.isArray(s?.boxes) ? s.boxes.length : 0;
  });
  return n;
}

function updateAnnotateContextHint(ctx) {
  const el = ann$("#annotate-context-hint");
  if (!el) return;
  if (!ctx) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.classList.remove("hidden");
  const srcLabel = annotateSourceLabel(ctx.annotation_source);
  const saveDir = annotateSaveDirForContext(ctx);
  const annPart = (ctx.annotations || [])
    .map((a) => {
      let tag = a.has_file ? `${a.box_count} 框` : "未创建";
      if (a.resolved_from === "master" && ctx.annotation_source !== "master" && !a.has_tier_file) {
        tag += "（母本）";
      } else if (a.has_tier_file) {
        tag += "（模型层）";
      }
      return `${a.annotation_id} · ${tag}`;
    })
    .join("、");
  const videoTier = ctx.video_pose_tier || "rtmpose-t";
  const videoPart = ctx.has_video
    ? `首帧来源：<code>${ctx.video_dir}/${ctx.video_file}</code>（视频层 ${videoTier}）`
    : "⚠️ 该机位下暂无内置视频，请先完成采集";
  const ro = ctx.annotation_readonly ? " · <strong>母本只读</strong>" : ` · 保存至 <code>${saveDir}/</code>`;
  el.innerHTML = `标注来源 <strong>${srcLabel}</strong>${ro} · 机位 <strong>${ctx.camera_label}</strong> → ${annPart || "—"} · ${videoPart}`;
}

function updateAnnotateVerifyPanel(info) {
  const el = document.getElementById("annotate-verify");
  if (!el) return;
  const {
    ok,
    annotationSource,
    saveDir,
    readonly,
    cameraLabel,
    annotationId,
    videoFile,
    frameLoaded,
    frameSize,
    framePosLabel = "",
    annotationLoaded,
    boxCount,
    annSize,
    resolvedFrom,
    errors = [],
  } = info;
  el.classList.remove("hidden", "ok", "warn", "fail");
  if (ok) el.classList.add("ok");
  else if (frameLoaded || annotationLoaded) el.classList.add("warn");
  else el.classList.add("fail");

  const lines = [];
  if (ok) {
    lines.push("<strong>对照预览已就绪</strong>：首帧 + 标注网格已叠加，请目视货框是否与视频一致。");
  } else if (frameLoaded && !annotationLoaded) {
    lines.push("<strong>仅有首帧</strong>：该标注文件尚未创建，可拖动货架角点后「生成货位」新建。");
  } else if (!frameLoaded && annotationLoaded) {
    lines.push("<strong>仅有标注数据</strong>：无首帧背景，无法目视对照。");
  } else {
    lines.push("<strong>加载未完成</strong>：请根据下方明细排查。");
  }
  if (errors.length) {
    lines.push(`<p class="verify-err">${errors.map((e) => `• ${e}`).join("<br/>")}</p>`);
  }

  el.innerHTML = `
    ${lines.join("")}
    <dl>
      <dt>标注来源</dt><dd>${annotateSourceLabel(annotationSource) || "—"}${readonly ? "（只读）" : ""}</dd>
      <dt>机位</dt><dd>${cameraLabel || "—"}</dd>
      <dt>货位标注</dt><dd>${annotationId ? `<code>${saveDir || "json/annotations"}/${annotationId}.json</code>` : "—"} ${annotationLoaded ? `（${boxCount} 个货框${resolvedFrom === "master" && annotationSource !== "master" ? "，来自母本" : ""}）` : "（未加载）"}${annSize ? `；分辨率 ${annSize.w}×${annSize.h}` : ""}</dd>
      <dt>视频背景帧</dt><dd>${frameLoaded ? `已显示 ${frameSize || ""}` : "未显示"}${videoFile ? ` · ${videoFile}` : ""}${framePosLabel ? ` · ${framePosLabel}` : ""}</dd>
    </dl>
  `;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetAnnotationState() {
  visualMode()?.reset();
  finalBoxes = [];
  loadedAnnotationSize = null;
  annotateFrameIndex = 0;
  annotateFrameCount = 0;
  annotateVideoFile = "";
  annotateVideoNames = [];
  annotateVideoIndex = 0;
  syncAnnotateCellPanel(null);
  syncAnnotateFrameNavUi();
}

function syncGridFromInputs() {
  const r = Math.max(1, Math.min(8, Number(ann$("#annotate-rows")?.value) || 4));
  const c = Math.max(1, Math.min(8, Number(ann$("#annotate-cols")?.value) || 4));
  gridRows = r;
  gridCols = c;
  visualMode()?.setGridSize(r, c);
}

function syncAnnotateCellPanel(panel) {
  const wrap = document.getElementById("annotate-cell-panel");
  const pos = document.getElementById("annotate-cell-pos");
  const input = document.getElementById("annotate-box-id");
  if (!wrap) return;
  if (!panel) {
    wrap.classList.add("hidden");
    if (input) input.value = "";
    return;
  }
  wrap.classList.remove("hidden");
  if (pos) {
    pos.textContent = `第 ${panel.row} 层 · 第 ${panel.col} 列 · 已锁定编辑（仅调整此货位；点空白取消）`;
  }
  if (input) {
    input.value = panel.value || "";
    input.placeholder = panel.defaultId || "";
  }
}

function showCanvasWithImage(dataUrl, w, h, { preserveShelf = false } = {}) {
  return new Promise((resolve, reject) => {
    const canvas = getAnnCanvas();
    if (!canvas) {
      reject(new Error("标注画布未找到，请刷新页面"));
      return;
    }
    const prevW = frameWidth;
    const prevH = frameHeight;
    bgImage = new Image();
    bgImage.onload = () => {
      canvas.width = w || bgImage.naturalWidth || bgImage.width;
      canvas.height = h || bgImage.naturalHeight || bgImage.height;
      frameWidth = canvas.width;
      frameHeight = canvas.height;
      canvas.classList.remove("hidden");
      if (!preserveShelf) {
        visualMode()?.ensureDefaultShelf(frameWidth, frameHeight);
      } else if (prevW > 0 && prevH > 0 && (prevW !== frameWidth || prevH !== frameHeight)) {
        setAnnotateStatus(
          `⚠️ 新帧分辨率 ${frameWidth}×${frameHeight} 与当前 ${prevW}×${prevH} 不同，货框位置可能偏移`,
          true
        );
      }
      renderAnnotator();
      resolve({ width: frameWidth, height: frameHeight });
    };
    bgImage.onerror = () => reject(new Error("视频帧图片加载失败"));
    bgImage.src = dataUrl;
  });
}

async function loadFirstFrameOntoCanvas(dataUrl, width, height, options = {}) {
  ensureAnnotatePanelVisible();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  await showCanvasWithImage(dataUrl, width, height, options);
}

function applyAnnotateFrameMeta(frame) {
  annotateFrameIndex = Number(frame?.frame_index) || 0;
  annotateFrameCount = Number(frame?.frame_count) || 0;
  annotateVideoFile = frame?.video_file || "";
  annotateVideoNames = Array.isArray(frame?.video_names) ? frame.video_names : annotateVideoNames;
  annotateVideoIndex = Number(frame?.video_index) || 0;
  if (annotateVideoFile) currentSourceVideo = annotateVideoFile;
  syncAnnotateFrameNavUi();
}

function formatAnnotateFramePosLabel() {
  if (!annotateVideoFile && annotateFrameCount <= 0) return "";
  const framePart =
    annotateFrameCount > 0
      ? `第 ${annotateFrameIndex + 1}/${annotateFrameCount} 帧`
      : `第 ${annotateFrameIndex + 1} 帧`;
  const videoPart =
    annotateVideoNames.length > 1
      ? `视频 ${annotateVideoIndex + 1}/${annotateVideoNames.length}`
      : "";
  return [videoPart, framePart].filter(Boolean).join(" · ");
}

function syncAnnotateFrameNavUi() {
  const wrap = ann$("#annotate-frame-nav");
  const framePos = ann$("#annotate-frame-pos");
  const videoPos = ann$("#annotate-video-pos");
  const prevFrame = ann$("#annotate-prev-frame");
  const nextFrame = ann$("#annotate-next-frame");
  const prevVideo = ann$("#annotate-prev-video");
  const nextVideo = ann$("#annotate-next-video");
  const hasFrame = !!annotateVideoFile;
  if (wrap) wrap.classList.toggle("hidden", !hasFrame);
  if (framePos) {
    framePos.textContent = annotateFrameCount > 0
      ? `帧 ${annotateFrameIndex + 1} / ${annotateFrameCount}`
      : `帧 ${annotateFrameIndex + 1}`;
  }
  if (videoPos) {
    const name = annotateVideoFile || "—";
    videoPos.textContent =
      annotateVideoNames.length > 1
        ? `视频 ${annotateVideoIndex + 1}/${annotateVideoNames.length} · ${name}`
        : name;
  }
  if (prevFrame) prevFrame.disabled = !hasFrame || annotateFrameIndex <= 0;
  if (nextFrame) {
    nextFrame.disabled =
      !hasFrame || (annotateFrameCount > 0 && annotateFrameIndex >= annotateFrameCount - 1);
  }
  const multiVideo = annotateVideoNames.length > 1;
  if (prevVideo) prevVideo.disabled = !hasFrame || !multiVideo;
  if (nextVideo) nextVideo.disabled = !hasFrame || !multiVideo;
}

async function loadAnnotateVideoFrame(
  { frameIndex, videoFile, preserveAnnotation = false } = {},
  { token = annotateLoadToken } = {}
) {
  const annotationSource = getAnnotateAnnotationSource();
  const camera = getAnnotateCamera();
  if (!camera) {
    setAnnotateStatus("请选择机位", true);
    return false;
  }
  const frame = await fetchAnnotateFrame(annotationSource, camera, { frameIndex, videoFile });
  if (token !== annotateLoadToken) return false;
  applyAnnotateFrameMeta(frame);
  await loadFirstFrameOntoCanvas(
    `data:image/jpeg;base64,${frame.image}`,
    frame.width,
    frame.height,
    { preserveShelf: preserveAnnotation }
  );
  return true;
}

function parsePrimaryShelf(data) {
  if (Array.isArray(data?.shelves) && data.shelves.length) {
    const shelf =
      data.shelves.find((s) => s && String(s.shelf_code || "") === currentAnnotationId) ||
      data.shelves.find((s) => s && s.shelf_code) ||
      data.shelves[0];
    return shelf || null;
  }
  if (Array.isArray(data?.boxes) && data.boxes.length) {
    const code =
      String(
        data?.source_info?.shelf_code || data?.source_info?.video_stem || currentAnnotationId || "SHELF_1"
      ).trim() || "SHELF_1";
    return {
      shelf_code: code,
      shelf_name: "",
      shelf_corners: Array.isArray(data.shelf_corners) ? data.shelf_corners : [],
      grid_shape: Array.isArray(data.grid_shape) ? data.grid_shape : [],
      boxes: data.boxes,
    };
  }
  return null;
}

function applyAnnotationPayload(data) {
  if (data?.annotation_size) {
    loadedAnnotationSize = {
      width: Number(data.annotation_size.width) || 0,
      height: Number(data.annotation_size.height) || 0,
    };
  } else {
    loadedAnnotationSize = null;
  }

  const shelf = parsePrimaryShelf(data);
  if (!shelf) return false;

  currentShelfCode =
    String(shelf.shelf_code || currentAnnotationId || "SHELF_1").trim() || "SHELF_1";
  const vm = visualMode();
  if (!vm) return false;

  vm.loadShelf(shelf, {
    mapPointsToFrame: createFramePointMapper(loadedAnnotationSize, frameWidth, frameHeight),
    isNormValid: isNormPolyValid,
    annotationSize: loadedAnnotationSize,
    frameWidth,
    frameHeight,
  });
  const gs = vm.getGridSize();
  gridRows = gs.rows;
  gridCols = gs.cols;
  ann$("#annotate-rows").value = String(gridRows);
  ann$("#annotate-cols").value = String(gridCols);
  syncAnnotateCellPanel(null);
  renderAnnotator();
  return vm.isGridReady();
}

async function loadExistingAnnotation(annotationId, { materialize = false } = {}) {
  if (!annotationId) return null;
  const src = getAnnotateAnnotationSource();
  try {
    const qs = new URLSearchParams({ annotation_source: src });
    if (materialize && src !== "master") qs.set("materialize", "1");
    const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annotationId)}?${qs}`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchAnnotateContext(annotationSource, camera) {
  const qs = new URLSearchParams({ annotation_source: annotationSource, camera });
  const res = await fetch(`/api/annotate/context?${qs}`);
  if (!res.ok) throw new Error(await readApiErrorDetail(res));
  return res.json();
}

async function fetchAnnotateFrame(annotationSource, camera, { frameIndex, videoFile } = {}) {
  const qs = new URLSearchParams({ annotation_source: annotationSource, camera });
  if (videoFile) qs.set("video_file", videoFile);
  if (frameIndex != null && Number(frameIndex) > 0) qs.set("frame_index", String(frameIndex));
  const res = await fetch(`/api/annotate/frame?${qs}`);
  if (!res.ok) throw new Error(await readApiErrorDetail(res));
  return res.json();
}

function populateAnnotationSelect(annotations, selectedId = "") {
  const sel = ann$("#annotate-annotation-id");
  if (!sel) return;
  const list = Array.isArray(annotations) ? annotations : [];
  sel.innerHTML = "";
  if (!list.length) {
    sel.disabled = true;
    sel.innerHTML = '<option value="">— 无机位标注 —</option>';
    return;
  }
  sel.disabled = false;
  list.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.annotation_id;
    let suffix = item.has_file ? `${item.box_count} 框` : "未创建";
    if (item.resolved_from === "master" && !item.has_tier_file && getAnnotateAnnotationSource() !== "master") {
      suffix += " · 母本";
    }
    opt.textContent = `${item.annotation_id} · ${suffix}`;
    sel.appendChild(opt);
  });
  const pick =
    selectedId && list.some((a) => a.annotation_id === selectedId)
      ? selectedId
      : list[0].annotation_id;
  sel.value = pick;
  currentAnnotationId = pick;
}

async function loadAnnotateCameras() {
  const sel = ann$("#annotate-camera");
  if (!sel) return;
  try {
    const res = await fetch("/api/reflection/cameras");
    if (!res.ok) throw new Error(await readApiErrorDetail(res));
    const body = await res.json();
    const cameras = Array.isArray(body.cameras) ? body.cameras : [];
    sel.innerHTML = '<option value="">— 请选择机位 —</option>';
    cameras.forEach((cam) => {
      const opt = document.createElement("option");
      opt.value = cam;
      opt.textContent = cam;
      sel.appendChild(opt);
    });
  } catch (err) {
    setAnnotateStatus(`❌ 机位列表加载失败：${err.message}`, true);
  }
}

async function loadAnnotateSession({ annotationId = "" } = {}) {
  const annotationSource = getAnnotateAnnotationSource();
  const camera = getAnnotateCamera();
  if (!camera) {
    setAnnotateStatus("请选择机位", true);
    return;
  }

  const token = ++annotateLoadToken;
  resetAnnotationState();
  setAnnotateStatus("正在加载机位上下文…");

  let ctx;
  try {
    ctx = await fetchAnnotateContext(annotationSource, camera);
  } catch (err) {
    notifyAnnotateFailure(err.message || String(err));
    return;
  }
  if (token !== annotateLoadToken) return;

  annotateContext = ctx;
  syncAnnotateSaveUi(ctx);
  populateAnnotationSelect(ctx.annotations, annotationId || getAnnotateAnnotationId());
  updateAnnotateContextHint(ctx);

  const annId = getAnnotateAnnotationId();
  if (!annId) {
    setAnnotateStatus("该机位无 reflection 标注编号", true);
    return;
  }
  currentAnnotationId = annId;

  const verifyErrors = [];
  let frameLoaded = false;
  let frameSourceVideo = "";
  let annotationLoaded = false;
  let boxCount = 0;
  let resolvedFrom = "none";
  const saveDir = annotateSaveDirForContext(ctx);
  const readonly = !!ctx.annotation_readonly;

  setAnnotateStatus("正在加载视频帧…");
  try {
    const frame = await fetchAnnotateFrame(annotationSource, camera);
    if (token !== annotateLoadToken) return;
    applyAnnotateFrameMeta(frame);
    frameSourceVideo = frame.video_file || "";
    await loadFirstFrameOntoCanvas(
      `data:image/jpeg;base64,${frame.image}`,
      frame.width,
      frame.height
    );
    frameLoaded = true;
  } catch (err) {
    verifyErrors.push(`视频帧：${err.message || err}`);
    syncAnnotateFrameNavUi();
  }

  try {
    const materialize = annotationSource !== "master";
    const existing = await loadExistingAnnotation(annId, { materialize });
    if (token !== annotateLoadToken) return;
    if (existing) {
      annotationLoaded = true;
      boxCount = countAnnotationBoxes(existing);
      resolvedFrom = existing?._meta?.resolved_from || "tier";
      const src = String(existing?.source_info?.source_video || "").trim();
      if (src) currentSourceVideo = src;
      currentShelfCode =
        String(
          existing?.shelves?.[0]?.shelf_code || existing?.source_info?.shelf_code || annId || "SHELF_1"
        ).trim() || annId;
      applyAnnotationPayload(existing);
    } else {
      currentShelfCode = annId;
      verifyErrors.push(`标注文件 ${saveDir}/${annId}.json 尚未创建，保存后将新建`);
    }
  } catch (err) {
    verifyErrors.push(`标注叠加失败：${err.message || err}`);
  }

  const annSize =
    loadedAnnotationSize?.width > 0
      ? { w: loadedAnnotationSize.width, h: loadedAnnotationSize.height }
      : null;
  const frameSize = frameWidth && frameHeight ? `${frameWidth}×${frameHeight}` : "";
  const previewReady = frameLoaded && annotationLoaded && visualMode()?.isGridReady();

  if (previewReady) {
    setAnnotateStatus(
      `✅ 已加载 <code>${saveDir}/${annId}.json</code>（${boxCount} 个货框）${readonly ? "，母本只读" : ""}，请对照背景帧检查货位；有遮挡可点「下一帧」或「下个视频」。`
    );
  } else if (frameLoaded && annotationLoaded) {
    setAnnotateStatus(
      `⚠️ 背景帧已显示，标注已读入。请点击「生成货位」以显示可编辑货位（${boxCount} 个货框数据）。`,
      true
    );
  } else if (frameLoaded) {
    setAnnotateStatus(
      `背景帧已加载：若画面有遮挡可切换帧/视频；拖动绿色货架角点，设置行列后点「生成货位」。${readonly ? "母本只读。" : `保存将写入 <code>${saveDir}/${annId}.json</code>。`}`
    );
  } else if (annotationLoaded) {
    setAnnotateStatus(`⚠️ 已读取标注，但首帧未显示：${verifyErrors[0] || "请确认该机位已采集视频"}`, true);
  } else {
    notifyAnnotateFailure(verifyErrors[0] || "首帧与标注均未加载", { alertUser: !frameLoaded });
  }

  if (annSize && frameWidth && (annSize.w !== frameWidth || annSize.h !== frameHeight)) {
    verifyErrors.push(`标注分辨率 ${annSize.w}×${annSize.h} 与首帧 ${frameWidth}×${frameHeight} 不同，已按比例映射坐标`);
  }

  const framePosLabel = formatAnnotateFramePosLabel();
  updateAnnotateVerifyPanel({
    ok: previewReady,
    annotationSource,
    saveDir,
    readonly,
    cameraLabel: camera,
    annotationId: annId,
    videoFile: frameSourceVideo || annotateVideoFile,
    frameLoaded,
    frameSize,
    framePosLabel,
    annotationLoaded,
    boxCount,
    annSize,
    resolvedFrom,
    errors: verifyErrors,
  });

  if (frameLoaded || annotationLoaded) {
    renderAnnotator();
  }
}

function buildSavePayload() {
  syncGridFromInputs();
  const vm = visualMode();
  finalBoxes = vm ? vm.buildBoxes(frameWidth, frameHeight) : [];
  const gs = vm?.getGridSize() || { rows: gridRows, cols: gridCols };
  const annId = getAnnotateAnnotationId() || currentAnnotationId;
  const shelfCode = currentShelfCode || annId || "SHELF_1";
  return {
    annotation_size: { width: frameWidth, height: frameHeight },
    source_info: {
      capture_source: "video",
      video_stem: annId,
      source_video: currentSourceVideo || `${annId}.mp4`,
      shelf_code: shelfCode,
    },
    shelves: [
      {
        shelf_code: shelfCode,
        shelf_name: "",
        shelf_corners: vm ? vm.getShelfCorners() : [],
        grid_shape: [gs.rows, gs.cols],
        boxes: finalBoxes,
      },
    ],
  };
}

async function saveAnnotation() {
  const annId = getAnnotateAnnotationId() || currentAnnotationId;
  const src = getAnnotateAnnotationSource();
  if (src === "master") {
    setAnnotateStatus("母本目录只读，请选择模型层（rtmpose-t/s/m）后保存", true);
    return;
  }
  if (!annId) {
    setAnnotateStatus("请选择货位标注编号", true);
    return;
  }
  const vm = visualMode();
  if (!vm?.shelfCornersReady()) {
    setAnnotateStatus("请先标定货架四角（拖动绿色角点）", true);
    return;
  }
  if (!vm.isGridReady()) {
    setAnnotateStatus("请先点击「生成货位」", true);
    return;
  }
  if (!frameWidth || !frameHeight) {
    setAnnotateStatus("请先加载首帧（选择机位后会自动加载）", true);
    return;
  }
  currentAnnotationId = annId;
  const payload = buildSavePayload();
  const saveDir = annotateSaveDirForContext(annotateContext);
  setAnnotateStatus("正在保存…");
  const qs = new URLSearchParams({ annotation_source: src });
  const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annId)}?${qs}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatApiDetail(body.detail) || body.detail || res.statusText);
  const savedId = body.video_stem || annId;
  currentAnnotationId = savedId;
  setAnnotateStatus(
    `✅ ${body.message || "已保存"}：<code>${body.annotation_dir || saveDir}/${savedId}.json</code>（${body.box_count ?? finalBoxes.length} 个货框）。`
  );
  if (annotateContext) {
    const item = (annotateContext.annotations || []).find((a) => a.annotation_id === savedId);
    if (item) {
      item.has_file = true;
      item.has_tier_file = true;
      item.resolved_from = "tier";
      item.box_count = body.box_count ?? finalBoxes.length;
      updateAnnotateContextHint(annotateContext);
      populateAnnotationSelect(annotateContext.annotations, savedId);
    }
  }
}

function renderAnnotator() {
  const ctx = getAnnCtx();
  if (!ctx || !bgImage.complete) return;
  visualMode()?.render(ctx, bgImage);
}

function bindCanvasEvents() {
  if (unbindVisualCanvas) return;
  const canvas = getAnnCanvas();
  const vm = visualMode();
  if (!canvas || !vm) return;
  unbindVisualCanvas = vm.bindCanvas(canvas, {
    onSelectionChange: (panel) => {
      syncAnnotateCellPanel(panel);
      renderAnnotator();
    },
    onRender: () => renderAnnotator(),
  });
}

function restartGridAnnotation() {
  syncGridFromInputs();
  visualMode()?.reset();
  visualMode()?.ensureDefaultShelf(frameWidth, frameHeight);
  syncAnnotateCellPanel(null);
  setAnnotateStatus("已重置货架：拖动绿色角点 →「生成货位」→ 编辑货位。");
  renderAnnotator();
}

function applyGridSizeFromInputs() {
  syncGridFromInputs();
}

function confirmGenerateGrid() {
  syncGridFromInputs();
  const vm = visualMode();
  if (!vm) return;
  const result = vm.confirmGenerateGrid();
  if (!result.ok) {
    setAnnotateStatus(result.message, true);
    return;
  }
  setAnnotateStatus(result.message);
  renderAnnotator();
}

let annotatePanelInited = false;

function initAnnotatePanel() {
  if (annotatePanelInited) return;
  annotatePanelInited = true;
  bindCanvasEvents();
  loadAnnotateCameras();
  syncAnnotateSaveUi();

  ann$("#annotate-annotation-source")?.addEventListener("change", () => {
    syncAnnotateSaveUi();
    if (getAnnotateCamera()) loadAnnotateSession();
  });

  ann$("#annotate-camera")?.addEventListener("change", () => {
    loadAnnotateSession();
  });

  ann$("#annotate-annotation-id")?.addEventListener("change", () => {
    currentAnnotationId = getAnnotateAnnotationId();
    if (currentAnnotationId) loadAnnotateSession({ annotationId: currentAnnotationId });
  });

  ann$("#annotate-reload")?.addEventListener("click", async () => {
    try {
      await loadAnnotateSession({ annotationId: getAnnotateAnnotationId() });
    } catch (err) {
      notifyAnnotateFailure(err.message || String(err));
    }
  });

  ann$("#annotate-save")?.addEventListener("click", async () => {
    try {
      await saveAnnotation();
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });

  ann$("#annotate-restart")?.addEventListener("click", () => restartGridAnnotation());

  ann$("#annotate-confirm-grid")?.addEventListener("click", () => confirmGenerateGrid());

  ann$("#annotate-delete-cell")?.addEventListener("click", () => {
    const panel = visualMode()?.getSelectedCellPanel();
    if (!panel) {
      setAnnotateStatus("请先在画面上点击选中一个货位", true);
      return;
    }
    if (
      !window.confirm(`确定删除第 ${panel.row} 层 · 第 ${panel.col} 列货位？删除后可重新「生成货位」恢复。`)
    ) {
      return;
    }
    visualMode()?.deleteSelectedCell();
    syncAnnotateCellPanel(null);
    renderAnnotator();
    setAnnotateStatus("已删除货位。");
  });

  ann$("#annotate-box-id")?.addEventListener("input", (e) => {
    const panel = visualMode()?.getSelectedCellPanel();
    if (!panel) return;
    visualMode()?.setBoxId(panel.rowIdx, panel.colIdx, e.target.value);
    renderAnnotator();
  });

  ann$("#annotate-rows")?.addEventListener("change", applyGridSizeFromInputs);
  ann$("#annotate-cols")?.addEventListener("change", applyGridSizeFromInputs);

  ann$("#annotate-prev-frame")?.addEventListener("click", async () => {
    if (annotateFrameIndex <= 0) return;
    try {
      setAnnotateStatus("正在加载上一帧…");
      const ok = await loadAnnotateVideoFrame(
        { frameIndex: annotateFrameIndex - 1, videoFile: annotateVideoFile, preserveAnnotation: true },
        { token: annotateLoadToken }
      );
      if (ok) setAnnotateStatus(`已切换至 ${formatAnnotateFramePosLabel()}，货框标注保持不变。`);
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });

  ann$("#annotate-next-frame")?.addEventListener("click", async () => {
    if (annotateFrameCount > 0 && annotateFrameIndex >= annotateFrameCount - 1) {
      setAnnotateStatus("已是最后一帧", true);
      return;
    }
    try {
      setAnnotateStatus("正在加载下一帧…");
      const ok = await loadAnnotateVideoFrame(
        { frameIndex: annotateFrameIndex + 1, videoFile: annotateVideoFile, preserveAnnotation: true },
        { token: annotateLoadToken }
      );
      if (ok) setAnnotateStatus(`已切换至 ${formatAnnotateFramePosLabel()}，货框标注保持不变。`);
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });

  ann$("#annotate-prev-video")?.addEventListener("click", async () => {
    if (annotateVideoNames.length <= 1) return;
    const nextIdx =
      (annotateVideoIndex - 1 + annotateVideoNames.length) % annotateVideoNames.length;
    const nextFile = annotateVideoNames[nextIdx];
    try {
      setAnnotateStatus("正在加载上个视频…");
      const ok = await loadAnnotateVideoFrame(
        { frameIndex: 0, videoFile: nextFile, preserveAnnotation: true },
        { token: annotateLoadToken }
      );
      if (ok) setAnnotateStatus(`已切换至 ${formatAnnotateFramePosLabel()}，货框标注保持不变。`);
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });

  ann$("#annotate-next-video")?.addEventListener("click", async () => {
    if (annotateVideoNames.length <= 1) return;
    const nextIdx = (annotateVideoIndex + 1) % annotateVideoNames.length;
    const nextFile = annotateVideoNames[nextIdx];
    try {
      setAnnotateStatus("正在加载下个视频…");
      const ok = await loadAnnotateVideoFrame(
        { frameIndex: 0, videoFile: nextFile, preserveAnnotation: true },
        { token: annotateLoadToken }
      );
      if (ok) setAnnotateStatus(`已切换至 ${formatAnnotateFramePosLabel()}，货框标注保持不变。`);
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });

  ann$("#annotate-download")?.addEventListener("click", async () => {
    const annId = getAnnotateAnnotationId() || currentAnnotationId;
    if (!annId) {
      setAnnotateStatus("请选择货位标注", true);
      return;
    }
    try {
      const src = getAnnotateAnnotationSource();
      const qs = new URLSearchParams({ annotation_source: src });
      if (src !== "master") qs.set("materialize", "1");
      const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annId)}?${qs}`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${annId}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) {
      setAnnotateStatus(`❌ ${err.message}`, true);
    }
  });
}

function bootAnnotatePanel() {
  initAnnotatePanel();
}

window.initAnnotatePanel = initAnnotatePanel;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootAnnotatePanel);
} else {
  bootAnnotatePanel();
}
