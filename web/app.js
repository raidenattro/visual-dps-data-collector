/** COCO-17 骨架连线（与 visual-dps 一致） */
const COCO_LINES = [
  [15, 13], [13, 11], [16, 14], [14, 12], [11, 12], [5, 11], [6, 12], [5, 6],
  [5, 7], [6, 8], [7, 9], [8, 10], [1, 2], [0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6],
];
const SCORE_MIN = 0.3;

const $ = (sel) => document.querySelector(sel);
const tabs = document.querySelectorAll(".tab");
const panels = { collect: $("#panel-collect"), playback: $("#panel-playback") };

let poseData = null;
let annotationBoxes = [];
let frameByTime = [];
let rafId = null;
let playbackId = null;
let playbackVideoObjectUrl = null;

/** object-fit: contain 布局（与 visual-dps previewLayout 一致） */
function computeContainLayout(containerW, containerH, frameW, frameH) {
  const cw = Math.max(1, containerW);
  const ch = Math.max(1, containerH);
  const fw = Math.max(1, frameW || cw);
  const fh = Math.max(1, frameH || ch);
  const scale = Math.min(cw / fw, ch / fh);
  const drawW = fw * scale;
  const drawH = fh * scale;
  return {
    offsetX: (cw - drawW) / 2,
    offsetY: (ch - drawH) / 2,
    drawW,
    drawH,
    frameW: fw,
    frameH: fh,
  };
}

/** 推理坐标 → 容器内显示坐标（对齐视频 letterbox 区域） */
function mapInferToDisplay(x, y, inferW, inferH, layout) {
  const iw = Math.max(1, inferW);
  const ih = Math.max(1, inferH);
  return [
    layout.offsetX + (Number(x) * layout.drawW) / iw,
    layout.offsetY + (Number(y) * layout.drawH) / ih,
  ];
}

function getVideoFrameSize() {
  const vw = videoEl.videoWidth;
  const vh = videoEl.videoHeight;
  if (vw > 0 && vh > 0) return { frameW: vw, frameH: vh };
  const f0 = poseData?.frames?.[0];
  return {
    frameW: f0?.infer_width || 640,
    frameH: f0?.infer_height || 480,
  };
}

function getDisplayLayout() {
  const wrap = document.querySelector(".stage-wrap");
  const rect = wrap.getBoundingClientRect();
  const { frameW, frameH } = getVideoFrameSize();
  return computeContainLayout(rect.width, rect.height, frameW, frameH);
}

// --- 回放临时视频清理（仅服务端临时目录；本地 blob 用 revoke） ---
async function cleanupPlaybackVideo() {
  if (playbackId) {
    try {
      await fetch(`/api/playback/video/${playbackId}`, { method: "DELETE" });
    } catch {
      /* ignore */
    }
    playbackId = null;
  }
  if (playbackVideoObjectUrl) {
    URL.revokeObjectURL(playbackVideoObjectUrl);
    playbackVideoObjectUrl = null;
  }
}

// --- 标签页 ---
tabs.forEach((btn) => {
  btn.addEventListener("click", () => {
    const leavingPlayback = panels.playback.classList.contains("active") && btn.dataset.tab !== "playback";
    if (leavingPlayback) {
      stopPlayback();
      cleanupPlaybackVideo();
      clearVideoElement();
    }
    tabs.forEach((b) => b.classList.toggle("active", b === btn));
    Object.values(panels).forEach((p) => p.classList.remove("active"));
    panels[btn.dataset.tab].classList.add("active");
    if (btn.dataset.tab === "collect") loadRecords();
  });
});

// --- 采集 ---
const collectForm = $("#collect-form");
const collectStatus = $("#collect-status");
const collectResult = $("#collect-result");
const collectBtn = $("#collect-btn");

function showStatus(html, isError = false) {
  collectStatus.classList.remove("hidden", "error");
  if (isError) collectStatus.classList.add("error");
  collectStatus.innerHTML = html;
}

