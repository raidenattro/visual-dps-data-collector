/** 区间增强标真：首帧选 person_id + 货框，首尾均包含并逐帧原子保存 */

/** 仅身份变化点/低置信度点需要人工确认；稳定的多人区间自动跟踪 */
const rangeAnnotManualPersonByFrame = new Map();
let rangeAnnotAwaitingPersonFrame = null;
let rangeAnnotSuggestedPersonId = null;
let rangeAnnotReviewResumeTimer = null;

/** 所有视频共用的稳定人物身份缓存：人物 A/B 与当帧 raw P0/P1 分离。 */
const stablePersonIdentityByFrame = new Map();
let stablePersonIdentityComputedThrough = 0;
let stablePersonIdentityNextId = 0;
const stablePersonIdentityActiveTracks = new Map();
const stablePersonIdentityTrackHints = new Map();
const STABLE_PERSON_MAX_GAP = 25;

function resetStablePersonIdentityCache() {
  stablePersonIdentityByFrame.clear();
  stablePersonIdentityComputedThrough = 0;
  stablePersonIdentityNextId = 0;
  stablePersonIdentityActiveTracks.clear();
  stablePersonIdentityTrackHints.clear();
}

/**
 * 后台分块可能晚于当前画面返回。只有新数据覆盖了已经计算过的帧时，
 * 才需要从头重建稳定人物编号；加载更靠后的分块不能让当前 A/B 突然跳变。
 */
function markStablePersonIdentityDirtyFrom(frameIdx) {
  const fi = Math.max(1, parseInt(frameIdx, 10) || 0);
  if (fi <= stablePersonIdentityComputedThrough) {
    resetStablePersonIdentityCache();
  }
}

function clearRangeAnnotBounds() {
  rangeAnnotStartFrame = null;
  rangeAnnotEndFrame = null;
  rangeAnnotTemplateSnapshot = null;
  rangeAnnotManualPersonByFrame.clear();
  rangeAnnotAwaitingPersonFrame = null;
  rangeAnnotSuggestedPersonId = null;
  if (rangeAnnotReviewResumeTimer) {
    clearTimeout(rangeAnnotReviewResumeTimer);
    rangeAnnotReviewResumeTimer = null;
  }
  updateRangeAnnotUi();
}

/**
 * 在首帧选取 person_id / 货框后立即缓存。
 *
 * 快照离开首帧后必须冻结：切到尾帧时，上一事件的 pending 选择会被正常清理，
 * 此时若再次从首帧事件重建快照，就会把已缓存的货框覆盖为空。
 */
function refreshRangeAnnotTemplateSnapshot({ force = false } = {}) {
  if (rangeAnnotStartFrame == null || rangeAnnotStartFrame <= 0) {
    rangeAnnotTemplateSnapshot = null;
    return;
  }
  const fi = rangeAnnotStartFrame;
  if (
    !force &&
    rangeAnnotTemplateSnapshot &&
    Number(rangeAnnotTemplateSnapshot.frameIdx ?? fi) === fi
  ) {
    return;
  }
  const currentFrameIdx =
    typeof getResolvedPlaybackFrameIdx === "function"
      ? parseInt(getResolvedPlaybackFrameIdx(), 10) || 0
      : 0;
  if (!force && currentFrameIdx !== fi) {
    return;
  }
  const template = getRangeAnnotTemplate(fi);
  if (!template?.ev) {
    if (force || !rangeAnnotTemplateSnapshot) {
      rangeAnnotTemplateSnapshot = null;
    }
    return;
  }
  let confirmed = normalizeBoxTokenList(template.confirmed);
  if (!confirmed.length) {
    confirmed = normalizeBoxTokenList(getEventConfirmedBoxes(template.ev));
  }
  let personId = template.personId ?? getEventPersonId(template.ev);
  const personIds = getFramePersonIds(fi);
  if (personId == null && personIds.length === 1) {
    personId = personIds[0];
  }
  rangeAnnotTemplateSnapshot = {
    frameIdx: fi,
    confirmed,
    personId: personId != null ? Number(personId) : null,
  };
}

function rangeAnnotEventIsStartFrame(ev) {
  const frameIdx = parseInt(ev?.frame_idx, 10) || 0;
  return (
    frameIdx > 0 &&
    rangeAnnotStartFrame != null &&
    frameIdx === Number(rangeAnnotStartFrame)
  );
}

/** 首帧货框的显式点选/清空可以更新冻结快照，其他帧绝不能覆盖它。 */
function updateRangeAnnotTemplateBoxesFromEvent(ev, tokens) {
  if (!rangeAnnotEventIsStartFrame(ev)) return;
  if (!rangeAnnotTemplateSnapshot) {
    refreshRangeAnnotTemplateSnapshot({ force: true });
  }
  const existing = rangeAnnotTemplateSnapshot || {
    frameIdx: Number(rangeAnnotStartFrame),
    confirmed: [],
    personId: null,
  };
  rangeAnnotTemplateSnapshot = {
    ...existing,
    frameIdx: Number(rangeAnnotStartFrame),
    confirmed: normalizeBoxTokenList(tokens),
  };
}

/** 首帧 person_id 的显式点选/清空只更新人员字段，保留已冻结货框。 */
function updateRangeAnnotTemplatePersonFromEvent(ev, personId) {
  if (!rangeAnnotEventIsStartFrame(ev)) return;
  if (!rangeAnnotTemplateSnapshot) {
    refreshRangeAnnotTemplateSnapshot({ force: true });
  }
  const existing = rangeAnnotTemplateSnapshot || {
    frameIdx: Number(rangeAnnotStartFrame),
    confirmed: [],
    personId: null,
  };
  const normalized =
    personId == null || personId === "" || !Number.isFinite(Number(personId))
      ? null
      : Number(personId);
  rangeAnnotTemplateSnapshot = {
    ...existing,
    frameIdx: Number(rangeAnnotStartFrame),
    personId: normalized,
  };
}

