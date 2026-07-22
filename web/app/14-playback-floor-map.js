/** 回放 Ground Map（脚部 floor_xy 轨迹）与地面网格 / 视频足部轨迹 overlay */

const FOOT_ANKLE_LEFT = 15;
const FOOT_ANKLE_RIGHT = 16;
const WRIST_LEFT = 9;
const WRIST_RIGHT = 10;
const FOOT_SCORE_MIN = 0.35;
const WRIST_SCORE_MIN = 0.35;

let playbackSpatialContext = null;
let playbackFloorTrajectory = [];
let playbackFootUvTrajectory = [];
let playbackWristTrajectories = { left: [], right: [] };
let playbackVolumeOverlay = {
  wireframe: [],
  columnLines: [],
  layerLines: { left: [], right: [] },
  boundariesXM: [],
  boundariesYM: [],
  columnAxis: "x",
  columnCount: 0,
  volumeEnabled: false,
};
const playbackFloorMapState = {
  widthM: 2.0,
  depthM: 9.6,
  spacingM: 2.4,
};
const playbackFaceMapState = {
  depthM: 9.6,
  heightM: 2.4,
};

const WRIST_HAND_COLORS = {
  left: { fill: "rgba(40, 120, 235, 0.94)", stroke: "rgba(80, 160, 255, 0.95)", trail: "rgba(40, 120, 235" },
  right: { fill: "rgba(235, 120, 40, 0.94)", stroke: "rgba(255, 160, 80, 0.95)", trail: "rgba(235, 120, 40" },
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
  playbackWristTrajectories = { left: [], right: [] };
  playbackVolumeOverlay = {
    wireframe: [],
    columnLines: [],
    layerLines: { left: [], right: [] },
    boundariesXM: [],
    boundariesYM: [],
    columnAxis: "x",
    columnCount: 0,
    volumeEnabled: false,
  };
  const coords = document.getElementById("playback-floor-coords");
  if (coords) coords.textContent = "";
  const mapCanvas = document.getElementById("playback-floor-map");
  if (mapCanvas) {
    const mctx = mapCanvas.getContext("2d");
    if (mctx) mctx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
  }
  ["playback-left-map", "playback-right-map"].forEach((id) => {
    const c = document.getElementById(id);
    if (!c) return;
    const ctx = c.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, c.width, c.height);
  });
  ["playback-floor-map-panel", "playback-left-map-panel", "playback-right-map-panel"].forEach((id) => {
    document.getElementById(id)?.classList.add("hidden");
  });
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

function applyPlaybackSpatialGridFromContext() {
  const vis = playbackSpatialContext?.calibration?.visualization || {};
  if (vis.grid_width_m != null) playbackFloorMapState.widthM = Number(vis.grid_width_m) || 2.0;
  if (vis.grid_depth_m != null) playbackFloorMapState.depthM = Number(vis.grid_depth_m) || 9.6;
  if (vis.grid_spacing_m != null) playbackFloorMapState.spacingM = Number(vis.grid_spacing_m) || 2.4;
  const cal = playbackSpatialContext?.calibration || {};
  const cfg = cal.config || cal;
  const vol = cal.volume || cfg.volume || {};
  if (vol.depth_m != null) playbackFaceMapState.depthM = Number(vol.depth_m) || playbackFloorMapState.depthM;
  if (vol.height_m != null) playbackFaceMapState.heightM = Number(vol.height_m) || 2.4;
}

function ensurePlaybackSpatialContextFallback(recordId) {
  if (playbackSpatialContext) return;
  const spatialMeta =
    typeof poseData !== "undefined" && poseData?.spatial && typeof poseData.spatial === "object"
      ? poseData.spatial
      : null;
  if (!spatialMeta) return;
  playbackSpatialContext = {
    record_id: recordId,
    spatial: spatialMeta,
    infer_width: Number(poseData.infer_width) || 0,
    infer_height: Number(poseData.infer_height) || 0,
    calibration: null,
  };
}