function hideStatus() {
  collectStatus.classList.add("hidden");
}

async function pollJob(jobId) {
  for (;;) {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(await res.text());
    const job = await res.json();
    const pct = job.progress ?? 0;
    showStatus(
      `<div>${job.message || job.status}</div>
       <div class="progress"><i style="width:${pct}%"></i></div>`
    );
    if (job.status === "done") return job;
    if (job.status === "error") throw new Error(job.message || "采集失败");
    await new Promise((r) => setTimeout(r, 800));
  }
}

collectForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("#collect-file").files[0];
  if (!file) return;

  const fd = new FormData();
  fd.append("file", file);
  fd.append("backend", $("#collect-backend").value);
  fd.append("det_variant", $("#collect-det").value);
  fd.append("width", $("#collect-width").value || "0");
  fd.append("height", $("#collect-height").value || "0");
  fd.append("frame_rate", $("#collect-fps").value ?? "0");
  fd.append("pose_frame_interval", $("#collect-interval").value || "1");
  fd.append("max_pose_frames", $("#collect-max").value || "0");
  fd.append("save_video", $("#collect-save-video").checked ? "1" : "0");
  const annFile = $("#collect-annotation").files[0];
  if (annFile) fd.append("annotation", annFile);

  collectBtn.disabled = true;
  const savingVideo = $("#collect-save-video").checked;
  showStatus(savingVideo ? "上传并推理中…（将保存 JSON 与配套视频）" : "上传并推理中…（仅保存 JSON）");

  try {
    const res = await fetch("/api/collect", { method: "POST", body: fd });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);

    const job = await pollJob(body.job_id);
    const rid = job.record_id || body.record_id || job.job_id;
    collectResult.classList.remove("hidden");
    const hasVideo = job.has_video || savingVideo;
    collectResult.innerHTML = `
      <p>✅ 已保存至 <code>localdata/json</code>${hasVideo ? " 与 <code>localdata/video</code>" : ""}${job.has_annotation ? " · 含碰撞检测" : ""}，共 <strong>${job.frame_count ?? "?"}</strong> 帧</p>
      <p>
        <a href="${job.pose_url}" download>下载 JSON</a>
        · <button type="button" class="primary-link" data-replay="${rid}" data-has-video="${hasVideo ? "1" : "0"}">回放</button>
      </p>`;
    const poseFile = job.pose_file || body.pose_file || "";
    const dispName =
      job.display_name || poseFile.replace(/_rtmpose_[tsm](?:_[\w]+)?\.json$/i, "") || rid;
    collectResult.querySelector("[data-replay]")?.addEventListener("click", async () => {
      try {
        await openRecordReplay(rid, dispName, poseFile, hasVideo);
      } catch (err) {
        setPlaybackInfo(`❌ ${err.message}`);
      }
    });
    hideStatus();
    loadRecords();
  } catch (err) {
    showStatus(`❌ ${err.message}`, true);
  } finally {
    collectBtn.disabled = false;
  }
});

async function loadRecords() {
  const list = $("#session-list");
  try {
    const res = await fetch("/api/records");
    const items = await res.json();
    if (!items.length) {
      list.innerHTML = "<li class='hint'>暂无记录</li>";
      return;
    }
    list.innerHTML = items
      .map((s) => {
        const name = s.display_name || s.record_id;
        const jsonFile = s.pose_label || s.pose_file || `${s.record_id}.json`;
        return `
      <li class="record-item">
        <div class="record-main">
          <span class="record-tag">名称</span>
          <strong class="record-name">${name}</strong>
          <span class="record-tag">骨架 JSON</span>
          <code class="record-json">${jsonFile}</code>
          <span class="record-meta">${s.backend || "?"}${s.det_backend ? ` · ${s.det_backend}` : ""} · ${s.frame_count ?? "?"} 帧${s.has_video ? ' · <span class="record-badge">有视频</span>' : ""}${s.collision_enabled ? ' · <span class="record-badge">碰撞</span>' : ""}</span>
        </div>
        <span class="record-actions">
          <a href="${s.pose_url}" download title="${jsonFile}">下载</a>
          <button type="button" data-replay="${s.record_id}" data-name="${name}" data-json="${jsonFile}" data-has-video="${s.has_video ? "1" : "0"}">回放</button>
        </span>
      </li>`;
      })
      .join("");
    list.querySelectorAll("[data-replay]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await openRecordReplay(
            btn.dataset.replay,
            btn.dataset.name,
            btn.dataset.json,
            btn.dataset.hasVideo === "1"
          );
        } catch (err) {
          setPlaybackInfo(`❌ ${err.message}`);
        }
      });
    });
  } catch {
    list.innerHTML = "<li class='hint'>无法加载列表</li>";
  }
}