function getRangeAnnotTemplateForApply(startFrame) {
  const fi = parseInt(startFrame, 10) || 0;
  if (
    rangeAnnotTemplateSnapshot &&
    rangeAnnotStartFrame === fi &&
    Number(rangeAnnotTemplateSnapshot.frameIdx ?? fi) === fi
  ) {
    const frameEvents = getEventsOnFrame(fi);
    return {
      ev: frameEvents[0] || null,
      confirmed: [...rangeAnnotTemplateSnapshot.confirmed],
      personId: rangeAnnotTemplateSnapshot.personId,
    };
  }
  return getRangeAnnotTemplate(fi);
}

function normalizeRangeAnnotBounds(start, end) {
  const a = parseInt(start, 10) || 0;
  const b = parseInt(end, 10) || 0;
  if (a <= 0 || b <= 0) return null;
  return { start: Math.min(a, b), end: Math.max(a, b) };
}

function getPersonTrackIdAtFrame(frameIdx, personId) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return null;
  const frame = frameCache.get(fi);
  if (!frame?.persons?.length) return null;
  const target = Number(personId);
  for (let idx = 0; idx < frame.persons.length; idx++) {
    const p = frame.persons[idx];
    const pid = p.person_id != null ? Number(p.person_id) : idx;
    if (pid !== target) continue;
    const tid = p.person_track_id;
    if (tid != null && String(tid).trim()) return String(tid).trim();
    return null;
  }
  return null;
}

function eventInRangeFrame(ev, start, end) {
  const fi = parseInt(ev?.frame_idx, 10) || 0;
  return fi >= start && fi <= end;
}

function collectRangeAnnotFrames(start, end) {
  const byFrame = new Map();
  (playbackEvents || []).forEach((ev) => {
    const fi = parseInt(ev?.frame_idx, 10) || 0;
    if (fi >= start && fi <= end && !byFrame.has(fi)) byFrame.set(fi, ev);
  });
  const events = [];
  const missingFrames = [];
  for (let fi = start; fi <= end; fi += 1) {
    const ev = byFrame.get(fi);
    if (ev) events.push(ev);
    else missingFrames.push(fi);
  }
  return { events, missingFrames };
}

function normalizeRangePersonBbox(person) {
  const raw = person?.bbox;
  if (!Array.isArray(raw) || raw.length < 4) return null;
  const values = raw.slice(0, 4).map((value) => Number(value));
  if (values.some((value) => !Number.isFinite(value))) return null;
  const [x1, y1, x2, y2] = values;
  if (x2 <= x1 || y2 <= y1) return null;
  return values;
}

function rangePersonCandidates(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  const persons = frameCache?.get(fi)?.persons || [];
  return persons.map((person, idx) => {
    const rawPid = person?.person_id != null ? Number(person.person_id) : idx;
    const personId = Number.isFinite(rawPid) ? rawPid : idx;
    const rawTrack = person?.person_track_id;
    return {
      personIndex: idx,
      personId,
      trackId: rawTrack != null && String(rawTrack).trim() ? String(rawTrack).trim() : null,
      bbox: normalizeRangePersonBbox(person),
    };
  });
}

function rangeBboxIou(a, b) {
  if (!a || !b) return 0;
  const ix1 = Math.max(a[0], b[0]);
  const iy1 = Math.max(a[1], b[1]);
  const ix2 = Math.min(a[2], b[2]);
  const iy2 = Math.min(a[3], b[3]);
  const intersection = Math.max(0, ix2 - ix1) * Math.max(0, iy2 - iy1);
  const areaA = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const areaB = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  const union = areaA + areaB - intersection;
  return union > 0 ? intersection / union : 0;
}

function rangeBboxContinuityScore(previous, candidate) {
  const iou = rangeBboxIou(previous?.bbox, candidate?.bbox);
  if (!previous?.bbox || !candidate?.bbox) {
    return {
      score: previous?.trackId && previous.trackId === candidate?.trackId ? 0.1 : 0,
      iou,
    };
  }
  const prevCx = (previous.bbox[0] + previous.bbox[2]) / 2;
  const prevCy = (previous.bbox[1] + previous.bbox[3]) / 2;
  const nextCx = (candidate.bbox[0] + candidate.bbox[2]) / 2;
  const nextCy = (candidate.bbox[1] + candidate.bbox[3]) / 2;
  const diagonal = Math.max(
    1,
    Math.hypot(previous.bbox[2] - previous.bbox[0], previous.bbox[3] - previous.bbox[1])
  );
  const distance = Math.hypot(nextCx - prevCx, nextCy - prevCy) / diagonal;
  const proximity = Math.max(0, 1 - distance);
  const areaA =
    (previous.bbox[2] - previous.bbox[0]) * (previous.bbox[3] - previous.bbox[1]);
  const areaB =
    (candidate.bbox[2] - candidate.bbox[0]) * (candidate.bbox[3] - candidate.bbox[1]);
  const areaSimilarity = Math.min(areaA, areaB) / Math.max(areaA, areaB, 1);
  const trackBonus =
    previous.trackId != null && previous.trackId === candidate.trackId ? 0.02 : 0;
  return {
    score: iou * 0.72 + proximity * 0.2 + areaSimilarity * 0.08 + trackBonus,
    iou,
  };
}

function rankRangePersonCandidates(previous, candidates) {
  if (!previous || !candidates.length) return [];
  return candidates
    .map((candidate) => ({
      candidate,
      ...rangeBboxContinuityScore(previous, candidate),
    }))
    .sort((a, b) => b.score - a.score);
}

function stablePersonLabel(stableId) {
  let value = Math.max(0, parseInt(stableId, 10) || 0);
  let label = "";
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
}

