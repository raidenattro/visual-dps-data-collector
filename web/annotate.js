/** 货位标注：按模型 + 机位编辑 annotations/{编号}.json（首帧来自内置视频） */

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

function getAnnotatePoseTier() {
  return (ann$("#annotate-pose-tier")?.value || "rtmpose-t").trim();
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
  const annPart = (ctx.annotations || [])
    .map((a) => `${a.annotation_id}${a.has_file ? `（${a.box_count} 框）` : "（未创建）"}`)
    .join("、");
  const videoPart = ctx.has_video
    ? `首帧来源：<code>${ctx.video_dir}/${ctx.video_file}</code>`
    : "⚠️ 该机位下暂无内置视频，请先完成采集";
  el.innerHTML = `机位 <strong>${ctx.camera_label}</strong> → 标注 ${annPart || "—"} · ${videoPart}`;
}

function updateAnnotateVerifyPanel(info) {
  const el = document.getElementById("annotate-verify");
  if (!el) return;
  const {
    ok,
    poseTier,
    cameraLabel,
    annotationId,
    videoFile,
    frameLoaded,
    frameSize,
    annotationLoaded,
    boxCount,
    annSize,
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
      <dt>姿态模型</dt><dd>${poseTier || "—"}</dd>
      <dt>机位</dt><dd>${cameraLabel || "—"}</dd>
      <dt>货位标注</dt><dd>${annotationId ? `annotations/${annotationId}.json` : "—"} ${annotationLoaded ? `（${boxCount} 个货框）` : "（未加载）"}${annSize ? `；分辨率 ${annSize.w}×${annSize.h}` : ""}</dd>
      <dt>视频首帧</dt><dd>${frameLoaded ? `已显示 ${frameSize || ""}` : "未显示"}${videoFile ? ` · ${videoFile}` : ""}</dd>
    </dl>
  `;
  el.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetAnnotationState() {
  visualMode()?.reset();
  finalBoxes = [];
  loadedAnnotationSize = null;
  syncAnnotateCellPanel(null);
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
  if (pos) pos.textContent = `第 ${panel.row} 层 · 第 ${panel.col} 列`;
  if (input) {
    input.value = panel.value || "";
    input.placeholder = panel.defaultId || "";
  }
}

function showCanvasWithImage(dataUrl, w, h) {
  return new Promise((resolve, reject) => {
    const canvas = getAnnCanvas();
    if (!canvas) {
      reject(new Error("标注画布未找到，请刷新页面"));
      return;
    }
    bgImage = new Image();
    bgImage.onload = () => {
      canvas.width = w || bgImage.naturalWidth || bgImage.width;
      canvas.height = h || bgImage.naturalHeight || bgImage.height;
      frameWidth = canvas.width;
      frameHeight = canvas.height;
      canvas.classList.remove("hidden");
      visualMode()?.ensureDefaultShelf(frameWidth, frameHeight);
      renderAnnotator();
      resolve({ width: frameWidth, height: frameHeight });
    };
    bgImage.onerror = () => reject(new Error("首帧图片加载失败"));
    bgImage.src = dataUrl;
  });
}

async function loadFirstFrameOntoCanvas(dataUrl, width, height) {
  ensureAnnotatePanelVisible();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  await showCanvasWithImage(dataUrl, width, height);
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

async function loadExistingAnnotation(annotationId) {
  if (!annotationId) return null;
  try {
    const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annotationId)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchAnnotateContext(poseTier, camera) {
  const qs = new URLSearchParams({ pose_tier: poseTier, camera });
  const res = await fetch(`/api/annotate/context?${qs}`);
  if (!res.ok) throw new Error(await readApiErrorDetail(res));
  return res.json();
}

async function fetchAnnotateFrame(poseTier, camera) {
  const qs = new URLSearchParams({ pose_tier: poseTier, camera });
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
    const suffix = item.has_file ? `${item.box_count} 框` : "未创建";
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
  const poseTier = getAnnotatePoseTier();
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
    ctx = await fetchAnnotateContext(poseTier, camera);
  } catch (err) {
    notifyAnnotateFailure(err.message || String(err));
    return;
  }
  if (token !== annotateLoadToken) return;

  annotateContext = ctx;
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

  setAnnotateStatus("正在加载视频首帧…");
  try {
    const frame = await fetchAnnotateFrame(poseTier, camera);
    if (token !== annotateLoadToken) return;
    frameSourceVideo = frame.video_file || "";
    currentSourceVideo = frameSourceVideo;
    await loadFirstFrameOntoCanvas(
      `data:image/jpeg;base64,${frame.image}`,
      frame.width,
      frame.height
    );
    frameLoaded = true;
  } catch (err) {
    verifyErrors.push(`首帧：${err.message || err}`);
  }

  try {
    const existing = await loadExistingAnnotation(annId);
    if (token !== annotateLoadToken) return;
    if (existing) {
      annotationLoaded = true;
      boxCount = countAnnotationBoxes(existing);
      const src = String(existing?.source_info?.source_video || "").trim();
      if (src) currentSourceVideo = src;
      currentShelfCode =
        String(
          existing?.shelves?.[0]?.shelf_code || existing?.source_info?.shelf_code || annId || "SHELF_1"
        ).trim() || annId;
      applyAnnotationPayload(existing);
    } else {
      currentShelfCode = annId;
      verifyErrors.push(`标注文件 annotations/${annId}.json 尚未创建，保存后将新建`);
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
      `✅ 已加载 <code>annotations/${annId}.json</code>（${boxCount} 个货框），请对照首帧检查货位。`
    );
  } else if (frameLoaded && annotationLoaded) {
    setAnnotateStatus(
      `⚠️ 首帧已显示，标注已读入。请点击「生成货位」以显示可编辑货位（${boxCount} 个货框数据）。`,
      true
    );
  } else if (frameLoaded) {
    setAnnotateStatus(
      `首帧已加载：拖动绿色货架角点，设置行列后点「生成货位」。保存将写入 <code>annotations/${annId}.json</code>。`
    );
  } else if (annotationLoaded) {
    setAnnotateStatus(`⚠️ 已读取标注，但首帧未显示：${verifyErrors[0] || "请确认该机位已采集视频"}`, true);
  } else {
    notifyAnnotateFailure(verifyErrors[0] || "首帧与标注均未加载", { alertUser: !frameLoaded });
  }

  if (annSize && frameWidth && (annSize.w !== frameWidth || annSize.h !== frameHeight)) {
    verifyErrors.push(`标注分辨率 ${annSize.w}×${annSize.h} 与首帧 ${frameWidth}×${frameHeight} 不同，已按比例映射坐标`);
  }

  updateAnnotateVerifyPanel({
    ok: previewReady,
    poseTier,
    cameraLabel: camera,
    annotationId: annId,
    videoFile: frameSourceVideo,
    frameLoaded,
    frameSize,
    annotationLoaded,
    boxCount,
    annSize,
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
  setAnnotateStatus("正在保存…");
  const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(formatApiDetail(body.detail) || body.detail || res.statusText);
  const savedId = body.video_stem || annId;
  currentAnnotationId = savedId;
  setAnnotateStatus(
    `✅ ${body.message || "已保存"}：<code>annotations/${savedId}.json</code>（${body.box_count ?? finalBoxes.length} 个货框），全局生效。`
  );
  if (annotateContext) {
    const item = (annotateContext.annotations || []).find((a) => a.annotation_id === savedId);
    if (item) {
      item.has_file = true;
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

  ann$("#annotate-pose-tier")?.addEventListener("change", () => {
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

  ann$("#annotate-download")?.addEventListener("click", async () => {
    const annId = getAnnotateAnnotationId() || currentAnnotationId;
    if (!annId) {
      setAnnotateStatus("请选择货位标注", true);
      return;
    }
    try {
      const res = await fetch(`/api/annotations/by-video/${encodeURIComponent(annId)}`);
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