async function loadSavedRecordVideo(recordId) {
  const url = `/api/records/${encodeURIComponent(recordId)}/video`;

  if (playbackVideoObjectUrl) {
    URL.revokeObjectURL(playbackVideoObjectUrl);
    playbackVideoObjectUrl = null;
  }
  videoEl.src = url;
  videoEl.style.display = "block";
  videoEl.load();

  return new Promise((resolve) => {
    const onReady = () => {
      videoEl.removeEventListener("loadedmetadata", onReady);
      videoEl.removeEventListener("error", onErr);
      resolve(true);
    };
    const onErr = () => {
      videoEl.removeEventListener("loadedmetadata", onReady);
      videoEl.removeEventListener("error", onErr);
      resolve(false);
    };
    if (videoEl.readyState >= 1) {
      resolve(true);
      return;
    }
    videoEl.addEventListener("loadedmetadata", onReady);
    videoEl.addEventListener("error", onErr);
  });
}

async function startVideoPlayback(hintPrefix = "") {
  try {
    await videoEl.play();
    cancelAnimationFrame(rafId);
    tick();
    if (hintPrefix) setPlaybackInfo(`${hintPrefix}正在播放…`);
    return true;
  } catch (err) {
    setPlaybackInfo(`${hintPrefix}视频已加载，请点击播放或视频控件（${err.message}）`);
    redrawCurrentFrame();
    return false;
  }
}

