/** 回放碰撞变体：读取 sidecar timeline，覆盖框色/事件碰撞源 */

const PLAYBACK_PROBE_STORAGE_KEY = "datacollect_playback_collision_probe";

/** select value → sidecar variant key */
const PLAYBACK_HAND_EXT_VARIANT_BY_ALPHA = {
  "0.1": "hand_ext_0.10",
  "0.2": "hand_ext_0.20",
  "0.3": "hand_ext_0.30",
  "0.4": "hand_ext_0.40",
};

let playbackCollisionOverlay = null;
let playbackCollisionVariantMeta = null;
/** 当前变体完整 timeline 行（用于重建事件列表） */
let playbackCollisionVariantTimeline = [];

function loadPlaybackProbePrefs() {
  try {
    const raw = localStorage.getItem(PLAYBACK_PROBE_STORAGE_KEY);
    if (!raw) return { useHand: false, alpha: "0.3" };
    const data = JSON.parse(raw);
    return {
      useHand: Boolean(data?.useHand),
      alpha: String(data?.alpha || "0.3"),
    };
  } catch {
    return { useHand: false, alpha: "0.3" };
  }
}

function savePlaybackProbePrefs() {
  const useHand = Boolean($("#playback-use-hand-probe")?.checked);
  const alpha = String($("#playback-hand-probe-alpha")?.value || "0.3");
  try {
    localStorage.setItem(
      PLAYBACK_PROBE_STORAGE_KEY,
      JSON.stringify({ useHand, alpha })
    );
  } catch {
    /* ignore */
  }
}

function applyPlaybackProbePrefsToForm() {
  const prefs = loadPlaybackProbePrefs();
  const handEl = $("#playback-use-hand-probe");
  const alphaEl = $("#playback-hand-probe-alpha");
  if (handEl) handEl.checked = prefs.useHand;
  if (alphaEl) alphaEl.value = prefs.alpha;
  if (alphaEl) alphaEl.disabled = !prefs.useHand;
}

function resolvePlaybackCollisionVariantKey() {
  const useHand = Boolean($("#playback-use-hand-probe")?.checked);
  if (!useHand) return "wrist";
  const alpha = String($("#playback-hand-probe-alpha")?.value || "0.3");
  return PLAYBACK_HAND_EXT_VARIANT_BY_ALPHA[alpha] || "hand_ext_0.30";
}

function resetPlaybackCollisionOverlay() {
  playbackCollisionOverlay = null;
  playbackCollisionVariantMeta = null;
  playbackCollisionVariantTimeline = [];
}

function playbackVariantEventsLabel(variantKey) {
  if (String(variantKey || "").startsWith("sandbox:")) return "沙盒重算";
  if (variantKey === "wrist") return "sidecar/wrist";
  if (variantKey === "hand_ext_0.10") return "sidecar/α0.1";
  if (variantKey === "hand_ext_0.20") return "sidecar/α0.2";
  if (variantKey === "hand_ext_0.30") return "sidecar/α0.3";
  if (variantKey === "hand_ext_0.40") return "sidecar/α0.4";
  return `sidecar/${variantKey}`;
}

function mergeVariantEventsVerifiedFromBaseline(variantEvents) {
  if (typeof applyVerifiedReviewToEvents === "function") {
    applyVerifiedReviewToEvents(variantEvents);
    return variantEvents;
  }
  return variantEvents;
}

function preserveActiveEventKeyAfterVariantSwap(prevEvents, nextEvents) {
  if (!activeEventKey || !prevEvents?.length || !nextEvents?.length) return;
  const prev = prevEvents.find((ev) => eventRowKey(ev) === activeEventKey);
  if (!prev) return;
  const fi = parseInt(prev.frame_idx, 10) || 0;
  if (!fi) return;
  if (nextEvents.some((ev) => eventRowKey(ev) === activeEventKey)) return;
  const sameFrame = nextEvents.find((ev) => (parseInt(ev.frame_idx, 10) || 0) === fi);
  if (sameFrame) activeEventKey = eventRowKey(sameFrame);
}

