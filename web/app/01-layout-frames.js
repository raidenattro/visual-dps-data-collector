/** 布局换算、帧分块拉取与缓存 */
/** object-fit: contain 布局（复用 previewLayout.js） */
function computeContainLayout(containerW, containerH, frameW, frameH) {
  const pl = window.previewLayout;
  if (pl?.computeContainLayout) {
    return pl.computeContainLayout(containerW, containerH, frameW, frameH);
  }
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
  const f0 = frameByTime[0];
  return {
    frameW: poseData?.infer_width || f0?.w || 640,
    frameH: poseData?.infer_height || f0?.h || 480,
  };
}

function chunkRangeForFrame(frameIdx) {
  const idx = Math.max(1, Number(frameIdx) || 1);
  const start = Math.floor((idx - 1) / FRAME_CHUNK_SIZE) * FRAME_CHUNK_SIZE + 1;
  return { from: start, to: start + FRAME_CHUNK_SIZE - 1 };
}

function displayLayoutCacheKey() {
  const wrap = stageWrap || document.querySelector(".playback-layout-main .stage-wrap");
  if (!wrap) return "";
  const rect = wrap.getBoundingClientRect();
  const { frameW, frameH } = getVideoFrameSize();
  return `${Math.round(rect.width)}x${Math.round(rect.height)}@${frameW}x${frameH}`;
}

function invalidateDisplayLayoutCache() {
  cachedDisplayLayout = null;
  cachedDisplayLayoutKey = "";
  if (typeof invalidateAnnotationDisplayCache === "function") {
    invalidateAnnotationDisplayCache();
  }
}

function resetFrameFetchState() {
  frameCache.clear();
  loadedChunkKeys.clear();
  prefetchPromises.clear();
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  renderGeneration++;
}

async function prefetchFrameChunk(from, to) {
  if (!currentRecordId || (poseData?.schema || 1) < 2) return;
  const lo = Math.max(1, from);
  const hi = Math.max(lo, to);
  const key = `${lo}-${hi}`;
  if (loadedChunkKeys.has(key)) return;
  if (prefetchPromises.has(key)) return prefetchPromises.get(key);

  const promise = (async () => {
    const res = await fetch(
      `${recordApiUrl(currentRecordId, "/frames")}?from_frame=${lo}&to_frame=${hi}`
    );
    if (!res.ok) return;
    const body = await res.json();
    (body.frames || []).forEach((fr) => {
      if (fr?.frame_idx != null) frameCache.set(fr.frame_idx, fr);
    });
    loadedChunkKeys.add(key);
  })().finally(() => {
    prefetchPromises.delete(key);
  });

  prefetchPromises.set(key, promise);
  return promise;
}

/** 从 from 起并行预取 count 个分块 */
async function prefetchFrameChunksParallel(from, count = 1) {
  const total = Number(poseData?.frame_count) || 0;
  if (!total || !currentRecordId) return;
  const promises = [];
  let start = Math.max(1, from);
  for (let i = 0; i < count && start <= total; i += 1) {
    const to = Math.min(start + FRAME_CHUNK_SIZE - 1, total);
    promises.push(prefetchFrameChunk(start, to));
    start = to + 1;
  }
  await Promise.all(promises);
}

/** 打开记录后预取前几块，减少开播后跨块等待 */
async function prefetchInitialPlaybackChunks() {
  await prefetchFrameChunksParallel(1, FRAME_CHUNK_PREFETCH_INITIAL);
}

function prefetchAheadFromFrame(frameIdx) {
  const idx = Number(frameIdx) || 0;
  if (!idx || !currentRecordId) return;
  const { to } = chunkRangeForFrame(idx);
  const total = Number(poseData?.frame_count) || 0;
  let nextFrom = to + 1;
  for (let i = 0; i < FRAME_CHUNK_PREFETCH_AHEAD && nextFrom <= total; i += 1) {
    const nextTo = Math.min(nextFrom + FRAME_CHUNK_SIZE - 1, total);
    void prefetchFrameChunk(nextFrom, nextTo);
    nextFrom = nextTo + 1;
  }
}

function maybePrefetchByChunkProgress(frameIdx) {
  const idx = Number(frameIdx) || 0;
  if (!idx) return;
  const { from, to } = chunkRangeForFrame(idx);
  const span = Math.max(1, to - from);
  const progress = (idx - from) / span;
  if (progress >= FRAME_CHUNK_PREFETCH_PROGRESS) {
    prefetchAheadFromFrame(idx);
  }
}

function prefetchNextChunkIfNeeded(frameIdx) {
  prefetchAheadFromFrame(frameIdx);
}

async function ensureFrame(frameIdx) {
  if (frameIdx == null) return null;
  if (frameCache.has(frameIdx)) return frameCache.get(frameIdx);
  if ((poseData?.schema || 1) >= 2 && currentRecordId) {
    const { from, to } = chunkRangeForFrame(frameIdx);
    await prefetchFrameChunk(from, to);
    return frameCache.get(frameIdx) || null;
  }
  return null;
}

async function ensureFrameChunkLoaded(frameIdx) {
  if (frameIdx == null) return;
  maybePrefetchByChunkProgress(frameIdx);
  if (frameCache.has(frameIdx)) {
    prefetchAheadFromFrame(frameIdx);
    return;
  }
  const { from, to } = chunkRangeForFrame(frameIdx);
  await prefetchFrameChunk(from, to);
  prefetchAheadFromFrame(frameIdx);
}

function getDisplayLayout() {
  const key = displayLayoutCacheKey();
  if (cachedDisplayLayout && key === cachedDisplayLayoutKey) {
    return cachedDisplayLayout;
  }
  const wrap = stageWrap || document.querySelector(".playback-layout-main .stage-wrap");
  if (!wrap) return computeContainLayout(640, 480, 640, 480);
  const rect = wrap.getBoundingClientRect();
  const { frameW, frameH } = getVideoFrameSize();
  const layout = computeContainLayout(rect.width, rect.height, frameW, frameH);
  cachedDisplayLayout = layout;
  cachedDisplayLayoutKey = key;
  return layout;
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

// --- 回放页：列表选中一条记录后加载 ---
