/** 回放：当前帧手腕速度与碰撞段特征展示（按帧懒加载速度，避免整表 JSON） */

const WRIST_LABEL_ZH = { left_wrist: "左手腕", right_wrist: "右手腕" };

let playbackWristFeaturesRecordId = "";
let playbackWristFeaturesAvailable = false;
/** @type {{ available?: boolean, segments?: any[], velocity_count?: number, segment_count?: number, hint?: string, error?: string } | null} */
let playbackWristSegmentsPayload = null;
/** frameIdx -> velocity rows */
const playbackWristVelocityCache = new Map();
let playbackWristSegmentsLoadInflight = null;
let playbackWristVelocityLoadInflight = null;

function wristFeaturesPanelEl() {
  return $("#playback-wrist-features");
}

function currentPlaybackWristFrameIdx() {
  if (typeof tickPoseFrameIdx === "number" && tickPoseFrameIdx > 0) {
    return tickPoseFrameIdx;
  }
  if (typeof getCurrentPlaybackFrameIdx === "function") {
    const fi = getCurrentPlaybackFrameIdx();
    if (fi > 0) return fi;
  }
  return 0;
}

function fmtNum(v, digits = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function escWristHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/"/g, "&quot;");
}

function clearPlaybackWristFeatures() {
  playbackWristFeaturesRecordId = "";
  playbackWristFeaturesAvailable = false;
  playbackWristSegmentsPayload = null;
  playbackWristVelocityCache.clear();
  playbackWristSegmentsLoadInflight = null;
  playbackWristVelocityLoadInflight = null;
  const panel = wristFeaturesPanelEl();
  if (!panel) return;
  panel.classList.add("hidden");
  const velEl = $("#playback-wrist-velocity-body");
  const segEl = $("#playback-wrist-segments-body");
  const metaEl = $("#playback-wrist-features-meta");
  if (velEl) velEl.innerHTML = "";
  if (segEl) segEl.innerHTML = '<tr><td colspan="8" class="hint">—</td></tr>';
  if (metaEl) metaEl.textContent = "";
}

async function loadPlaybackWristFeatureSegments(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) {
    clearPlaybackWristFeatures();
    return null;
  }
  if (rid === playbackWristFeaturesRecordId && playbackWristSegmentsPayload) {
    return playbackWristSegmentsPayload;
  }
  if (playbackWristSegmentsLoadInflight?.rid === rid) {
    return playbackWristSegmentsLoadInflight.promise;
  }

  playbackWristFeaturesRecordId = rid;
  playbackWristFeaturesAvailable = false;

  const panel = wristFeaturesPanelEl();
  const metaEl = $("#playback-wrist-features-meta");
  if (panel) panel.classList.remove("hidden");
  if (metaEl) metaEl.textContent = "正在加载手腕特征…";

  const promise = (async () => {
    try {
      const res = await fetch(recordApiUrl(rid, "/wrist-features"));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      playbackWristSegmentsPayload = body;
      playbackWristFeaturesAvailable = !!body.available;
      playbackWristVelocityCache.clear();
      if (metaEl) {
        if (!body.available) {
          metaEl.textContent = body.hint || "未提取手腕特征";
        } else {
          metaEl.textContent = `速度共 ${body.velocity_count ?? 0} 行 · 碰撞段 ${body.segment_count ?? 0} 条`;
        }
      }
      panel?.classList.toggle("playback-wrist-features--empty", !body.available);
      const fi = currentPlaybackWristFrameIdx();
      if (fi > 0) {
        updatePlaybackWristFeaturesUi(fi);
      } else {
        renderSegmentsForFrame(0);
        renderVelocityRowsForFrame(0, [], { loading: playbackWristFeaturesAvailable });
      }
      return body;
    } catch (err) {
      playbackWristSegmentsPayload = { available: false, error: err.message };
      playbackWristFeaturesAvailable = false;
      if (metaEl) metaEl.textContent = `加载失败：${err.message}`;
      panel?.classList.add("playback-wrist-features--empty");
      return null;
    } finally {
      playbackWristSegmentsLoadInflight = null;
    }
  })();

  playbackWristSegmentsLoadInflight = { rid, promise };
  return promise;
}

/** 打开记录后后台加载（不阻塞 manifest / events / 视频） */
function loadPlaybackWristFeatures(recordId) {
  const rid = String(recordId || "").trim();
  if (!rid) return;
  window.setTimeout(() => {
    void loadPlaybackWristFeatureSegments(rid);
  }, 200);
}