/** 用碰撞变体 sidecar 重建 playbackEvents（计数/进度条/漏误报标点） */
function syncPlaybackEventsFromCollisionVariant() {
  const variantKey = resolvePlaybackCollisionVariantKey();
  const hasSidecar =
    playbackCollisionOverlay &&
    Array.isArray(playbackCollisionVariantTimeline) &&
    playbackCollisionVariantTimeline.length > 0;
  const variantAvailable = Boolean(
    playbackCollisionVariantMeta?.variants?.[variantKey]?.available
  );

  if (!hasSidecar || !variantAvailable) {
    if (playbackEventsFromVariant && typeof restorePlaybackEventsBaseline === "function") {
      restorePlaybackEventsBaseline();
    }
    return false;
  }

  if (typeof buildEventsFromFrames !== "function") return false;

  const prevEvents = playbackEvents;
  let events = buildEventsFromFrames(playbackCollisionVariantTimeline);
  events = mergeVariantEventsVerifiedFromBaseline(events);
  if (typeof syncConfirmedBoxFromReview === "function" && playbackVerifiedReviewEntries.length) {
    syncConfirmedBoxFromReview({ verified_true: playbackVerifiedReviewEntries }, events);
  }

  playbackEvents = events;
  playbackEventsFromVariant = true;
  playbackActiveVariantKey = variantKey;
  playbackEventsFromRealtime = false;

  if (typeof applyVerifiedFlagsToEvents === "function") applyVerifiedFlagsToEvents();
  preserveActiveEventKeyAfterVariantSwap(prevEvents, playbackEvents);

  if (typeof rebuildPlaybackEventsFrameIndex === "function") {
    rebuildPlaybackEventsFrameIndex();
  }
  if (typeof renderEventReviewList === "function") renderEventReviewList();
  if (typeof invalidatePlaybackAccuracyOverlay === "function") {
    invalidatePlaybackAccuracyOverlay();
  }
  if (typeof updateEventMarkerActiveState === "function") updateEventMarkerActiveState();
  return true;
}

function getPlaybackCollisionOverlayForFrame(frame) {
  if (typeof playbackSandboxOverlay !== "undefined" && playbackSandboxOverlay && frame) {
    const sfi = Number(frame.source_frame_idx) || Number(frame.frame_idx) || 0;
    const fi = Number(frame.frame_idx) || 0;
    return (
      playbackSandboxOverlay.get(`s:${sfi}`) ||
      playbackSandboxOverlay.get(`f:${fi}`) ||
      null
    );
  }
  if (!playbackCollisionOverlay || !frame) return null;
  const sfi = Number(frame.source_frame_idx) || Number(frame.frame_idx) || 0;
  const fi = Number(frame.frame_idx) || 0;
  return (
    playbackCollisionOverlay.get(`s:${sfi}`) ||
    playbackCollisionOverlay.get(`f:${fi}`) ||
    null
  );
}

function playbackCollisionVariantHint() {
  if (typeof playbackSandboxSessionId !== "undefined" && playbackSandboxSessionId) {
    return typeof playbackSandboxHint === "function" ? playbackSandboxHint() : "沙盒碰撞";
  }
  const key = resolvePlaybackCollisionVariantKey();
  if (key === "wrist") return "碰撞：手腕 baseline";
  const alpha = $("#playback-hand-probe-alpha")?.value || "0.3";
  return `碰撞：延长手腕 α=${alpha} · 橙/黄点为模拟手部探针`;
}

/** 与 event_engine/wrist_hits.py 一致 */
const HAND_PROBE_KPT_SCORE_MIN = 0.3;
const HAND_PROBE_MIN_FOREARM_PX = 15;
const HAND_PROBE_ARM_SIDES = [
  [7, 9, "left"],
  [8, 10, "right"],
];

function isPlaybackHandProbeEnabled() {
  return Boolean($("#playback-use-hand-probe")?.checked);
}

function getPlaybackHandProbeAlpha() {
  const alpha = parseFloat($("#playback-hand-probe-alpha")?.value || "0.3");
  return Number.isFinite(alpha) ? Math.max(0, alpha) : 0.3;
}

function readHandProbeKeypoint(kpts, idx, scoreMin = HAND_PROBE_KPT_SCORE_MIN) {
  const kp = kpts?.[idx];
  if (!kp || kp.length < 3 || Number(kp[2]) <= scoreMin) return null;
  const x = Number(kp[0]);
  const y = Number(kp[1]);
  const score = Number(kp[2]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x, y, score };
}

/** P_sim = P_wrist + α × (P_wrist − P_elbow) */
function computeHandExtendedProbe(person, elbowIdx, wristIdx, alpha) {
  const kpts = person?.keypoints || [];
  const wrist = readHandProbeKeypoint(kpts, wristIdx);
  if (!wrist) return null;

  const elbow = readHandProbeKeypoint(kpts, elbowIdx);
  if (!elbow) {
    return {
      x: wrist.x,
      y: wrist.y,
      score: wrist.score,
      wx: wrist.x,
      wy: wrist.y,
      kind: "wrist_fallback",
    };
  }

  const fx = wrist.x - elbow.x;
  const fy = wrist.y - elbow.y;
  const forearmLen = Math.hypot(fx, fy);
  if (forearmLen < HAND_PROBE_MIN_FOREARM_PX) {
    return {
      x: wrist.x,
      y: wrist.y,
      score: wrist.score,
      wx: wrist.x,
      wy: wrist.y,
      kind: "wrist_fallback",
    };
  }

  const ratio = Math.max(0, alpha);
  return {
    x: wrist.x + ratio * fx,
    y: wrist.y + ratio * fy,
    score: wrist.score,
    wx: wrist.x,
    wy: wrist.y,
    kind: "hand_sim",
  };
}

