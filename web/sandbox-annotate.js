/** 沙盒可视化货框标注（复用 AnnotateVisualMode，绑定 #sandbox-canvas） */

let sandboxBgImage = new Image();
let sandboxGridRows = 4;
let sandboxGridCols = 4;
let sandboxFrameWidth = 0;
let sandboxFrameHeight = 0;
let sandboxLoadedAnnotationSize = null;
let sandboxShelfCode = "SHELF_1";
let sandboxSourceRecordId = "";
let sandboxSourceVideo = "";
let sandboxVisualMounted = false;
let sandboxUnbindVisualCanvas = null;
let sandboxVisualLoadToken = 0;

const sandboxPreviewLayout = window.previewLayout || {};
const sandboxMapPtsToVideoFrame = sandboxPreviewLayout.mapPointsToVideoFrame;
const sandboxIsNormPolyValid = sandboxPreviewLayout.isNormPolygonValid;

function sandboxVisualMode() {
  return window.AnnotateVisualMode;
}

function sandboxAnn$(sel) {
  return document.querySelector(sel);
}

function sandboxCreateFramePointMapper(annotationSize, frameW, frameH) {
  return (points, normPolygon) =>
    sandboxMapPtsToVideoFrame(points, normPolygon, annotationSize, frameW, frameH);
}

function syncSandboxGridFromInputs() {
  const vm = sandboxVisualMode();
  if (!vm) return;
  sandboxGridRows = Math.max(1, Math.min(8, parseInt(sandboxAnn$("#sandbox-annotate-rows")?.value, 10) || 4));
  sandboxGridCols = Math.max(1, Math.min(8, parseInt(sandboxAnn$("#sandbox-annotate-cols")?.value, 10) || 4));
  vm.setGridSize(sandboxGridRows, sandboxGridCols);
}

function syncSandboxCellPanel(panel) {
  const wrap = sandboxAnn$("#sandbox-annotate-cell-panel");
  const pos = sandboxAnn$("#sandbox-annotate-cell-pos");
  const input = sandboxAnn$("#sandbox-annotate-box-id");
  if (!wrap) return;
  if (!panel) {
    wrap.classList.add("hidden");
    if (input) input.value = "";
    return;
  }
  wrap.classList.remove("hidden");
  if (pos) {
    pos.textContent = `第 ${panel.row} 层 · 第 ${panel.col} 列 · 点空白取消选中`;
  }
  if (input) {
    input.value = panel.value || "";
    input.placeholder = panel.defaultId || "";
  }
}

let sandboxCanvasResizeObserver = null;

/** 按容器尺寸放大/缩小画布显示（内部像素不变，便于手标） */
function fitSandboxCanvasDisplay() {
  const canvas = sandboxAnn$("#sandbox-canvas");
  const wrap = canvas?.closest(".sandbox-canvas-wrap");
  if (!canvas || !wrap) return;

  if (canvas.classList.contains("hidden") || !canvas.width || !canvas.height) {
    canvas.style.width = "";
    canvas.style.height = "";
    return;
  }

  const pad = 8;
  const availW = Math.max(120, wrap.clientWidth - pad);
  const availH = Math.max(120, wrap.clientHeight - pad);
  const scale = Math.min(availW / canvas.width, availH / canvas.height);
  canvas.style.width = `${Math.round(canvas.width * scale)}px`;
  canvas.style.height = `${Math.round(canvas.height * scale)}px`;
}

function scheduleFitSandboxCanvasDisplay() {
  requestAnimationFrame(() => {
    fitSandboxCanvasDisplay();
    requestAnimationFrame(fitSandboxCanvasDisplay);
  });
}

function ensureSandboxCanvasResizeObserver() {
  if (ensureSandboxCanvasResizeObserver._done) return;
  ensureSandboxCanvasResizeObserver._done = true;
  const wrap = sandboxAnn$(".sandbox-canvas-wrap");
  if (wrap && typeof ResizeObserver !== "undefined") {
    sandboxCanvasResizeObserver = new ResizeObserver(() => fitSandboxCanvasDisplay());
    sandboxCanvasResizeObserver.observe(wrap);
  }
  window.addEventListener("resize", fitSandboxCanvasDisplay);
}

function renderSandboxAnnotator() {
  const canvas = sandboxAnn$("#sandbox-canvas");
  if (!canvas || !sandboxBgImage.complete) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  sandboxVisualMode()?.render(ctx, sandboxBgImage);
}

