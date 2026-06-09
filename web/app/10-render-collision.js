/** 画布渲染、货框叠加与碰撞追踪 */

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
  const shelf = String(box.shelf_code || "").trim();
  const id = String(box.box_id ?? box.id ?? "").trim();
  if (!id) return "";
  return shelf ? `${shelf}:${id}` : `Box_${id}`;
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

function distPointToSegment(p, a, b) {
  const px = Number(p[0]), py = Number(p[1]);
  const ax = Number(a[0]), ay = Number(a[1]);
  const bx = Number(b[0]), by = Number(b[1]);
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy || 1e-9;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
  const x = ax + t * dx, y = ay + t * dy;
  return Math.hypot(px - x, py - y);
}

function pointToPolygonDistance(p, polygon) {
  if (!Array.isArray(polygon) || polygon.length < 3) return Infinity;
  let best = Infinity;
  for (let i = 0; i < polygon.length; i++) {
    best = Math.min(best, distPointToSegment(p, polygon[i], polygon[(i + 1) % polygon.length]));
  }
  return best;
}

function segmentsIntersect(a, b, c, d) {
  const orient = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
  const onSeg = (p, q, r) =>
    Math.min(p[0], r[0]) - 1e-6 <= q[0] && q[0] <= Math.max(p[0], r[0]) + 1e-6 &&
    Math.min(p[1], r[1]) - 1e-6 <= q[1] && q[1] <= Math.max(p[1], r[1]) + 1e-6;
  const o1 = orient(a, b, c), o2 = orient(a, b, d), o3 = orient(c, d, a), o4 = orient(c, d, b);
  if ((o1 > 0) !== (o2 > 0) && (o3 > 0) !== (o4 > 0)) return true;
  return (
    Math.abs(o1) <= 1e-6 && onSeg(a, c, b) ||
    Math.abs(o2) <= 1e-6 && onSeg(a, d, b) ||
    Math.abs(o3) <= 1e-6 && onSeg(c, a, d) ||
    Math.abs(o4) <= 1e-6 && onSeg(c, b, d)
  );
}

function segmentIntersectsPolygon(a, b, polygon) {
  if (pointInPolygon(a, polygon) || pointInPolygon(b, polygon)) return true;
  for (let i = 0; i < polygon.length; i++) {
    if (segmentsIntersect(a, b, polygon[i], polygon[(i + 1) % polygon.length])) return true;
  }
  return false;
}

function polygonArea(points) {
  let area = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i], b = points[(i + 1) % points.length];
    area += Number(a[0]) * Number(b[1]) - Number(b[0]) * Number(a[1]);
  }
  return Math.abs(area) / 2;
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
        return token ? { token, inferPts, scale: Math.sqrt(Math.max(1, polygonArea(inferPts))) } : null;
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

class PlaybackHandStateCollisionTracker extends PlaybackCollisionTracker {
  constructor(cfg = {}) {
    super(3, cfg.cooldown_frames || 30);
    this.cfg = { ...COLLISION_METHOD_DEFAULTS.hand_state, ...cfg, method: "hand_state" };
    this.states = new Map();
    this.runtime = new Map();
  }

  reset() {
    super.reset();
    this.states = new Map();
    this.runtime = new Map();
  }

  update(frame, inferW, inferH) {
    const boxes = this.getBoxesInInferSpace(inferW, inferH);
    if (!boxes.length) return { collisions: [], alarm_collisions: [] };
    const frameIdx = Number(frame?.frame_idx ?? frame?.source_frame_idx ?? 0);
    const active = new Set();
    const alarms = new Set();
    (frame?.persons || []).forEach((person, pIdx) => {
      const trackId = person?.person_track_id ?? person?.person_id ?? pIdx;
      const kpts = person?.keypoints || [];
      [
        ["left", 7, 9],
        ["right", 8, 10],
      ].forEach(([hand, elbowI, wristI]) => {
        const key = `${trackId}:${hand}`;
        const signal = this.observeHand(key, kpts, Number(elbowI), Number(wristI), boxes);
        const result = this.updateState(key, signal, frameIdx);
        result.active.forEach((t) => active.add(t));
        result.alarms.forEach((t) => alarms.add(t));
      });
    });
    return { collisions: [...active], alarm_collisions: [...alarms] };
  }

