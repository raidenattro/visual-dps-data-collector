/** 回放 Ground Map（脚部 floor_xy 轨迹）与地面网格 / 视频足部轨迹 overlay */

const FOOT_ANKLE_LEFT = 15;
const FOOT_ANKLE_RIGHT = 16;
const FOOT_SCORE_MIN = 0.35;

let playbackSpatialContext = null;
let playbackFloorTrajectory = [];
let playbackFootUvTrajectory = [];
const playbackFloorMapState = {
  widthM: 2.0,
  depthM: 9.6,
  spacingM: 2.4,
};

function resetPlaybackFloorMap() {
  playbackSpatialContext = null;
  playbackFloorTrajectory = [];
  playbackFootUvTrajectory = [];
  const coords = document.getElementById("playback-floor-coords");
  if (coords) coords.textContent = "";
  const mapCanvas = document.getElementById("playback-floor-map");
  if (mapCanvas) {
    const mctx = mapCanvas.getContext("2d");
    if (mctx) mctx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
  }
  const panel = document.getElementById("playback-floor-map-panel");
  if (panel) panel.classList.add("hidden");
  const trailToggle = document.getElementById("playback-show-foot-trail");
  if (trailToggle) trailToggle.disabled = true;
}

function isPlaybackFootTrailEnabled() {
  const toggle = document.getElementById("playback-show-foot-trail");
  if (!toggle || toggle.disabled) return false;
  return Boolean(toggle.checked);
}

function hasPlaybackFootTrailData() {
  return playbackFootUvTrajectory.length > 0 || playbackFloorTrajectory.length > 0;
}

function syncPlaybackFootTrailToggle() {
  const toggle = document.getElementById("playback-show-foot-trail");
  if (!toggle) return;
  const hasData = hasPlaybackFootTrailData();
  toggle.disabled = !hasData;
  if (hasData && !toggle.dataset.userTouched) {
    toggle.checked = true;
  }
}

async function loadPlaybackSpatialContext(recordId) {
  resetPlaybackFloorMap();
  if (!recordId) return;
  try {
    const res = await fetch(recordApiUrl(recordId, "/spatial"));
    if (!res.ok) return;
    playbackSpatialContext = await res.json();
    const vis = playbackSpatialContext?.calibration?.visualization || {};
    playbackFloorMapState.widthM = Number(vis.grid_width_m) || 2.0;
    playbackFloorMapState.depthM = Number(vis.grid_depth_m) || 9.6;
    playbackFloorMapState.spacingM = Number(vis.grid_spacing_m) || 2.4;
  } catch (_err) {
    playbackSpatialContext = null;
  }

  try {
    const tlRes = await fetch(recordApiUrl(recordId, "/timeline"));
    if (!tlRes.ok) return;
    const body = await tlRes.json();
    playbackFloorTrajectory = (body.timeline || [])
      .filter((row) => Array.isArray(row.floor_xy_m) && row.floor_xy_m.length >= 2)
      .map((row) => ({
        frameIdx: Number(row.frame_idx) || 0,
        t: Number(row.timestamp_sec) || 0,
        xy: [Number(row.floor_xy_m[0]), Number(row.floor_xy_m[1])],
      }));
    playbackFootUvTrajectory = (body.timeline || [])
      .filter((row) => Array.isArray(row.foot_uv_px) && row.foot_uv_px.length >= 2)
      .map((row) => ({
        frameIdx: Number(row.frame_idx) || 0,
        uv: [Number(row.foot_uv_px[0]), Number(row.foot_uv_px[1])],
      }));
  } catch (_err) {
    playbackFloorTrajectory = [];
    playbackFootUvTrajectory = [];
  }

  const panel = document.getElementById("playback-floor-map-panel");
  const calibEnabled =
    playbackSpatialContext?.spatial?.floor_xy_enabled ||
    playbackSpatialContext?.calibration?.enabled;
  const hasFloor = playbackFloorTrajectory.length > 0;
  if (panel) panel.classList.toggle("hidden", !calibEnabled && !hasFloor);
  syncPlaybackFootTrailToggle();
}

function floorMapToPixel(x, y, canvas) {
  const { widthM, depthM } = playbackFloorMapState;
  const scale = Math.min(
    (canvas.width - 96) / Math.max(depthM, 0.1),
    (canvas.height - 48) / Math.max(widthM, 0.1)
  );
  const originX = 48;
  const centerY = canvas.height * 0.52;
  return [
    originX + y * scale,
    centerY + (x - widthM / 2) * scale,
  ];
}

