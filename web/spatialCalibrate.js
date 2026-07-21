/** 地面标定页：在机位视频帧上点击 10 地面控制点，保存 spatial JSON */

const SPATIAL_EXPECTED_POINTS = 10;
const SPATIAL_POINT_LABELS = [
  "远-左", "远-右",
  "远2-左", "远2-右",
  "中-左", "中-右",
  "近2-左", "近2-右",
  "近-左", "近-右",
];

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
  gridSegments: [],
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
  const physical = readSpatialPhysical();
  const depth = physical.marker_spacing_m * Math.max(0, physical.marker_pairs - 1);
  const hint = sp$("#spatial-point-hint");
  if (!hint) return;
  if (physical.marker_spacing_m < 1.0) {
    hint.dataset.spacingWarn = "1";
  } else {
    delete hint.dataset.spacingWarn;
  }
  if (depth > 0 && depth < 4) {
    setSpatialRmse(
      `提示：当前网格深度仅 ${depth.toFixed(1)} m（行间距 ${physical.marker_spacing_m} m），通道通常约 9.6 m；请确认 physical 参数`,
      true
    );
  }
}

function readSpatialPhysical() {
  const pairs = Math.max(1, Math.min(10, Number(sp$("#spatial-marker-pairs")?.value) || 5));
  const spacing = Math.max(0.1, Number(sp$("#spatial-marker-spacing")?.value) || 2.4);
  const width = Math.max(0.1, Number(sp$("#spatial-aisle-width")?.value) || 2.0);
  return {
    aisle_width_m: width,
    marker_spacing_m: spacing,
    marker_pairs: pairs,
  };
}

function syncSpatialPointHint() {
  const hint = sp$("#spatial-point-hint");
  if (!hint) return;
  const n = spatialState.imagePoints.length;
  const nextLabel = SPATIAL_POINT_LABELS[n] || `P${n + 1}`;
  if (!spatialState.slug) {
    hint.textContent = "请选择或输入机位 slug，然后点击「加载」";
    return;
  }
  if (n >= SPATIAL_EXPECTED_POINTS) {
    hint.textContent = `已点满 ${SPATIAL_EXPECTED_POINTS} 个控制点，可预览网格或保存`;
    return;
  }
  hint.textContent = `下一步：${nextLabel}（${n + 1}/${SPATIAL_EXPECTED_POINTS}）`;
}

