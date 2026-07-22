/** 地面/立体作业空间标定页 */

const SPATIAL_VOLUME_POINTS = 8;
const VOLUME_CORNER_LABELS = ["BL", "BR", "FR", "FL", "TL", "TR", "FR_top", "FL_top"];

/** 与后端 volume_wireframe_segments 一致的 12 棱边（角点索引） */
const VOLUME_WIREFRAME_EDGES = [
  [0, 1], [1, 2], [2, 3], [3, 0],
  [4, 5], [5, 6], [6, 7], [7, 4],
  [0, 4], [1, 5], [2, 6], [3, 7],
];

/** 地面列：第 1 点 BL–FL 左纵深边，第 2 点 BR–FR 右纵深边 */
const COLUMN_BL_FL_EDGE_IDX = 3;
const COLUMN_BR_FR_EDGE_IDX = 1;
/** 左侧层线：左侧面四边 */
const LEFT_FACE_PICK_EDGE_IDX = [3, 8, 7, 11];
/** 右侧层线：右侧面四边 */
const RIGHT_FACE_PICK_EDGE_IDX = [1, 9, 5, 10];

const WIREframe_SNAP_MAX_PX = 28;

/** 8 角点中文说明：L/R=通道左右，近/远=纵深 D，B/T=底/顶面 H */
const VOLUME_CORNER_META = [
  { id: "BL", zh: "近左下", desc: "底面 · 通道左侧 · 靠近相机 · 地面" },
  { id: "BR", zh: "近右下", desc: "底面 · 通道右侧 · 靠近相机 · 地面" },
  { id: "FR", zh: "远右下", desc: "底面 · 通道右侧 · 远离相机 · 地面" },
  { id: "FL", zh: "远左下", desc: "底面 · 通道左侧 · 远离相机 · 地面" },
  { id: "TL", zh: "近左上", desc: "顶面 · 通道左侧 · 靠近相机 · 高度 H" },
  { id: "TR", zh: "近右上", desc: "顶面 · 通道右侧 · 靠近相机 · 高度 H" },
  { id: "FR_top", zh: "远右上", desc: "顶面 · 通道右侧 · 远离相机 · 高度 H" },
  { id: "FL_top", zh: "远左上", desc: "顶面 · 通道左侧 · 远离相机 · 高度 H" },
];

function volumeCornerMeta(index) {
  return VOLUME_CORNER_META[index] || { id: VOLUME_CORNER_LABELS[index] || `C${index + 1}`, zh: "", desc: "" };
}

const spatialState = {
  slug: "",
  poseTier: "rtmpose-m",
  inferHeight: 480,
  frameWidth: 0,
  frameHeight: 0,
  sourceWidth: 0,
  sourceHeight: 0,
  videoFile: "",
  imagePoints: [],
  volumeCorners: [],
  columnLines: [],
  leftLayerLines: [],
  rightLayerLines: [],
  gridSegments: [],
  volumeWireframe: [],
  columnLinesPreview: [],
  layerLinesPreview: { left: [], right: [] },
  calibStep: "volume",
  pendingLinePoint: null,
  compositePreview: false,
  loadedConfig: null,
  loadToken: 0,
  initialized: false,
};

function sp$(sel) {
  return document.querySelector(sel);
}

async function readSpatialApiError(res) {
  try {
    const body = await res.json();
    return body.detail || body.message || res.statusText;
  } catch (_err) {
    return res.statusText || `HTTP ${res.status}`;
  }
}

function setSpatialStatus(msg, isError = false) {
  const el = sp$("#spatial-status");
  if (!el) return;
  if (!msg) {
    el.textContent = "";
    el.classList.add("hidden");
    el.classList.remove("err");
    return;
  }
  el.textContent = msg;
  el.classList.remove("hidden");
  el.classList.toggle("err", !!isError);
}

function setSpatialRmse(text, isWarn = false) {
  const el = sp$("#spatial-rmse");
  if (!el) return;
  if (!text) {
    el.textContent = "";
    el.classList.add("hidden");
    return;
  }
  el.textContent = text;
  el.classList.remove("hidden");
  el.classList.toggle("spatial-rmse-warn", !!isWarn);
}

function getSpatialSlug() {
  const manual = (sp$("#spatial-camera-slug-manual")?.value || "").trim();
  if (manual) return manual;
  return (sp$("#spatial-camera-slug")?.value || "").trim();
}

function getSpatialPoseTier() {
  return (sp$("#spatial-pose-tier")?.value || "rtmpose-m").trim();
}

function getSpatialInferHeight() {
  return Math.max(120, Number(sp$("#spatial-infer-height")?.value) || spatialState.inferHeight || 480);
}

function scaleSpatialImagePoints(points, fromW, fromH, toW, toH) {
  if (!fromW || !fromH || !toW || !toH || (fromW === toW && fromH === toH)) {
    return points.map((p) => [p[0], p[1]]);
  }
  const sx = toW / fromW;
  const sy = toH / fromH;
  return points.map(([x, y]) => [x * sx, y * sy]);
}

function syncSpatialPhysicalHint() {
  const vol = readVolumePhysical();
  const hint = sp$("#spatial-point-hint");
  if (!hint) return;
  if (vol.depth_m < 4) {
    setSpatialRmse(`提示：立体纵深 D=${vol.depth_m.toFixed(1)} m 偏小，请确认 W/D/H`, true);
  }
}

function readSpatialPhysical() {
  const vol = readVolumePhysical();
  return {
    aisle_width_m: vol.width_m,
    marker_spacing_m: Math.max(0.1, vol.depth_m / 4),
    marker_pairs: 5,
  };
}