function syncPlaybackSpatialVolumeToggle() {
  const volToggle = document.getElementById("playback-show-volume-wireframe");
  if (volToggle) {
    const canShow =
      playbackVolumeOverlay.volumeEnabled &&
      Array.isArray(playbackVolumeOverlay.wireframe) &&
      playbackVolumeOverlay.wireframe.length > 0;
    volToggle.disabled = !canShow;
    if (canShow && !volToggle.dataset.userTouched) {
      volToggle.checked = true;
    }
  }
  const gridToggle = document.getElementById("playback-show-floor-grid");
  const gridSegs = playbackSpatialContext?.calibration?.grid_segments;
  if (gridToggle && Array.isArray(gridSegs) && gridSegs.length && !gridToggle.dataset.userTouched) {
    gridToggle.checked = true;
  }
}

function refreshPlaybackSpatialMapPanelsVisibility() {
  const spatialMeta = playbackSpatialContext?.spatial || {};
  const calibEnabled = spatialMeta.floor_xy_enabled || playbackSpatialContext?.calibration?.enabled;
  const hasFloor = playbackFloorTrajectory.length > 0;
  const volOn = playbackVolumeOverlay.volumeEnabled || spatialMeta.volume_enabled;
  const hasLeft = playbackWristTrajectories.left.length > 0;
  const hasRight = playbackWristTrajectories.right.length > 0;

  const groundPanel = document.getElementById("playback-floor-map-panel");
  if (groundPanel) groundPanel.classList.toggle("hidden", !calibEnabled && !hasFloor && !volOn);

  const leftPanel = document.getElementById("playback-left-map-panel");
  const rightPanel = document.getElementById("playback-right-map-panel");
  if (leftPanel) leftPanel.classList.toggle("hidden", !(volOn || hasLeft) || !isShelfFaceEnabled("left"));
  if (rightPanel) rightPanel.classList.toggle("hidden", !(volOn || hasRight) || !isShelfFaceEnabled("right"));

  const mapsRow = document.getElementById("playback-spatial-maps-row");
  if (mapsRow) {
    const anyVisible =
      (groundPanel && !groundPanel.classList.contains("hidden")) ||
      (leftPanel && !leftPanel.classList.contains("hidden")) ||
      (rightPanel && !rightPanel.classList.contains("hidden"));
    mapsRow.classList.toggle("hidden", !anyVisible);
  }
}

function finalizePlaybackSpatialMapsDraw() {
  const frameIdx =
    Number(typeof lastRenderedFrameIdx !== "undefined" ? lastRenderedFrameIdx : 0) ||
    Number(playbackFloorTrajectory[0]?.frameIdx) ||
    Number(playbackWristTrajectories.left[0]?.frameIdx) ||
    Number(playbackWristTrajectories.right[0]?.frameIdx) ||
    1;
  const hit = frameByTime?.find((r) => r.frameIdx === frameIdx) || { frameIdx };
  const frame =
    frameIdx > 0 && typeof frameCache !== "undefined" ? frameCache.get(frameIdx) : null;
  if (typeof updatePlaybackFloorMapUi === "function") {
    updatePlaybackFloorMapUi(hit, frame);
  }
  if (typeof refreshPlaybackSpatialOverlay === "function") {
    refreshPlaybackSpatialOverlay();
  }
}