  observeHand(key, kpts, elbowI, wristI, boxes) {
    const elbow = kpts[elbowI];
    const wrist = kpts[wristI];
    if (!elbow || !wrist || elbow.length < 3 || wrist.length < 3) return { obs: "UNKNOWN" };
    const e = [Number(elbow[0]), Number(elbow[1]), Number(elbow[2])];
    const w = [Number(wrist[0]), Number(wrist[1]), Number(wrist[2])];
    if (w[2] < this.cfg.wrist_score_min || e[2] < this.cfg.elbow_score_min) return { obs: "UNKNOWN" };
    const rt = this.runtime.get(key) || {};
    const scale = this.personScale(kpts);
    const forearmLen = Math.max(1e-6, Math.hypot(w[0] - e[0], w[1] - e[1]));
    const jump = rt.prevWrist ? Math.hypot(w[0] - rt.prevWrist[0], w[1] - rt.prevWrist[1]) / scale : 0;
    const ratio = rt.forearmLen ? forearmLen / rt.forearmLen : 1;
    const jumpBad = jump > this.cfg.jump_max;
    const limbBad = ratio < this.cfg.forearm_min_ratio || ratio > this.cfg.forearm_max_ratio;
    if (!jumpBad) {
      this.runtime.set(key, {
        prevWrist: [w[0], w[1]],
        forearmLen: rt.forearmLen ? rt.forearmLen * 0.75 + forearmLen * 0.25 : forearmLen,
      });
    }
    if (jumpBad || limbBad) return { obs: "UNKNOWN" };
    const scored = boxes.map((box) => {
      const wristIn = pointInPolygon([w[0], w[1]], box.inferPts);
      const forearmHit = segmentIntersectsPolygon([e[0], e[1]], [w[0], w[1]], box.inferPts);
      const near = pointToPolygonDistance([w[0], w[1]], box.inferPts) <= box.scale * this.cfg.near_edge_ratio;
      const score = 0.55 * Number(wristIn) + 0.30 * Number(forearmHit) + 0.15 * Number(near);
      return { token: box.token, score };
    }).sort((a, b) => b.score - a.score);
    const best = scored[0];
    if (!best) return { obs: "NO_HIT" };
    const second = scored[1]?.score ?? -1;
    if (best.score >= this.cfg.hit_threshold && best.score - second >= this.cfg.box_margin) {
      return { obs: "HIT", token: best.token, score: best.score };
    }
    if (best.score >= this.cfg.hit_threshold) return { obs: "UNKNOWN", token: best.token };
    return { obs: "NO_HIT" };
  }

