/** 回放：暂停时显示骨骼特征参数（速度 + 角度 + 门控预览） */

const FEATURE_VELOCITY_LABELS = {
  torso_speed: "躯干速度",
  body_mean_speed: "全身均值",
  body_max_speed: "全身最大",
  upper_mean_speed: "上肢均值",
  lower_mean_speed: "下肢均值",
  knee_ankle_mean_speed: "膝踝均值",
  ankle_mean_speed: "踝均值",
  ankle_max_speed: "踝最大",
  wrist_max_speed: "腕最大速",
  elbow_max_speed: "肘最大速",
  wrist_torso_ratio: "腕/躯干比",
};

const FEATURE_ANGLE_LABELS = {
  arm_torso_angle_max: "肩肘躯干max",
  arm_torso_angle_mean: "肩肘躯干mean",
  elbow_angle_mean: "肘角mean",
  elbow_angle_max: "肘角max",
  elbow_angle_min: "肘角min",
  wrist_elevation_angle_max: "腕抬升max",
  wrist_elevation_angle_mean: "腕抬升mean",
  forearm_direction_angle_max: "前臂指向max",
  forearm_direction_angle_mean: "前臂指向mean",
  elbow_waist_angle_max: "肘腰角max",
  elbow_waist_angle_mean: "肘腰角mean",
  shoulder_angle_mean: "肩角mean",
  joint_open_vel_max: "关节角速max",
  elbow_angle_vel_max: "肘角速度max",
};

const FEATURE_LEG_POSE_LABELS = {
  torso_leg_angle_mean: "肩髋踝mean",
  torso_leg_angle_min: "肩髋踝min",
  torso_leg_angle_max: "肩髋踝max",
  center_torso_leg_angle: "肩髋踝中心",
  left_torso_leg_angle: "左肩髋踝",
  right_torso_leg_angle: "右肩髋踝",
  knee_angle_mean: "膝角mean",
  knee_angle_min: "膝角min",
  knee_angle_max: "膝角max",
  left_knee_angle: "左膝角",
  right_knee_angle: "右膝角",
  leg_span_ratio: "腿长/躯干比",
  hip_knee_ankle_vertical_ratio: "大腿/小腿比",
};

let playbackFeaturesRecordId = "";
let playbackFeaturesCache = new Map();
let playbackFeaturesLoadInflight = null;
/** 当前帧特征（供 canvas 标签） */
let playbackFeaturesCurrentPersons = [];
/** 视频就绪前不请求 API，避免与视频/骨架加载争抢后端 */
let playbackFeaturesFetchEnabled = false;
let playbackFeaturesFetchEnableTimer = null;
let playbackFeaturesDebounceTimer = null;

function playbackFeaturesPanelEl() {
  return $("#playback-skeleton-features");
}

function isPlaybackVideoPlaying() {
  const v = $("#playback-video-el");
  return !!(v && v.src && !v.paused && !v.ended);
}

function currentPlaybackFeaturesFrameIdx() {
  if (typeof tickPoseFrameIdx === "number" && tickPoseFrameIdx > 0) return tickPoseFrameIdx;
  if (typeof getCurrentPlaybackFrameIdx === "function") {
    const fi = getCurrentPlaybackFrameIdx();
    if (fi > 0) return fi;
  }
  return 0;
}