function unmountSandboxVisualAnnotate() {
  if (typeof sandboxUnbindVisualCanvas === "function") {
    sandboxUnbindVisualCanvas();
    sandboxUnbindVisualCanvas = null;
  }
  sandboxVisualMode()?.reset();
  sandboxVisualMounted = false;
  sandboxBgImage = new Image();
  sandboxFrameWidth = 0;
  sandboxFrameHeight = 0;
  const canvas = sandboxAnn$("#sandbox-canvas");
  if (canvas) {
    canvas.classList.add("hidden");
    canvas.style.width = "";
    canvas.style.height = "";
    const ctx = canvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  syncSandboxCellPanel(null);
}

function mountSandboxVisualCanvas() {
  if (typeof window.releaseAnnotateVisualCanvas === "function") {
    window.releaseAnnotateVisualCanvas();
  }
  if (typeof sandboxUnbindVisualCanvas === "function") {
    sandboxUnbindVisualCanvas();
    sandboxUnbindVisualCanvas = null;
  }
  const canvas = sandboxAnn$("#sandbox-canvas");
  const vm = sandboxVisualMode();
  if (!canvas || !vm) return;
  sandboxUnbindVisualCanvas = vm.bindCanvas(canvas, {
    onSelectionChange: (panel) => {
      syncSandboxCellPanel(panel);
      renderSandboxAnnotator();
    },
    onRender: () => renderSandboxAnnotator(),
  });
  sandboxVisualMounted = true;
}

function showSandboxCanvasWithImage(dataUrl, w, h, { preserveShelf = false, skipDefaultShelf = false } = {}) {
  return new Promise((resolve, reject) => {
    const canvas = sandboxAnn$("#sandbox-canvas");
    if (!canvas) {
      reject(new Error("沙盒画布未找到"));
      return;
    }
    sandboxBgImage = new Image();
    sandboxBgImage.onload = () => {
      canvas.width = w || sandboxBgImage.naturalWidth || sandboxBgImage.width;
      canvas.height = h || sandboxBgImage.naturalHeight || sandboxBgImage.height;
      sandboxFrameWidth = canvas.width;
      sandboxFrameHeight = canvas.height;
      canvas.classList.remove("hidden");
      const vm = sandboxVisualMode();
      if (!preserveShelf && !skipDefaultShelf) {
        vm?.ensureDefaultShelf(sandboxFrameWidth, sandboxFrameHeight);
        vm?.setShelfOutlineVisible(true);
        vm?.setCellRenderHighContrast(false);
      }
      renderSandboxAnnotator();
      scheduleFitSandboxCanvasDisplay();
      resolve({ width: sandboxFrameWidth, height: sandboxFrameHeight });
    };
    sandboxBgImage.onerror = () => reject(new Error("视频帧图片加载失败"));
    sandboxBgImage.src = dataUrl;
  });
}

function sandboxParsePrimaryShelf(data) {
  const annId = sandboxShelfCode || sandboxSourceRecordId || "SHELF_1";
  if (Array.isArray(data?.shelves) && data.shelves.length) {
    const shelf =
      data.shelves.find((s) => s && String(s.shelf_code || "") === annId) ||
      data.shelves.find((s) => s && s.shelf_code) ||
      data.shelves[0];
    return shelf || null;
  }
  if (Array.isArray(data?.boxes) && data.boxes.length) {
    const code =
      String(data?.source_info?.shelf_code || data?.source_info?.video_stem || annId).trim() ||
      "SHELF_1";
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

function applySandboxAnnotationPayload(data) {
  if (data?.annotation_size) {
    sandboxLoadedAnnotationSize = {
      width: Number(data.annotation_size.width) || 0,
      height: Number(data.annotation_size.height) || 0,
    };
  } else {
    sandboxLoadedAnnotationSize = null;
  }
  const shelf = sandboxParsePrimaryShelf(data);
  if (!shelf) return false;
  sandboxShelfCode = String(shelf.shelf_code || sandboxShelfCode || "SHELF_1").trim() || "SHELF_1";
  const vm = sandboxVisualMode();
  if (!vm) return false;
  vm.loadShelf(shelf, {
    mapPointsToFrame: sandboxCreateFramePointMapper(
      sandboxLoadedAnnotationSize,
      sandboxFrameWidth,
      sandboxFrameHeight
    ),
    isNormValid: sandboxIsNormPolyValid,
    annotationSize: sandboxLoadedAnnotationSize,
    frameWidth: sandboxFrameWidth,
    frameHeight: sandboxFrameHeight,
  });
  const gs = vm.getGridSize();
  sandboxGridRows = gs.rows;
  sandboxGridCols = gs.cols;
  const rowsEl = sandboxAnn$("#sandbox-annotate-rows");
  const colsEl = sandboxAnn$("#sandbox-annotate-cols");
  if (rowsEl) rowsEl.value = String(sandboxGridRows);
  if (colsEl) colsEl.value = String(sandboxGridCols);
  syncSandboxCellPanel(null);
  renderSandboxAnnotator();
  scheduleFitSandboxCanvasDisplay();
  return vm.isGridReady();
}

/** 已有货位网格时进入「微调模式」：隐藏货架大框，只编辑货位 */
function enterSandboxEditExistingMode() {
  const vm = sandboxVisualMode();
  if (!vm?.isGridReady()) return false;
  vm.setShelfOutlineVisible(false);
  vm.setCellRenderHighContrast(true);
  renderSandboxAnnotator();
  return true;
}

async function fetchSandboxRecordFrame(recordId) {
  const res = await fetch(recordApiUrl(recordId, "/annotation/frame"));
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `加载首帧失败 (${res.status})`);
  }
  return res.json();
}

/** 加载源记录首帧 + 沙盒 annotation 到画布 */
async function loadSandboxVisualAnnotate(recordId, annotationData) {
  const rid = String(recordId || "").trim();
  if (!rid) return false;
  const token = ++sandboxVisualLoadToken;
  sandboxSourceRecordId = rid;
  mountSandboxVisualCanvas();

  const frame = await fetchSandboxRecordFrame(rid);
  if (token !== sandboxVisualLoadToken) return false;

  const img = frame.image ? `data:image/jpeg;base64,${frame.image}` : "";
  if (!img) throw new Error("首帧无图像数据");
  sandboxSourceVideo = frame.video_file || frame.source_video || "";
  await showSandboxCanvasWithImage(img, frame.width, frame.height, {
    preserveShelf: false,
    skipDefaultShelf: true,
  });
  if (token !== sandboxVisualLoadToken) return false;

  if (annotationData && typeof annotationData === "object") {
    const src = String(annotationData?.source_info?.source_video || "").trim();
    if (src) sandboxSourceVideo = src;
    sandboxShelfCode =
      String(
        annotationData?.shelves?.[0]?.shelf_code ||
          annotationData?.source_info?.shelf_code ||
          annotationData?.source_info?.video_stem ||
          "SHELF_1"
      ).trim() || "SHELF_1";
    applySandboxAnnotationPayload(annotationData);
    if (enterSandboxEditExistingMode()) {
      if (typeof setSandboxStatus === "function") {
        setSandboxStatus("已加载沙盒标注：点击货位微调形状/编号，无需重标货架。");
      }
    } else {
      const vm = sandboxVisualMode();
      vm?.ensureDefaultShelf(sandboxFrameWidth, sandboxFrameHeight);
      vm?.setShelfOutlineVisible(true);
      vm?.setCellRenderHighContrast(false);
      if (typeof setSandboxStatus === "function") {
        setSandboxStatus("标注不完整：请拖动绿色货架角点 →「生成货位」。");
      }
      renderSandboxAnnotator();
    }
  } else {
    const vm = sandboxVisualMode();
    vm?.ensureDefaultShelf(sandboxFrameWidth, sandboxFrameHeight);
    vm?.setShelfOutlineVisible(true);
    vm?.setCellRenderHighContrast(false);
    renderSandboxAnnotator();
  }
  scheduleFitSandboxCanvasDisplay();
  return true;
}

function buildSandboxVisualSavePayload() {
  syncSandboxGridFromInputs();
  const vm = sandboxVisualMode();
  const finalBoxes = vm ? vm.buildBoxes(sandboxFrameWidth, sandboxFrameHeight) : [];
  const gs = vm?.getGridSize() || { rows: sandboxGridRows, cols: sandboxGridCols };
  const shelfCode = sandboxShelfCode || "SHELF_1";
  const stem =
    sandboxSourceRecordId.split("/").pop()?.replace(/\.(json|parquet)$/i, "") ||
    sandboxSourceRecordId;
  return {
    annotation_size: { width: sandboxFrameWidth, height: sandboxFrameHeight },
    source_info: {
      capture_source: "video",
      video_stem: stem,
      source_video: sandboxSourceVideo || `${stem}.mp4`,
      shelf_code: shelfCode,
      sandbox_source_record_id: sandboxSourceRecordId,
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

function syncSandboxAnnotationEditorFromVisual() {
  const editor = sandboxAnn$("#sandbox-annotation-editor");
  if (!editor || !sandboxVisualMounted) return;
  try {
    const payload = buildSandboxVisualSavePayload();
    editor.value = JSON.stringify(payload, null, 2);
  } catch {
    /* ignore */
  }
}

function sandboxConfirmGenerateGrid() {
  syncSandboxGridFromInputs();
  const vm = sandboxVisualMode();
  if (!vm) return;
  const result = vm.confirmGenerateGrid();
  if (!result.ok) {
    if (typeof setSandboxStatus === "function") setSandboxStatus(result.message, "error");
    return;
  }
  if (typeof setSandboxStatus === "function") setSandboxStatus(result.message);
  enterSandboxEditExistingMode();
  syncSandboxAnnotationEditorFromVisual();
  renderSandboxAnnotator();
}

function sandboxRestartGridAnnotation() {
  syncSandboxGridFromInputs();
  sandboxVisualMode()?.reset();
  sandboxVisualMode()?.ensureDefaultShelf(sandboxFrameWidth, sandboxFrameHeight);
  sandboxVisualMode()?.setShelfOutlineVisible(true);
  sandboxVisualMode()?.setCellRenderHighContrast(false);
  syncSandboxCellPanel(null);
  if (typeof setSandboxStatus === "function") {
    setSandboxStatus("已重置货架：拖动绿色角点 →「生成货位」→ 编辑货位。");
  }
  renderSandboxAnnotator();
}

function getSandboxAnnotationPayloadForSave() {
  const mode = sandboxAnn$("#sandbox-annotation-mode")?.value || "visual";
  if (mode === "json") {
    const raw = sandboxAnn$("#sandbox-annotation-editor")?.value || "";
    return JSON.parse(raw);
  }
  const vm = sandboxVisualMode();
  if (!vm?.shelfCornersReady()) {
    throw new Error("请先标定货架四角（拖动绿色角点）");
  }
  if (!vm.isGridReady()) {
    throw new Error("请先点击「生成货位」");
  }
  if (!sandboxFrameWidth || !sandboxFrameHeight) {
    throw new Error("请先加载记录首帧");
  }
  return buildSandboxVisualSavePayload();
}

function bindSandboxVisualAnnotateEvents() {
  if (bindSandboxVisualAnnotateEvents._bound) return;
  bindSandboxVisualAnnotateEvents._bound = true;

  sandboxAnn$("#sandbox-annotate-confirm-grid")?.addEventListener("click", sandboxConfirmGenerateGrid);
  sandboxAnn$("#sandbox-annotate-restart")?.addEventListener("click", sandboxRestartGridAnnotation);
  sandboxAnn$("#sandbox-annotate-rows")?.addEventListener("change", syncSandboxGridFromInputs);
  sandboxAnn$("#sandbox-annotate-cols")?.addEventListener("change", syncSandboxGridFromInputs);

  sandboxAnn$("#sandbox-annotate-box-id")?.addEventListener("change", () => {
    const vm = sandboxVisualMode();
    const val = sandboxAnn$("#sandbox-annotate-box-id")?.value?.trim();
    if (vm?.getSelectedCellPanel()) vm.setBoxId(val);
    syncSandboxAnnotationEditorFromVisual();
    renderSandboxAnnotator();
  });

  sandboxAnn$("#sandbox-annotate-delete-cell")?.addEventListener("click", () => {
    sandboxVisualMode()?.deleteSelectedCell();
    syncSandboxCellPanel(null);
    syncSandboxAnnotationEditorFromVisual();
    renderSandboxAnnotator();
  });

  sandboxAnn$("#sandbox-annotation-mode")?.addEventListener("change", () => {
    const mode = sandboxAnn$("#sandbox-annotation-mode")?.value || "visual";
    sandboxAnn$("#sandbox-visual-annotate-wrap")?.classList.toggle("hidden", mode !== "visual");
    sandboxAnn$("#sandbox-json-annotate-wrap")?.classList.toggle("hidden", mode !== "json");
    if (mode === "json") syncSandboxAnnotationEditorFromVisual();
    else scheduleFitSandboxCanvasDisplay();
  });
}

function initSandboxVisualAnnotate() {
  bindSandboxVisualAnnotateEvents();
  ensureSandboxCanvasResizeObserver();
}

window.fitSandboxCanvasDisplay = fitSandboxCanvasDisplay;