  personScale(kpts) {
    const pts = (kpts || []).filter((k) => k && k.length >= 3 && Number(k[2]) > 0.2);
    if (!pts.length) return 100;
    const xs = pts.map((p) => Number(p[0]));
    const ys = pts.map((p) => Number(p[1]));
    return Math.max(20, Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  }

  updateState(key, signal, frameIdx) {
    const st = this.states.get(key) || { state: "IDLE", token: "", history: [], stateFrame: 0, cooldownUntil: 0 };
    st.history.push(signal);
    st.history = st.history.slice(-16);
    const active = new Set();
    const alarms = new Set();
    if (st.state === "COOLDOWN") {
      if (frameIdx >= st.cooldownUntil) st.state = "IDLE";
      else {
        this.states.set(key, st);
        return { active, alarms };
      }
    }
    if (st.state === "IDLE") {
      if (signal.obs === "HIT") Object.assign(st, { state: "ENTER_PENDING", token: signal.token, stateFrame: frameIdx });
    } else if (st.state === "ENTER_PENDING") {
      active.add(st.token);
      const win = st.history.slice(-this.cfg.enter_window_frames);
      const same = win.filter((s) => s.obs === "HIT" && s.token === st.token).length;
      const other = win.filter((s) => s.obs === "HIT" && s.token && s.token !== st.token).length;
      if (same >= this.cfg.enter_min_hits && other <= 1) Object.assign(st, { state: "INSIDE", stateFrame: frameIdx });
      else if (frameIdx - st.stateFrame > this.cfg.enter_timeout_frames) Object.assign(st, { state: "IDLE", token: "", history: [] });
    } else if (st.state === "INSIDE") {
      active.add(st.token);
      if (frameIdx - st.stateFrame > this.cfg.max_inside_frames) Object.assign(st, { state: "IDLE", token: "", history: [] });
      else if (signal.obs !== "UNKNOWN" && !(signal.obs === "HIT" && signal.token === st.token)) Object.assign(st, { state: "EXIT_PENDING", stateFrame: frameIdx });
    } else if (st.state === "EXIT_PENDING") {
      active.add(st.token);
      if (signal.obs === "HIT" && signal.token === st.token) Object.assign(st, { state: "INSIDE", stateFrame: frameIdx });
      else {
        const win = st.history.slice(-this.cfg.exit_window_frames);
        const releases = win.filter((s) => s.obs !== "UNKNOWN" && !(s.obs === "HIT" && s.token === st.token)).length;
        if (releases >= this.cfg.exit_min_releases) {
          alarms.add(st.token);
          Object.assign(st, { state: "COOLDOWN", cooldownUntil: frameIdx + this.cfg.cooldown_frames, stateFrame: frameIdx });
        } else if (frameIdx - st.stateFrame > this.cfg.exit_timeout_frames) Object.assign(st, { state: "IDLE", token: "", history: [] });
      }
    }
    this.states.set(key, st);
    return { active, alarms };
  }
}

let playbackCollisionTracker = null;

function resetPlaybackCollisionTracker() {
  playbackCollisionTracker = null;
}

function getPlaybackCollisionTracker() {
  if (!playbackCollisionTracker) {
    const cfg = getEffectiveCollisionConfig();
    playbackCollisionTracker = normalizeCollisionMethod(cfg.method) === "hand_state"
      ? new PlaybackHandStateCollisionTracker(cfg)
      : new PlaybackCollisionTracker(cfg.alarm_min_consecutive_frames, cfg.alarm_cooldown_frames);
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
    syncActiveEventFromPlaybackPosition({ timeSec: videoEl.currentTime });
  }
  rafId = requestAnimationFrame(tick);
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

let jsonOnlyTimer = null;
let jsonOnlyFrameIdx = 0;

function restartJsonOnlyPlaybackIfActive() {
  if (!jsonOnlyTimer) return;
  startJsonOnlyPlayback(jsonOnlyFrameIdx);
}

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

function startJsonOnlyPlayback(startIdx = 0) {
  if (!frameByTime.length) return;
  const fps = poseData.fps || 15;
  const rate = Number.isFinite(playbackSpeed) && playbackSpeed > 0 ? playbackSpeed : 1;
  let idx = Math.max(0, Math.min(startIdx, frameByTime.length - 1));
  jsonOnlyFrameIdx = idx;
  clearInterval(jsonOnlyTimer);
  videoEl.style.display = "none";

  jsonOnlyTimer = setInterval(async () => {
    if (idx >= frameByTime.length) idx = 0;
    jsonOnlyFrameIdx = idx;
    const entry = frameByTime[idx];
    await renderFrameEntry(entry);
    seekBar.value = String((idx / frameByTime.length) * 1000);
    timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    syncActiveEventFromPlaybackPosition({ timeSec: entry?.t, frameIdx: entry?.frameIdx });
    idx += 1;
  }, 1000 / (fps * rate));
}
