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
  if (canvas) {
    canvas._layoutCssW = 0;
    canvas._layoutCssH = 0;
  }
  if (typeof invalidateAnnotationDisplayCache === "function") {
    invalidateAnnotationDisplayCache();
  }
}

function resetFrameFetchState() {
  if (frameFetchController) frameFetchController.abort();
  frameFetchController = new AbortController();
  frameFetchGeneration++;
  frameCache.clear();
  if (typeof resetStablePersonIdentityCache === "function") {
    resetStablePersonIdentityCache();
  }
  loadedChunkKeys.clear();
  prefetchPromises.clear();
  loadedFrameChunks.clear();
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  playbackSkeletonReady = false;
  renderGeneration++;
  if (typeof playbackDebugLog === "function") {
    playbackDebugLog("frame-cache-reset", {
      generation: frameFetchGeneration,
      recordId: currentRecordId,
    });
  }
  if (typeof invalidateVerifiedSegmentsCache === "function") {
    invalidateVerifiedSegmentsCache();
  }
}

function touchFrameChunk(key) {
  const entries = loadedFrameChunks.get(key);
  if (!entries) return;
  loadedFrameChunks.delete(key);
  loadedFrameChunks.set(key, entries);
}

function touchFrameChunkForFrame(frameIdx) {
  const { from, to } = chunkRangeForFrame(frameIdx);
  const total = Number(poseData?.frame_count) || to;
  touchFrameChunk(`${from}-${Math.min(total, to)}`);
}

function evictOldFrameChunks(protectedKey = "") {
  if (frameCacheEvictionSuspended) return;
  while (loadedFrameChunks.size > FRAME_CHUNK_CACHE_MAX) {
    const oldestKey = loadedFrameChunks.keys().next().value;
    if (!oldestKey) return;
    if (oldestKey === protectedKey) {
      touchFrameChunk(oldestKey);
      continue;
    }
    const entries = loadedFrameChunks.get(oldestKey);
    loadedFrameChunks.delete(oldestKey);
    loadedChunkKeys.delete(oldestKey);
    for (const [frameIdx, frame] of entries || []) {
      if (frameCache.get(frameIdx) === frame) frameCache.delete(frameIdx);
    }
    if (typeof playbackDebugLog === "function") {
      playbackDebugLog("frame-chunk-evicted", {
        recordId: currentRecordId,
        chunk: oldestKey,
        cachedChunks: loadedFrameChunks.size,
      });
    }
  }
}

/** 缓存中不大于 targetIdx 的最近帧（缺失时临时显示，避免骨架冻结） */
function findNearestCachedFrameEntry(targetIdx) {
  const target = Math.max(1, Number(targetIdx) || 0);
  if (!target || !frameByTime.length) return null;
  if (frameCache.has(target)) {
    return frameByTimeEntryOf(target);
  }
  let lo = 0;
  let hi = frameByTime.length - 1;
  let bestPos = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const fi = Number(frameByTime[mid].frameIdx) || 0;
    if (fi <= target) {
      bestPos = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  for (let i = bestPos; i >= 0; i -= 1) {
    const entry = frameByTime[i];
    if (frameCache.has(entry.frameIdx)) return entry;
  }
  for (let i = bestPos + 1; i < frameByTime.length; i += 1) {
    const entry = frameByTime[i];
    if (frameCache.has(entry.frameIdx)) return entry;
  }
  return null;
}

/** 按播放位置前瞻预取（tick 内调用，不阻塞绘制） */
function prefetchLookaheadFromFrame(frameIdx) {
  const idx = Math.max(1, Number(frameIdx) || 0);
  if (!idx || !currentRecordId || playbackSkeletonReady) return;
  const fps = Number(poseData?.fps) || 25;
  const rate = Number.isFinite(playbackSpeed) && playbackSpeed > 0 ? playbackSpeed : 1;
  const lookaheadFrames = Math.ceil(fps * FRAME_CHUNK_PREFETCH_LOOKAHEAD_SEC * rate);
  prefetchAheadFromFrame(idx);
  prefetchAheadFromFrame(Math.min(idx + lookaheadFrames, Number(poseData?.frame_count) || idx));
}

async function prefetchFrameChunk(from, to) {
  if (!currentRecordId || (poseData?.schema || 1) < 2) return;
  const lo = Math.max(1, from);
  const total = Number(poseData?.frame_count) || Math.max(lo, to);
  const hi = Math.min(total, Math.max(lo, to));
  const key = `${lo}-${hi}`;
  if (loadedChunkKeys.has(key)) {
    touchFrameChunk(key);
    return;
  }
  if (prefetchPromises.has(key)) return prefetchPromises.get(key);

  const recordId = currentRecordId;
  const generation = frameFetchGeneration;
  const signal = frameFetchController?.signal;
  const promise = (async () => {
    let res;
    try {
      res = await fetch(`${recordApiUrl(recordId, "/frames")}?from_frame=${lo}&to_frame=${hi}`, {
        signal,
      });
    } catch (err) {
      if (err?.name === "AbortError") return;
      console.warn("[playback] 骨骼分块请求失败", { recordId, chunk: key, error: err });
      throw err;
    }
    if (!res.ok) {
      console.warn("[playback] 骨骼分块响应异常", {
        recordId,
        chunk: key,
        status: res.status,
      });
      return;
    }
    const body = await res.json();
    if (
      signal?.aborted ||
      generation !== frameFetchGeneration ||
      recordId !== currentRecordId
    ) {
      return;
    }
    const chunkEntries = new Map();
    (body.frames || []).forEach((fr) => {
      const fi = Number(fr?.frame_idx);
      const sfi = Number(fr?.source_frame_idx);
      if (fi > 0) {
        frameCache.set(fi, fr);
        chunkEntries.set(fi, fr);
      }
      // 回放索引用 source_frame_idx，缓存双键避免取不到帧
      if (sfi > 0) {
        frameCache.set(sfi, fr);
        chunkEntries.set(sfi, fr);
      }
    });
    if (typeof markStablePersonIdentityDirtyFrom === "function") {
      markStablePersonIdentityDirtyFrom(lo);
    } else if (typeof resetStablePersonIdentityCache === "function") {
      resetStablePersonIdentityCache();
    }
    loadedChunkKeys.add(key);
    loadedFrameChunks.set(key, chunkEntries);
    evictOldFrameChunks(key);
    if (typeof playbackDebugLog === "function") {
      playbackDebugLog("frame-chunk-loaded", {
        recordId,
        chunk: key,
        rows: Array.isArray(body.frames) ? body.frames.length : 0,
        cacheHeader: res.headers?.get?.("X-Playback-Cache") || "",
        serverTiming: res.headers?.get?.("Server-Timing") || "",
        cachedChunks: loadedFrameChunks.size,
      });
    }
  })().finally(() => {
    if (prefetchPromises.get(key) === promise) prefetchPromises.delete(key);
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
  if (frameCache.has(frameIdx)) {
    touchFrameChunkForFrame(frameIdx);
    return frameCache.get(frameIdx);
  }
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
    touchFrameChunkForFrame(frameIdx);
    prefetchAheadFromFrame(frameIdx);
    return;
  }
  const { from, to } = chunkRangeForFrame(frameIdx);
  await prefetchFrameChunk(from, to);
  prefetchAheadFromFrame(frameIdx);
}

function getDisplayLayout() {
  if (frozenPlaybackLayout && typeof playbackRenderLoopActive !== "undefined" && playbackRenderLoopActive) {
    return frozenPlaybackLayout;
  }
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