/**
 * 右侧人物按钮必须保持固定操作位置：人物 A 永远排在人物 B 前面。
 * raw P0/P1 只决定当帧保存值，不能决定按钮顺序。
 */
function sortStablePersonDisplayOptions(options) {
  return [...(Array.isArray(options) ? options : [])].sort((a, b) => {
    const aStable = Number(a?.stableId);
    const bStable = Number(b?.stableId);
    const aRank = Number.isFinite(aStable) ? aStable : Number.MAX_SAFE_INTEGER;
    const bRank = Number.isFinite(bStable) ? bStable : Number.MAX_SAFE_INTEGER;
    if (aRank !== bRank) return aRank - bRank;
    const labelOrder = String(a?.stableLabel ?? "").localeCompare(
      String(b?.stableLabel ?? "")
    );
    if (labelOrder) return labelOrder;
    return Number(a?.pid ?? 0) - Number(b?.pid ?? 0);
  });
}

function rangePersonCenterX(candidate) {
  const bbox = candidate?.bbox;
  return bbox ? (bbox[0] + bbox[2]) / 2 : Number.POSITIVE_INFINITY;
}

function rangePersonCenter(candidate) {
  const bbox = candidate?.bbox;
  if (!bbox) return null;
  return {
    x: (bbox[0] + bbox[2]) / 2,
    y: (bbox[1] + bbox[3]) / 2,
  };
}

/**
 * 稳定人物匹配使用“上一位置 + 运动趋势”的预测，避免两个人交叉时仅凭
 * 当前 IoU 把 A/B 互换。track 只提供很小的同轨加分，不主导身份。
 */
function scoreStablePersonTrack(track, candidate, frameIdx) {
  const continuity = rangeBboxContinuityScore(track?.candidate, candidate);
  const previousCenter = rangePersonCenter(track?.candidate);
  const nextCenter = rangePersonCenter(candidate);
  const dt = Math.max(1, Number(frameIdx) - Number(track?.frameIdx || frameIdx - 1));
  let prediction = 0;
  if (previousCenter && nextCenter) {
    const predictedX = previousCenter.x + Number(track?.velocityX || 0) * dt;
    const predictedY = previousCenter.y + Number(track?.velocityY || 0) * dt;
    const bbox = track.candidate?.bbox;
    const diagonal = bbox
      ? Math.max(40, Math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]))
      : 120;
    const normalizedDistance =
      Math.hypot(nextCenter.x - predictedX, nextCenter.y - predictedY) /
      (diagonal * Math.max(1, Math.sqrt(dt)));
    prediction = Math.max(0, 1 - normalizedDistance);
  }
  const sameTrack =
    track?.trackId != null &&
    candidate?.trackId != null &&
    track.trackId === candidate.trackId;
  return {
    score: continuity.score * 0.3 + prediction * 0.65 + (sameTrack ? 0.05 : 0),
    prediction,
    iou: continuity.iou,
    sameTrack,
  };
}

/**
 * 为已加载帧建立稳定的“人物 A/B/…”身份。
 * 相邻帧空间连续性与运动趋势为主，track 仅作弱提示；raw person_id
 * 只作为保存值，不参与稳定人物编号。身份编号不复用，因此人员交叉、
 * 短时遮挡或离场后重新出现时不会因为左右位置变化直接交换 A/B。
 */