async function ensurePlaybackWristVelocityForFrame(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  const velBody = $("#playback-wrist-velocity-body");

  if (fi <= 0 || !playbackWristFeaturesRecordId) {
    renderVelocityRowsForFrame(fi, []);
    return;
  }

  if (playbackWristSegmentsLoadInflight && !playbackWristFeaturesAvailable) {
    if (velBody) velBody.innerHTML = '<p class="hint">正在加载手腕特征…</p>';
    return;
  }

  if (!playbackWristFeaturesAvailable) {
    renderVelocityRowsForFrame(fi, [], {
      hint: playbackWristSegmentsPayload?.hint || playbackWristSegmentsPayload?.error || "无速度数据",
    });
    return;
  }

  if (playbackWristVelocityCache.has(fi)) {
    renderVelocityRowsForFrame(fi, playbackWristVelocityCache.get(fi));
    return;
  }

  if (playbackWristVelocityLoadInflight?.frameIdx === fi) {
    await playbackWristVelocityLoadInflight.promise;
    if (currentPlaybackWristFrameIdx() === fi) {
      renderVelocityRowsForFrame(fi, playbackWristVelocityCache.get(fi) || []);
    }
    return;
  }

  if (velBody) velBody.innerHTML = '<p class="hint">加载速度…</p>';

  const rid = playbackWristFeaturesRecordId;
  const promise = (async () => {
    try {
      const qs = new URLSearchParams({ frame_idx: String(fi) });
      const res = await fetch(`${recordApiUrl(rid, "/wrist-features")}?${qs}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      if (!body.available) return [];
      const rows = Array.isArray(body.frame_velocity) ? body.frame_velocity : [];
      playbackWristVelocityCache.set(fi, rows);
      if (playbackWristVelocityCache.size > 120) {
        const oldest = playbackWristVelocityCache.keys().next().value;
        playbackWristVelocityCache.delete(oldest);
      }
      return rows;
    } catch {
      playbackWristVelocityCache.set(fi, []);
      return [];
    } finally {
      playbackWristVelocityLoadInflight = null;
    }
  })();

  playbackWristVelocityLoadInflight = { frameIdx: fi, promise };
  const rows = await promise;
  if (currentPlaybackWristFrameIdx() === fi) {
    renderVelocityRowsForFrame(fi, rows);
  }
}

function renderVelocityRowsForFrame(frameIdx, rows, opts = {}) {
  const body = $("#playback-wrist-velocity-body");
  if (!body) return;

  if (opts.loading) {
    body.innerHTML = '<p class="hint">正在加载手腕特征…</p>';
    return;
  }

  if (opts.hint) {
    body.innerHTML = `<p class="hint">${escWristHtml(opts.hint)}</p>`;
    return;
  }

  if (!playbackWristFeaturesAvailable) {
    const payload = playbackWristSegmentsPayload;
    body.innerHTML = `<p class="hint">${escWristHtml(payload?.hint || payload?.error || "无速度数据")}</p>`;
    return;
  }

  const fi = parseInt(frameIdx, 10) || 0;
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) {
    body.innerHTML = `<p class="hint">帧 ${fi} 无有效手腕速度（可能无人或关键点置信度低）</p>`;
    return;
  }

  const order = { left_wrist: 0, right_wrist: 1 };
  const sorted = [...list].sort(
    (a, b) => (order[a.wrist] ?? 9) - (order[b.wrist] ?? 9) || (a.person_track_id || 0) - (b.person_track_id || 0)
  );

  body.innerHTML = `<table class="wrist-features-table">
    <thead><tr>
      <th>手腕</th><th>track</th><th>x</th><th>y</th><th>vx</th><th>vy</th><th>speed</th><th>norm</th>
    </tr></thead>
    <tbody>${sorted
      .map((r) => {
        const wrist = WRIST_LABEL_ZH[r.wrist] || r.wrist || "—";
        const speedCell = r.velocity_valid
          ? `${fmtNum(r.speed, 1)} <span class="hint">px/s</span>`
          : '<span class="hint">—</span>';
        return `<tr>
          <td>${escWristHtml(wrist)}</td>
          <td>${escWristHtml(r.person_track_id ?? "—")}</td>
          <td>${fmtNum(r.x)}</td>
          <td>${fmtNum(r.y)}</td>
          <td>${r.velocity_valid ? fmtNum(r.vx, 1) : "—"}</td>
          <td>${r.velocity_valid ? fmtNum(r.vy, 1) : "—"}</td>
          <td>${speedCell}</td>
          <td>${r.velocity_valid ? fmtNum(r.speed_norm, 4) : "—"}</td>
        </tr>`;
      })
      .join("")}</tbody>
  </table>`;
}

function renderSegmentsForFrame(frameIdx) {
  const body = $("#playback-wrist-segments-body");
  if (!body) return;

  if (playbackWristSegmentsLoadInflight && !playbackWristSegmentsPayload) {
    body.innerHTML = `<tr><td colspan="8" class="hint">加载中…</td></tr>`;
    return;
  }

  const payload = playbackWristSegmentsPayload;
  if (!payload?.available) {
    body.innerHTML = `<tr><td colspan="8" class="hint">${escWristHtml(payload?.hint || payload?.error || "无碰撞段数据")}</td></tr>`;
    return;
  }

  const fi = parseInt(frameIdx, 10) || 0;
  const segments = Array.isArray(payload.segments) ? payload.segments : [];
  const active = segments.filter((s) => {
    const a = parseInt(s.frame_enter, 10) || 0;
    const b = parseInt(s.frame_exit, 10) || 0;
    return fi >= a && fi <= b;
  });

  if (!active.length) {
    body.innerHTML = `<tr><td colspan="8" class="hint">帧 ${fi} 无进行中的碰撞段</td></tr>`;
    return;
  }

  body.innerHTML = active
    .map((s) => {
      const wrist = WRIST_LABEL_ZH[s.wrist] || s.wrist || "—";
      const alarm = s.had_alarm ? '<span class="wrist-seg-alarm" title="段内曾告警">告警</span>' : "";
      const isEnter = fi === (parseInt(s.frame_enter, 10) || 0);
      const isExit = fi === (parseInt(s.frame_exit, 10) || 0);
      let phase = "";
      if (isEnter && isExit) phase = "瞬时";
      else if (isEnter) phase = "进入";
      else if (isExit) phase = "离开";
      else phase = "段内";
      return `<tr>
        <td>${escWristHtml(wrist)}</td>
        <td><code>${escWristHtml(s.box_token)}</code></td>
        <td>${parseInt(s.frame_enter, 10) || "—"}–${parseInt(s.frame_exit, 10) || "—"}</td>
        <td>${escWristHtml(phase)}</td>
        <td>${fmtNum(s.x_enter)}/${fmtNum(s.y_enter)}</td>
        <td>${fmtNum(s.x_exit)}/${fmtNum(s.y_exit)}</td>
        <td>Δ${fmtNum(s.dx)}/${fmtNum(s.dy)} · ${fmtNum(s.displacement, 1)}</td>
        <td>${alarm}</td>
      </tr>`;
    })
    .join("");
}

function updatePlaybackWristFeaturesUi(frameIdx) {
  const panel = wristFeaturesPanelEl();
  if (!panel || panel.classList.contains("hidden")) return;
  const fi = parseInt(frameIdx, 10);
  const frameLabel = $("#playback-wrist-features-frame");
  if (!Number.isFinite(fi) || fi <= 0) {
    renderVelocityRowsForFrame(0, []);
    renderSegmentsForFrame(0);
    if (frameLabel) frameLabel.textContent = "—";
    return;
  }
  if (frameLabel) frameLabel.textContent = String(fi);

  // 仅暂停时加载帧级手腕速度
  if (typeof isPlaybackVideoPlaying === "function" && isPlaybackVideoPlaying()) {
    renderWristFeaturesPlayingPlaceholder(fi);
    return;
  }

  renderSegmentsForFrame(fi);
  void ensurePlaybackWristVelocityForFrame(fi);
}

function renderWristFeaturesPlayingPlaceholder(frameIdx) {
  const velBody = $("#playback-wrist-velocity-body");
  if (velBody) {
    velBody.innerHTML = '<p class="hint">播放中，暂停后可查看本帧手腕速度</p>';
  }
  const segBody = $("#playback-wrist-segments-body");
  if (segBody) {
    segBody.innerHTML = '<tr><td colspan="8" class="hint">播放中，暂停后显示</td></tr>';
  }
}

function onPlaybackWristFeaturesPlayStateChange() {
  const fi =
    typeof currentPlaybackWristFrameIdx === "function" ? currentPlaybackWristFrameIdx() : 0;
  if (typeof isPlaybackVideoPlaying === "function" && isPlaybackVideoPlaying()) {
    if (fi > 0) renderWristFeaturesPlayingPlaceholder(fi);
    return;
  }
  if (fi > 0) updatePlaybackWristFeaturesUi(fi);
}
