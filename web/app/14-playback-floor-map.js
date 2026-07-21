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

function footTrailTailMaxFrames() {
  const rt =
    playbackSpatialContext?.calibration?.runtime ||
    playbackSpatialContext?.calibration?.config?.runtime ||
    {};
  return Math.max(15, Number(rt.trail_tail_frames) || 90);
}

function isWithinGroundMapXy(xy) {
  if (!Array.isArray(xy) || xy.length < 2) return false;
  const { widthM, depthM } = playbackFloorMapState;
  const x = Number(xy[0]);
  const y = Number(xy[1]);
  return x >= 0 && x <= widthM && y >= 0 && y <= depthM;
}

function filterFootTrailTail(points, currentFrameIdx) {
  const fi = Number(currentFrameIdx) || 0;
  if (fi <= 0 || !points?.length) return [];
  const minFrame = Math.max(1, fi - footTrailTailMaxFrames() + 1);
  return points.filter((pt) => pt.frameIdx >= minFrame && pt.frameIdx <= fi);
}

function playbackFootTrailPointsForFrame(currentFrameIdx, space = "floor") {
  const fi = Number(currentFrameIdx) || 0;
  if (fi <= 0) return [];
  const source = space === "uv" ? playbackFootUvTrajectory : playbackFloorTrajectory;
  const inRange = source.filter((pt) => pt.frameIdx > 0 && pt.frameIdx <= fi);
  return filterFootTrailTail(inRange, fi);
}

function footTrailBreakOptions(inferW = 852) {
  const tuning =
    playbackSpatialContext?.calibration?.config?.tuning ||
    playbackSpatialContext?.calibration?.runtime ||
    {};
  const jumpM = Number(tuning.smooth_jump_threshold_m) || 1.4;
  const maxFrameGap = Number(tuning.sticky_max_frame_gap) || 25;
  const uvRatio = Number(tuning.sticky_max_uv_jump_ratio) || 0.12;
  return {
    jumpM,
    maxFrameGap,
    maxUvJumpPx: Math.max(40, inferW * uvRatio),
  };
}

function shouldBreakFootTrail(prev, curr, opts) {
  if (!prev || !curr) return true;
  if (
    prev.trailSegmentId != null &&
    curr.trailSegmentId != null &&
    prev.trailSegmentId !== curr.trailSegmentId
  ) {
    return true;
  }
  const frameGap = Number(curr.frameIdx) - Number(prev.frameIdx);
  if (frameGap > opts.maxFrameGap) return true;
  if (
    prev.personId >= 0 &&
    curr.personId >= 0 &&
    prev.personId !== curr.personId
  ) {
    return true;
  }
  if (prev.xy && curr.xy) {
    const dx = curr.xy[0] - prev.xy[0];
    const dy = curr.xy[1] - prev.xy[1];
    if (Math.hypot(dx, dy) > opts.jumpM) return true;
  }
  if (prev.uv && curr.uv) {
    const du = curr.uv[0] - prev.uv[0];
    const dv = curr.uv[1] - prev.uv[1];
    if (Math.hypot(du, dv) > opts.maxUvJumpPx) return true;
  }
  return false;
}

function splitFootTrailPoints(points, opts) {
  if (!points?.length) return [];
  const sorted = [...points].sort((a, b) => a.frameIdx - b.frameIdx);
  const segments = [];
  let seg = [sorted[0]];
  for (let i = 1; i < sorted.length; i += 1) {
    if (shouldBreakFootTrail(sorted[i - 1], sorted[i], opts)) {
      if (seg.length) segments.push(seg);
      seg = [sorted[i]];
    } else {
      seg.push(sorted[i]);
    }
  }
  if (seg.length) segments.push(seg);
  return segments;
}

function mergeFloorXyOntoUvTrail(uvTrail) {
  const floorByFrame = new Map(playbackFloorTrajectory.map((p) => [p.frameIdx, p]));
  return uvTrail.map((pt) => ({
    ...pt,
    xy: floorByFrame.get(pt.frameIdx)?.xy || pt.xy || null,
  }));
}