function buildStablePersonIdentityThrough(frameIdx) {
  const target = Math.max(1, parseInt(frameIdx, 10) || 0);
  if (!target || stablePersonIdentityByFrame.has(target)) return;
  if (target < stablePersonIdentityComputedThrough) {
    resetStablePersonIdentityCache();
  }

  for (
    let fi = stablePersonIdentityComputedThrough + 1;
    fi <= target;
    fi += 1
  ) {
    const candidates = rangePersonCandidates(fi);
    const stableByPersonIndex = new Map();
    const usedStableIds = new Set();
    const hasLoadedFrame = frameCache?.has(fi);

    if (!hasLoadedFrame) {
      stablePersonIdentityByFrame.set(fi, []);
      stablePersonIdentityComputedThrough = fi;
      continue;
    }

    const activeTracks = [...stablePersonIdentityActiveTracks.values()].filter(
      (track) => fi - Number(track.frameIdx || 0) <= STABLE_PERSON_MAX_GAP
    );
    if (activeTracks.length && candidates.length) {
      const pairs = [];
      activeTracks.forEach((track) => {
        candidates.forEach((candidate) => {
          const match = scoreStablePersonTrack(track, candidate, fi);
          pairs.push({
            stableId: track.stableId,
            candidate,
            ...match,
          });
        });
      });
      pairs.sort((a, b) => b.score - a.score);
      const usedPersonIndices = new Set();
      pairs.forEach((pair) => {
        if (
          usedStableIds.has(pair.stableId) ||
          usedPersonIndices.has(pair.candidate.personIndex)
        ) {
          return;
        }
        if (
          pair.score < 0.2 &&
          pair.prediction < 0.25 &&
          pair.iou < 0.03 &&
          !pair.sameTrack
        ) {
          return;
        }
        stableByPersonIndex.set(pair.candidate.personIndex, pair.stableId);
        usedStableIds.add(pair.stableId);
        usedPersonIndices.add(pair.candidate.personIndex);
      });
    }

    // 断帧或短暂消失后，track 只用于找回尚未占用的稳定编号。
    candidates.forEach((candidate) => {
      if (stableByPersonIndex.has(candidate.personIndex)) return;
      const hinted =
        candidate.trackId != null
          ? stablePersonIdentityTrackHints.get(candidate.trackId)
          : null;
      if (hinted == null || usedStableIds.has(hinted)) return;
      stableByPersonIndex.set(candidate.personIndex, hinted);
      usedStableIds.add(hinted);
    });

    [...candidates]
      .sort(
        (a, b) =>
          rangePersonCenterX(a) - rangePersonCenterX(b) ||
          a.personIndex - b.personIndex
      )
      .forEach((candidate) => {
        if (stableByPersonIndex.has(candidate.personIndex)) return;
        while (usedStableIds.has(stablePersonIdentityNextId)) {
          stablePersonIdentityNextId += 1;
        }
        stableByPersonIndex.set(
          candidate.personIndex,
          stablePersonIdentityNextId
        );
        usedStableIds.add(stablePersonIdentityNextId);
        stablePersonIdentityNextId += 1;
      });

    const rows = candidates.map((candidate) => {
      const stableId = stableByPersonIndex.get(candidate.personIndex);
      const previousTrack = stablePersonIdentityActiveTracks.get(stableId);
      const previousCenter = rangePersonCenter(previousTrack?.candidate);
      const nextCenter = rangePersonCenter(candidate);
      const dt = Math.max(
        1,
        fi - Number(previousTrack?.frameIdx || fi - 1)
      );
      const measuredVelocityX =
        previousCenter && nextCenter ? (nextCenter.x - previousCenter.x) / dt : 0;
      const measuredVelocityY =
        previousCenter && nextCenter ? (nextCenter.y - previousCenter.y) / dt : 0;
      const hasMeasuredVelocity = Boolean(previousCenter && nextCenter);
      const previousVelocitySamples = Number(
        previousTrack?.velocitySamples || 0
      );
      return {
        frameIdx: fi,
        personIndex: candidate.personIndex,
        personId: candidate.personId,
        trackId: candidate.trackId,
        stableId,
        stableLabel: stablePersonLabel(stableId),
        candidate,
        velocityX:
          previousTrack && previousVelocitySamples > 0
            ? Number(previousTrack.velocityX || 0) * 0.35 +
              measuredVelocityX * 0.65
            : measuredVelocityX,
        velocityY:
          previousTrack && previousVelocitySamples > 0
            ? Number(previousTrack.velocityY || 0) * 0.35 +
              measuredVelocityY * 0.65
            : measuredVelocityY,
        velocitySamples: hasMeasuredVelocity
          ? previousVelocitySamples + 1
          : previousVelocitySamples,
      };
    });
    stablePersonIdentityByFrame.set(fi, rows);
    [...stablePersonIdentityActiveTracks.entries()].forEach(
      ([stableId, track]) => {
        if (fi - Number(track.frameIdx || 0) > STABLE_PERSON_MAX_GAP) {
          stablePersonIdentityActiveTracks.delete(stableId);
        }
      }
    );
    rows.forEach((row) => {
      stablePersonIdentityActiveTracks.set(row.stableId, row);
      if (
        row.trackId != null &&
        !stablePersonIdentityTrackHints.has(row.trackId)
      ) {
        stablePersonIdentityTrackHints.set(row.trackId, row.stableId);
      }
    });
    stablePersonIdentityComputedThrough = fi;
  }
}

function getStablePersonDisplayInfo(frameIdx, person, personIndex = 0) {
  const fi = Math.max(1, parseInt(frameIdx, 10) || 0);
  if (!fi) return null;
  buildStablePersonIdentityThrough(fi);
  const rawPid =
    person?.person_id != null && Number.isFinite(Number(person.person_id))
      ? Number(person.person_id)
      : Number(personIndex);
  const trackId =
    person?.person_track_id != null && String(person.person_track_id).trim()
      ? String(person.person_track_id).trim()
      : null;
  const rows = stablePersonIdentityByFrame.get(fi) || [];
  const row =
    rows.find((item) => item.personIndex === Number(personIndex)) ||
    rows.find(
      (item) =>
        item.personId === rawPid &&
        (trackId == null || item.trackId === trackId)
    ) ||
    null;
  if (row) return row;
  return {
    frameIdx: fi,
    personIndex: Number(personIndex),
    personId: rawPid,
    trackId,
    stableId: Number(personIndex),
    stableLabel: stablePersonLabel(personIndex),
  };
}

function getStablePersonDisplayInfoByRawId(frameIdx, personId) {
  const fi = Math.max(1, parseInt(frameIdx, 10) || 0);
  const frame = frameCache?.get(fi);
  const persons = frame?.persons || [];
  const rawPid = Number(personId);
  const personIndex = persons.findIndex((person, idx) => {
    const pid =
      person?.person_id != null && Number.isFinite(Number(person.person_id))
        ? Number(person.person_id)
        : idx;
    return pid === rawPid;
  });
  if (personIndex < 0) return null;
  return getStablePersonDisplayInfo(fi, persons[personIndex], personIndex);
}

function getRawPersonIdForStablePerson(frameIdx, stableId) {
  const fi = Math.max(1, parseInt(frameIdx, 10) || 0);
  buildStablePersonIdentityThrough(fi);
  const row = (stablePersonIdentityByFrame.get(fi) || []).find(
    (item) => Number(item.stableId) === Number(stableId)
  );
  return row?.personId ?? null;
}

/**
 * 空间连续性建议：IoU 为主，中心距离与面积相似度为辅，track 只有 0.02 弱加分。
 * 返回 null 表示空间证据不足，仍可交给人工选择，但不会自动写入任何 person_id。
 */
function suggestRangePersonByContinuity(previous, candidates) {
  const ranked = rankRangePersonCandidates(previous, candidates);
  const best = ranked[0];
  if (!best) return null;
  const second = ranked[1];
  const margin = second ? best.score - second.score : best.score;
  const iouSeparation = second ? best.iou - second.iou : best.iou;
  if (best.iou < 0.2) return null;
  if (second && margin < 0.12 && iouSeparation < 0.15) return null;
  return best.candidate;
}

