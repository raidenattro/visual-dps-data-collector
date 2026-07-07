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
    const [annRes, metaRes] = await Promise.all([
      fetch(`/api/sandbox/sessions/${encodeURIComponent(sid)}/annotation.json`),
      fetch(`/api/sandbox/sessions/${encodeURIComponent(sid)}`),
    ]);
    if (!annRes.ok) {
      syncAnnotationBoxesFromPose();
      return {
        ok: false,
        label: "沙盒标注",
        fromPose: annotationBoxes.length > 0,
        error: `HTTP ${annRes.status}`,
      };
    }
    const data = await annRes.json();
    loadAnnotationBoxesFromData(data);
    const meta = metaRes.ok ? await metaRes.json() : {};
    const edited = Boolean(meta.annotation_edited);
    const srcAnn = meta.annotation_info?.source_annotation || meta.annotation?.source_annotation || "";
    if (typeof updatePlaybackAnnotationSourceUiForSandbox === "function") {
      updatePlaybackAnnotationSourceUiForSandbox(annotationBoxes.length, edited);
    }
    const editNote = edited ? "已保存编辑" : "创建时复制的源标注（未点保存则与源记录相同）";
    const srcNote = srcAnn ? ` · 源自 ${srcAnn}` : "";
    return {
      ok: true,
      label: `沙盒标注（${annotationBoxes.length} 框 · ${editNote}${srcNote}）`,
      sandbox: true,
      annotationEdited: edited,
    };
  } catch (err) {
    syncAnnotationBoxesFromPose();
    return {
      ok: false,
      label: "沙盒标注",
      fromPose: annotationBoxes.length > 0,
      error: err?.message || String(err),
    };
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
    if (typeof updatePlaybackAnnotationSourceUiForSandbox === "function") {
      updatePlaybackAnnotationSourceUiForSandbox(null);
    }
    return;
  }
  const boxNote = annotationBoxes?.length ? ` · 沙盒标注 ${annotationBoxes.length} 框` : "";
  el.textContent = `🧪 沙盒模式 · 未写入正式数据${boxNote} · ${playbackSandboxHint()}`;
  el.classList.remove("hidden");
}

/** 回放页标注来源：沙盒模式下锁定为沙盒标注 */
function updatePlaybackAnnotationSourceUiForSandbox(boxCount = null, edited = null) {
  const annSrcSel = $("#playback-annotation-source");
  if (!annSrcSel) return;
  const sandboxActive = Boolean(playbackSandboxSessionId);
  annSrcSel.disabled = sandboxActive;
  annSrcSel.title = sandboxActive
    ? "沙盒回放：货框来自 localdata/upload/sandbox/{session}/annotation.json"
    : "回放货框叠加来源：母本或当前所选模型层标注目录";
  if (sandboxActive) {
    let opt = annSrcSel.querySelector('option[value="sandbox"]');
    if (!opt) {
      opt = document.createElement("option");
      opt.value = "sandbox";
      annSrcSel.insertBefore(opt, annSrcSel.firstChild);
    }
    const n = boxCount != null ? boxCount : annotationBoxes?.length || 0;
    const editTag = edited === true ? "已保存" : edited === false ? "未保存编辑" : "";
    opt.textContent = editTag ? `沙盒标注（${n} 框 · ${editTag}）` : `沙盒标注（${n} 框）`;
    annSrcSel.value = "sandbox";
    return;
  }
  const sandboxOpt = annSrcSel.querySelector('option[value="sandbox"]');
  if (sandboxOpt) sandboxOpt.remove();
  if (annSrcSel.value === "sandbox") {
    annSrcSel.value = "tier";
  }
}

/** 供 sandbox 页跳转回放 */
async function openSandboxPlayback(recordId, sessionId) {
  if (typeof openRecordReplay !== "function") return;
  playbackSandboxSessionId = String(sessionId || "").trim() || null;
  await openRecordReplay(recordId, "", "", true);
}