function drawFootTrailSegments(ctx, segments, drawSegmentLine) {
  const flat = segments.flat();
  if (flat.length < 2) return;
  const minFrame = flat[0].frameIdx;
  const maxFrame = flat[flat.length - 1].frameIdx;
  const span = Math.max(1, maxFrame - minFrame);
  segments.forEach((seg) => {
    if (seg.length < 2) return;
    for (let i = 1; i < seg.length; i += 1) {
      const t = (Number(seg[i].frameIdx) - minFrame) / span;
      const alpha = 0.08 + t * 0.87;
      drawSegmentLine(seg[i - 1], seg[i], alpha);
    }
  });
}

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
    const footRes = await fetch(recordApiUrl(recordId, "/floor-foot"));
    if (footRes.ok) {
      const body = await footRes.json();
      const rows = body.rows || [];
      playbackFloorTrajectory = rows
        .filter((row) => Array.isArray(row.floor_xy_m) && row.floor_xy_m.length >= 2)
        .filter((row) => isWithinGroundMapXy(row.floor_xy_m))
        .map((row) => ({
          frameIdx: Number(row.frame_idx) || 0,
          t: Number(row.timestamp_sec) || 0,
          xy: [Number(row.floor_xy_m[0]), Number(row.floor_xy_m[1])],
          personId: Number(row.person_id),
          personTrackId: Number(row.person_track_id) || 0,
          trailSegmentId:
            row.trail_segment_id != null ? Number(row.trail_segment_id) : null,
        }));
      playbackFootUvTrajectory = rows
        .filter((row) => Array.isArray(row.foot_uv_px) && row.foot_uv_px.length >= 2)
        .filter((row) => isWithinGroundMapXy(row.floor_xy_m))
        .map((row) => ({
          frameIdx: Number(row.frame_idx) || 0,
          uv: [Number(row.foot_uv_px[0]), Number(row.foot_uv_px[1])],
          personId: Number(row.person_id),
          personTrackId: Number(row.person_track_id) || 0,
          trailSegmentId:
            row.trail_segment_id != null ? Number(row.trail_segment_id) : null,
        }));
    }
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
  return playbackFootTrailPointsForFrame(currentFrameIdx, "floor");
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

  const trail = playbackFootTrailPointsForFrame(fi, "uv");
  const trailWithFloor = mergeFloorXyOntoUvTrail(trail);
  const opts = footTrailBreakOptions(inferW);
  const segments = splitFootTrailPoints(trailWithFloor, opts);
  if (segments.length) {
    ctx.save();
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    drawFootTrailSegments(ctx, segments, (prev, curr, alpha) => {
      ctx.strokeStyle = `rgba(225, 130, 45, ${alpha.toFixed(3)})`;
      ctx.lineWidth = 2.5;
      const [x0, y0] = mapInferToDisplay(prev.uv[0], prev.uv[1], inferW, inferH, layout);
      const [x1, y1] = mapInferToDisplay(curr.uv[0], curr.uv[1], inferW, inferH, layout);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    });
    ctx.restore();
  }

  const currentUv = resolveFootUvAtFrame(fi, frame);
  const currentXy = resolveFloorXyAtFrame(fi, null, frame);
  const showCurrentFoot = currentUv && currentXy && isWithinGroundMapXy(currentXy);
  if (!showCurrentFoot) return;

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

  const trail = isPlaybackFootTrailEnabled()
    ? playbackFootTrailPointsForFrame(currentFrameIdx, "floor")
    : [];
  const opts = footTrailBreakOptions();
  const segments = splitFootTrailPoints(trail, opts);
  if (segments.length) {
    mctx.save();
    mctx.beginPath();
    mctx.rect(
      Math.min(p0[0], p1[0]),
      Math.min(p0[1], p1[1]),
      Math.abs(p1[0] - p0[0]),
      Math.abs(p1[1] - p0[1])
    );
    mctx.clip();
    mctx.lineJoin = "round";
    mctx.lineCap = "round";
    drawFootTrailSegments(mctx, segments, (prev, curr, alpha) => {
      mctx.strokeStyle = `rgba(225, 130, 45, ${alpha.toFixed(3)})`;
      mctx.lineWidth = 2;
      const [x0, y0] = floorMapToPixel(prev.xy[0], prev.xy[1], canvas);
      const [x1, y1] = floorMapToPixel(curr.xy[0], curr.xy[1], canvas);
      mctx.beginPath();
      mctx.moveTo(x0, y0);
      mctx.lineTo(x1, y1);
      mctx.stroke();
    });
    mctx.restore();
  }

  const showCurrentMarker = currentXy && currentXy.length >= 2;
  if (showCurrentMarker) {
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
  const xyInMap = xy && isWithinGroundMapXy(xy) ? xy : null;
  if (coords) {
    coords.textContent = xyInMap
      ? `floor X=${xyInMap[0].toFixed(2)} Y=${xyInMap[1].toFixed(2)} m · 帧 ${frameIdx}`
      : frameIdx > 0
        ? `帧 ${frameIdx} · 无 floor_xy`
        : "";
  }
  drawPlaybackFloorMapCanvas(xyInMap, frameIdx);
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
