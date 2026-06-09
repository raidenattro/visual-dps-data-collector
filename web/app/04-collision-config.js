/** 碰撞参数（表单 + localStorage） */
const COLLISION_METHOD_DEFAULTS = {
  wrist_point: {
    method: "wrist_point",
    alarm_min_consecutive_frames: 3,
    alarm_cooldown_frames: 6,
  },
  hand_state: {
    method: "hand_state",
    enter_window_frames: 6,
    enter_min_hits: 3,
    enter_timeout_frames: 12,
    exit_window_frames: 8,
    exit_min_releases: 5,
    exit_timeout_frames: 20,
    max_inside_frames: 75,
    cooldown_frames: 30,
    hit_threshold: 0.55,
    box_margin: 0.15,
    wrist_score_min: 0.45,
    elbow_score_min: 0.35,
    jump_max: 0.45,
    forearm_min_ratio: 0.5,
    forearm_max_ratio: 1.8,
    near_edge_ratio: 0.05,
  },
};

function normalizeCollisionMethod(method) {
  const key = String(method || "").trim().toLowerCase().replace(/-/g, "_");
  if (key === "hand" || key === "state" || key === "hand_state") return "hand_state";
  return "wrist_point";
}

function numberFromInput(selector, fallback, { integer = true, min = 0 } = {}) {
  const raw = $(selector)?.value;
  const parsed = integer ? parseInt(raw, 10) : parseFloat(raw);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, parsed);
}

function readCollisionConfigFromForm() {
  const method = normalizeCollisionMethod($("#collect-collision-method")?.value);
  if (method === "hand_state") {
    const cfg = {
      ...COLLISION_METHOD_DEFAULTS.hand_state,
      enter_window_frames: numberFromInput("#collect-enter-window", 6, { min: 1 }),
      enter_min_hits: numberFromInput("#collect-enter-min-hits", 3, { min: 1 }),
      exit_window_frames: numberFromInput("#collect-exit-window", 8, { min: 1 }),
      exit_min_releases: numberFromInput("#collect-exit-min-releases", 5, { min: 1 }),
      max_inside_frames: numberFromInput("#collect-max-inside", 75, { min: 1 }),
      cooldown_frames: numberFromInput("#collect-state-cooldown", 30, { min: 1 }),
      hit_threshold: numberFromInput("#collect-hit-threshold", 0.55, { integer: false, min: 0 }),
      box_margin: numberFromInput("#collect-box-margin", 0.15, { integer: false, min: 0 }),
      wrist_score_min: numberFromInput("#collect-wrist-score-min", 0.45, { integer: false, min: 0 }),
      jump_max: numberFromInput("#collect-jump-max", 0.45, { integer: false, min: 0 }),
    };
    cfg.enter_min_hits = Math.min(cfg.enter_min_hits, cfg.enter_window_frames);
    cfg.exit_min_releases = Math.min(cfg.exit_min_releases, cfg.exit_window_frames);
    return cfg;
  }
  return {
    ...COLLISION_METHOD_DEFAULTS.wrist_point,
    alarm_min_consecutive_frames: numberFromInput("#collect-alarm-min", 3, { min: 1 }),
    alarm_cooldown_frames: numberFromInput("#collect-alarm-cooldown", 6, { min: 1 }),
  };
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
    const method = normalizeCollisionMethod(data.method || data.collision_method);
    return { ...COLLISION_METHOD_DEFAULTS[method], ...data, method };
  } catch {
    return null;
  }
}

function applyCollisionConfigToForm(cfg) {
  if (!cfg) return;
  const method = normalizeCollisionMethod(cfg.method || cfg.collision_method);
  const merged = { ...COLLISION_METHOD_DEFAULTS[method], ...cfg, method };
  const methodEl = $("#collect-collision-method");
  if (methodEl) methodEl.value = method;
  document.querySelectorAll(".collision-param-group").forEach((el) => {
    el.classList.toggle("hidden", el.dataset.method !== method);
  });
  const minEl = $("#collect-alarm-min");
  const cdEl = $("#collect-alarm-cooldown");
  if (minEl && merged.alarm_min_consecutive_frames != null) {
    minEl.value = String(merged.alarm_min_consecutive_frames);
  }
  if (cdEl && merged.alarm_cooldown_frames != null) {
    cdEl.value = String(merged.alarm_cooldown_frames);
  }
  const map = {
    "#collect-enter-window": "enter_window_frames",
    "#collect-enter-min-hits": "enter_min_hits",
    "#collect-exit-window": "exit_window_frames",
    "#collect-exit-min-releases": "exit_min_releases",
    "#collect-max-inside": "max_inside_frames",
    "#collect-state-cooldown": "cooldown_frames",
    "#collect-hit-threshold": "hit_threshold",
    "#collect-box-margin": "box_margin",
    "#collect-wrist-score-min": "wrist_score_min",
    "#collect-jump-max": "jump_max",
  };
  Object.entries(map).forEach(([sel, key]) => {
    const el = $(sel);
    if (el && merged[key] != null) el.value = String(merged[key]);
  });
}

/** 采集落盘用 manifest；回放实时补算用表单/本地缓存 */
function getEffectiveCollisionConfig() {
  const stored = poseData?.collision;
  if (stored && typeof stored === "object" && stored.enabled) {
    return {
      alarm_min_consecutive_frames:
        stored.alarm_min_consecutive_frames ?? 3,
      alarm_cooldown_frames: stored.alarm_cooldown_frames ?? 6,
      method: normalizeCollisionMethod(stored.method),
      ...stored,
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
  if (serverCfg?.collision_methods) {
    Object.entries(serverCfg.collision_methods).forEach(([method, spec]) => {
      if (spec?.params) COLLISION_METHOD_DEFAULTS[normalizeCollisionMethod(method)] = spec.params;
    });
  }
  const stored = loadCollisionConfigFromStorage();
  applyCollisionConfigToForm(
    stored ||
      serverCfg?.collision_params || {
        method: serverCfg?.collision_method || "wrist_point",
        alarm_min_consecutive_frames: serverCfg?.alarm_min_consecutive_frames ?? 3,
        alarm_cooldown_frames: serverCfg?.alarm_cooldown_frames ?? 6,
      }
  );
  if (serverCfg?.frame_rate != null && $("#collect-fps") && !$("#collect-fps").dataset.userTouched) {
    $("#collect-fps").value = String(serverCfg.frame_rate);
  }
}

function bindCollisionConfigControls() {
  $("#collect-collision-method")?.addEventListener("change", () => {
    const method = normalizeCollisionMethod($("#collect-collision-method")?.value);
    applyCollisionConfigToForm(loadCollisionConfigFromStorage()?.method === method
      ? loadCollisionConfigFromStorage()
      : COLLISION_METHOD_DEFAULTS[method]);
    saveCollisionConfigToStorage(readCollisionConfigFromForm());
    resetPlaybackCollisionTracker();
  });
  document.querySelectorAll(".collision-config input").forEach((el) => {
    el.addEventListener("change", () => {
      saveCollisionConfigToStorage(readCollisionConfigFromForm());
      resetPlaybackCollisionTracker();
    });
  });
}

