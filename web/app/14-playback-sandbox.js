/** 沙盒碰撞回放：timeline/events 来自 sandbox API，pose/视频/标真仍用源记录 */

let playbackSandboxSessionId = null;
let playbackSandboxMeta = null;
let playbackSandboxTimeline = [];
let playbackSandboxOverlay = null;

function clearPlaybackSandbox() {
  playbackSandboxSessionId = null;
  playbackSandboxMeta = null;
  playbackSandboxTimeline = [];
  playbackSandboxOverlay = null;
  updatePlaybackSandboxBanner();
}

function isPlaybackSandboxActive() {
  return Boolean(playbackSandboxSessionId && playbackSandboxOverlay);
}

function playbackSandboxHint() {
  if (!playbackSandboxSessionId) return "";
  const sid = playbackSandboxSessionId.slice(0, 8);
  const params = playbackSandboxMeta?.params || {};
  const pm = params.probe_mode === "hand_extended" ? "延长手腕" : "手腕";
  const alpha =
    params.probe_mode === "hand_extended" ? ` α=${params.extension_ratio ?? 0.3}` : "";
  return `沙盒 ${sid} · ${pm}${alpha} · alarm_min=${params.alarm_min_consecutive_frames ?? "—"} cooldown=${params.alarm_cooldown_frames ?? "—"}`;
}

async function fetchSandboxSessionMeta(sessionId) {
  const res = await fetch(`/api/sandbox/sessions/${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `沙盒 session 不可用 (${res.status})`);
  }
  return res.json();
}

/** 沙盒货框标注（覆盖源记录 annotation） */
async function applyPlaybackSandboxAnnotation(sessionId) {
  const sid = String(sessionId || "").trim();
  if (!sid) return { ok: false, label: "", fromPose: false };
  try {
    const res = await fetch(`/api/sandbox/sessions/${encodeURIComponent(sid)}/annotation.json`);
    if (!res.ok) {
      syncAnnotationBoxesFromPose();
      return { ok: false, label: "沙盒标注", fromPose: annotationBoxes.length > 0 };
    }
    const data = await res.json();
    loadAnnotationBoxesFromData(data);
    return { ok: true, label: `沙盒 annotation（${annotationBoxes.length} 框）`, sandbox: true };
  } catch {
    syncAnnotationBoxesFromPose();
    return { ok: false, label: "沙盒标注", fromPose: annotationBoxes.length > 0 };
  }
}

function buildSandboxOverlayMap(timeline) {
  const map = new Map();
  (timeline || []).forEach((row) => {
    const payload = {
      collisions: Array.isArray(row.collisions) ? row.collisions : [],
      alarm_collisions: Array.isArray(row.alarm_collisions) ? row.alarm_collisions : [],
    };
    const sfi = Number(row.source_frame_idx) || Number(row.frame_idx) || 0;
    const fi = Number(row.frame_idx) || 0;
    if (sfi > 0) map.set(`s:${sfi}`, payload);
    if (fi > 0) map.set(`f:${fi}`, payload);
  });
  return map;
}

/** 用沙盒 timeline 重建 playbackEvents */
function syncPlaybackEventsFromSandbox() {
  if (!playbackSandboxSessionId || !playbackSandboxTimeline.length) {
    return false;
  }
  if (typeof buildEventsFromFrames !== "function") return false;

  const prevEvents = playbackEvents;
  let events = buildEventsFromFrames(playbackSandboxTimeline);
  if (typeof mergeVariantEventsVerifiedFromBaseline === "function") {
    events = mergeVariantEventsVerifiedFromBaseline(events);
  }
  if (typeof syncConfirmedBoxFromReview === "function" && playbackVerifiedReviewEntries?.length) {
    syncConfirmedBoxFromReview({ verified_true: playbackVerifiedReviewEntries }, events);
  }

  playbackEvents = events;
  playbackEventsFromVariant = true;
  playbackActiveVariantKey = `sandbox:${playbackSandboxSessionId}`;
  playbackEventsFromRealtime = false;

  if (typeof applyVerifiedFlagsToEvents === "function") applyVerifiedFlagsToEvents();
  if (typeof preserveActiveEventKeyAfterVariantSwap === "function") {
    preserveActiveEventKeyAfterVariantSwap(prevEvents, playbackEvents);
  }
  if (typeof rebuildPlaybackEventsFrameIndex === "function") rebuildPlaybackEventsFrameIndex();
  if (typeof renderEventReviewList === "function") renderEventReviewList();
  if (typeof invalidatePlaybackAccuracyOverlay === "function") invalidatePlaybackAccuracyOverlay();
  if (typeof updateEventMarkerActiveState === "function") updateEventMarkerActiveState();
  if (typeof refreshEventCountLabel === "function") refreshEventCountLabel();
  return true;
}

async function loadPlaybackSandbox(sessionId) {
  const sid = String(sessionId || "").trim();
  if (!sid) {
    clearPlaybackSandbox();
    return false;
  }

  playbackSandboxMeta = await fetchSandboxSessionMeta(sid);
  if (!playbackSandboxMeta?.recomputed && !playbackSandboxMeta?.has_timeline) {
    playbackSandboxSessionId = sid;
    playbackSandboxTimeline = [];
    playbackSandboxOverlay = null;
    updatePlaybackSandboxBanner();
    console.warn("沙盒尚未重算碰撞", playbackSandboxMeta);
    return false;
  }

  const qs = new URLSearchParams({ include_events: "1" });
  const res = await fetch(`/api/sandbox/sessions/${encodeURIComponent(sid)}/timeline?${qs.toString()}`);
  if (!res.ok) {
    playbackSandboxSessionId = sid;
    playbackSandboxTimeline = [];
    playbackSandboxOverlay = null;
    updatePlaybackSandboxBanner();
    console.warn("加载沙盒 timeline 失败", res.status);
    return false;
  }
  const body = await res.json();
  playbackSandboxSessionId = sid;
  playbackSandboxTimeline = Array.isArray(body.timeline) ? body.timeline : [];
  playbackSandboxOverlay = buildSandboxOverlayMap(playbackSandboxTimeline);

  if (typeof resetPlaybackCollisionTracker === "function") resetPlaybackCollisionTracker();
  if (playbackEventsBaseline.length || playbackEvents.length) {
    syncPlaybackEventsFromSandbox();
  }
  updatePlaybackSandboxBanner();
  return playbackSandboxTimeline.length > 0;
}

function updatePlaybackSandboxBanner() {
  const el = $("#playback-sandbox-banner");
  if (!el) return;
  if (!playbackSandboxSessionId) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.textContent = `🧪 沙盒模式 · 未写入正式数据 · ${playbackSandboxHint()}`;
  el.classList.remove("hidden");
}

/** 供 sandbox 页跳转回放 */
async function openSandboxPlayback(recordId, sessionId) {
  if (typeof openRecordReplay !== "function") return;
  playbackSandboxSessionId = String(sessionId || "").trim() || null;
  await openRecordReplay(recordId, "", "", true);
}