function fmtFeatureNum(v, digits = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function escFeatureHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function clearPlaybackSkeletonFeatures() {
  playbackFeaturesRecordId = "";
  playbackFeaturesCache.clear();
  playbackFeaturesLoadInflight = null;
  playbackFeaturesCurrentPersons = [];
  playbackFeaturesFetchEnabled = false;
  if (playbackFeaturesFetchEnableTimer) {
    clearTimeout(playbackFeaturesFetchEnableTimer);
    playbackFeaturesFetchEnableTimer = null;
  }
  if (playbackFeaturesDebounceTimer) {
    clearTimeout(playbackFeaturesDebounceTimer);
    playbackFeaturesDebounceTimer = null;
  }
  const panel = playbackFeaturesPanelEl();
  if (panel) {
    panel.classList.add("hidden");
    const body = $("#playback-skeleton-features-body");
    if (body) body.innerHTML = "";
    const meta = $("#playback-skeleton-features-meta");
    if (meta) meta.textContent = "";
    const frameLbl = $("#playback-skeleton-features-frame");
    if (frameLbl) frameLbl.textContent = "—";
  }
}

function loadPlaybackSkeletonFeatures(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return;
  playbackFeaturesRecordId = rid;
  playbackFeaturesCache.clear();
  playbackFeaturesCurrentPersons = [];
  playbackFeaturesFetchEnabled = false;
  const panel = playbackFeaturesPanelEl();
  if (panel) panel.classList.remove("hidden");
  const meta = $("#playback-skeleton-features-meta");
  if (meta) meta.textContent = "暂停时显示本帧特征参数";
  const body = $("#playback-skeleton-features-body");
  if (body) body.innerHTML = '<p class="hint">暂停后可查看特征…</p>';
}

/** 视频加载完成后再允许特征请求，避免阻塞首屏 */
function enablePlaybackSkeletonFeatureFetch(opts = {}) {
  const delayMs = Number(opts.delayMs) || 600;
  if (playbackFeaturesFetchEnableTimer) {
    clearTimeout(playbackFeaturesFetchEnableTimer);
  }
  playbackFeaturesFetchEnableTimer = setTimeout(() => {
    playbackFeaturesFetchEnableTimer = null;
    playbackFeaturesFetchEnabled = true;
    const meta = $("#playback-skeleton-features-meta");
    if (meta) meta.textContent = "暂停时显示本帧特征参数";
    if (!isPlaybackVideoPlaying()) {
      const fi = currentPlaybackFeaturesFrameIdx();
      if (fi > 0) updatePlaybackSkeletonFeaturesUi(fi);
    }
  }, delayMs);
}

function renderPlaybackFeaturesMeta(payload) {
  const meta = $("#playback-skeleton-features-meta");
  if (!meta) return;
  if (!payload || typeof payload !== "object") {
    meta.textContent = "暂停时显示本帧特征参数";
    return;
  }
  const parts = [];
  if (payload.is_export_frame === false) {
    parts.push("非 export 抽帧，特征仅作参考");
  } else if (payload.is_export_frame) {
    parts.push(`export 帧 · interval=${payload.pose_frame_interval ?? "?"}`);
    if (payload.export_indices_source) parts.push(payload.export_indices_source);
  }
  meta.textContent = parts.length ? parts.join(" · ") : "暂停时显示本帧特征参数（与 export 同路径）";
}

function renderPlaybackFeaturesPlayingPlaceholder() {
  const body = $("#playback-skeleton-features-body");
  if (body) {
    body.innerHTML = '<p class="hint">播放中，暂停后可查看本帧特征参数</p>';
  }
  playbackFeaturesCurrentPersons = [];
  const fi = currentPlaybackFeaturesFrameIdx();
  const frameLbl = $("#playback-skeleton-features-frame");
  if (frameLbl) frameLbl.textContent = fi > 0 ? String(fi) : "—";
}

function onPlaybackVideoPlayStateChange() {
  if (isPlaybackVideoPlaying()) {
    if (playbackFeaturesDebounceTimer) {
      clearTimeout(playbackFeaturesDebounceTimer);
      playbackFeaturesDebounceTimer = null;
    }
    renderPlaybackFeaturesPlayingPlaceholder();
  } else {
    const fi = currentPlaybackFeaturesFrameIdx();
    if (fi > 0) updatePlaybackSkeletonFeaturesUi(fi);
  }
  if (typeof onPlaybackWristFeaturesPlayStateChange === "function") {
    onPlaybackWristFeaturesPlayStateChange();
  }
}

function renderFeatureKvTable(obj, labels, unit) {
  const keys = Object.keys(labels);
  const rows = keys
    .map((k) => {
      const v = obj?.[k];
      const txt = v == null ? "—" : `${fmtFeatureNum(v, k.includes("ratio") ? 4 : 1)}${unit ? ` ${unit}` : ""}`;
      return `<tr><th>${escFeatureHtml(labels[k])}</th><td><code>${escFeatureHtml(txt)}</code></td></tr>`;
    })
    .join("");
  return `<table class="skeleton-features-kv">${rows}</table>`;
}

function renderWristSubTable(wrists) {
  const list = Array.isArray(wrists) ? wrists : [];
  if (!list.length) return '<p class="hint">无手腕速度（默认跳过手腕 Parquet）</p>';
  return `<table class="wrist-features-table">
    <thead><tr><th>手腕</th><th>speed</th><th>vx</th><th>vy</th></tr></thead>
    <tbody>${list
      .map((w) => {
        const name = w.wrist === "left_wrist" ? "左" : w.wrist === "right_wrist" ? "右" : w.wrist;
        return `<tr>
          <td>${escFeatureHtml(name)}</td>
          <td>${w.velocity_valid ? fmtFeatureNum(w.speed, 1) : "—"}</td>
          <td>${w.velocity_valid ? fmtFeatureNum(w.vx, 1) : "—"}</td>
          <td>${w.velocity_valid ? fmtFeatureNum(w.vy, 1) : "—"}</td>
        </tr>`;
      })
      .join("")}</tbody></table>`;
}

function renderGatePreview(gate) {
  if (!gate || typeof gate !== "object") return "";
  const blocked = gate.would_block_collision ? "是（跳过碰撞）" : "否";
  const speedTxt = gate.speed_value != null
    ? `${gate.speed_feature}=${fmtFeatureNum(gate.speed_value, 1)} > ${gate.speed_threshold}? ${gate.speed_high ? "是" : "否"}`
    : "速度无效";
  const exemptTxt = (gate.angle_exempt_detail || [])
    .map((d) => `${d.feature}≥${d.min_threshold}: ${d.met ? "✓" : "✗"} (${fmtFeatureNum(d.value, 1)})`)
    .join("<br>");
  const stanceTxt = gate.stance_feature
    ? `${gate.stance_feature}≥${gate.stance_threshold}: ${gate.is_standing ? "站立✓" : "蹲姿✗"} (${fmtFeatureNum(gate.stance_value, 1)})`
    : "";
  return `<div class="skeleton-features-gate">
    <div><strong>门控预览</strong> · ankle_max@80 + triple90 + 肩髋踝≥160</div>
    <div><strong>block</strong>=${escFeatureHtml(blocked)}</div>
    <div class="hint">${escFeatureHtml(speedTxt)}</div>
    <div class="hint">${exemptTxt || "—"}</div>
    ${stanceTxt ? `<div class="hint">${escFeatureHtml(stanceTxt)}</div>` : ""}
  </div>`;
}

function renderPersonFeatureCard(person) {
  const tid = person.person_track_id ?? "—";
  const pid = person.person_id != null ? person.person_id : "—";
  return `<article class="skeleton-features-person-card" data-track-id="${escFeatureHtml(tid)}">
    <header class="skeleton-features-person-head">
      <strong>track #${escFeatureHtml(tid)}</strong>
      <span class="hint">person_id #${escFeatureHtml(pid)}</span>
    </header>
    <h5 class="playback-wrist-features-subtitle">速度 px/s</h5>
    ${renderFeatureKvTable(person.velocity, FEATURE_VELOCITY_LABELS, "")}
    <h5 class="playback-wrist-features-subtitle">上肢角度 °</h5>
    ${renderFeatureKvTable(person.angles, FEATURE_ANGLE_LABELS, "")}
    <h5 class="playback-wrist-features-subtitle">下肢姿态 ° / 比</h5>
    ${renderFeatureKvTable(person.angles, FEATURE_LEG_POSE_LABELS, "")}
    <h5 class="playback-wrist-features-subtitle">手腕明细</h5>
    ${renderWristSubTable(person.wrists)}
    ${renderGatePreview(person.gate_preview)}
  </article>`;
}

function renderPlaybackFeaturesBody(frameIdx, persons, opts = {}) {
  const body = $("#playback-skeleton-features-body");
  if (!body) return;

  if (opts.loading) {
    body.innerHTML = '<p class="hint">计算特征…</p>';
    return;
  }
  if (opts.error) {
    body.innerHTML = `<p class="hint">${escFeatureHtml(opts.error)}</p>`;
    return;
  }
  if (opts.hint) {
    body.innerHTML = `<p class="hint">${escFeatureHtml(opts.hint)}</p>`;
    return;
  }

  const fi = parseInt(frameIdx, 10) || 0;
  const list = Array.isArray(persons) ? persons : [];
  if (!list.length) {
    body.innerHTML = `<p class="hint">帧 ${fi} 无有效人体特征（可能无人或关键点置信度低）</p>`;
    return;
  }
  body.innerHTML = list.map(renderPersonFeatureCard).join("");
}

function applyCachedPlaybackFeatures(fi) {
  if (playbackFeaturesCache.has(fi)) {
    const cached = playbackFeaturesCache.get(fi);
    playbackFeaturesCurrentPersons = cached?.persons || [];
    renderPlaybackFeaturesMeta(cached?.meta || null);
    if (cached?.error) renderPlaybackFeaturesBody(fi, [], { error: cached.error });
    else if (cached?.hint) renderPlaybackFeaturesBody(fi, [], { hint: cached.hint });
    else renderPlaybackFeaturesBody(fi, playbackFeaturesCurrentPersons);
    return true;
  }
  return false;
}

function scheduleFeatureLabelRepaint() {
  if (typeof repaintFeatureTrackLabelsOnly === "function") {
    repaintFeatureTrackLabelsOnly();
  }
}

async function ensurePlaybackFeaturesForFrame(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || !playbackFeaturesRecordId) {
    playbackFeaturesCurrentPersons = [];
    renderPlaybackFeaturesBody(0, []);
    return;
  }

  if (!playbackFeaturesFetchEnabled) {
    if (!applyCachedPlaybackFeatures(fi)) {
      renderPlaybackFeaturesBody(fi, [], { hint: "视频加载中…" });
    }
    return;
  }

  if (playbackFeaturesCache.has(fi)) {
    applyCachedPlaybackFeatures(fi);
    scheduleFeatureLabelRepaint();
    return;
  }

  if (playbackFeaturesLoadInflight?.frameIdx === fi) {
    await playbackFeaturesLoadInflight.promise;
    if (currentPlaybackFeaturesFrameIdx() === fi) {
      applyCachedPlaybackFeatures(fi);
      scheduleFeatureLabelRepaint();
    }
    return;
  }

  renderPlaybackFeaturesBody(fi, [], { loading: true });

  const rid = playbackFeaturesRecordId;
  const promise = (async () => {
    try {
      const qs = new URLSearchParams({ frame_idx: String(fi) });
      const res = await fetch(`${recordApiUrl(rid, "/playback-features")}?${qs}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      const meta = {
        is_export_frame: body.is_export_frame,
        pose_frame_interval: body.pose_frame_interval,
        export_indices_source: body.export_indices_source,
        export_frame_idx: body.export_frame_idx,
      };
      if (!body.available) {
        return { persons: [], hint: body.hint || body.error || "无特征", meta };
      }
      return { persons: body.persons || [], hint: null, meta };
    } catch (err) {
      return { persons: [], error: err.message, meta: null };
    } finally {
      playbackFeaturesLoadInflight = null;
    }
  })();

  playbackFeaturesLoadInflight = { frameIdx: fi, promise };
  const result = await promise;
  playbackFeaturesCache.set(fi, result);
  if (playbackFeaturesCache.size > 80) {
    const oldest = playbackFeaturesCache.keys().next().value;
    playbackFeaturesCache.delete(oldest);
  }

  if (currentPlaybackFeaturesFrameIdx() === fi) {
    applyCachedPlaybackFeatures(fi);
    scheduleFeatureLabelRepaint();
  }
}

function updatePlaybackSkeletonFeaturesUi(frameIdx) {
  const panel = playbackFeaturesPanelEl();
  if (!panel || panel.classList.contains("hidden")) return;
  const fi = parseInt(frameIdx, 10);
  const frameLbl = $("#playback-skeleton-features-frame");
  if (frameLbl) frameLbl.textContent = Number.isFinite(fi) && fi > 0 ? String(fi) : "—";

  // 仅暂停时拉取并展示特征参数
  if (isPlaybackVideoPlaying()) {
    renderPlaybackFeaturesPlayingPlaceholder();
    return;
  }

  if (playbackFeaturesDebounceTimer) {
    clearTimeout(playbackFeaturesDebounceTimer);
  }
  playbackFeaturesDebounceTimer = setTimeout(() => {
    playbackFeaturesDebounceTimer = null;
    if (isPlaybackVideoPlaying()) {
      renderPlaybackFeaturesPlayingPlaceholder();
      return;
    }
    const targetFi = currentPlaybackFeaturesFrameIdx();
    if (targetFi > 0) void ensurePlaybackFeaturesForFrame(targetFi);
  }, 280);
}

/** 供 canvas：门控预览边框色（按 track 查缓存，坐标仍由当前帧骨架决定） */
function getPlaybackFeatureGateByTrack() {
  const map = new Map();
  if (!showFeatureTrackLabels) return map;
  (playbackFeaturesCurrentPersons || []).forEach((fp) => {
    const tid = Number(fp.person_track_id);
    if (Number.isFinite(tid) && tid > 0) map.set(tid, fp.gate_preview);
  });
  return map;
}

function isPlaybackFeatureTrackLabelsEnabled() {
  return showFeatureTrackLabels && !isPlaybackVideoPlaying();
}

/** @deprecated 标签改由当前帧 skeleton 绘制，保留兼容 */
function getPlaybackFeaturePersonsForCanvas() {
  if (!showFeatureTrackLabels) return [];
  return playbackFeaturesCurrentPersons || [];
}

let showFeatureTrackLabels = false;
const FEATURE_LABELS_STORAGE_KEY = "datacollect_playback_show_feature_labels";

function initPlaybackFeatureLabelToggle() {
  const cb = $("#playback-show-feature-labels");
  if (!cb) return;
  try {
    const saved = localStorage.getItem(FEATURE_LABELS_STORAGE_KEY);
    if (saved === "0") showFeatureTrackLabels = false;
  } catch (_) {
    /* ignore */
  }
  cb.checked = showFeatureTrackLabels;
  cb.addEventListener("change", () => {
    showFeatureTrackLabels = !!cb.checked;
    try {
      localStorage.setItem(FEATURE_LABELS_STORAGE_KEY, showFeatureTrackLabels ? "1" : "0");
    } catch (_) {
      /* ignore */
    }
    scheduleFeatureLabelRepaint();
  });
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", initPlaybackFeatureLabelToggle);
}