function playbackFloorTrailUntilFrame(currentFrameIdx) {
  const fi = Number(currentFrameIdx) || 0;
  if (fi <= 0) return playbackFloorTrajectory;
  return playbackFloorTrajectory.filter((pt) => pt.frameIdx > 0 && pt.frameIdx <= fi);
}

function pickPrimaryPersonForFoot(frame) {
  const persons = frame?.persons || [];
  if (!persons.length) return null;
  let best = null;
  let bestScore = -1;
  persons.forEach((person) => {
    if (!person || !Array.isArray(person.keypoints)) return;
    const kpts = person.keypoints;
    const ls = kpts[5];
    const rs = kpts[6];
    const la = kpts[FOOT_ANKLE_LEFT];
    const ra = kpts[FOOT_ANKLE_RIGHT];
    const laOk = la && la.length >= 3 && Number(la[2]) >= FOOT_SCORE_MIN;
    const raOk = ra && ra.length >= 3 && Number(ra[2]) >= FOOT_SCORE_MIN;
    if (!laOk && !raOk) return;
    let width = 0;
    if (
      ls &&
      rs &&
      ls.length >= 3 &&
      rs.length >= 3 &&
      Number(ls[2]) > 0.2 &&
      Number(rs[2]) > 0.2
    ) {
      width = Math.abs(Number(ls[0]) - Number(rs[0]));
    }
    const ankleVis =
      (laOk ? Number(la[2]) : 0) + (raOk ? Number(ra[2]) : 0);
    const score = width + ankleVis * 100;
    if (score > bestScore) {
      bestScore = score;
      best = person;
    }
  });
  return best || persons[0] || null;
}

function footUvFromPerson(person) {
  if (!person?.keypoints) return null;
  const feet = [];
  [FOOT_ANKLE_LEFT, FOOT_ANKLE_RIGHT].forEach((idx) => {
    const kp = person.keypoints[idx];
    if (!kp || kp.length < 3 || Number(kp[2]) < FOOT_SCORE_MIN) return;
    feet.push([Number(kp[0]), Number(kp[1])]);
  });
  if (!feet.length) return null;
  const sx = feet.reduce((s, p) => s + p[0], 0) / feet.length;
  const sy = feet.reduce((s, p) => s + p[1], 0) / feet.length;
  return [sx, sy];
}

function resolveFootUvAtFrame(frameIdx, frame) {
  if (Array.isArray(frame?.foot_uv_px) && frame.foot_uv_px.length >= 2) {
    return [Number(frame.foot_uv_px[0]), Number(frame.foot_uv_px[1])];
  }
  const person = pickPrimaryPersonForFoot(frame);
  const live = person ? footUvFromPerson(person) : null;
  if (live) return live;
  const fi = Number(frameIdx) || 0;
  if (!fi) return null;
  const hit = playbackFootUvTrajectory.find((p) => p.frameIdx === fi);
  return hit?.uv || null;
}

function resolveFloorXyAtFrame(frameIdx, hit, frame) {
  if (Array.isArray(hit?.floor_xy_m) && hit.floor_xy_m.length >= 2) {
    return hit.floor_xy_m;
  }
  if (Array.isArray(frame?.floor_xy_m) && frame.floor_xy_m.length >= 2) {
    return frame.floor_xy_m;
  }
  const fi = Number(frameIdx) || 0;
  if (!fi) return null;
  const row = playbackFloorTrajectory.find((p) => p.frameIdx === fi);
  return row?.xy || null;
}