/**
 * Resolve the same physical person by adjacent-frame bbox continuity.
 * - 多人但目标空间轨迹稳定时自动通过；
 * - raw person_id 重排自动映射，不视为物理身份变化；
 * - 仅遮挡后重现或空间证据不足时要求人工确认；
 * - 人工选择成为该帧锚点，后续继续自动跟踪；
 * - track 只作 0.02 的弱参考。
 */
function resolveRangePersonAssignments(
  start,
  end,
  selectedPersonId,
  manualPersonByFrame = rangeAnnotManualPersonByFrame
) {
  const assignments = [];
  const ambiguousFrames = [];
  const confirmationFrames = [];
  const confirmationSuggestions = [];
  const missingPersonFrames = [];
  let previous = null;
  let previousAssignment = null;
  let needsReacquire = false;

  for (let fi = start; fi <= end; fi += 1) {
    const candidates = rangePersonCandidates(fi);
    if (!candidates.length) {
      assignments.push({ frameIdx: fi, personId: null, trackId: null });
      missingPersonFrames.push(fi);
      previous = null;
      needsReacquire = true;
      continue;
    }

    let chosen = null;
    let reason = "";
    let continuity = null;
    const confirmedPersonId =
      fi === start ? selectedPersonId : manualPersonByFrame?.get(fi);
    if (confirmedPersonId != null) {
      chosen = candidates.find(
        (candidate) => Number(candidate.personId) === Number(confirmedPersonId)
      );
      if (!chosen) {
        manualPersonByFrame?.delete(fi);
        confirmationFrames.push(fi);
        confirmationSuggestions.push({
          frameIdx: fi,
          personId: null,
          reason: "invalid_anchor",
        });
        break;
      }
    } else if (previous) {
      const ranked = rankRangePersonCandidates(previous, candidates);
      const best = ranked[0];
      const second = ranked[1];
      chosen = best?.candidate || null;
      continuity = best || null;
      if (chosen && candidates.length >= 2) {
        const margin = second ? best.score - second.score : best.score;
        const iouSeparation = second ? best.iou - second.iou : best.iou;
        if (
          best.iou < 0.2 ||
          (second && margin < 0.06 && iouSeparation < 0.08)
        ) {
          reason = "low_confidence";
        }
      }
    } else if (candidates.length === 1) {
      chosen = candidates[0];
    } else {
      chosen =
        candidates.find(
          (candidate) =>
            previousAssignment != null &&
            Number(candidate.personId) === Number(previousAssignment.personId)
        ) || candidates[0];
      reason = needsReacquire ? "reacquire" : "low_confidence";
    }

    if (!chosen) {
      ambiguousFrames.push(fi);
      break;
    }
    if (
      needsReacquire &&
      fi !== start &&
      !manualPersonByFrame?.has(fi) &&
      !reason
    ) {
      reason = "reacquire";
    }
    assignments.push({
      frameIdx: fi,
      personId: chosen.personId,
      trackId: chosen.trackId,
    });

    const manuallyConfirmed =
      fi !== start && manualPersonByFrame?.has(fi);
    if (reason && !manuallyConfirmed) {
      confirmationFrames.push(fi);
      confirmationSuggestions.push({
        frameIdx: fi,
        personId: chosen.personId,
        previousPersonId: previousAssignment?.personId ?? null,
        reason,
        score: continuity?.score ?? null,
        iou: continuity?.iou ?? null,
      });
      break;
    }

    previous = chosen;
    previousAssignment = {
      frameIdx: fi,
      personId: chosen.personId,
      trackId: chosen.trackId,
    };
    needsReacquire = false;
  }
  return {
    assignments,
    ambiguousFrames,
    confirmationFrames,
    confirmationSuggestions,
    missingPersonFrames,
  };
}

function summarizeRangePersonAssignments(assignments, maxGroups = 8) {
  const rows = Array.isArray(assignments) ? assignments : [];
  if (!rows.length) return "";
  const groups = [];
  for (const row of rows) {
    const frameIdx = parseInt(row?.frameIdx, 10) || 0;
    if (frameIdx <= 0) continue;
    const personLabel =
      row?.personId == null || !Number.isFinite(Number(row.personId))
        ? "无人"
        : `P${Number(row.personId)}`;
    const previous = groups[groups.length - 1];
    if (
      previous &&
      previous.personLabel === personLabel &&
      previous.end + 1 === frameIdx
    ) {
      previous.end = frameIdx;
    } else {
      groups.push({ start: frameIdx, end: frameIdx, personLabel });
    }
  }
  const visible = groups.slice(0, Math.max(1, Number(maxGroups) || 8));
  const text = visible
    .map((group) =>
      group.start === group.end
        ? `${group.start}:${group.personLabel}`
        : `${group.start}–${group.end}:${group.personLabel}`
    )
    .join("，");
  return groups.length > visible.length
    ? `${text}，另 ${groups.length - visible.length} 段`
    : text;
}

function isRangePersonConfirmationRequired(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  return fi > 0 && fi === rangeAnnotAwaitingPersonFrame;
}

function getRangePersonConfirmationSuggestion(frameIdx) {
  return isRangePersonConfirmationRequired(frameIdx)
    ? rangeAnnotSuggestedPersonId
    : null;
}

/**
 * 由人员单选框/骨架点击回调：
 * - 首帧/尾帧选择作为区间锚点；
 * - 当前变化点选择作为人工确认，并自动继续检查下一变化点。
 */