function getSpatialCalibStep() {
  return sp$("#spatial-calib-step")?.value || spatialState.calibStep || "volume";
}

function isLeftFaceEnabled() {
  return sp$("#spatial-left-face-enabled")?.checked !== false;
}

function isRightFaceEnabled() {
  return sp$("#spatial-right-face-enabled")?.checked !== false;
}

function readVolumePhysical() {
  return {
    width_m: Math.max(0.1, Number(sp$("#spatial-volume-width")?.value) || 2.0),
    depth_m: Math.max(0.1, Number(sp$("#spatial-volume-depth")?.value) || 9.6),
    height_m: Math.max(0.1, Number(sp$("#spatial-volume-height")?.value) || 2.4),
    enabled: !!sp$("#spatial-volume-enabled")?.checked,
  };
}

function readGroundColumns() {
  const lineCount = spatialState.columnLines.length;
  return {
    column_count: Math.max(1, lineCount + 1),
    boundaries_x_m: [],
    boundaries_y_m: [],
    boundaries_image_px: spatialState.columnLines.map((seg) => seg),
  };
}

function readShelfFaces() {
  const leftLines = spatialState.leftLayerLines.length;
  const rightLines = spatialState.rightLayerLines.length;
  return {
    left: {
      enabled: isLeftFaceEnabled(),
      layer_count: Math.max(1, leftLines + 1),
      layer_lines_image_px: spatialState.leftLayerLines.map((seg) => seg),
      layer_z_m: [],
    },
    right: {
      enabled: isRightFaceEnabled(),
      layer_count: Math.max(1, rightLines + 1),
      layer_lines_image_px: spatialState.rightLayerLines.map((seg) => seg),
      layer_z_m: [],
    },
  };
}

function addTwoPointLine(x, y, lines) {
  const pt = [x, y];
  if (!spatialState.pendingLinePoint) {
    spatialState.pendingLinePoint = pt;
    return true;
  }
  lines.push([spatialState.pendingLinePoint, pt]);
  spatialState.pendingLinePoint = null;
  return true;
}

function buildVolumeWireframeFromCorners(corners) {
  if (!Array.isArray(corners) || corners.length < SPATIAL_VOLUME_POINTS) return [];
  return VOLUME_WIREFRAME_EDGES.map(([i, j]) => ({
    image: [
      [corners[i][0], corners[i][1]],
      [corners[j][0], corners[j][1]],
    ],
  }));
}

function effectiveVolumeWireframe() {
  if (spatialState.volumeWireframe.length) return spatialState.volumeWireframe;
  return buildVolumeWireframeFromCorners(spatialState.volumeCorners);
}

function wireframeSegmentsForStep(step) {
  const corners = spatialState.volumeCorners;
  if (corners.length < SPATIAL_VOLUME_POINTS) return [];
  let edgeIdx = null;
  if (step === "columns") {
    edgeIdx = spatialState.pendingLinePoint ? [COLUMN_BR_FR_EDGE_IDX] : [COLUMN_BL_FL_EDGE_IDX];
  } else if (step === "left_layers") edgeIdx = LEFT_FACE_PICK_EDGE_IDX;
  else if (step === "right_layers") edgeIdx = RIGHT_FACE_PICK_EDGE_IDX;
  else return [];
  return edgeIdx.map((ei) => {
    const [i, j] = VOLUME_WIREFRAME_EDGES[ei];
    return {
      image: [
        [corners[i][0], corners[i][1]],
        [corners[j][0], corners[j][1]],
      ],
    };
  });
}

function pickEdgeIndicesForStep(step) {
  if (step === "columns") {
    return spatialState.pendingLinePoint ? [COLUMN_BR_FR_EDGE_IDX] : [COLUMN_BL_FL_EDGE_IDX];
  }
  if (step === "left_layers") return LEFT_FACE_PICK_EDGE_IDX;
  if (step === "right_layers") return RIGHT_FACE_PICK_EDGE_IDX;
  return [];
}

function wireframeSegmentsExcludingPick(step) {
  const corners = spatialState.volumeCorners;
  const all = effectiveVolumeWireframe();
  if (!step || showAllSavedOverlays() || corners.length < SPATIAL_VOLUME_POINTS) return all;
  const pickSet = new Set(pickEdgeIndicesForStep(step));
  if (!pickSet.size) return all;
  return VOLUME_WIREFRAME_EDGES.map(([i, j], ei) => ({
    edgeIdx: ei,
    image: [[corners[i][0], corners[i][1]], [corners[j][0], corners[j][1]]],
  })).filter((seg) => !pickSet.has(seg.edgeIdx));
}

function isVolumeBoundaryGridSegment(seg, widthM, depthM) {
  const w = seg?.world;
  if (!Array.isArray(w) || w.length < 2) return false;
  const [x0, y0] = w[0];
  const [x1, y1] = w[1];
  const eps = 0.08;
  if (Math.abs(y0 - y1) < eps && (Math.abs(y0) < eps || Math.abs(y0 - depthM) < eps)) return true;
  if (Math.abs(x0 - x1) < eps && (Math.abs(x0) < eps || Math.abs(x0 - widthM) < eps)) return true;
  return false;
}

function isHorizontalGridSegment(seg) {
  const w = seg?.world;
  if (!Array.isArray(w) || w.length < 2) return false;
  return Math.abs(w[0][1] - w[1][1]) < 0.08;
}

