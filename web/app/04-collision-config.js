/** 碰撞参数（表单 + localStorage） */
function readCollisionConfigFromForm() {
  const min = Math.max(1, parseInt($("#collect-alarm-min")?.value, 10) || 3);
  const cd = Math.max(1, parseInt($("#collect-alarm-cooldown")?.value, 10) || 6);
  return { alarm_min_consecutive_frames: min, alarm_cooldown_frames: cd };
}

function saveCollisionConfigToStorage(cfg) {
  try {
    localStorage.setItem(COLLISION_CFG_STORAGE_KEY, JSON.stringify(cfg));
  } catch {
    /* ignore */
  }
}

function loadCollisionConfigFromStorage() {
  try {
    const raw = localStorage.getItem(COLLISION_CFG_STORAGE_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return null;
    return {
      alarm_min_consecutive_frames: Math.max(
        1,
        parseInt(data.alarm_min_consecutive_frames, 10) || 3
      ),
      alarm_cooldown_frames: Math.max(1, parseInt(data.alarm_cooldown_frames, 10) || 6),
    };
  } catch {
    return null;
  }
}

function applyCollisionConfigToForm(cfg) {
  if (!cfg) return;
  const minEl = $("#collect-alarm-min");
  const cdEl = $("#collect-alarm-cooldown");
  if (minEl && cfg.alarm_min_consecutive_frames != null) {
    minEl.value = String(cfg.alarm_min_consecutive_frames);
  }
  if (cdEl && cfg.alarm_cooldown_frames != null) {
    cdEl.value = String(cfg.alarm_cooldown_frames);
  }
}

/** 采集落盘用 manifest；回放实时补算用表单/本地缓存 */
function getEffectiveCollisionConfig() {
  const stored = poseData?.collision;
  if (stored && typeof stored === "object" && stored.enabled) {
    return {
      alarm_min_consecutive_frames:
        stored.alarm_min_consecutive_frames ?? 3,
      alarm_cooldown_frames: stored.alarm_cooldown_frames ?? 6,
    };
  }
  return loadCollisionConfigFromStorage() || readCollisionConfigFromForm();
}

async function loadInferenceConfigDefaults() {
  let serverCfg = null;
  try {
    const res = await fetch("/api/config/inference");
    if (res.ok) serverCfg = await res.json();
  } catch {
    /* ignore */
  }
  const stored = loadCollisionConfigFromStorage();
  const merged = {
    alarm_min_consecutive_frames:
      stored?.alarm_min_consecutive_frames ??
      serverCfg?.alarm_min_consecutive_frames ??
      3,
    alarm_cooldown_frames:
      stored?.alarm_cooldown_frames ?? serverCfg?.alarm_cooldown_frames ?? 6,
  };
  applyCollisionConfigToForm(merged);
  if (typeof applyAccuracyCollisionConfigToForm === "function") {
    applyAccuracyCollisionConfigToForm(merged);
  }
  if (serverCfg?.frame_rate != null && $("#collect-fps") && !$("#collect-fps").dataset.userTouched) {
    $("#collect-fps").value = String(serverCfg.frame_rate);
  }
}

function readAccuracyCollisionConfigFromForm() {
  const min = Math.max(1, parseInt(document.querySelector("#accuracy-alarm-min")?.value, 10) || 3);
  const cd = Math.max(1, parseInt(document.querySelector("#accuracy-alarm-cooldown")?.value, 10) || 6);
  return { alarm_min_consecutive_frames: min, alarm_cooldown_frames: cd };
}

function applyAccuracyCollisionConfigToForm(cfg) {
  if (!cfg) return;
  const minEl = document.querySelector("#accuracy-alarm-min");
  const cdEl = document.querySelector("#accuracy-alarm-cooldown");
  if (minEl && cfg.alarm_min_consecutive_frames != null) {
    minEl.value = String(cfg.alarm_min_consecutive_frames);
  }
  if (cdEl && cfg.alarm_cooldown_frames != null) {
    cdEl.value = String(cfg.alarm_cooldown_frames);
  }
}

window.readAccuracyCollisionConfigFromForm = readAccuracyCollisionConfigFromForm;
window.applyAccuracyCollisionConfigToForm = applyAccuracyCollisionConfigToForm;