async function loadPlaybackSpatialContext(recordId) {
  resetPlaybackFloorMap();
  if (!recordId) return;
  try {
    const res = await fetch(recordApiUrl(recordId, "/spatial"));
    if (res.ok) {
      playbackSpatialContext = await res.json();
    }
  } catch (_err) {
    playbackSpatialContext = null;
  }
  ensurePlaybackSpatialContextFallback(recordId);
  if (playbackSpatialContext) {
    applyPlaybackSpatialGridFromContext();
    syncPlaybackVolumeOverlayFromContext();
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

  try {
    const wristRes = await fetch(recordApiUrl(recordId, "/wrist-face"));
    if (wristRes.ok) {
      const body = await wristRes.json();
      playbackWristTrajectories.left = (body.left || [])
        .filter((row) => Array.isArray(row.face_yz_m) && row.face_yz_m.length >= 2)
        .map((row) => ({
          frameIdx: Number(row.frame_idx) || 0,
          yz: [Number(row.face_yz_m[0]), Number(row.face_yz_m[1])],
          column: row.column != null ? Number(row.column) : null,
          layer: row.layer != null ? Number(row.layer) : null,
          uv: Array.isArray(row.wrist_uv_px) ? row.wrist_uv_px.map(Number) : null,
          trailSegmentId: row.trail_segment_id != null ? Number(row.trail_segment_id) : null,
        }));
      playbackWristTrajectories.right = (body.right || [])
        .filter((row) => Array.isArray(row.face_yz_m) && row.face_yz_m.length >= 2)
        .map((row) => ({
          frameIdx: Number(row.frame_idx) || 0,
          yz: [Number(row.face_yz_m[0]), Number(row.face_yz_m[1])],
          column: row.column != null ? Number(row.column) : null,
          layer: row.layer != null ? Number(row.layer) : null,
          uv: Array.isArray(row.wrist_uv_px) ? row.wrist_uv_px.map(Number) : null,
          trailSegmentId: row.trail_segment_id != null ? Number(row.trail_segment_id) : null,
        }));
    }
  } catch (_err) {
    playbackWristTrajectories = { left: [], right: [] };
  }

  refreshPlaybackSpatialMapPanelsVisibility();
  syncPlaybackFootTrailToggle();
  syncPlaybackSpatialVolumeToggle();
  finalizePlaybackSpatialMapsDraw();
}

function isShelfFaceEnabled(side) {
  const face = playbackVolumeOverlay.shelfFaces?.[side];
  if (face && face.enabled === false) return false;
  return true;
}

function syncPlaybackSpatialMapPanels() {
  refreshPlaybackSpatialMapPanelsVisibility();
}

function floorMapToPixel(x, y, canvas) {
  const { widthM, depthM } = playbackFloorMapState;
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  const scale = Math.min(
    (cssW - 96) / Math.max(depthM, 0.1),
    (cssH - 48) / Math.max(widthM, 0.1)
  );
  const originX = 48;
  const centerY = cssH * 0.52;
  return [
    originX + y * scale,
    centerY + (x - widthM / 2) * scale,
  ];
}

function faceMapToPixel(yM, zM, canvas) {
  const { depthM, heightM } = playbackFaceMapState;
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  const scale = Math.min(
    (cssW - 48) / Math.max(depthM, 0.1),
    (cssH - 40) / Math.max(heightM, 0.1)
  );
  const originX = 24;
  const originY = cssH - 16;
  return [originX + Number(yM) * scale, originY - Number(zM) * scale];
}

function isWithinFaceMapYz(yz) {
  if (!Array.isArray(yz) || yz.length < 2) return false;
  const { depthM, heightM } = playbackFaceMapState;
  const y = Number(yz[0]);
  const z = Number(yz[1]);
  return y >= 0 && y <= depthM && z >= 0 && z <= heightM;
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

  drawPlaybackFloorMapColumns(mctx, canvas);

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
  const wristHands = frame && playbackVolumeOverlay.volumeEnabled ? resolveWristHandsAtFrame(frameIdx, frame) : null;
  if (coords) {
    const parts = [];
    if (xyInMap) parts.push(`floor X=${xyInMap[0].toFixed(2)} Y=${xyInMap[1].toFixed(2)} m`);
    if (wristHands?.left?.column || wristHands?.left?.layer) {
      parts.push(`左腕 L${wristHands.left.layer || "?"}·C${wristHands.left.column || "?"}`);
    }
    if (wristHands?.right?.column || wristHands?.right?.layer) {
      parts.push(`右腕 R${wristHands.right.layer || "?"}·C${wristHands.right.column || "?"}`);
    }
    coords.textContent = parts.length
      ? `${parts.join(" · ")} · 帧 ${frameIdx}`
      : frameIdx > 0
        ? `帧 ${frameIdx} · 无 floor_xy`
        : "";
  }
  drawPlaybackFloorMapCanvas(xyInMap, frameIdx);
  if (playbackVolumeOverlay.volumeEnabled) {
    if (isShelfFaceEnabled("left")) drawPlaybackFaceMapCanvas("left", wristHands?.left, frameIdx);
    if (isShelfFaceEnabled("right")) drawPlaybackFaceMapCanvas("right", wristHands?.right, frameIdx);
  }
}

function applyHomographyUv(uv, hMat) {
  if (!hMat || hMat.length < 3) return null;
  const u = Number(uv[0]);
  const v = Number(uv[1]);
  const x = hMat[0][0] * u + hMat[0][1] * v + hMat[0][2];
  const y = hMat[1][0] * u + hMat[1][1] * v + hMat[1][2];
  const w = hMat[2][0] * u + hMat[2][1] * v + hMat[2][2];
  if (Math.abs(w) < 1e-9) return null;
  return [x / w, y / w];
}

function syncPlaybackVolumeOverlayFromContext() {
  const cal = playbackSpatialContext?.calibration || {};
  const cfg = cal.config || cal;
  const vol = cal.volume || cfg.volume || {};
  playbackVolumeOverlay.volumeEnabled = !!(
    vol.enabled || playbackSpatialContext?.spatial?.volume_enabled
  );
  playbackVolumeOverlay.wireframe = cal.volume_wireframe_segments || cfg.computed?.volume_wireframe_segments || [];
  playbackVolumeOverlay.columnLines = cal.column_lines_image || cfg.computed?.column_lines_image || [];
  playbackVolumeOverlay.layerLines = cal.layer_lines_image || cfg.computed?.layer_lines_image || { left: [], right: [] };
  const gc = cal.ground_columns || cfg.ground_columns || {};
  playbackVolumeOverlay.columnCount = Number(gc.column_count) || 0;
  playbackVolumeOverlay.boundariesXM = Array.isArray(gc.boundaries_x_m) ? gc.boundaries_x_m.map(Number) : [];
  playbackVolumeOverlay.boundariesYM = Array.isArray(gc.boundaries_y_m) ? gc.boundaries_y_m.map(Number) : [];
  const axis = gc.column_axis || cal.computed?.column_axis;
  playbackVolumeOverlay.columnAxis = axis === "y" || playbackVolumeOverlay.boundariesYM.length >= 2 ? "y" : "x";
  playbackVolumeOverlay.faceHomographies = (cfg.computed?.face_homographies) || cal.face_homographies || {};
  playbackVolumeOverlay.shelfFaces = cal.shelf_faces || cfg.shelf_faces || {};
  playbackVolumeOverlay.volumeCorners = vol.corners_image_px || [];
  playbackVolumeOverlay.imageToGroundH = cal.image_to_ground_homography || cfg.computed?.image_to_ground_homography;
  playbackFaceMapState.depthM = Number(vol.depth_m) || playbackFloorMapState.depthM;
  playbackFaceMapState.heightM = Number(vol.height_m) || 2.4;
  syncPlaybackSpatialMapPanels();
}

function floorXyToColumn(xy) {
  if (!xy || xy.length < 2) return null;
  const axis = playbackVolumeOverlay.columnAxis;
  const bounds = axis === "y" ? playbackVolumeOverlay.boundariesYM : playbackVolumeOverlay.boundariesXM;
  if (!bounds || bounds.length < 2) return null;
  const v = Number(axis === "y" ? xy[1] : xy[0]);
  for (let i = 0; i < bounds.length - 1; i += 1) {
    const lo = bounds[i];
    const hi = bounds[i + 1];
    if (i === bounds.length - 2 ? v >= lo && v <= hi + 1e-6 : v >= lo && v < hi) return i + 1;
  }
  return null;
}

function singleWristUvFromPerson(person, hand) {
  if (!person?.keypoints) return null;
  const idx = hand === "left" ? WRIST_LEFT : WRIST_RIGHT;
  const kp = person.keypoints[idx];
  if (!kp || kp.length < 3 || Number(kp[2]) < WRIST_SCORE_MIN) return null;
  return [Number(kp[0]), Number(kp[1])];
}

function yzToLayer(zM, side) {
  const face = playbackVolumeOverlay.shelfFaces?.[side] || {};
  let layerZ = face.layer_z_m;
  if (!Array.isArray(layerZ) || layerZ.length < 2) {
    const h = playbackFaceMapState.heightM;
    const lc = Math.max(1, Number(face.layer_count) || 4);
    layerZ = [];
    for (let i = 0; i <= lc; i += 1) layerZ.push((i * h) / lc);
  }
  const z = Number(zM);
  for (let i = 0; i < layerZ.length - 1; i += 1) {
    const lo = layerZ[i];
    const hi = layerZ[i + 1];
    if (i === layerZ.length - 2 ? z >= lo && z <= hi + 1e-6 : z >= lo && z < hi) return i + 1;
  }
  return null;
}

function resolveSingleWristVolume(frameIdx, frame, hand) {
  const fromSidecar = wristVolumeAtFrameFromSidecar(hand, frameIdx);
  if (fromSidecar) return fromSidecar;
  const faceSide = hand;
  if (!isShelfFaceEnabled(faceSide)) return null;
  const person = pickPrimaryPersonForFoot(frame);
  const uv = person ? singleWristUvFromPerson(person, hand) : null;
  if (!uv) return null;
  const h = playbackVolumeOverlay.imageToGroundH;
  const xy = h ? applyHomographyUv(uv, h) : null;
  const column = xy ? floorXyToColumn(xy) : null;
  const faceH = playbackVolumeOverlay.faceHomographies?.[faceSide]?.image_to_face_yz;
  const yz = faceH ? applyHomographyUv(uv, faceH) : null;
  if (yz && !isWithinFaceMapYz(yz)) return { uv, xy, column, side: faceSide, layer: null, yz: null };
  const layer = yz ? yzToLayer(yz[1], faceSide) : null;
  return { uv, xy, column, side: faceSide, layer, yz };
}

function resolveWristHandsAtFrame(frameIdx, frame) {
  return {
    left: resolveSingleWristVolume(frameIdx, frame, "left"),
    right: resolveSingleWristVolume(frameIdx, frame, "right"),
  };
}

function resolveWristVolumeAtFrame(frameIdx, frame) {
  const hands = resolveWristHandsAtFrame(frameIdx, frame);
  return hands.left || hands.right || null;
}

function wristTrailFromSidecar(hand, frameIdx) {
  const fi = Number(frameIdx) || 0;
  if (fi <= 0) return [];
  const source = playbackWristTrajectories[hand] || [];
  if (!source.length) return [];
  const minFrame = Math.max(1, fi - footTrailTailMaxFrames() + 1);
  return source
    .filter((pt) => pt.frameIdx >= minFrame && pt.frameIdx <= fi)
    .sort((a, b) => a.frameIdx - b.frameIdx);
}

function wristVolumeAtFrameFromSidecar(hand, frameIdx) {
  const fi = Number(frameIdx) || 0;
  const hit = (playbackWristTrajectories[hand] || []).find((pt) => pt.frameIdx === fi);
  if (!hit || !hit.yz) return null;
  return {
    uv: hit.uv,
    xy: null,
    column: hit.column,
    side: hand,
    layer: hit.layer,
    yz: hit.yz,
  };
}

function collectWristFaceTrail(hand, frameIdx) {
  const sidecarTrail = wristTrailFromSidecar(hand, frameIdx);
  if (sidecarTrail.length) return sidecarTrail;
  if (!isShelfFaceEnabled(hand)) return [];
  const fi = Number(frameIdx) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return [];
  const minFrame = Math.max(1, fi - footTrailTailMaxFrames() + 1);
  const points = [];
  frameCache.forEach((fr, fidx) => {
    if (fidx < minFrame || fidx > fi) return;
    const vol = resolveSingleWristVolume(fidx, fr, hand);
    if (vol?.yz && isWithinFaceMapYz(vol.yz)) {
      points.push({ frameIdx: fidx, yz: vol.yz, column: vol.column, uv: vol.uv });
    }
  });
  return points.sort((a, b) => a.frameIdx - b.frameIdx);
}

function layerZBounds(side) {
  const face = playbackVolumeOverlay.shelfFaces?.[side] || {};
  let layerZ = face.layer_z_m;
  if (!Array.isArray(layerZ) || layerZ.length < 2) {
    const h = playbackFaceMapState.heightM;
    const lc = Math.max(1, Number(face.layer_count) || 4);
    layerZ = [];
    for (let i = 0; i <= lc; i += 1) layerZ.push((i * h) / lc);
  }
  return layerZ;
}

function drawFaceMapLayerLines(mctx, canvas, side) {
  const layerZ = layerZBounds(side);
  mctx.strokeStyle = side === "left" ? "rgba(80, 160, 255, 0.55)" : "rgba(255, 160, 80, 0.55)";
  mctx.lineWidth = 1;
  const { depthM } = playbackFaceMapState;
  layerZ.forEach((zM) => {
    const a = faceMapToPixel(0, zM, canvas);
    const b = faceMapToPixel(depthM, zM, canvas);
    mctx.beginPath();
    mctx.moveTo(a[0], a[1]);
    mctx.lineTo(b[0], b[1]);
    mctx.stroke();
  });
}

function prepareMapCanvas(canvas) {
  if (!canvas) return { mctx: null, cssW: 0, cssH: 0 };
  const mctx = canvas.getContext("2d");
  if (!mctx) return { mctx: null, cssW: 0, cssH: 0 };
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(180, canvas.clientWidth || 240);
  const cssH = Math.max(140, canvas.clientHeight || 180);
  if (canvas.width !== Math.floor(cssW * dpr) || canvas.height !== Math.floor(cssH * dpr)) {
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  return { mctx, cssW, cssH };
}

function drawPlaybackFaceMapCanvas(hand, currentVol, frameIdx) {
  const canvasId = hand === "left" ? "playback-left-map" : "playback-right-map";
  const canvas = document.getElementById(canvasId);
  const { mctx, cssW, cssH } = prepareMapCanvas(canvas);
  if (!mctx || !canvas) return;

  const { depthM, heightM } = playbackFaceMapState;
  const sideLabel = hand === "left" ? "LEFT" : "RIGHT";
  mctx.fillStyle = "#f7f7f4";
  mctx.fillRect(0, 0, cssW, cssH);
  mctx.fillStyle = "#373737";
  mctx.font = "12px sans-serif";
  mctx.fillText(`${sideLabel} MAP ${depthM.toFixed(1)} m × ${heightM.toFixed(1)} m`, 10, 18);

  const p0 = faceMapToPixel(0, 0, canvas);
  const p1 = faceMapToPixel(depthM, heightM, canvas);
  mctx.strokeStyle = "#505050";
  mctx.lineWidth = 2;
  mctx.strokeRect(
    Math.min(p0[0], p1[0]),
    Math.min(p0[1], p1[1]),
    Math.abs(p1[0] - p0[0]),
    Math.abs(p1[1] - p0[1])
  );
  drawFaceMapLayerLines(mctx, canvas, hand);

  const trail = collectWristFaceTrail(hand, frameIdx);
  if (trail.length >= 2 && isPlaybackFootTrailEnabled()) {
    const opts = footTrailBreakOptions();
    const segments = splitFootTrailPoints(
      trail.map((pt) => ({
        frameIdx: pt.frameIdx,
        yz: pt.yz,
        uv: pt.uv,
        xy: pt.yz,
        trailSegmentId: pt.trailSegmentId,
      })),
      opts
    );
    const colors = WRIST_HAND_COLORS[hand];
    mctx.save();
    mctx.lineJoin = "round";
    mctx.lineCap = "round";
    mctx.beginPath();
    mctx.rect(
      Math.min(p0[0], p1[0]),
      Math.min(p0[1], p1[1]),
      Math.abs(p1[0] - p0[0]),
      Math.abs(p1[1] - p0[1])
    );
    mctx.clip();
    drawFootTrailSegments(mctx, segments, (prev, curr, alpha) => {
      mctx.strokeStyle = `${colors.trail}, ${alpha.toFixed(3)})`;
      mctx.lineWidth = 2;
      const [x0, y0] = faceMapToPixel(prev.yz[0], prev.yz[1], canvas);
      const [x1, y1] = faceMapToPixel(curr.yz[0], curr.yz[1], canvas);
      mctx.beginPath();
      mctx.moveTo(x0, y0);
      mctx.lineTo(x1, y1);
      mctx.stroke();
    });
    mctx.restore();
  }

  if (currentVol?.yz && isWithinFaceMapYz(currentVol.yz)) {
    const [cx, cy] = faceMapToPixel(currentVol.yz[0], currentVol.yz[1], canvas);
    const colors = WRIST_HAND_COLORS[hand];
    mctx.strokeStyle = "#ffffff";
    mctx.lineWidth = 2;
    mctx.beginPath();
    mctx.arc(cx, cy, 8, 0, Math.PI * 2);
    mctx.stroke();
    mctx.fillStyle = colors.fill;
    mctx.beginPath();
    mctx.arc(cx, cy, 5, 0, Math.PI * 2);
    mctx.fill();
    const layerTag = currentVol.layer ? `L${currentVol.layer}` : "L?";
    const colTag = currentVol.column ? `·C${currentVol.column}` : "";
    mctx.fillStyle = "#2d2d2d";
    mctx.font = "11px sans-serif";
    mctx.fillText(
      `Y=${currentVol.yz[0].toFixed(2)} Z=${currentVol.yz[1].toFixed(2)} m · ${layerTag}${colTag}`,
      10,
      cssH - 8
    );
  }
}

function drawPlaybackFloorMapColumns(mctx, canvas) {
  const axis = playbackVolumeOverlay.columnAxis;
  const bounds = axis === "y" ? playbackVolumeOverlay.boundariesYM : playbackVolumeOverlay.boundariesXM;
  const { widthM, depthM } = playbackFloorMapState;
  if (!bounds || bounds.length < 2) return;
  mctx.setLineDash([6, 4]);
  mctx.strokeStyle = "rgba(40, 160, 90, 0.95)";
  mctx.lineWidth = 1.5;
  bounds.forEach((m, idx) => {
    if (idx === 0 || idx === bounds.length - 1) return;
    const a = axis === "y" ? floorMapToPixel(0, m, canvas) : floorMapToPixel(m, 0, canvas);
    const b = axis === "y" ? floorMapToPixel(widthM, m, canvas) : floorMapToPixel(m, depthM, canvas);
    mctx.beginPath();
    mctx.moveTo(a[0], a[1]);
    mctx.lineTo(b[0], b[1]);
    mctx.stroke();
    mctx.fillStyle = "rgba(40, 120, 70, 0.95)";
    mctx.font = "10px sans-serif";
    const label = `C${idx}`;
    mctx.fillText(label, (a[0] + b[0]) / 2 - 8, a[1] - 4);
  });
  mctx.setLineDash([]);
}

function drawVolumeWireframeOverlay(inferW, inferH) {
  const toggle = document.getElementById("playback-show-volume-wireframe");
  if (!toggle?.checked || !playbackVolumeOverlay.volumeEnabled) return;
  const segs = playbackVolumeOverlay.wireframe;
  if (!Array.isArray(segs) || !segs.length) return;
  const calW = Number(playbackSpatialContext?.calibration?.infer_width) || inferW;
  const calH = Number(playbackSpatialContext?.calibration?.infer_height) || inferH;
  const layout =
    typeof frozenPlaybackLayout !== "undefined" &&
    frozenPlaybackLayout &&
    typeof playbackRenderLoopActive !== "undefined" &&
    playbackRenderLoopActive
      ? frozenPlaybackLayout
      : getDisplayLayout();
  ctx.strokeStyle = "rgba(100, 180, 255, 0.88)";
  ctx.lineWidth = 2;
  segs.forEach((seg) => {
    const img = seg.image;
    if (!Array.isArray(img) || img.length < 2) return;
    const p0 = mapInferToDisplay(img[0][0], img[0][1], calW, calH, layout);
    const p1 = mapInferToDisplay(img[1][0], img[1][1], calW, calH, layout);
    ctx.beginPath();
    ctx.moveTo(p0[0], p0[1]);
    ctx.lineTo(p1[0], p1[1]);
    ctx.stroke();
  });
  const layers = playbackVolumeOverlay.layerLines || {};
  ctx.lineWidth = 1.5;
  if (isShelfFaceEnabled("left")) {
    (layers.left || []).forEach((seg) => {
      const img = seg.image || seg;
      if (!img || img.length < 2) return;
      ctx.strokeStyle = "rgba(80, 160, 255, 0.85)";
      const p0 = mapInferToDisplay(img[0][0], img[0][1], calW, calH, layout);
      const p1 = mapInferToDisplay(img[1][0], img[1][1], calW, calH, layout);
      ctx.beginPath();
      ctx.moveTo(p0[0], p0[1]);
      ctx.lineTo(p1[0], p1[1]);
      ctx.stroke();
    });
  }
  if (isShelfFaceEnabled("right")) {
    (layers.right || []).forEach((seg) => {
      const img = seg.image || seg;
      if (!img || img.length < 2) return;
      ctx.strokeStyle = "rgba(255, 160, 80, 0.85)";
      const p0 = mapInferToDisplay(img[0][0], img[0][1], calW, calH, layout);
      const p1 = mapInferToDisplay(img[1][0], img[1][1], calW, calH, layout);
      ctx.beginPath();
      ctx.moveTo(p0[0], p0[1]);
      ctx.lineTo(p1[0], p1[1]);
      ctx.stroke();
    });
  }
}

function drawPlaybackWristVolumeOverlay(frame, inferW, inferH, layout, currentFrameIdx) {
  if (!playbackVolumeOverlay.volumeEnabled) return;
  const fi = Number(currentFrameIdx) || Number(frame?.frame_idx) || 0;
  if (fi <= 0) return;
  const hands = resolveWristHandsAtFrame(fi, frame);
  const person = pickPrimaryPersonForFoot(frame);
  [["left", hands.left], ["right", hands.right]].forEach(([hand, vol]) => {
    if (!vol?.uv) return;
    const colors = WRIST_HAND_COLORS[hand];
    const [x, y] = mapInferToDisplay(vol.uv[0], vol.uv[1], inferW, inferH, layout);
    ctx.save();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
    ctx.fillStyle = colors.fill;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x, y, 8, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (person?.keypoints) {
      const idx = hand === "left" ? WRIST_LEFT : WRIST_RIGHT;
      const kp = person.keypoints[idx];
      if (kp && kp.length >= 3 && Number(kp[2]) >= WRIST_SCORE_MIN) {
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = colors.stroke.replace("0.95", "0.55");
        const [ax, ay] = mapInferToDisplay(Number(kp[0]), Number(kp[1]), inferW, inferH, layout);
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
    const sideTag = hand === "left" ? "L" : "R";
    const label = `${sideTag}${vol.layer || "?"}·C${vol.column || "?"}`;
    ctx.fillStyle = "rgba(255, 255, 255, 0.96)";
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillText(label, x + 10, y - 8);
    ctx.restore();
  });
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
  const volToggle = document.getElementById("playback-show-volume-wireframe");
  if (volToggle && !volToggle.dataset.bound) {
    volToggle.dataset.bound = "1";
    volToggle.addEventListener("change", () => refreshPlaybackSpatialOverlay());
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
  let segs = playbackSpatialContext?.calibration?.grid_segments;
  if (!Array.isArray(segs) || !segs.length) return;
  const colLines = playbackVolumeOverlay.columnLines;
  if (Array.isArray(colLines) && colLines.length > 0) {
    segs = segs.filter((seg) => {
      const w = seg?.world;
      if (!Array.isArray(w) || w.length < 2) return true;
      return Math.abs(w[0][1] - w[1][1]) >= 0.08;
    });
  }
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
  const drawImageSeg = (seg, strokeStyle, lineWidth = 2) => {
    const img = seg?.image || seg;
    if (!Array.isArray(img) || img.length < 2) return;
    ctx.strokeStyle = strokeStyle;
    ctx.lineWidth = lineWidth;
    const p0 = mapInferToDisplay(img[0][0], img[0][1], mapW, mapH, layout);
    const p1 = mapInferToDisplay(img[1][0], img[1][1], mapW, mapH, layout);
    ctx.beginPath();
    ctx.moveTo(p0[0], p0[1]);
    ctx.lineTo(p1[0], p1[1]);
    ctx.stroke();
  };
  if (segs.length) {
    segs.forEach((seg) => drawImageSeg(seg, "rgba(80, 210, 80, 0.85)", 2));
  }
  if (Array.isArray(colLines) && colLines.length) {
    ctx.setLineDash([8, 5]);
    colLines.forEach((seg, idx) => drawImageSeg(seg, "rgba(40, 180, 100, 0.95)", 2.5));
    ctx.setLineDash([]);
  } else if (!segs.length) {
    return;
  }
}

document.addEventListener("DOMContentLoaded", bindPlaybackFloorMapControls);