function drawPlaybackFootTrailOverlay(frame, inferW, inferH, layout, currentFrameIdx) {
  if (!isPlaybackFootTrailEnabled() || !hasPlaybackFootTrailData()) return;
  const fi = Number(currentFrameIdx) || Number(frame?.frame_idx) || 0;
  if (fi <= 0) return;

  const trail = playbackFootUvTrajectory.filter((pt) => pt.frameIdx > 0 && pt.frameIdx <= fi);
  if (trail.length >= 2) {
    ctx.save();
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    for (let i = 1; i < trail.length; i += 1) {
      const prev = trail[i - 1];
      const curr = trail[i];
      const alpha = 0.25 + (i / trail.length) * 0.65;
      ctx.strokeStyle = `rgba(225, 130, 45, ${alpha.toFixed(3)})`;
      ctx.lineWidth = 2.5;
      const [x0, y0] = mapInferToDisplay(prev.uv[0], prev.uv[1], inferW, inferH, layout);
      const [x1, y1] = mapInferToDisplay(curr.uv[0], curr.uv[1], inferW, inferH, layout);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }
    ctx.restore();
  }

  const currentUv = resolveFootUvAtFrame(fi, frame);
  if (!currentUv) return;

  const person = pickPrimaryPersonForFoot(frame);
  if (person?.keypoints) {
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "rgba(225, 130, 45, 0.55)";
    [FOOT_ANKLE_LEFT, FOOT_ANKLE_RIGHT].forEach((idx) => {
      const kp = person.keypoints[idx];
      if (!kp || kp.length < 3 || Number(kp[2]) < FOOT_SCORE_MIN) return;
      const [ax, ay] = mapInferToDisplay(Number(kp[0]), Number(kp[1]), inferW, inferH, layout);
      const [fx, fy] = mapInferToDisplay(currentUv[0], currentUv[1], inferW, inferH, layout);
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.lineTo(fx, fy);
      ctx.stroke();
      ctx.fillStyle = "rgba(225, 130, 45, 0.85)";
      ctx.beginPath();
      ctx.arc(ax, ay, 4, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  const [x, y] = mapInferToDisplay(currentUv[0], currentUv[1], inferW, inferH, layout);
  ctx.save();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.98)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(x, y, 10, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "rgba(40, 67, 235, 0.94)";
  ctx.beginPath();
  ctx.arc(x, y, 7, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x - 14, y);
  ctx.lineTo(x + 14, y);
  ctx.moveTo(x, y - 14);
  ctx.lineTo(x, y + 14);
  ctx.stroke();
  ctx.fillStyle = "rgba(255, 255, 255, 0.95)";
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillText("FOOT", x + 12, y - 10);
  ctx.restore();
}

function drawPlaybackFloorMapCanvas(currentXy, currentFrameIdx) {
  const canvas = document.getElementById("playback-floor-map");
  if (!canvas) return;
  const mctx = canvas.getContext("2d");
  if (!mctx) return;
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(180, canvas.clientWidth || 240);
  const cssH = Math.max(140, canvas.clientHeight || 180);
  if (canvas.width !== Math.floor(cssW * dpr) || canvas.height !== Math.floor(cssH * dpr)) {
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  const { widthM, depthM, spacingM } = playbackFloorMapState;
  mctx.fillStyle = "#f7f7f4";
  mctx.fillRect(0, 0, cssW, cssH);
  mctx.fillStyle = "#373737";
  mctx.font = "12px sans-serif";
  mctx.fillText(`GROUND MAP ${widthM.toFixed(1)} m × ${depthM.toFixed(1)} m`, 10, 18);

  const p0 = floorMapToPixel(0, 0, canvas);
  const p1 = floorMapToPixel(widthM, depthM, canvas);
  mctx.strokeStyle = "#505050";
  mctx.lineWidth = 2;
  mctx.strokeRect(
    Math.min(p0[0], p1[0]),
    Math.min(p0[1], p1[1]),
    Math.abs(p1[0] - p0[0]),
    Math.abs(p1[1] - p0[1])
  );

  mctx.strokeStyle = "#b2b2b2";
  mctx.lineWidth = 1;
  for (let y = 0; y <= depthM + 1e-6; y += spacingM) {
    const a = floorMapToPixel(0, y, canvas);
    const b = floorMapToPixel(widthM, y, canvas);
    mctx.beginPath();
    mctx.moveTo(a[0], a[1]);
    mctx.lineTo(b[0], b[1]);
    mctx.stroke();
    mctx.fillStyle = "#5f5f5f";
    mctx.font = "10px sans-serif";
    mctx.fillText(y.toFixed(1), a[0] - 10, a[1] + 14);
  }

  const trail = playbackFloorTrailUntilFrame(currentFrameIdx);
  if (trail.length > 1) {
    mctx.lineJoin = "round";
    mctx.lineCap = "round";
    for (let i = 1; i < trail.length; i += 1) {
      const alpha = 0.25 + (i / trail.length) * 0.65;
      mctx.strokeStyle = `rgba(225, 130, 45, ${alpha.toFixed(3)})`;
      mctx.lineWidth = 2;
      const [x0, y0] = floorMapToPixel(trail[i - 1].xy[0], trail[i - 1].xy[1], canvas);
      const [x1, y1] = floorMapToPixel(trail[i].xy[0], trail[i].xy[1], canvas);
      mctx.beginPath();
      mctx.moveTo(x0, y0);
      mctx.lineTo(x1, y1);
      mctx.stroke();
    }
  }

  if (currentXy && currentXy.length >= 2) {
    const [cx, cy] = floorMapToPixel(currentXy[0], currentXy[1], canvas);
    mctx.strokeStyle = "#ffffff";
    mctx.lineWidth = 2;
    mctx.beginPath();
    mctx.arc(cx, cy, 8, 0, Math.PI * 2);
    mctx.stroke();
    mctx.fillStyle = "#2843eb";
    mctx.beginPath();
    mctx.arc(cx, cy, 5, 0, Math.PI * 2);
    mctx.fill();
    mctx.fillStyle = "#2d2d2d";
    mctx.font = "11px sans-serif";
    mctx.fillText(
      `X=${currentXy[0].toFixed(2)}  Y=${currentXy[1].toFixed(2)} m`,
      10,
      cssH - 8
    );
  }
}

function updatePlaybackFloorMapUi(hit, frame) {
  const frameIdx =
    Number(hit?.frameIdx) ||
    Number(hit?.frame_idx) ||
    Number(frame?.frame_idx) ||
    Number(typeof lastRenderedFrameIdx !== "undefined" ? lastRenderedFrameIdx : 0) ||
    0;
  const coords = document.getElementById("playback-floor-coords");
  const xy = resolveFloorXyAtFrame(frameIdx, hit, frame);
  if (coords) {
    coords.textContent = xy
      ? `floor X=${xy[0].toFixed(2)} Y=${xy[1].toFixed(2)} m · 帧 ${frameIdx}`
      : frameIdx > 0
        ? `帧 ${frameIdx} · 无 floor_xy`
        : "";
  }
  drawPlaybackFloorMapCanvas(xy, frameIdx);
}

function bindPlaybackFloorMapControls() {
  const gridToggle = document.getElementById("playback-show-floor-grid");
  if (gridToggle && !gridToggle.dataset.bound) {
    gridToggle.dataset.bound = "1";
    gridToggle.addEventListener("change", () => refreshPlaybackSpatialOverlay());
  }

  const trailToggle = document.getElementById("playback-show-foot-trail");
  if (trailToggle && !trailToggle.dataset.bound) {
    trailToggle.dataset.bound = "1";
    trailToggle.addEventListener("change", (e) => {
      trailToggle.dataset.userTouched = "1";
      if (!e.target.checked) return;
      refreshPlaybackSpatialOverlay();
    });
  }
}

function refreshPlaybackSpatialOverlay() {
  if (typeof playbackRenderLoopActive !== "undefined" && playbackRenderLoopActive && videoEl && !videoEl.paused) {
    const { w, h } = typeof playbackInferSize === "function" ? playbackInferSize() : { w: 852, h: 480 };
    const fi =
      typeof tickPoseFrameIdx !== "undefined" && tickPoseFrameIdx > 0
        ? tickPoseFrameIdx
        : lastRenderedFrameIdx;
    const frame = fi > 0 ? frameCache.get(fi) : null;
    if (frame && typeof drawSkeletonPlaybackOnly === "function") {
      drawSkeletonPlaybackOnly(frame, w, h);
    }
    if (fi > 0 && typeof updatePlaybackFloorMapUi === "function") {
      const hit = frameByTime?.find((r) => r.frameIdx === fi) || { frameIdx: fi };
      updatePlaybackFloorMapUi(hit, frame);
    }
    return;
  }
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
}

function drawSpatialGroundGridOverlay(inferW, inferH) {
  const toggle = document.getElementById("playback-show-floor-grid");
  if (!toggle?.checked) return;
  const segs = playbackSpatialContext?.calibration?.grid_segments;
  if (!Array.isArray(segs) || !segs.length) return;
  const calW = Number(playbackSpatialContext?.calibration?.infer_width) || 0;
  const calH = Number(playbackSpatialContext?.calibration?.infer_height) || 0;
  const mapW = calW > 0 ? calW : inferW;
  const mapH = calH > 0 ? calH : inferH;
  const layout =
    typeof frozenPlaybackLayout !== "undefined" &&
    frozenPlaybackLayout &&
    typeof playbackRenderLoopActive !== "undefined" &&
    playbackRenderLoopActive
      ? frozenPlaybackLayout
      : getDisplayLayout();
  ctx.strokeStyle = "rgba(80, 210, 80, 0.85)";
  ctx.lineWidth = 2;
  segs.forEach((seg) => {
    const img = seg.image;
    if (!Array.isArray(img) || img.length < 2) return;
    const p0 = mapInferToDisplay(img[0][0], img[0][1], mapW, mapH, layout);
    const p1 = mapInferToDisplay(img[1][0], img[1][1], mapW, mapH, layout);
    ctx.beginPath();
    ctx.moveTo(p0[0], p0[1]);
    ctx.lineTo(p1[0], p1[1]);
    ctx.stroke();
  });
}

document.addEventListener("DOMContentLoaded", bindPlaybackFloorMapControls);