function confirmRangePersonSelection(ev, personId) {
  const fi = parseInt(ev?.frame_idx, 10) || 0;
  const normalized = Number(personId);
  const candidates = rangePersonCandidates(fi);
  const confirmsRangeEndpoint =
    fi > 0 &&
    typeof rangeAnnotStartFrame !== "undefined" &&
    typeof rangeAnnotEndFrame !== "undefined" &&
    (fi === Number(rangeAnnotStartFrame) ||
      fi === Number(rangeAnnotEndFrame));
  const wasAwaitingConfirmation = isRangePersonConfirmationRequired(fi);
  if (
    (!wasAwaitingConfirmation && !confirmsRangeEndpoint) ||
    !Number.isFinite(normalized) ||
    !candidates.some(
      (candidate) => Number(candidate.personId) === normalized
    )
  ) {
    return false;
  }
  rangeAnnotManualPersonByFrame.set(fi, normalized);
  if (wasAwaitingConfirmation) {
    rangeAnnotAwaitingPersonFrame = null;
    rangeAnnotSuggestedPersonId = null;
  }
  return wasAwaitingConfirmation;
}

function continueRangeIdentityReviewAfterSelection() {
  if (rangeAnnotReviewResumeTimer) clearTimeout(rangeAnnotReviewResumeTimer);
  rangeAnnotReviewResumeTimer = setTimeout(() => {
    rangeAnnotReviewResumeTimer = null;
    void applyRangeAnnotVerified();
  }, 0);
}

async function focusRangePersonConfirmation(frameIdx, suggestedPersonId = null) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0) return;
  rangeAnnotAwaitingPersonFrame = fi;
  rangeAnnotSuggestedPersonId =
    suggestedPersonId == null || !Number.isFinite(Number(suggestedPersonId))
      ? null
      : Number(suggestedPersonId);

  const ev = getEventsOnFrame(fi)[0] || null;
  if (ev && typeof seekToEvent === "function") {
    await seekToEvent(ev);
  } else if (typeof seekToTimestamp === "function") {
    const row = typeof frameEntryByIdx === "function" ? frameEntryByIdx(fi) : null;
    if (row) await seekToTimestamp(row.t, fi, { skipEventSync: false });
  }
  if (typeof updateReviewDock === "function") updateReviewDock();
}

/** 首帧模板：优先当前钉住事件，否则首帧上已有货框/person 选取的事件 */
function getRangeAnnotTemplate(startFrame) {
  const fi = parseInt(startFrame, 10) || 0;
  const frameEvents = getEventsOnFrame(fi);
  if (!frameEvents.length) return null;

  const pinned = typeof getPinnedPlaybackEvent === "function" ? getPinnedPlaybackEvent() : null;
  if (pinned && (parseInt(pinned.frame_idx, 10) || 0) === fi) {
    return {
      ev: pinned,
      confirmed: getEventConfirmedBoxes(pinned),
      personId: getEventPersonId(pinned),
    };
  }

  for (const ev of frameEvents) {
    const confirmed = getEventConfirmedBoxes(ev);
    const personId = getEventPersonId(ev);
    if (confirmed.length || personId != null) {
      return { ev, confirmed, personId };
    }
  }

  return { ev: frameEvents[0], confirmed: [], personId: null };
}

function validateRangeAnnotTemplate(template, startFrame) {
  if (!template?.ev) {
    return { ok: false, message: `首帧 ${startFrame} 缺少帧数据，无法区间标真` };
  }

  let confirmed = normalizeBoxTokenList(template.confirmed);
  if (!confirmed.length) {
    return { ok: false, message: "请先在首帧人工点选确认货框" };
  }

  const personIds = getFramePersonIds(startFrame);
  let personId = template.personId;
  if (personId == null && personIds.length === 1) {
    personId = personIds[0];
  }
  if (personIds.length >= 2 && personId == null) {
    return { ok: false, message: "首帧有多人，请先选择 person_id（侧栏或点击骨架）" };
  }
  if (personId != null && personIds.length && !personIds.includes(Number(personId))) {
    return { ok: false, message: `首帧 person_id ${personId} 不在当前画面人员列表中` };
  }

  const trackId = personId != null ? getPersonTrackIdAtFrame(startFrame, personId) : null;
  return { ok: true, confirmed, personId, trackId };
}

async function ensureFrameRangeLoaded(start, end) {
  if (typeof ensureFrameChunkLoaded !== "function") return;
  const tasks = [];
  for (let fi = start; fi <= end; fi += 1) {
    tasks.push(ensureFrameChunkLoaded(fi));
  }
  await Promise.all(tasks);
}

function setRangeAnnotStartFromCurrent() {
  const fi =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
  if (!fi || fi <= 0) {
    setEventReviewSaveStatus("无法读取当前帧，请先暂停到目标画面", "error");
    return;
  }
  rangeAnnotStartFrame = fi;
  rangeAnnotManualPersonByFrame.clear();
  rangeAnnotAwaitingPersonFrame = null;
  rangeAnnotSuggestedPersonId = null;
  if (rangeAnnotEndFrame != null && rangeAnnotEndFrame < rangeAnnotStartFrame) {
    rangeAnnotEndFrame = null;
  }
  refreshRangeAnnotTemplateSnapshot({ force: true });
  updateRangeAnnotUi();
  setEventReviewSaveStatus(`已设首帧 ${fi} · 请在本帧选择 person_id 与货框`, "");
}