function buildSpatialConfigBody() {
  const physical = readSpatialPhysical();
  const depth = physical.marker_spacing_m * Math.max(0, physical.marker_pairs - 1);
  const base = spatialState.loadedConfig && typeof spatialState.loadedConfig === "object"
    ? structuredClone(spatialState.loadedConfig)
    : {
        schema: 1,
        camera_slug: spatialState.slug,
        enabled: false,
        physical,
        calibration: { resolution: [0, 0], image_points_px: [] },
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

  base.camera_slug = spatialState.slug;
  base.enabled = !!sp$("#spatial-enabled")?.checked;
  base.physical = physical;
  base.calibration = {
    resolution: [spatialState.frameWidth, spatialState.frameHeight],
    image_points_px: spatialState.imagePoints.map((p) => [p[0], p[1]]),
  };
  base.visualization = {
    grid_width_m: physical.aisle_width_m,
    grid_depth_m: depth || 9.6,
    grid_spacing_m: physical.marker_spacing_m,
  };
  return base;
}

function applySpatialConfigToForm(cfg, targetW = 0, targetH = 0) {
  if (!cfg || typeof cfg !== "object") return;
  spatialState.loadedConfig = cfg;
  const physical = cfg.physical || {};
  if (sp$("#spatial-aisle-width")) {
    sp$("#spatial-aisle-width").value = String(physical.aisle_width_m ?? 2.0);
  }
  if (sp$("#spatial-marker-spacing")) {
    sp$("#spatial-marker-spacing").value = String(physical.marker_spacing_m ?? 2.4);
  }
  if (sp$("#spatial-marker-pairs")) {
    sp$("#spatial-marker-pairs").value = String(physical.marker_pairs ?? 5);
  }
  if (sp$("#spatial-enabled")) {
    sp$("#spatial-enabled").checked = cfg.enabled !== false;
  }
  const res = cfg.calibration?.resolution || [0, 0];
  const fromW = Number(res[0]) || 0;
  const fromH = Number(res[1]) || 0;
  const pts = cfg.calibration?.image_points_px;
  if (Array.isArray(pts) && pts.length) {
    let scaled = pts
      .filter((p) => Array.isArray(p) && p.length >= 2)
      .map((p) => [Number(p[0]), Number(p[1])]);
    if (targetW > 0 && targetH > 0 && fromW > 0 && fromH > 0) {
      scaled = scaleSpatialImagePoints(scaled, fromW, fromH, targetW, targetH);
    }
    spatialState.imagePoints = scaled;
  }
  syncSpatialPhysicalHint();
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

function renderSpatialCanvas() {
  const canvas = sp$("#spatial-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx || !spatialState.bgImage?.complete) return;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(spatialState.bgImage, 0, 0, canvas.width, canvas.height);

  if (Array.isArray(spatialState.gridSegments) && spatialState.gridSegments.length) {
    ctx.strokeStyle = "rgba(80, 210, 80, 0.9)";
    ctx.lineWidth = 2;
    spatialState.gridSegments.forEach((seg) => {
      const img = seg.image;
      if (!Array.isArray(img) || img.length < 2) return;
      ctx.beginPath();
      ctx.moveTo(img[0][0], img[0][1]);
      ctx.lineTo(img[1][0], img[1][1]);
      ctx.stroke();
    });
  }

  spatialState.imagePoints.forEach((pt, i) => {
    const x = pt[0];
    const y = pt[1];
    ctx.fillStyle = "#f31414";
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fill();
    const label = SPATIAL_POINT_LABELS[i] || `P${i + 1}`;
    ctx.font = "14px sans-serif";
    ctx.fillStyle = "#ffffff";
    ctx.strokeStyle = "#000000";
    ctx.lineWidth = 3;
    ctx.strokeText(label, x + 10, y - 8);
    ctx.fillText(label, x + 10, y - 8);
  });
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
    spatialState.imagePoints = [];
    spatialState.gridSegments = [];
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
  if (spatialState.imagePoints.length !== SPATIAL_EXPECTED_POINTS) {
    setSpatialStatus(`需要 ${SPATIAL_EXPECTED_POINTS} 个控制点才能预览`, true);
    return;
  }
  setSpatialStatus("正在计算网格预览…");
  const body = buildSpatialConfigBody();
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
    renderSpatialCanvas();
    const rmse = Number(payload.ground_control_rmse_px);
    if (Number.isFinite(rmse)) {
      setSpatialRmse(`预览 RMSE: ${rmse.toFixed(2)} px${rmse >= 8 ? "（建议 < 8 px）" : ""}`, rmse >= 8);
    }
    setSpatialStatus("网格预览已更新");
  } catch (err) {
    setSpatialStatus(`❌ 预览失败：${err.message || err}`, true);
  }
}

async function saveSpatialCalibration() {
  if (spatialState.imagePoints.length !== SPATIAL_EXPECTED_POINTS) {
    setSpatialStatus(`保存需要 ${SPATIAL_EXPECTED_POINTS} 个控制点`, true);
    return;
  }
  const slug = spatialState.slug || getSpatialSlug();
  if (!slug) {
    setSpatialStatus("机位 slug 为空", true);
    return;
  }
  const body = buildSpatialConfigBody();
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
    spatialState.gridSegments = Array.isArray(saved.grid_segments) ? saved.grid_segments : [];
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

function bindSpatialCanvasEvents() {
  const canvas = sp$("#spatial-canvas");
  if (!canvas || canvas.dataset.bound) return;
  canvas.dataset.bound = "1";

  canvas.addEventListener("click", (ev) => {
    if (spatialState.imagePoints.length >= SPATIAL_EXPECTED_POINTS) return;
    const [x, y] = canvasImageCoords(canvas, ev.clientX, ev.clientY);
    spatialState.imagePoints.push([x, y]);
    spatialState.gridSegments = [];
    syncSpatialPointHint();
    renderSpatialCanvas();
  });

  canvas.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    if (!spatialState.imagePoints.length) return;
    spatialState.imagePoints.pop();
    spatialState.gridSegments = [];
    syncSpatialPointHint();
    renderSpatialCanvas();
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
    spatialState.imagePoints.pop();
    spatialState.gridSegments = [];
    syncSpatialPointHint();
    renderSpatialCanvas();
  });
  sp$("#spatial-clear")?.addEventListener("click", () => {
    spatialState.imagePoints = [];
    spatialState.gridSegments = [];
    syncSpatialPointHint();
    renderSpatialCanvas();
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
  ["spatial-aisle-width", "spatial-marker-spacing", "spatial-marker-pairs"].forEach((id) => {
    sp$(`#${id}`)?.addEventListener("change", () => syncSpatialPhysicalHint());
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