async function openRecordReplay(recordId, displayName = "", jsonFileName = "", expectVideo = false) {
  tabs.forEach((b) => b.classList.toggle("active", b.dataset.tab === "playback"));
  panels.collect.classList.remove("active");
  panels.playback.classList.add("active");
  await cleanupPlaybackVideo();
  clearVideoElement();
  const poseRes = await fetch(`/api/records/${encodeURIComponent(recordId)}/pose.json`);
  if (!poseRes.ok) throw new Error("无法加载骨架 JSON");
  poseData = await poseRes.json();
  buildFrameIndex();
  $("#playback-video").value = "";
  const label = displayName || recordId;
  const jsonFile = jsonFileName || `${recordId}.json`;
  const baseHint = `【${label}】${jsonFile}（${poseData.frame_count ?? 0} 帧）`;

  const videoLoaded = await loadSavedRecordVideo(recordId);
  if (videoLoaded) {
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `${baseHint} · 已加载配套视频 ${frameW}×${frameH}。`;
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理 ${f0.w}×${f0.h}，将自动对齐。`;
    }
    setPlaybackInfo(hint);
    redrawCurrentFrame();
    await startVideoPlayback("");
    return;
  }

  if (expectVideo) {
    setPlaybackInfo(`${baseHint} · 未找到已保存视频（可能采集时关闭了保存）。可上传替换或仅播放骨骼。`);
  } else {
    setPlaybackInfo(`${baseHint} · 无配套视频，可上传或仅播放骨骼。`);
  }
  redrawCurrentFrame();
}

// --- 回放 ---
const videoEl = $("#playback-video-el");
const canvas = $("#playback-canvas");
const ctx = canvas.getContext("2d");
const seekBar = $("#seek-bar");
const timeLabel = $("#time-label");

function setPlaybackInfo(text) {
  $("#playback-info").textContent = text;
}

function clearVideoElement() {
  stopPlayback();
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

function syncAnnotationBoxesFromPose() {
  annotationBoxes = Array.isArray(poseData?.annotation?.boxes) ? poseData.annotation.boxes : [];
}

async function loadAnnotationBoxesFromFile(file) {
  const data = JSON.parse(await file.text());
  if (Array.isArray(data?.annotation?.boxes)) {
    annotationBoxes = data.annotation.boxes;
    return;
  }
  if (Array.isArray(data?.boxes)) {
    annotationBoxes = data.boxes;
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
    return;
  }
  annotationBoxes = [];
}

function buildFrameIndex() {
  frameByTime = [];
  syncAnnotationBoxesFromPose();
  if (!poseData?.frames?.length) return;
  poseData.frames.forEach((f) => {
    frameByTime.push({
      t: f.timestamp_sec ?? 0,
      frame: f,
      w: f.infer_width || 640,
      h: f.infer_height || 480,
    });
  });
  frameByTime.sort((a, b) => a.t - b.t);
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
  const wrap = document.querySelector(".stage-wrap");
  const rect = wrap.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, Math.floor(rect.width));
  const cssH = Math.max(1, Math.floor(rect.height));
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { cw: cssW, ch: cssH };
}

function drawAnnotationBoxes(frame, inferW, inferH) {
  if (!annotationBoxes.length) return;
  const layout = getDisplayLayout();
  const collisionSet = new Set(frame?.collisions || []);
  const alarmSet = new Set(frame?.alarm_collisions || []);

  annotationBoxes.forEach((box) => {
    const poly = box.video_polygon;
    if (!Array.isArray(poly) || poly.length < 3) return;
    const token = boxCollisionToken(box);
    const isAlarm = alarmSet.has(token);
    const isHit = collisionSet.has(token);
    ctx.strokeStyle = isAlarm ? "rgba(255, 71, 87, 0.95)" : isHit ? "rgba(255, 209, 102, 0.95)" : "rgba(0, 255, 0, 0.35)";
    ctx.lineWidth = isAlarm || isHit ? 2.5 : 1.5;
    ctx.beginPath();
    poly.forEach((pt, i) => {
      const [x, y] = mapInferToDisplay(pt[0], pt[1], inferW, inferH, layout);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.stroke();
  });
}

function drawSkeleton(frame, inferW, inferH) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  drawAnnotationBoxes(frame, inferW, inferH);
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
  if (videoEl.src && videoEl.readyState >= 1) {
    renderAtTime(videoEl.currentTime);
  } else if (frameByTime.length) {
    drawSkeleton(frameByTime[0].frame, frameByTime[0].w, frameByTime[0].h);
  }
}

function renderAtTime(timeSec) {
  const hit = findFrameAt(timeSec);
  if (!hit) {
    const { cw, ch } = syncCanvasSize();
    ctx.clearRect(0, 0, cw, ch);
    return;
  }
  drawSkeleton(hit.frame, hit.w, hit.h);
  if (hit.frame?.collisions?.length || hit.frame?.alarm_collisions?.length) {
    const c = (hit.frame.collisions || []).join(", ") || "—";
    const a = (hit.frame.alarm_collisions || []).join(", ") || "—";
    timeLabel.title = `碰撞: ${c} | 报警: ${a}`;
  } else {
    timeLabel.title = "";
  }
}

function tick() {
  if (videoEl.readyState >= 2) {
    renderAtTime(videoEl.currentTime);
  }
  if (videoEl.duration && Number.isFinite(videoEl.duration)) {
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
  }
  rafId = requestAnimationFrame(tick);
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

let jsonOnlyTimer = null;

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

function startJsonOnlyPlayback() {
  if (!frameByTime.length) return;
  const fps = poseData.fps || 15;
  let idx = 0;
  clearInterval(jsonOnlyTimer);
  videoEl.style.display = "none";

  jsonOnlyTimer = setInterval(() => {
    if (idx >= frameByTime.length) idx = 0;
    drawSkeleton(frameByTime[idx].frame, frameByTime[idx].w, frameByTime[idx].h);
    seekBar.value = String((idx / frameByTime.length) * 1000);
    timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    idx += 1;
  }, 1000 / fps);
}

$("#playback-json").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await cleanupPlaybackVideo();
  clearVideoElement();
  poseData = JSON.parse(await file.text());
  buildFrameIndex();
  $("#playback-annotation").value = "";
  const f0 = frameByTime[0];
  setPlaybackInfo(
    `已导入 ${file.name}，${poseData.frame_count ?? poseData.frames?.length ?? 0} 帧` +
      (f0 ? `（推理 ${f0.w}×${f0.h}）` : "") +
      "。请上传配套视频后播放。"
  );
  redrawCurrentFrame();
});

$("#playback-annotation").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    await loadAnnotationBoxesFromFile(file);
    setPlaybackInfo(`已导入标注 ${file.name}，${annotationBoxes.length} 个货框`);
    redrawCurrentFrame();
  } catch (err) {
    setPlaybackInfo(`❌ 标注 JSON 无效: ${err.message}`);
  }
});

$("#playback-video").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await cleanupPlaybackVideo();
  stopPlayback();

  playbackVideoObjectUrl = URL.createObjectURL(file);
  videoEl.src = playbackVideoObjectUrl;
  videoEl.style.display = "block";
  videoEl.load();

  const onMeta = () => {
    videoEl.removeEventListener("loadedmetadata", onMeta);
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `视频 ${frameW}×${frameH} 已加载。`;
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理分辨率为 ${f0.w}×${f0.h}，将按视频画面自动对齐。`;
    }
    hint += " 点击播放。";
    setPlaybackInfo(hint);
    redrawCurrentFrame();
  };
  videoEl.addEventListener("loadedmetadata", onMeta);
});