function setRangeAnnotEndFromCurrent() {
  const fi =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
  if (!fi || fi <= 0) {
    setEventReviewSaveStatus("无法读取当前帧，请先暂停到目标画面", "error");
    return;
  }
  rangeAnnotEndFrame = fi;
  for (const frameIdx of [...rangeAnnotManualPersonByFrame.keys()]) {
    if (frameIdx !== Number(rangeAnnotStartFrame)) {
      rangeAnnotManualPersonByFrame.delete(frameIdx);
    }
  }
  rangeAnnotAwaitingPersonFrame = null;
  rangeAnnotSuggestedPersonId = null;
  if (rangeAnnotStartFrame != null && rangeAnnotEndFrame < rangeAnnotStartFrame) {
    const tmp = rangeAnnotStartFrame;
    rangeAnnotStartFrame = rangeAnnotEndFrame;
    rangeAnnotEndFrame = tmp;
    rangeAnnotManualPersonByFrame.clear();
    rangeAnnotTemplateSnapshot = null;
    refreshRangeAnnotTemplateSnapshot({ force: true });
  }
  updateRangeAnnotUi();
  const frozenBoxes = normalizeBoxTokenList(
    rangeAnnotTemplateSnapshot?.confirmed || []
  );
  setEventReviewSaveStatus(
    `已设尾帧 ${fi}${
      frozenBoxes.length
        ? ` · 首帧货框已保留 ${formatConfirmedBoxes(frozenBoxes)}，尾帧无需重复选择`
        : ""
    }`,
    ""
  );
}

function updateRangeAnnotUi() {
  const startEl = $("#event-range-start-label");
  const endEl = $("#event-range-end-label");
  const hintEl = $("#event-range-hint");
  const applyBtn = $("#event-range-apply-btn");

  const bounds = normalizeRangeAnnotBounds(rangeAnnotStartFrame, rangeAnnotEndFrame);
  if (startEl) {
    startEl.textContent =
      rangeAnnotStartFrame != null ? `首帧：${rangeAnnotStartFrame}` : "首帧：—";
    startEl.classList.toggle("is-set", rangeAnnotStartFrame != null);
  }
  if (endEl) {
    endEl.textContent = rangeAnnotEndFrame != null ? `尾帧：${rangeAnnotEndFrame}` : "尾帧：—";
    endEl.classList.toggle("is-set", rangeAnnotEndFrame != null);
  }

  let hint = "首帧选择取货人员与货框；设置尾帧后只复核身份变化点";
  let canApply = false;
  let previewN = 0;

  if (bounds) {
    refreshRangeAnnotTemplateSnapshot();
    const template = getRangeAnnotTemplateForApply(bounds.start);
    const check = validateRangeAnnotTemplate(template, bounds.start);
    const collected = check.ok
      ? collectRangeAnnotFrames(bounds.start, bounds.end)
      : { events: [], missingFrames: [] };
    previewN = collected.events.length;
    const expectedN = bounds.end - bounds.start + 1;
    canApply =
      check.ok &&
      previewN === expectedN &&
      collected.missingFrames.length === 0 &&
      !!currentRecordId;

    if (!check.ok) {
      hint = check.message;
    } else if (collected.missingFrames.length) {
      const sample = collected.missingFrames.slice(0, 5).join(", ");
      hint = `区间帧数据不完整，缺少 ${sample}${collected.missingFrames.length > 5 ? " …" : ""}`;
    } else {
      const personNote =
        check.personId != null
          ? ` · 人员 P${check.personId}${check.trackId != null ? `（track ${check.trackId}）` : ""}`
          : "";
      hint = `帧 ${bounds.start}–${bounds.end}（含首尾）· 将逐帧标真 ${previewN} 帧 · 货框 ${formatConfirmedBoxes(check.confirmed)}${personNote}`;
    }
  } else if (rangeAnnotStartFrame != null || rangeAnnotEndFrame != null) {
    hint = "请同时设置首帧与尾帧";
  }

  if (hintEl) {
    hintEl.textContent = hint;
    hintEl.classList.toggle("is-ready", canApply);
    hintEl.classList.toggle("is-error", bounds && previewN === 0 && rangeAnnotStartFrame && rangeAnnotEndFrame);
  }
  if (applyBtn) {
    applyBtn.disabled = !canApply;
    applyBtn.textContent = previewN > 0 ? `区间标真（${previewN} 帧）` : "区间标真";
  }
}

function buildRangeAnnotEventPayload(ev, check, assignment) {
  const frameIdx = parseInt(ev?.frame_idx, 10) || 0;
  const payload = {
    event_type: String(ev?.event_type || "frame").trim() || "frame",
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev?.source_frame_idx ?? frameIdx, 10) || frameIdx,
    box_tokens: normalizeBoxTokenList(ev?.box_tokens),
    confirmed_box_tokens: [...check.confirmed],
  };
  if (assignment?.personId != null) payload.person_id = Number(assignment.personId);
  if (assignment?.trackId != null && String(assignment.trackId).trim()) {
    payload.person_track_id = String(assignment.trackId).trim();
  }
  return payload;
}

function savedRangePayloadMatches(review, payloads) {
  const byFrame = new Map();
  (review?.verified_true || []).forEach((item) => {
    const fi = parseInt(item?.frame_idx, 10) || 0;
    if (fi > 0) byFrame.set(fi, item);
  });
  const missingFrames = [];
  payloads.forEach((payload) => {
    const saved = byFrame.get(payload.frame_idx);
    const expectedBoxes = normalizeBoxTokenList(payload.confirmed_box_tokens);
    const bindings = normalizeReviewBindings(saved?.bindings);
    const matched = bindings.some((binding) => {
      if (
        payload.person_track_id != null &&
        String(binding.person_track_id ?? "") !== String(payload.person_track_id)
      ) {
        return false;
      }
      if (
        payload.person_track_id == null &&
        payload.person_id != null &&
        Number(binding.person_id) !== Number(payload.person_id)
      ) {
        return false;
      }
      const actualBoxes = normalizeBoxTokenList(binding.confirmed_box_tokens);
      return (
        actualBoxes.length === expectedBoxes.length &&
        expectedBoxes.every((token) => actualBoxes.includes(token))
      );
    });
    if (!matched) missingFrames.push(payload.frame_idx);
  });
  return missingFrames;
}