/** 当前帧左右手延长探针（仅勾选「延长手腕」时返回非空） */
function collectPlaybackHandProbePoints(frame) {
  if (!isPlaybackHandProbeEnabled() || !frame?.persons?.length) return [];
  const alpha = getPlaybackHandProbeAlpha();
  const points = [];
  frame.persons.forEach((person, personIdx) => {
    HAND_PROBE_ARM_SIDES.forEach(([elbowIdx, wristIdx, side]) => {
      const probe = computeHandExtendedProbe(person, elbowIdx, wristIdx, alpha);
      if (probe) points.push({ ...probe, side, personIdx });
    });
  });
  return points;
}

async function fetchPlaybackCollisionVariants(recordId) {
  const res = await fetch(recordApiUrl(recordId, "/collision-variants"));
  if (!res.ok) return null;
  return res.json();
}

async function loadPlaybackCollisionVariant(recordId) {
  resetPlaybackCollisionOverlay();
  if (!recordId) return false;

  playbackCollisionVariantMeta = await fetchPlaybackCollisionVariants(recordId);
  const variantKey = resolvePlaybackCollisionVariantKey();
  const variantInfo = playbackCollisionVariantMeta?.variants?.[variantKey];
  if (!variantInfo?.available) {
    console.warn(`碰撞变体不可用: ${variantKey}`, playbackCollisionVariantMeta);
    playbackCollisionVariantTimeline = [];
    if (typeof restorePlaybackEventsBaseline === "function") {
      restorePlaybackEventsBaseline();
    }
    return false;
  }

  const qs = new URLSearchParams({
    variant: variantKey,
    include_events: "1",
  });
  const res = await fetch(recordApiUrl(recordId, `/timeline?${qs.toString()}`));
  if (!res.ok) {
    console.warn("加载碰撞变体 timeline 失败", res.status);
    playbackCollisionVariantTimeline = [];
    if (typeof restorePlaybackEventsBaseline === "function") {
      restorePlaybackEventsBaseline();
    }
    return false;
  }
  const body = await res.json();
  playbackCollisionVariantTimeline = Array.isArray(body.timeline) ? body.timeline : [];
  const map = new Map();
  playbackCollisionVariantTimeline.forEach((row) => {
    const payload = {
      collisions: Array.isArray(row.collisions) ? row.collisions : [],
      alarm_collisions: Array.isArray(row.alarm_collisions)
        ? row.alarm_collisions
        : [],
    };
    const sfi = Number(row.source_frame_idx) || Number(row.frame_idx) || 0;
    const fi = Number(row.frame_idx) || 0;
    if (sfi > 0) map.set(`s:${sfi}`, payload);
    if (fi > 0) map.set(`f:${fi}`, payload);
  });
  playbackCollisionOverlay = map;
  if (typeof resetPlaybackCollisionTracker === "function") {
    resetPlaybackCollisionTracker();
  }
  if (playbackEventsBaseline.length || playbackEvents.length) {
    syncPlaybackEventsFromCollisionVariant();
  } else if (typeof invalidatePlaybackAccuracyOverlay === "function") {
    invalidatePlaybackAccuracyOverlay();
  }
  return true;
}

function initPlaybackCollisionVariantControls() {
  applyPlaybackProbePrefsToForm();

  const handEl = $("#playback-use-hand-probe");
  const alphaEl = $("#playback-hand-probe-alpha");

  handEl?.addEventListener("change", () => {
    if (alphaEl) alphaEl.disabled = !handEl.checked;
    savePlaybackProbePrefs();
    if (currentRecordId) {
      void reloadPlaybackCollisionVariant();
    }
  });

  alphaEl?.addEventListener("change", () => {
    savePlaybackProbePrefs();
    if (currentRecordId && handEl?.checked) {
      void reloadPlaybackCollisionVariant();
    } else if (typeof redrawCurrentFrame === "function") {
      redrawCurrentFrame();
    }
  });
}

async function reloadPlaybackCollisionVariant() {
  if (!currentRecordId) return;
  const ok = await loadPlaybackCollisionVariant(currentRecordId);
  const info = $("#playback-info");
  if (info && ok) {
    const base = String(info.textContent || "").replace(/ · 碰撞：.*$/, "");
    info.textContent = `${base} · ${playbackCollisionVariantHint()}`;
  } else if (info && !ok) {
    const base = String(info.textContent || "").replace(/ · 碰撞：.*$/, "");
    info.textContent = base;
  }
  if (typeof redrawCurrentFrame === "function") {
    redrawCurrentFrame();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initPlaybackCollisionVariantControls();
});