function gridSegmentsForDisplay() {
  if (!spatialState.gridSegments.length) return [];
  const hasVolume = spatialState.volumeCorners.length === SPATIAL_VOLUME_POINTS
    || spatialState.volumeWireframe.length > 0;
  let segs = hasVolume
    ? spatialState.gridSegments.filter(
      (seg) => !isVolumeBoundaryGridSegment(seg, readVolumePhysical().width_m, readVolumePhysical().depth_m)
    )
    : spatialState.gridSegments.slice();
  // 手标列线已覆盖纵深分割，不再叠加 homography 横向网格（避免底部多重绿线）
  if (spatialState.columnLines.length > 0) {
    segs = segs.filter((seg) => !isHorizontalGridSegment(seg));
  }
  return segs;
}

function nearestPointOnSegment(px, py, x0, y0, x1, y1) {
  const dx = x1 - x0;
  const dy = y1 - y0;
  const lenSq = dx * dx + dy * dy;
  if (lenSq < 1e-9) {
    return { x: x0, y: y0, dist: Math.hypot(px - x0, py - y0) };
  }
  let t = ((px - x0) * dx + (py - y0) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  const x = x0 + t * dx;
  const y = y0 + t * dy;
  return { x, y, dist: Math.hypot(px - x, py - y) };
}

function snapClickToWireframe(x, y, segments, maxPx = WIREframe_SNAP_MAX_PX) {
  let best = null;
  segments.forEach((seg) => {
    const img = seg.image || seg;
    if (!Array.isArray(img) || img.length < 2) return;
    const hit = nearestPointOnSegment(x, y, img[0][0], img[0][1], img[1][0], img[1][1]);
    if (!best || hit.dist < best.dist) {
      best = { x: hit.x, y: hit.y, dist: hit.dist };
    }
  });
  if (!best || best.dist > maxPx) return null;
  return [best.x, best.y];
}

function pickLinePointOnWireframe(step, x, y) {
  if (spatialState.volumeCorners.length < SPATIAL_VOLUME_POINTS) {
    setSpatialStatus("请先完成 8 角点立体框", true);
    return null;
  }
  const segments = wireframeSegmentsForStep(step);
  const snapped = snapClickToWireframe(x, y, segments);
  if (!snapped) {
    const labels = {
      columns: "BL–FL / BR–FR 纵深棱边",
      left_layers: "左侧面棱边",
      right_layers: "右侧面棱边",
    };
    setSpatialStatus(`请点击立体线框上的${labels[step] || "棱边"}（距线框 ${WIREframe_SNAP_MAX_PX}px 内）`, true);
    return null;
  }
  return snapped;
}

function clearPendingLinePoint() {
  spatialState.pendingLinePoint = null;
}

function drawPendingLinePoint(ctx) {
  const pt = spatialState.pendingLinePoint;
  if (!pt) return;
  ctx.fillStyle = "#fbbf24";
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(pt[0], pt[1], 8, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.font = "12px sans-serif";
  ctx.fillStyle = "#fbbf24";
  ctx.fillText("第1点", pt[0] + 10, pt[1] - 8);
}

function syncSpatialPointHint() {
  const hint = sp$("#spatial-point-hint");
  if (!hint) return;
  const step = getSpatialCalibStep();
  if (!spatialState.slug) {
    hint.textContent = "请选择或输入机位 slug，然后点击「加载」";
    return;
  }
  if (step === "volume") {
    const n = spatialState.volumeCorners.length;
    if (n >= SPATIAL_VOLUME_POINTS) {
      hint.textContent = "8 角点已满，请确认 W/D/H 后点击「预览」";
      return;
    }
    const meta = volumeCornerMeta(n);
    hint.textContent = `第 ${n + 1}/${SPATIAL_VOLUME_POINTS} 点 · ${meta.zh}（${meta.id}）— ${meta.desc}`;
    return;
  }
  if (step === "columns") {
    const n = spatialState.columnLines.length;
    if (spatialState.pendingLinePoint) {
      hint.textContent = `地面列：已 ${n} 条 · 请在 BR–FR 右纵深边点第 2 点`;
      return;
    }
    hint.textContent = `地面列：第 1 点在 BL–FL 左纵深边，第 2 点在 BR–FR 右纵深边（沿通道纵深分列）`;
    return;
  }
  if (step === "left_layers") {
    if (!isLeftFaceEnabled()) {
      hint.textContent = "左侧货架未启用；请勾选「启用左侧货架」或跳过此步";
      return;
    }
    const n = spatialState.leftLayerLines.length;
    if (spatialState.pendingLinePoint) {
      hint.textContent = `左侧层线：已 ${n} 条（${n + 1} 层）· 请点击第 2 点完成当前层线`;
      return;
    }
    hint.textContent = `左侧层线：在左侧面线框上左键两点确认水平层线（已 ${n} 条 → ${n + 1} 层）`;
    return;
  }
  if (step === "right_layers") {
    if (!isRightFaceEnabled()) {
      hint.textContent = "右侧货架未启用；请勾选「启用右侧货架」或跳过此步";
      return;
    }
    const n = spatialState.rightLayerLines.length;
    if (spatialState.pendingLinePoint) {
      hint.textContent = `右侧层线：已 ${n} 条（${n + 1} 层）· 请点击第 2 点完成当前层线`;
      return;
    }
    hint.textContent = `右侧层线：在右侧面线框上左键两点确认水平层线（已 ${n} 条 → ${n + 1} 层）`;
  }
  syncSpatialColumnLayerCounts();
}

function buildSpatialConfigBody() {
  const physical = readSpatialPhysical();
  const depth = physical.marker_spacing_m * Math.max(0, physical.marker_pairs - 1);
  const volPhys = readVolumePhysical();
  const base = spatialState.loadedConfig && typeof spatialState.loadedConfig === "object"
    ? structuredClone(spatialState.loadedConfig)
    : {
        schema: 2,
        camera_slug: spatialState.slug,
        enabled: false,
        physical,
        calibration: { resolution: [0, 0], image_points_px: [] },
        volume: {
          enabled: false,
          width_m: volPhys.width_m,
          depth_m: volPhys.depth_m,
          height_m: volPhys.height_m,
          corners_image_px: [],
          corner_labels: VOLUME_CORNER_LABELS,
        },
        ground_columns: readGroundColumns(),
        shelf_faces: readShelfFaces(),
        computed: {},
        tuning: {
          homography_override: null,
          scale_xy: [1.0, 1.0],
          offset_xy_m: [0.0, 0.0],
          notes: "",
        },
        runtime: {
          floor_bounds_m: { x_min: -0.7, x_max: 2.7, y_min: -1.5, y_max: 12.5 },
          foot_score_min: 0.35,
          wrist_score_min: 0.35,
          smooth_alpha_normal: 0.2,
          smooth_alpha_jump: 0.08,
          smooth_jump_threshold_m: 1.4,
        },
        visualization: {
          grid_width_m: physical.aisle_width_m,
          grid_depth_m: depth || 9.6,
          grid_spacing_m: physical.marker_spacing_m,
        },
      };

  base.schema = 2;
  base.camera_slug = spatialState.slug;
  const volEnabled = !!sp$("#spatial-volume-enabled")?.checked;
  base.enabled = volEnabled;
  base.physical = physical;
  base.calibration = {
    resolution: [spatialState.frameWidth, spatialState.frameHeight],
    image_points_px: [],
  };
  base.volume = {
    enabled: volEnabled,
    width_m: volPhys.width_m,
    depth_m: volPhys.depth_m,
    height_m: volPhys.height_m,
    corners_image_px: spatialState.volumeCorners.map((p) => [p[0], p[1]]),
    corner_labels: VOLUME_CORNER_LABELS,
  };
  base.ground_columns = readGroundColumns();
  base.shelf_faces = readShelfFaces();
  base.visualization = {
    grid_width_m: volPhys.width_m,
    grid_depth_m: volPhys.depth_m,
    grid_spacing_m: Math.max(0.1, volPhys.depth_m / 4),
  };
  return base;
}

function applySpatialConfigToForm(cfg, targetW = 0, targetH = 0) {
  if (!cfg || typeof cfg !== "object") return;
  spatialState.loadedConfig = cfg;
  const vol = cfg.volume || {};
  if (sp$("#spatial-volume-width")) sp$("#spatial-volume-width").value = String(vol.width_m ?? 2.0);
  if (sp$("#spatial-volume-depth")) sp$("#spatial-volume-depth").value = String(vol.depth_m ?? 9.6);
  if (sp$("#spatial-volume-height")) sp$("#spatial-volume-height").value = String(vol.height_m ?? 2.4);
  if (sp$("#spatial-volume-enabled")) {
    sp$("#spatial-volume-enabled").checked = cfg.enabled !== false && vol.enabled !== false;
  }
  const res = cfg.calibration?.resolution || [0, 0];
  const fromW = Number(res[0]) || 0;
  const fromH = Number(res[1]) || 0;
  const vpts = vol.corners_image_px;
  if (Array.isArray(vpts) && vpts.length && !spatialState.volumeCorners.length) {
    let vscaled = vpts.filter((p) => Array.isArray(p) && p.length >= 2).map((p) => [Number(p[0]), Number(p[1])]);
    if (targetW > 0 && targetH > 0 && fromW > 0 && fromH > 0) {
      vscaled = scaleSpatialImagePoints(vscaled, fromW, fromH, targetW, targetH);
    }
    spatialState.volumeCorners = vscaled;
  }
  const gc = cfg.ground_columns || {};
  const colLines = gc.boundaries_image_px;
  if (Array.isArray(colLines) && colLines.length && !spatialState.columnLines.length) {
    spatialState.columnLines = colLines
      .filter((seg) => Array.isArray(seg) && seg.length >= 2)
      .map((seg) => [[Number(seg[0][0]), Number(seg[0][1])], [Number(seg[1][0]), Number(seg[1][1])]]);
  }
  const sf = cfg.shelf_faces || {};
  if (sp$("#spatial-left-face-enabled")) {
    sp$("#spatial-left-face-enabled").checked = sf.left?.enabled !== false;
  }
  if (sp$("#spatial-right-face-enabled")) {
    sp$("#spatial-right-face-enabled").checked = sf.right?.enabled !== false;
  }
  if (Array.isArray(sf.left?.layer_lines_image_px) && sf.left.layer_lines_image_px.length && !spatialState.leftLayerLines.length) {
    spatialState.leftLayerLines = sf.left.layer_lines_image_px.map((seg) => [
      [Number(seg[0][0]), Number(seg[0][1])],
      [Number(seg[1][0]), Number(seg[1][1])],
    ]);
  }
  if (Array.isArray(sf.right?.layer_lines_image_px) && sf.right.layer_lines_image_px.length && !spatialState.rightLayerLines.length) {
    spatialState.rightLayerLines = sf.right.layer_lines_image_px.map((seg) => [
      [Number(seg[0][0]), Number(seg[0][1])],
      [Number(seg[1][0]), Number(seg[1][1])],
    ]);
  }
  syncSpatialColumnLayerCounts();
}

function syncSpatialColumnLayerCounts() {
  const colEl = sp$("#spatial-column-count-display");
  const leftEl = sp$("#spatial-left-layer-count-display");
  const rightEl = sp$("#spatial-right-layer-count-display");
  const colN = spatialState.columnLines.length;
  const leftN = spatialState.leftLayerLines.length;
  const rightN = spatialState.rightLayerLines.length;
  if (colEl) colEl.textContent = `${colN} 条分割线 → ${colN + 1} 列`;
  if (leftEl) leftEl.textContent = `${leftN} 条层线 → ${leftN + 1} 层`;
  if (rightEl) rightEl.textContent = `${rightN} 条层线 → ${rightN + 1} 层`;
}

function showAllSavedOverlays() {
  return !!spatialState.compositePreview;
}

function clearStepPreviewOverlays() {
  spatialState.compositePreview = false;
  spatialState.volumeWireframe = [];
  spatialState.columnLinesPreview = [];
  spatialState.layerLinesPreview = { left: [], right: [] };
  clearGroundGridPreview();
}

function shouldShowVolumeOverlays() {
  const step = getSpatialCalibStep();
  return step === "volume" || step === "columns" || step === "left_layers" || step === "right_layers";
}

function shouldShowColumnOverlays() {
  const step = getSpatialCalibStep();
  return step === "columns" || step === "left_layers" || step === "right_layers";
}

function shouldShowLeftLayerOverlays() {
  const step = getSpatialCalibStep();
  return step === "left_layers" || step === "right_layers";
}

function shouldShowRightLayerOverlays() {
  return getSpatialCalibStep() === "right_layers";
}

function clearGroundGridPreview() {
  spatialState.gridSegments = [];
}

function canvasImageCoords(canvas, clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / Math.max(1, rect.width);
  const scaleY = canvas.height / Math.max(1, rect.height);
  return [
    (clientX - rect.left) * scaleX,
    (clientY - rect.top) * scaleY,
  ];
}

function drawSegmentList(ctx, segments, color, width = 2) {
  if (!Array.isArray(segments)) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  segments.forEach((seg) => {
    const img = seg.image || seg;
    if (!Array.isArray(img) || img.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(img[0][0], img[0][1]);
    ctx.lineTo(img[1][0], img[1][1]);
    ctx.stroke();
  });
}

function renderSpatialCanvas() {
  const canvas = sp$("#spatial-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx || !spatialState.bgImage?.complete) return;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(spatialState.bgImage, 0, 0, canvas.width, canvas.height);

  const step = getSpatialCalibStep();
  const showAll = showAllSavedOverlays();
  const wireframe = wireframeSegmentsExcludingPick(step);
  if (wireframe.length && (showAll || shouldShowVolumeOverlays())) {
    drawSegmentList(ctx, wireframe, "rgba(100, 180, 255, 0.95)", 2);
  }
  // 列/层编辑步骤高亮可点击棱边（预览全部时不重复画）
  const pickSegs = wireframeSegmentsForStep(step);
  if (!showAll && pickSegs.length && (step === "columns" || step === "left_layers" || step === "right_layers")) {
    const pickColor = step === "columns"
      ? "rgba(80, 255, 120, 0.85)"
      : step === "left_layers"
        ? "rgba(120, 200, 255, 0.9)"
        : "rgba(255, 180, 100, 0.9)";
    drawSegmentList(ctx, pickSegs, pickColor, 4);
  }
  const gridToDraw = gridSegmentsForDisplay();
  if (gridToDraw.length && showAll) {
    drawSegmentList(ctx, gridToDraw, "rgba(80, 210, 80, 0.55)", 1.5);
  }
  if (spatialState.columnLines.length && (showAll || shouldShowColumnOverlays())) {
    spatialState.columnLines.forEach((seg) => {
      ctx.strokeStyle = "rgba(80, 255, 120, 0.95)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(seg[0][0], seg[0][1]);
      ctx.lineTo(seg[1][0], seg[1][1]);
      ctx.stroke();
    });
  }
  if (spatialState.leftLayerLines.length && isLeftFaceEnabled() && (showAll || shouldShowLeftLayerOverlays())) {
    spatialState.leftLayerLines.forEach((seg) => {
      ctx.strokeStyle = "rgba(80, 160, 255, 0.95)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(seg[0][0], seg[0][1]);
      ctx.lineTo(seg[1][0], seg[1][1]);
      ctx.stroke();
    });
  }
  if (spatialState.rightLayerLines.length && isRightFaceEnabled() && (showAll || shouldShowRightLayerOverlays())) {
    spatialState.rightLayerLines.forEach((seg) => {
      ctx.strokeStyle = "rgba(255, 160, 80, 0.95)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(seg[0][0], seg[0][1]);
      ctx.lineTo(seg[1][0], seg[1][1]);
      ctx.stroke();
    });
  }
  if (step === "columns" || step === "left_layers" || step === "right_layers") {
    drawPendingLinePoint(ctx);
  }

  if (spatialState.volumeCorners.length && (showAll || shouldShowVolumeOverlays())) {
    spatialState.volumeCorners.forEach((pt, i) => {
      const meta = volumeCornerMeta(i);
      ctx.fillStyle = "#2b84eb";
      ctx.beginPath();
      ctx.arc(pt[0], pt[1], 7, 0, Math.PI * 2);
      ctx.fill();
      const label = `${meta.id}`;
      const sub = meta.zh;
      ctx.font = "13px sans-serif";
      ctx.fillStyle = "#fff";
      ctx.strokeStyle = "#000";
      ctx.lineWidth = 3;
      ctx.strokeText(label, pt[0] + 8, pt[1] - 6);
      ctx.fillText(label, pt[0] + 8, pt[1] - 6);
      if (sub) {
        ctx.font = "11px sans-serif";
        ctx.strokeText(sub, pt[0] + 8, pt[1] + 10);
        ctx.fillText(sub, pt[0] + 8, pt[1] + 10);
      }
    });
  }
}

async function loadSpatialFrameImage(slug, poseTier, inferHeight) {
  const url =
    `/api/spatial/calibration/${encodeURIComponent(slug)}/preview-frame` +
    `?pose_tier=${encodeURIComponent(poseTier)}&infer_height=${encodeURIComponent(inferHeight)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(await readSpatialApiError(res));
  return res.json();
}

async function loadSpatialCalibrationConfig(slug) {
  const res = await fetch(`/api/spatial/calibration/${encodeURIComponent(slug)}`);
  if (!res.ok) throw new Error(await readSpatialApiError(res));
  const body = await res.json();
  return body.config || body;
}

function showSpatialCanvasWithImage(dataUrl, width, height) {
  return new Promise((resolve, reject) => {
    const canvas = sp$("#spatial-canvas");
    if (!canvas) {
      reject(new Error("标定画布未找到"));
      return;
    }
    const img = new Image();
    img.onload = () => {
      canvas.width = width || img.naturalWidth || img.width;
      canvas.height = height || img.naturalHeight || img.height;
      spatialState.frameWidth = canvas.width;
      spatialState.frameHeight = canvas.height;
      spatialState.bgImage = img;
      canvas.classList.remove("hidden");
      spatialState.gridSegments = [];
      renderSpatialCanvas();
      resolve({ width: canvas.width, height: canvas.height });
    };
    img.onerror = () => reject(new Error("视频帧图片加载失败"));
    img.src = dataUrl;
  });
}

async function loadSpatialSession({ keepPoints = false } = {}) {
  const slug = getSpatialSlug();
  const poseTier = getSpatialPoseTier();
  const inferHeight = getSpatialInferHeight();
  if (!slug) {
    setSpatialStatus("请选择或输入机位 slug", true);
    return;
  }

  const token = ++spatialState.loadToken;
  spatialState.slug = slug;
  spatialState.poseTier = poseTier;
  spatialState.inferHeight = inferHeight;
  if (!keepPoints) {
    spatialState.volumeCorners = [];
    spatialState.columnLines = [];
    spatialState.leftLayerLines = [];
    spatialState.rightLayerLines = [];
    clearPendingLinePoint();
    clearStepPreviewOverlays();
  }
  setSpatialStatus("正在加载视频帧…");
  if (!keepPoints) setSpatialRmse("");

  let framePayload;
  try {
    framePayload = await loadSpatialFrameImage(slug, poseTier, inferHeight);
  } catch (err) {
    setSpatialStatus(`❌ ${err.message || err}`, true);
    return;
  }
  if (token !== spatialState.loadToken) return;

  spatialState.videoFile = framePayload.video || "";
  spatialState.sourceWidth = Number(framePayload.source_width) || 0;
  spatialState.sourceHeight = Number(framePayload.source_height) || 0;

  let cfg;
  try {
    cfg = await loadSpatialCalibrationConfig(slug);
  } catch (err) {
    setSpatialStatus(`⚠️ 标定配置读取失败：${err.message}`, true);
    cfg = null;
  }
  if (token !== spatialState.loadToken) return;

  const b64 =
    (typeof framePayload.frame_base64 === "string" && framePayload.frame_base64) ||
    (typeof framePayload.image === "string" && framePayload.image) ||
    (framePayload.frame_base64 && typeof framePayload.frame_base64.image === "string"
      ? framePayload.frame_base64.image
      : "");
  if (!b64) {
    const hint =
      framePayload.config != null
        ? "预览帧 API 路由异常（请重启 server.py 后重试）"
        : "未返回视频帧数据";
    setSpatialStatus(`❌ ${hint}`, true);
    return;
  }

  const fw = Number(framePayload.width) || 0;
  const fh = Number(framePayload.height) || 0;

  try {
    await showSpatialCanvasWithImage(`data:image/jpeg;base64,${b64}`, fw, fh);
  } catch (err) {
    setSpatialStatus(`❌ ${err.message || err}`, true);
    return;
  }
  if (token !== spatialState.loadToken) return;

  if (cfg && !keepPoints) {
    applySpatialConfigToForm(cfg, spatialState.frameWidth, spatialState.frameHeight);
  }
  clearStepPreviewOverlays();
  renderSpatialCanvas();

  syncSpatialPointHint();
  syncSpatialPhysicalHint();
  const srcHint =
    spatialState.sourceWidth > 0 && spatialState.sourceHeight > 0
      ? `（源 ${spatialState.sourceWidth}×${spatialState.sourceHeight} → 推理 ${spatialState.frameWidth}×${spatialState.frameHeight}）`
      : `（推理 ${spatialState.frameWidth}×${spatialState.frameHeight}）`;
  const videoHint = spatialState.videoFile
    ? ` · ${spatialState.videoFile.split(/[/\\]/).pop()}`
    : "";
  setSpatialStatus(`已加载 ${slug}${srcHint}${videoHint}`, false);

  const rmse = cfg?.computed?.ground_control_rmse_px;
  const cfgRes = cfg?.calibration?.resolution || [0, 0];
  const cfgW = Number(cfgRes[0]) || 0;
  const cfgH = Number(cfgRes[1]) || 0;
  if (cfgW > 0 && cfgH > 0 && (cfgW !== spatialState.frameWidth || cfgH !== spatialState.frameHeight)) {
    setSpatialRmse(
      `已按分辨率 ${cfgW}×${cfgH} → ${spatialState.frameWidth}×${spatialState.frameHeight} 缩放控制点，建议重新预览并保存`,
      true
    );
  } else if (typeof rmse === "number" && Number.isFinite(rmse)) {
    setSpatialRmse(`已存标定 RMSE: ${rmse.toFixed(2)} px`, rmse >= 8);
  }
}

async function previewSpatialGrid() {
  const hasVolume = spatialState.volumeCorners.length === SPATIAL_VOLUME_POINTS;
  const hasColumns = spatialState.columnLines.length > 0;
  const hasLayers = spatialState.leftLayerLines.length > 0 || spatialState.rightLayerLines.length > 0;
  if (!hasVolume && !hasColumns && !hasLayers) {
    setSpatialStatus("请先标定立体框或列/层线", true);
    return;
  }
  setSpatialStatus("正在计算预览…");
  const body = buildSpatialConfigBody();
  if (hasVolume || readVolumePhysical().enabled) {
    body.volume.enabled = true;
  }
  const slug = spatialState.slug || getSpatialSlug();
  try {
    const res = await fetch(`/api/spatial/calibration/${encodeURIComponent(slug)}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readSpatialApiError(res));
    const payload = await res.json();
    spatialState.gridSegments = Array.isArray(payload.grid_segments) ? payload.grid_segments : [];
    spatialState.volumeWireframe = Array.isArray(payload.volume_wireframe_segments)
      ? payload.volume_wireframe_segments
      : [];
    spatialState.columnLinesPreview = Array.isArray(payload.column_lines_image)
      ? payload.column_lines_image
      : [];
    spatialState.layerLinesPreview = payload.layer_lines_image && typeof payload.layer_lines_image === "object"
      ? payload.layer_lines_image
      : { left: [], right: [] };
    spatialState.compositePreview = true;
    renderSpatialCanvas();
    const rmse = Number(payload.ground_control_rmse_px);
    const vrmse = Number(payload.volume_rmse_px);
    const parts = [];
    if (Number.isFinite(rmse)) parts.push(`底面 RMSE ${rmse.toFixed(2)} px`);
    if (Number.isFinite(vrmse)) parts.push(`立体 RMSE ${vrmse.toFixed(2)} px`);
    const layers = [];
    if (hasVolume) layers.push("立体框");
    if (hasColumns) layers.push(`列×${spatialState.columnLines.length}`);
    if (spatialState.leftLayerLines.length) layers.push(`左层×${spatialState.leftLayerLines.length}`);
    if (spatialState.rightLayerLines.length) layers.push(`右层×${spatialState.rightLayerLines.length}`);
    if (parts.length) setSpatialRmse(parts.join(" · "), (rmse >= 8) || (vrmse >= 12));
    setSpatialStatus(`预览已更新：${layers.join(" · ")}`);
  } catch (err) {
    setSpatialStatus(`❌ 预览失败：${err.message || err}`, true);
  }
}

async function saveSpatialCalibration() {
  const volComplete = spatialState.volumeCorners.length === SPATIAL_VOLUME_POINTS;
  if (!volComplete) {
    setSpatialStatus(`立体框需要 ${SPATIAL_VOLUME_POINTS} 个角点`, true);
    return;
  }
  const slug = spatialState.slug || getSpatialSlug();
  if (!slug) {
    setSpatialStatus("机位 slug 为空", true);
    return;
  }
  const body = buildSpatialConfigBody();
  body.volume.enabled = true;
  body.enabled = !!sp$("#spatial-volume-enabled")?.checked;
  setSpatialStatus("正在保存标定…");
  try {
    const res = await fetch(`/api/spatial/calibration/${encodeURIComponent(slug)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await readSpatialApiError(res));
    const payload = await res.json();
    const saved = payload.config || body;
    spatialState.loadedConfig = saved;
    spatialState.gridSegments = Array.isArray(payload.grid_segments) ? payload.grid_segments : [];
    spatialState.volumeWireframe = Array.isArray(payload.volume_wireframe_segments)
      ? payload.volume_wireframe_segments
      : [];
    spatialState.columnLinesPreview = Array.isArray(payload.column_lines_image)
      ? payload.column_lines_image
      : [];
    spatialState.layerLinesPreview = payload.layer_lines_image && typeof payload.layer_lines_image === "object"
      ? payload.layer_lines_image
      : { left: [], right: [] };
    spatialState.compositePreview = true;
    renderSpatialCanvas();
    const rmse = Number(saved.computed?.ground_control_rmse_px ?? saved.ground_control_rmse_px);
    if (Number.isFinite(rmse)) {
      setSpatialRmse(`已保存 · RMSE: ${rmse.toFixed(2)} px${rmse >= 8 ? "（建议 < 8 px）" : ""}`, rmse >= 8);
    }
    setSpatialStatus(`✅ 已保存 localdata/spatial/${slug}.json`);
  } catch (err) {
    setSpatialStatus(`❌ 保存失败：${err.message || err}`, true);
  }
}

async function loadSpatialCameraSlugs() {
  const sel = sp$("#spatial-camera-slug");
  if (!sel) return;
  const tier = getSpatialPoseTier();
  try {
    const res = await fetch(`/api/spatial/cameras?pose_tier=${encodeURIComponent(tier)}`);
    if (!res.ok) throw new Error(await readSpatialApiError(res));
    const body = await res.json();
    const slugs = Array.isArray(body.camera_slugs) ? body.camera_slugs : [];
    const prev = sel.value;
    sel.innerHTML = '<option value="">— 请选择机位 slug —</option>';
    slugs.forEach((slug) => {
      const opt = document.createElement("option");
      opt.value = slug;
      opt.textContent = slug;
      sel.appendChild(opt);
    });
    if (prev && slugs.includes(prev)) sel.value = prev;
  } catch (err) {
    sel.innerHTML = '<option value="">— 加载失败 —</option>';
    setSpatialStatus(`机位列表加载失败：${err.message}`, true);
  }
}

function undoSpatialCurrentStep() {
  const step = getSpatialCalibStep();
  if (step === "volume") spatialState.volumeCorners.pop();
  else if (step === "columns") {
    if (spatialState.pendingLinePoint) clearPendingLinePoint();
    else spatialState.columnLines.pop();
  } else if (step === "left_layers") {
    if (spatialState.pendingLinePoint) clearPendingLinePoint();
    else spatialState.leftLayerLines.pop();
  } else if (step === "right_layers") {
    if (spatialState.pendingLinePoint) clearPendingLinePoint();
    else spatialState.rightLayerLines.pop();
  }
  spatialState.gridSegments = [];
  syncSpatialPointHint();
  renderSpatialCanvas();
}

function clearSpatialCurrentStep() {
  const step = getSpatialCalibStep();
  clearPendingLinePoint();
  if (step === "volume") spatialState.volumeCorners = [];
  else if (step === "columns") spatialState.columnLines = [];
  else if (step === "left_layers") spatialState.leftLayerLines = [];
  else if (step === "right_layers") spatialState.rightLayerLines = [];
  spatialState.gridSegments = [];
  syncSpatialPointHint();
  renderSpatialCanvas();
}

function bindSpatialCanvasEvents() {
  const canvas = sp$("#spatial-canvas");
  if (!canvas || canvas.dataset.bound) return;
  canvas.dataset.bound = "1";

  canvas.addEventListener("click", (ev) => {
    const [x, y] = canvasImageCoords(canvas, ev.clientX, ev.clientY);
    const step = getSpatialCalibStep();
    if (step === "volume") {
      if (spatialState.volumeCorners.length >= SPATIAL_VOLUME_POINTS) return;
      spatialState.volumeCorners.push([x, y]);
    } else if (step === "columns") {
      const pt = pickLinePointOnWireframe(step, x, y);
      if (!pt) return;
      addTwoPointLine(pt[0], pt[1], spatialState.columnLines);
    } else if (step === "left_layers") {
      if (!isLeftFaceEnabled()) return;
      const pt = pickLinePointOnWireframe(step, x, y);
      if (!pt) return;
      addTwoPointLine(pt[0], pt[1], spatialState.leftLayerLines);
    } else if (step === "right_layers") {
      if (!isRightFaceEnabled()) return;
      const pt = pickLinePointOnWireframe(step, x, y);
      if (!pt) return;
      addTwoPointLine(pt[0], pt[1], spatialState.rightLayerLines);
    }
    spatialState.gridSegments = [];
    syncSpatialPointHint();
    renderSpatialCanvas();
  });

  canvas.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    undoSpatialCurrentStep();
  });
}

function bindSpatialPanelEvents() {
  if (spatialState.initialized) return;
  spatialState.initialized = true;

  sp$("#spatial-load")?.addEventListener("click", () => {
    void loadSpatialSession();
  });
  sp$("#spatial-reload")?.addEventListener("click", () => {
    void loadSpatialSession({ keepPoints: false });
  });
  sp$("#spatial-undo")?.addEventListener("click", () => {
    undoSpatialCurrentStep();
  });
  sp$("#spatial-clear")?.addEventListener("click", () => {
    clearSpatialCurrentStep();
  });
  sp$("#spatial-preview")?.addEventListener("click", () => {
    void previewSpatialGrid();
  });
  sp$("#spatial-save")?.addEventListener("click", () => {
    void saveSpatialCalibration();
  });
  sp$("#spatial-pose-tier")?.addEventListener("change", () => {
    void loadSpatialCameraSlugs();
  });
  sp$("#spatial-infer-height")?.addEventListener("change", () => {
    syncSpatialPhysicalHint();
  });
  ["spatial-volume-width", "spatial-volume-depth", "spatial-volume-height"].forEach((id) => {
    sp$(`#${id}`)?.addEventListener("change", () => syncSpatialPhysicalHint());
  });
  sp$("#spatial-calib-step")?.addEventListener("change", () => {
    clearPendingLinePoint();
    spatialState.calibStep = getSpatialCalibStep();
    syncSpatialPointHint();
    renderSpatialCanvas();
  });
  ["spatial-left-face-enabled", "spatial-right-face-enabled"].forEach((id) => {
    sp$(`#${id}`)?.addEventListener("change", () => syncSpatialPointHint());
  });
  ["spatial-volume-width", "spatial-volume-depth", "spatial-volume-height"].forEach((id) => {
    sp$(`#${id}`)?.addEventListener("change", () => syncSpatialPointHint());
  });
  sp$("#spatial-camera-slug-manual")?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") void loadSpatialSession();
  });
  sp$("#spatial-camera-slug")?.addEventListener("change", () => {
    const manual = sp$("#spatial-camera-slug-manual");
    if (manual) manual.value = "";
  });

  bindSpatialCanvasEvents();
}

function initSpatialCalibratePanel() {
  bindSpatialPanelEvents();
  void fetch("/api/config/inference")
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      const h = Number(cfg?.height);
      if (h > 0 && sp$("#spatial-infer-height")) {
        sp$("#spatial-infer-height").value = String(h);
        spatialState.inferHeight = h;
      }
    })
    .catch(() => {})
    .then(() => loadSpatialCameraSlugs())
    .then(() => {
      syncSpatialPointHint();
      const slug = getSpatialSlug();
      if (slug && !spatialState.frameWidth) {
        void loadSpatialSession();
      }
    });
}

window.initSpatialCalibratePanel = initSpatialCalibratePanel;