async function persistEventReviewRange(payloads, bounds, statusMessage) {
  const recordId = currentRecordId;
  if (!recordId || !payloads.length) return false;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus(statusMessage, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "set_range_verified",
          range_start: bounds.start,
          range_end: bounds.end,
          events: payloads,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `区间保存失败 (${res.status})`);
      }
      const body = await res.json();
      const expectedN = bounds.end - bounds.start + 1;
      if (Number(body.range_applied_count) !== expectedN) {
        throw new Error(
          `区间保存数量异常：应保存 ${expectedN} 帧，服务端返回 ${body.range_applied_count ?? 0} 帧`
        );
      }
      const missingFrames = savedRangePayloadMatches(body.event_review, payloads);
      if (missingFrames.length) {
        throw new Error(
          `区间保存后校验失败，缺少帧 ${missingFrames.slice(0, 8).join(", ")}${
            missingFrames.length > 8 ? " …" : ""
          }`
        );
      }
      return applyEventReviewResponse(body, seq, recordId, {
        skipAutoConfirmBoxes: true,
        statusMessage: `区间标真完成 · 帧 ${bounds.start}–${bounds.end} · 共 ${expectedN} 帧`,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "区间保存失败", "error");
      }
      return false;
    }
  });
}

async function applyRangeAnnotVerified() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  const bounds = normalizeRangeAnnotBounds(rangeAnnotStartFrame, rangeAnnotEndFrame);
  if (!bounds) {
    setEventReviewSaveStatus("请先设置首帧与尾帧", "error");
    updateRangeAnnotUi();
    return;
  }

  await ensureFrameRangeLoaded(bounds.start, bounds.end);

  refreshRangeAnnotTemplateSnapshot();
  const template = getRangeAnnotTemplateForApply(bounds.start);
  const check = validateRangeAnnotTemplate(template, bounds.start);
  if (!check.ok) {
    setEventReviewSaveStatus(check.message, "error");
    updateRangeAnnotUi();
    return;
  }

  const collected = collectRangeAnnotFrames(bounds.start, bounds.end);
  if (collected.missingFrames.length) {
    const sample = collected.missingFrames.slice(0, 8).join(", ");
    setEventReviewSaveStatus(
      `区间帧数据不完整，缺少 ${sample}${collected.missingFrames.length > 8 ? " …" : ""}`,
      "error"
    );
    updateRangeAnnotUi();
    return;
  }
  const events = collected.events;
  const expectedN = bounds.end - bounds.start + 1;
  if (events.length !== expectedN) {
    setEventReviewSaveStatus(
      `区间帧数异常：应有 ${expectedN} 帧，实际找到 ${events.length} 帧`,
      "error"
    );
    return;
  }

  const personResolution = resolveRangePersonAssignments(
    bounds.start,
    bounds.end,
    check.personId
  );
  if (personResolution.confirmationFrames.length) {
    const frameIdx = personResolution.confirmationFrames[0];
    const suggestion = personResolution.confirmationSuggestions.find(
      (item) => item.frameIdx === frameIdx
    );
    await focusRangePersonConfirmation(frameIdx, suggestion?.personId ?? null);
    const suggestionStable =
      suggestion?.personId != null
        ? getStablePersonDisplayInfoByRawId(frameIdx, suggestion.personId)
        : null;
    const reasonLabel =
      suggestion?.reason === "reacquire"
          ? "人员消失后重新出现"
          : suggestion?.reason === "invalid_anchor"
            ? "原人工锚点已不在当前画面"
            : "空间连续性置信度较低";
    const suggestionNote =
      suggestion?.personId != null
        ? `；空间连续性建议人物 ${suggestionStable?.stableLabel ?? "?"}（本帧 raw P${suggestion.personId}），请以画面为准`
        : "；空间连续性不足，不能给出可靠建议";
    setEventReviewSaveStatus(
      `区间身份复核：帧 ${frameIdx} 检测到${reasonLabel}。整段尚未保存，请确认本帧${suggestionNote}；可按 1/2 快选人物 A/B`,
      "error"
    );
    return;
  }
  if (personResolution.ambiguousFrames.length) {
    const sample = personResolution.ambiguousFrames.slice(0, 5).join(", ");
    setEventReviewSaveStatus(
      `检测到人员身份歧义，已停止且未保存。请人工检查帧：${sample}`,
      "error"
    );
    return;
  }
  const assignmentByFrame = new Map(
    personResolution.assignments.map((assignment) => [assignment.frameIdx, assignment])
  );
  const payloads = events.map((ev) =>
    buildRangeAnnotEventPayload(
      ev,
      check,
      assignmentByFrame.get(parseInt(ev.frame_idx, 10) || 0)
    )
  );

  const personAssignmentSummary = summarizeRangePersonAssignments(
    personResolution.assignments
  );
  const personNote = personAssignmentSummary
    ? `\nraw person_id 逐帧映射：${personAssignmentSummary}`
    : "";
  const missingPersonWarning = personResolution.missingPersonFrames.length
    ? `\n人员提示：${personResolution.missingPersonFrames.length} 帧没有人体检测，将保留逐帧货框标真但不填写 person_id。`
    : "";
  if (
    !window.confirm(
      `确定区间逐帧标真？\n\n帧范围：${bounds.start} – ${bounds.end}（含首尾）\n帧数：${expectedN}\n货框：${formatConfirmedBoxes(check.confirmed)}${personNote}\n\n人物 A/B 已按相邻帧人体框连续性稳定跟踪；raw P0/P1 重排已自动映射，低置信度点已经人工确认，track 仅作弱辅助。每帧仍写入该帧真实 person_id。${missingPersonWarning}`
    )
  ) {
    return;
  }

  const ok = await persistEventReviewRange(
    payloads,
    bounds,
    `区间逐帧标真 ${expectedN} 帧 · 原子保存中…`
  );
  if (ok) {
    updateRangeAnnotUi();
  }
}