$("#play-btn").addEventListener("click", async () => {
  if (videoEl.src) {
    videoEl.style.display = "block";
    try {
      await videoEl.play();
    } catch (err) {
      setPlaybackInfo(`播放失败: ${err.message}（可点击视频控件或检查格式）`);
      return;
    }
    cancelAnimationFrame(rafId);
    tick();
  } else if (poseData) {
    startJsonOnlyPlayback();
  } else {
    setPlaybackInfo("请先导入 JSON");
  }
});

$("#pause-btn").addEventListener("click", () => {
  stopPlayback();
});

$("#end-playback-btn").addEventListener("click", () => {
  finishPlaybackSession();
});

videoEl.addEventListener("ended", () => {
  stopPlayback();
  if (videoEl.src && videoEl.src.startsWith("blob:")) {
    cleanupPlaybackVideo();
    videoEl.removeAttribute("src");
    videoEl.load();
    setPlaybackInfo("播放结束。可重新选择视频。");
  } else {
    setPlaybackInfo("播放结束。可再次点击播放。");
  }
});

videoEl.addEventListener("loadedmetadata", () => {
  syncCanvasSize();
  redrawCurrentFrame();
});

videoEl.addEventListener("seeked", () => {
  renderAtTime(videoEl.currentTime);
});

window.addEventListener("resize", () => {
  syncCanvasSize();
  redrawCurrentFrame();
});

window.addEventListener("beforeunload", () => {
  cleanupPlaybackVideo();
});

seekBar.addEventListener("input", () => {
  if (!videoEl.duration || !Number.isFinite(videoEl.duration)) {
    const idx = Math.floor((seekBar.value / 1000) * frameByTime.length);
    const item = frameByTime[Math.min(idx, frameByTime.length - 1)];
    if (item) drawSkeleton(item.frame, item.w, item.h);
    return;
  }
  videoEl.currentTime = (seekBar.value / 1000) * videoEl.duration;
  renderAtTime(videoEl.currentTime);
});

loadRecords();
