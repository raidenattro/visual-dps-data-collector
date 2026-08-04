/** 事件复核（标真 / 取消 / 保存） */

/** 复核 PATCH 串行队列，避免连按 Y 导致并发 toggle 互相覆盖 */
let eventReviewSaveChain = Promise.resolve();

function drainEventReviewSaveQueue() {
  return eventReviewSaveChain;
}

function runSerializedEventReviewSave(task) {
  const run = eventReviewSaveChain.then(() => task());
  eventReviewSaveChain = run.catch(() => {});
  return run;
}

/** 切换/关闭记录前：等待队列中 PATCH 落盘，并作废已切换记录后的过期 UI 响应 */
async function prepareEventReviewRecordSwitch(options = {}) {
  if (
    !options.force &&
    typeof hasUnsavedEventReviewDrafts === "function" &&
    hasUnsavedEventReviewDrafts() &&
    !window.confirm("当前记录还有未保存的人物/货框修改。确定放弃草稿并切换记录吗？")
  ) {
    return false;
  }
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  await drainEventReviewSaveQueue();
  eventReviewSaveSeq++;
  return true;
}

/** 与后端 event_signature 一致 */
function eventRowKey(ev) {
  if (ev && typeof ev === "object" && typeof ev.__playbackEventRowKey === "string") {
    return ev.__playbackEventRowKey;
  }
  const tokens = canonicalizeBoxTokenList(ev?.box_tokens);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const eventType = String(ev.event_type || "").trim();
  const key = `${eventType}:${frameIdx}:${tokens.join(",")}`;
  if (ev && typeof ev === "object") {
    try {
      Object.defineProperty(ev, "__playbackEventRowKey", {
        value: key,
        configurable: true,
        enumerable: false,
      });
    } catch {
      /* frozen/imported objects can still use the computed key */
    }
  }
  return key;
}

/**
 * 帧 → 该帧事件的索引。原先按帧取事件是全表 filter，一条记录上万条事件，
 * 放进播放热路径会直接拖垮帧率（复核高亮因此长期只在暂停时才算）。
 *
 * playbackEvents 只会整体替换、从不原地增删，所以用数组身份判断失效就够了；
 * 索引只存事件引用，标真/配对状态仍每次实时读，标注完不会看到旧高亮。
 */
let playbackEventFrameIndex = null;
let playbackEventFrameIndexSource = null;

function getPlaybackEventFrameIndex() {
  if (playbackEventFrameIndex && playbackEventFrameIndexSource === playbackEvents) {
    return playbackEventFrameIndex;
  }
  const index = new Map();
  const add = (fi, ev) => {
    if (!(fi > 0)) return;
    const list = index.get(fi);
    if (!list) index.set(fi, [ev]);
    else if (!list.includes(ev)) list.push(ev);
  };
  playbackEvents.forEach((ev) => {
    // eventMatchesPlaybackFrame 认 frame_idx 与 source_frame_idx 两者之一，索引也要两边都挂。
    add(eventDisplayFrameIdx(ev), ev);
    add(eventSourceFrameIdx(ev), ev);
  });
  playbackEventFrameIndex = index;
  playbackEventFrameIndexSource = playbackEvents;
  return index;
}

function getEventsOnFrame(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi) return [];
  const hit = getPlaybackEventFrameIndex().get(fi);
  // 复制一份：调用方拿去排序/增删不能改到索引内部。
  return hit ? [...hit] : [];
}

/**
 * 货框点选时定位应操作的事件。
 *
 * 当前事件在播放帧上时必须优先使用当前事件：用户点中的货框可能正是
 * 对检测结果的人工纠正，不能因为同帧另一事件包含该货框就偷偷切换事件。
 */
function resolveEventForBoxAnnotation(token) {
  const active = getActiveEvent() ?? getActiveFilteredEvent();
  const hit = canonicalBoxToken(token);
  if (!hit) return active;

  const frameIdx =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;

  if (
    active &&
    (frameIdx == null ||
      frameIdx <= 0 ||
      typeof eventMatchesPlaybackFrame !== "function" ||
      eventMatchesPlaybackFrame(active, frameIdx))
  ) {
    return active;
  }

  const eventHasToken = (ev) => {
    if (!ev) return false;
    const boxes = [...getEventConfirmedBoxes(ev), ...normalizeBoxTokenList(ev.box_tokens)];
    return boxes.some((t) => canonicalBoxToken(t) === hit);
  };

  if (frameIdx != null && frameIdx > 0) {
    const onFrame = getEventsOnFrame(frameIdx).filter(eventHasToken);
    if (onFrame.length === 1) return onFrame[0];
    if (onFrame.length > 1) {
      const verified = onFrame.find((e) => isEventVerified(e));
      return verified || onFrame.find((e) => e.event_type === "alarm") || onFrame[0];
    }
  }

  if (active && eventHasToken(active)) return active;

  if (frameIdx != null && frameIdx > 0) {
    const anyOnFrame = getEventsOnFrame(frameIdx);
    if (anyOnFrame.length === 1) return anyOnFrame[0];
  }

  return active;
}

function getVerifiedEventsOnFrame(frameIdx) {
  return getEventsOnFrame(frameIdx).filter((e) => isEventVerified(e));
}

function countFrameConfirmedBoxes(frameIdx) {
  return getEventsOnFrame(frameIdx).filter((e) => getEventConfirmedBoxes(e).length > 0).length;
}

function countVerifiedFrameConfirmedBoxes(frameIdx) {
  return getVerifiedEventsOnFrame(frameIdx).filter((e) => getEventConfirmedBoxes(e).length > 0).length;
}

function isAnnotationBoxToken(token) {
  const hit = canonicalBoxToken(token);
  if (!hit || !annotationBoxes.length) return false;
  return annotationBoxes.some((box) => boxCollisionToken(box) === hit);
}

function normalizeBoxTokenList(tokens) {
  return canonicalizeBoxTokenList(tokens);
}

function eventDisplayFrameIdx(ev) {
  return parseInt(ev?.frame_idx, 10) || 0;
}

function eventSourceFrameIdx(ev) {
  const sfi = parseInt(ev?.source_frame_idx, 10) || 0;
  const fi = eventDisplayFrameIdx(ev);
  return sfi > 0 ? sfi : fi;
}

/** 回放当前帧是否与事件对应（兼容 source_frame_idx） */
function eventMatchesPlaybackFrame(ev, playbackFrameIdx) {
  const fi = parseInt(playbackFrameIdx, 10) || 0;
  if (!fi || !ev) return false;
  return fi === eventDisplayFrameIdx(ev) || fi === eventSourceFrameIdx(ev);
}

function formatConfirmedBoxes(tokens) {
  const list = normalizeBoxTokenList(tokens);
  if (!list.length) return "";
  if (list.length <= 2) return list.join(", ");
  return `${list.slice(0, 2).join(", ")} +${list.length - 2}`;
}

function getEventConfirmedBoxes(ev) {
  if (!ev) return [];
  const key = eventRowKey(ev);
  if (pendingReviewBindingsByKey.has(key)) {
    const bindings = normalizeReviewBindings(pendingReviewBindingsByKey.get(key));
    const selectedPersonId = selectedPersonIdForBinding(ev);
    if (selectedPersonId != null) {
      return bindingConfirmedBoxesForPerson(bindings, selectedPersonId);
    }
    return unionReviewBindingBoxes(bindings);
  }
  if (pendingConfirmedBoxesByKey.has(key)) {
    return normalizeBoxTokenList(pendingConfirmedBoxesByKey.get(key) || []);
  }
  return getEventPersistedConfirmedBoxes(ev);
}

function normalizeReviewBindings(bindings) {
  if (!Array.isArray(bindings)) return [];
  const out = [];
  const seen = new Set();
  bindings.forEach((raw) => {
    if (!raw || typeof raw !== "object") return;
    const confirmed = normalizeBoxTokenList(raw.confirmed_box_tokens || []);
    if (!confirmed.length) return;
    const personId =
      raw.person_id == null || raw.person_id === "" || !Number.isFinite(Number(raw.person_id))
        ? null
        : Number(raw.person_id);
    const trackId = String(raw.person_track_id ?? raw.track_id ?? "").trim();
    const key = `${personId ?? ""}|${trackId}|${confirmed.join(",")}`;
    if (seen.has(key)) return;
    seen.add(key);
    const binding = { confirmed_box_tokens: confirmed };
    if (personId != null) binding.person_id = personId;
    if (trackId) binding.person_track_id = trackId;
    out.push(binding);
  });
  return out;
}

/** 按帧内 raw person_id 读取骨架追踪 ID；找不到时绝不猜测。 */
function getPersonTrackIdAtFrame(frameIdx, personId) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return null;
  const frame = frameCache.get(fi);
  if (!frame?.persons?.length) return null;
  const target = Number(personId);
  for (let idx = 0; idx < frame.persons.length; idx++) {
    const person = frame.persons[idx];
    const pid = person.person_id != null ? Number(person.person_id) : idx;
    if (pid !== target) continue;
    const trackId = person.person_track_id;
    return trackId != null && String(trackId).trim() ? String(trackId).trim() : null;
  }
  return null;
}

/** 为缺少追踪 ID 的 binding 补入该帧骨架值，已有值保持不变。 */
function addFrameTrackIdToBindings(bindings, frameIdx) {
  return normalizeReviewBindings(bindings).map((binding) => {
    if (binding.person_track_id != null || binding.person_id == null) return binding;
    const trackId = getPersonTrackIdAtFrame(frameIdx, binding.person_id);
    return trackId ? { ...binding, person_track_id: trackId } : binding;
  });
}

function bindingConfirmedBoxesForPerson(bindings, personId) {
  const pid = Number(personId);
  if (!Number.isFinite(pid)) return [];
  const binding = normalizeReviewBindings(bindings).find(
    (item) => Number(item.person_id) === pid
  );
  return binding ? [...binding.confirmed_box_tokens] : [];
}

/** 草稿与磁盘比对用的签名，顺序无关。 */
function reviewBoxListSignature(tokens) {
  return [...normalizeBoxTokenList(tokens)].sort().join(",");
}

function reviewBindingsSignature(bindings) {
  return normalizeReviewBindings(bindings)
    .map(
      (binding) =>
        `${binding.person_id ?? ""}|${binding.person_track_id ?? ""}|` +
        reviewBoxListSignature(binding.confirmed_box_tokens)
    )
    .sort()
    .join(";");
}

/**
 * 丢掉已经不代表「与磁盘有差异」的草稿：值已和磁盘一致的，以及事件不在
 * playbackEvents 里的孤儿键。
 *
 * 三个 pending map 原本只由 setEventVerified() 消化，而区间标真与按 Y 全量写入
 * 都不走那条路。区间标真的既定流程恰恰是「首帧选人选货框后按 R」，于是每做一次
 * 区间标真都留下一条永久草稿：数据其实已落盘，但「有未保存修改」再也不消失，
 * 连按 Y 也清不掉，且指不出是哪一帧。
 *
 * @returns {number} 清掉的草稿条数
 */
function pruneSettledEventReviewDrafts() {
  const eventByKey = new Map();
  playbackEvents.forEach((ev) => eventByKey.set(eventRowKey(ev), ev));
  let dropped = 0;

  for (const [key, tokens] of [...pendingConfirmedBoxesByKey]) {
    const ev = eventByKey.get(key);
    const persisted = ev
      ? eventPersistedBindings(ev).flatMap((binding) => binding.confirmed_box_tokens)
      : [];
    if (!ev || reviewBoxListSignature(tokens) === reviewBoxListSignature(persisted)) {
      pendingConfirmedBoxesByKey.delete(key);
      dropped += 1;
    }
  }

  for (const [key, bindings] of [...pendingReviewBindingsByKey]) {
    const ev = eventByKey.get(key);
    if (
      !ev ||
      reviewBindingsSignature(bindings) === reviewBindingsSignature(eventPersistedBindings(ev))
    ) {
      pendingReviewBindingsByKey.delete(key);
      dropped += 1;
    }
  }

  for (const [key, personId] of [...pendingPersonIdByKey]) {
    const ev = eventByKey.get(key);
    const draft = personId == null ? null : Number(personId);
    if (!ev || getEventPersistedPersonId(ev) === draft) {
      pendingPersonIdByKey.delete(key);
      dropped += 1;
    }
  }
  return dropped;
}

function eventPersistedBindings(ev) {
  if (!ev) return [];
  const bindings = normalizeReviewBindings(ev.bindings);
  if (bindings.length) return addFrameTrackIdToBindings(bindings, ev.frame_idx);
  const confirmed = Array.isArray(ev.confirmed_box_tokens)
    ? normalizeBoxTokenList(ev.confirmed_box_tokens)
    : String(ev.confirmed_box_token || "").trim()
      ? normalizeBoxTokenList([ev.confirmed_box_token])
      : [];
  const personId =
    ev.person_id == null || ev.person_id === "" || !Number.isFinite(Number(ev.person_id))
      ? null
      : Number(ev.person_id);
  if (!confirmed.length) return [];
  const binding = { confirmed_box_tokens: confirmed };
  if (personId != null) {
    binding.person_id = personId;
    const trackId =
      String(ev.person_track_id ?? ev.track_id ?? "").trim() ||
      getPersonTrackIdAtFrame(ev.frame_idx, personId);
    if (trackId) binding.person_track_id = trackId;
  }
  return [binding];
}

/** 当前画面使用的完整多人配对；草稿优先，离开事件前不会污染已保存数据。 */
function getEventEffectiveBindings(ev) {
  if (!ev) return [];
  const key = eventRowKey(ev);
  if (pendingReviewBindingsByKey.has(key)) {
    return normalizeReviewBindings(pendingReviewBindingsByKey.get(key));
  }
  return eventPersistedBindings(ev);
}

function setBindingBoxesForPerson(bindings, personId, tokens, frameIdx = 0) {
  const pid = Number(personId);
  if (!Number.isFinite(pid)) return normalizeReviewBindings(bindings);
  const boxes = normalizeBoxTokenList(tokens);
  const normalized = normalizeReviewBindings(bindings);
  let replaced = false;
  const next = [];
  normalized.forEach((binding) => {
    if (Number(binding.person_id) !== pid) {
      next.push(binding);
      return;
    }
    if (!replaced && boxes.length) {
      next.push({
        ...binding,
        person_id: pid,
        confirmed_box_tokens: boxes,
      });
      replaced = true;
    }
  });
  if (!replaced && boxes.length) {
    const binding = { person_id: pid, confirmed_box_tokens: boxes };
    const trackId = getPersonTrackIdAtFrame(frameIdx, pid);
    if (trackId) binding.person_track_id = trackId;
    next.push(binding);
  }
  return normalizeReviewBindings(next);
}

function unionReviewBindingBoxes(bindings) {
  return normalizeBoxTokenList(
    normalizeReviewBindings(bindings).flatMap(
      (binding) => binding.confirmed_box_tokens || []
    )
  );
}

function formatReviewBindingSummary(ev) {
  return getEventEffectiveBindings(ev)
    .filter((binding) => binding.person_id != null)
    .map((binding) => {
      const info =
        typeof getStablePersonDisplayInfoByRawId === "function"
          ? getStablePersonDisplayInfoByRawId(ev.frame_idx, binding.person_id)
          : null;
      const label = info?.stableLabel ?? `P${binding.person_id}`;
      return `人物 ${label} → ${formatConfirmedBoxes(binding.confirmed_box_tokens)}`;
    })
    .join(" · ");
}

function escapeReviewHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * 人物配对颜色按稳定身份固定，而不是按每帧 raw P0/P1 固定。
 * A=紫、B=橙，更多人物继续使用青、绿循环。
 */
function reviewPersonAccentIndex(stableId) {
  const value = Number(stableId);
  return Number.isFinite(value) ? Math.abs(Math.trunc(value)) % 4 : 0;
}

// 提到模块级：取色在绘制热路径上（每人每帧、每个配对每帧各一次），
// 原先每次调用都要新建整个调色板。冻结以防调用方就地改坏共享对象。
const REVIEW_PERSON_ACCENT_PALETTE = Object.freeze([
  Object.freeze({ index: 0, name: "purple", fill: "rgba(168, 85, 247, 0.36)", labelFill: "rgba(126, 34, 206, 0.94)", stroke: "rgba(192, 132, 252, 0.98)" }),
  Object.freeze({ index: 1, name: "orange", fill: "rgba(249, 115, 22, 0.36)", labelFill: "rgba(194, 65, 12, 0.94)", stroke: "rgba(251, 146, 60, 0.98)" }),
  Object.freeze({ index: 2, name: "cyan", fill: "rgba(6, 182, 212, 0.34)", labelFill: "rgba(14, 116, 144, 0.94)", stroke: "rgba(34, 211, 238, 0.98)" }),
  Object.freeze({ index: 3, name: "green", fill: "rgba(34, 197, 94, 0.32)", labelFill: "rgba(21, 128, 61, 0.94)", stroke: "rgba(74, 222, 128, 0.98)" }),
]);

function getReviewPersonAccentStyle(
  ev,
  personId,
  fallbackIndex = 0,
  binding = null
) {
  const info =
    ev && personId != null && typeof getStablePersonDisplayInfoByRawId === "function"
      ? getStablePersonDisplayInfoByRawId(ev.frame_idx, personId)
      : null;
  const trackId = String(
    binding?.person_track_id ??
      (ev && personId != null && typeof getPersonTrackIdAtFrame === "function"
        ? getPersonTrackIdAtFrame(ev.frame_idx, personId)
        : "") ??
      ""
  ).trim();
  const hintedStableId =
    trackId &&
    typeof stablePersonIdentityTrackHints !== "undefined" &&
    stablePersonIdentityTrackHints?.has(trackId)
      ? stablePersonIdentityTrackHints.get(trackId)
      : null;
  const index = reviewPersonAccentIndex(
    info?.stableId ?? hintedStableId ?? fallbackIndex
  );
  return REVIEW_PERSON_ACCENT_PALETTE[index];
}

function getStableReviewPersonOptions(ev) {
  if (!ev) return [];
  const options = getFramePersonIds(ev.frame_idx).map((pid) => {
    const info =
      typeof getStablePersonDisplayInfoByRawId === "function"
        ? getStablePersonDisplayInfoByRawId(ev.frame_idx, pid)
        : null;
    return {
      pid,
      stableId: info?.stableId ?? pid,
      stableLabel: info?.stableLabel ?? String(Number(pid) + 1),
    };
  });
  return typeof sortStablePersonDisplayOptions === "function"
    ? sortStablePersonDisplayOptions(options)
    : options.sort((a, b) => Number(a.stableId) - Number(b.stableId));
}

/** Alt/按钮：只切换当前配对人物，已经选择的其他人物货框保持不变。 */
function cycleEventReviewPersonSelection() {
  const ev =
    (typeof getPinnedPlaybackEvent === "function" ? getPinnedPlaybackEvent() : null) ??
    (typeof getActiveEvent === "function" ? getActiveEvent() : null) ??
    (typeof getActiveFilteredEvent === "function" ? getActiveFilteredEvent() : null);
  if (!ev) return false;
  const options = getStableReviewPersonOptions(ev);
  if (options.length < 2) return false;
  const selected = getEventPersonId(ev);
  const currentIndex = options.findIndex(
    (option) => Number(option.pid) === Number(selected)
  );
  const next = options[(currentIndex + 1 + options.length) % options.length];
  if (!next) return false;
  void setPersonIdForEvent(ev, next.pid);
  return true;
}

function selectedPersonIdForBinding(ev) {
  if (!ev) return null;
  const key = eventRowKey(ev);
  if (pendingPersonIdByKey.has(key)) {
    const pending = pendingPersonIdByKey.get(key);
    return pending == null ? null : Number(pending);
  }
  if (ev.person_id != null && ev.person_id !== "" && Number.isFinite(Number(ev.person_id))) {
    return Number(ev.person_id);
  }
  return null;
}

/** 已写入 event_review.json 的货框 */
function getEventPersistedConfirmedBoxes(ev) {
  if (!ev) return [];
  const bindings = normalizeReviewBindings(ev.bindings);
  if (bindings.length) {
    const selectedPersonId = selectedPersonIdForBinding(ev);
    if (selectedPersonId != null) {
      const selected = bindings.find((binding) => binding.person_id === selectedPersonId);
      if (selected) return [...selected.confirmed_box_tokens];
      return [];
    }
    if (bindings.length === 1) return [...bindings[0].confirmed_box_tokens];
    return normalizeBoxTokenList(
      bindings.flatMap((binding) => binding.confirmed_box_tokens || [])
    );
  }
  if (Array.isArray(ev.confirmed_box_tokens)) {
    return normalizeBoxTokenList(ev.confirmed_box_tokens);
  }
  const legacy = String(ev.confirmed_box_token || "").trim();
  return legacy ? [legacy] : [];
}

/** 当前事件是否有尚未按 Y 落盘的 box 点选 */
function hasPendingBoxAnnotation(ev) {
  if (!ev) return false;
  const key = eventRowKey(ev);
  return (
    pendingConfirmedBoxesByKey.has(key) ||
    pendingReviewBindingsByKey.has(key)
  );
}

function getEventConfirmedBox(ev) {
  const boxes = getEventConfirmedBoxes(ev);
  return boxes[0] || "";
}

function setEventConfirmedBoxes(ev, tokens, { commitToEvent = false } = {}) {
  if (!ev) return;
  const list = normalizeBoxTokenList(tokens);
  const key = eventRowKey(ev);
  const personId = getEventPersonId(ev);
  boxAnnotationTouchedKeys.add(key);
  if (personId != null) {
    const baseBindings = pendingReviewBindingsByKey.has(key)
      ? pendingReviewBindingsByKey.get(key)
      : eventPersistedBindings(ev);
    const nextBindings = setBindingBoxesForPerson(baseBindings, personId, list, ev.frame_idx);
    pendingConfirmedBoxesByKey.delete(key);
    if (!commitToEvent) {
      pendingReviewBindingsByKey.set(key, nextBindings);
      return;
    }
    if (nextBindings.length) {
      ev.bindings = nextBindings.map((binding) => ({
        ...binding,
        confirmed_box_tokens: [...binding.confirmed_box_tokens],
      }));
      ev.confirmed_box_tokens = unionReviewBindingBoxes(nextBindings);
      if (nextBindings.length === 1 && nextBindings[0].person_id != null) {
        ev.person_id = nextBindings[0].person_id;
      } else {
        delete ev.person_id;
      }
    } else {
      delete ev.bindings;
      delete ev.confirmed_box_tokens;
      delete ev.person_id;
    }
    delete ev.confirmed_box_token;
    pendingReviewBindingsByKey.delete(key);
    return;
  }
  if (!commitToEvent) {
    pendingConfirmedBoxesByKey.set(key, [...list]);
    return;
  }
  if (list.length) {
    ev.confirmed_box_tokens = [...list];
    pendingConfirmedBoxesByKey.delete(key);
  } else {
    delete ev.confirmed_box_tokens;
    pendingConfirmedBoxesByKey.delete(key);
  }
  delete ev.confirmed_box_token;
  pendingReviewBindingsByKey.delete(key);
}

/** 标真落盘用的货框列表：优先人工点选，否则为空（标真时由 applyAuto 填充默认） */
function resolveConfirmedBoxesForSave(ev) {
  return getEventConfirmedBoxes(ev);
}

function applyAutoConfirmedBoxOnVerify(ev) {
  if (!ev) return;
  const key = eventRowKey(ev);
  if (boxAnnotationTouchedKeys.has(key)) return;
  if (getEventEffectiveBindings(ev).length) return;
  if (getEventConfirmedBoxes(ev).length) return;
  const defaults = normalizeBoxTokenList(ev.box_tokens);
  if (!defaults.length) return;
  setEventConfirmedBoxes(ev, defaults, { commitToEvent: true });
  boxAnnotationTouchedKeys.delete(key);
}

/** 当前帧画面中的 person_id 列表（与骨架绘制一致） */
function getFramePersonIds(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return [];
  const frame = frameCache.get(fi);
  const persons = frame?.persons || [];
  if (!persons.length) return [];
  return persons.map((person, idx) => {
    const pid = person?.person_id != null ? Number(person.person_id) : idx;
    return Number.isFinite(pid) ? pid : idx;
  });
}

function getEventPersistedPersonId(ev) {
  if (!ev) return null;
  if (ev.person_id != null && ev.person_id !== "") {
    const n = Number(ev.person_id);
    if (Number.isFinite(n)) return n;
  }
  const bindings = normalizeReviewBindings(ev.bindings);
  if (bindings.length === 1 && bindings[0].person_id != null) {
    return bindings[0].person_id;
  }
  return null;
}

function getEventPersonId(ev) {
  if (!ev) return null;
  const key = eventRowKey(ev);
  if (pendingPersonIdByKey.has(key)) {
    const pending = pendingPersonIdByKey.get(key);
    return pending == null ? null : pending;
  }
  return getEventPersistedPersonId(ev);
}

function hasPendingPersonIdAnnotation(ev) {
  if (!ev) return false;
  return pendingPersonIdByKey.has(eventRowKey(ev));
}

function setEventPersonId(ev, personId, { commitToEvent = false } = {}) {
  if (!ev) return;
  const key = eventRowKey(ev);
  const normalized =
    personId == null || personId === "" ? null : Number.isFinite(Number(personId)) ? Number(personId) : null;
  personIdTouchedKeys.add(key);
  if (!commitToEvent) {
    pendingPersonIdByKey.set(key, normalized);
    return;
  }
  if (normalized != null) {
    ev.person_id = normalized;
    pendingPersonIdByKey.delete(key);
  } else {
    delete ev.person_id;
    pendingPersonIdByKey.delete(key);
  }
}

function resolvePersonIdForSave(ev) {
  return getEventPersonId(ev);
}

function applyAutoPersonIdOnVerify(ev) {
  if (!ev) return;
  const key = eventRowKey(ev);
  if (personIdTouchedKeys.has(key)) return;
  if (getEventPersonId(ev) != null) return;
  const ids = getFramePersonIds(ev.frame_idx);
  if (ids.length === 1) {
    setEventPersonId(ev, ids[0], { commitToEvent: true });
    personIdTouchedKeys.delete(key);
  }
}

function validatePersonIdBeforeVerify(ev) {
  if (!ev) return { ok: false, message: "无事件" };
  const ids = getFramePersonIds(ev.frame_idx);
  if (!ids.length) return { ok: true };
  applyAutoPersonIdOnVerify(ev);
  const selected = getEventPersonId(ev);
  if (ids.length >= 2 && selected == null) {
    return { ok: false, message: "本帧有多人，请先选择 person_id（侧栏或点击骨架）" };
  }
  if (selected != null && !ids.includes(selected)) {
    return { ok: false, message: `person_id ${selected} 不在当前帧画面中，请重新选择` };
  }
  return { ok: true };
}

/** 复核画面：已确认 box 与检测参考 box（有事件即展示 box_tokens，无需标真） */
function getEventReviewBoxLayers(ev) {
  const detection = normalizeBoxTokenList(ev?.box_tokens);
  const effectiveBindings = getEventEffectiveBindings(ev);
  const confirmed = effectiveBindings.length
    ? unionReviewBindingBoxes(effectiveBindings)
    : getEventConfirmedBoxes(ev);
  let detectionRef = [];
  if (detection.length) {
    detectionRef = confirmed.length
      ? detection.filter((t) => !confirmed.includes(t))
      : [...detection];
  }
  return { confirmed, detectionRef };
}

/** 回放画面上展示的货框 token（含检测参考） */
function getEventDisplayBoxTokens(ev) {
  if (!ev) return [];
  const { confirmed, detectionRef } = getEventReviewBoxLayers(ev);
  if (confirmed.length || detectionRef.length) {
    return [...confirmed, ...detectionRef];
  }
  return getEventConfirmedBoxes(ev);
}

function eventToReviewPayload(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const payload = {
    event_type: String(ev.event_type || "").trim(),
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev.source_frame_idx ?? ev.frame_idx, 10) || frameIdx,
    box_tokens: tokens,
  };
  const confirmed = resolveConfirmedBoxesForSave(ev);
  const key = eventRowKey(ev);
  if (
    confirmed.length > 0 ||
    pendingConfirmedBoxesByKey.has(key) ||
    boxAnnotationTouchedKeys.has(key)
  ) {
    payload.confirmed_box_tokens = confirmed;
  }
  const personId = resolvePersonIdForSave(ev);
  if (
    personId != null ||
    pendingPersonIdByKey.has(key) ||
    personIdTouchedKeys.has(key)
  ) {
    if (personId != null) payload.person_id = personId;
  }
  const personTrackId =
    personId != null ? getPersonTrackIdAtFrame(frameIdx, personId) : null;
  if (personTrackId) payload.person_track_id = personTrackId;
  const hasBindingDraft = pendingReviewBindingsByKey.has(key);
  let bindings = addFrameTrackIdToBindings(getEventEffectiveBindings(ev), frameIdx);
  if (
    !hasBindingDraft &&
    confirmed.length &&
    (personId != null || bindings.length <= 1)
  ) {
    const incoming = { confirmed_box_tokens: [...confirmed] };
    if (personId != null) {
      incoming.person_id = personId;
      if (personTrackId) incoming.person_track_id = personTrackId;
    }
    if (personId != null) {
      bindings = bindings.filter((binding) => binding.person_id !== personId);
    } else if (bindings.length <= 1) {
      bindings = [];
    }
    bindings.push(incoming);
  }
  if (bindings.length) payload.bindings = addFrameTrackIdToBindings(bindings, frameIdx);
  return payload;
}

function syncConfirmedBoxFromReview(reviewPayload, events = playbackEvents) {
  const list = reviewPayload?.verified_true;
  if (!Array.isArray(list)) return;
  const byKey = new Map();
  const personByKey = new Map();
  const tokensByFrame = new Map();
  const personByFrame = new Map();
  const bindingsByFrame = new Map();
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const key = eventRowKey(item);
    const confirmed = normalizeBoxTokenList(
      item.confirmed_box_tokens || (item.confirmed_box_token ? [item.confirmed_box_token] : [])
    );
    byKey.set(key, confirmed);
    const frameIdx = parseInt(item.frame_idx, 10) || 0;
    const bindings = normalizeReviewBindings(item.bindings);
    if (bindings.length) bindingsByFrame.set(frameIdx, bindings);
    const frameTokens = confirmed.length ? confirmed : normalizeBoxTokenList(item.box_tokens);
    if (frameTokens.length) {
      tokensByFrame.set(
        frameIdx,
        normalizeBoxTokenList([...(tokensByFrame.get(frameIdx) || []), ...frameTokens])
      );
    }
    if (bindings.length === 1 && bindings[0].person_id != null) {
      personByKey.set(key, bindings[0].person_id);
      personByFrame.set(frameIdx, bindings[0].person_id);
    } else if (item.person_id != null && item.person_id !== "") {
      const pid = Number(item.person_id);
      if (Number.isFinite(pid)) {
        personByKey.set(key, pid);
        if (!personByFrame.has(frameIdx)) personByFrame.set(frameIdx, pid);
      }
    }
  }
  (events || []).forEach((ev) => {
    const key = eventRowKey(ev);
    const frameIdx = parseInt(ev.frame_idx, 10) || 0;
    let tokens = byKey.has(key)
      ? byKey.get(key)
      : Array.isArray(ev.confirmed_box_tokens)
        ? normalizeBoxTokenList(ev.confirmed_box_tokens)
        : tokensByFrame.get(frameIdx);
    const personId = personByKey.has(key)
      ? personByKey.get(key)
      : ev.person_id != null
        ? Number(ev.person_id)
        : personByFrame.get(frameIdx);
    const bindings = bindingsByFrame.get(frameIdx);
    if (bindings) {
      ev.bindings = bindings.map((binding) => ({
        ...binding,
        confirmed_box_tokens: [...binding.confirmed_box_tokens],
      }));
      pendingReviewBindingsByKey.delete(key);
    }
    if (tokens !== undefined) {
      if (tokens.length) {
        ev.confirmed_box_tokens = [...tokens];
        delete ev.confirmed_box_token;
        pendingConfirmedBoxesByKey.delete(key);
        pendingReviewBindingsByKey.delete(key);
      } else {
        delete ev.confirmed_box_tokens;
        delete ev.confirmed_box_token;
        pendingConfirmedBoxesByKey.delete(key);
        pendingReviewBindingsByKey.delete(key);
      }
    }
    if (personId !== undefined) {
      ev.person_id = personId;
      pendingPersonIdByKey.delete(key);
    }
  });
}

function buildBoxPickStatusHint(ev, confirmed, detN) {
  const pendingNote = hasPendingBoxAnnotation(ev)
    ? " · 暂选未落盘，切换事件会保留，按 Y 写入"
    : "";
  const countNote =
    detN > 0 ? ` · 已选 ${confirmed.length}/${detN}（可不选满）` : ` · 已选 ${confirmed.length} 个`;
  return `${countNote}${pendingNote} · 按 Y 写入`;
}

function buildEventsFromFrames(frames) {
  const events = [];
  (frames || []).forEach((fr) => {
    if (!fr || typeof fr !== "object") return;
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = canonicalizeBoxTokenList(fr.alarm_collisions || []);
    const collisions = canonicalizeBoxTokenList(fr.collisions || []);
    const frameTokens = canonicalizeBoxTokenList([...alarms, ...collisions]);
    events.push({
      event_type: alarms.length ? "alarm" : collisions.length ? "collision" : "frame",
      frame_idx: fi,
      source_frame_idx: sfi,
      timestamp_sec: ts,
      box_tokens: frameTokens,
    });
  });
  events.sort((a, b) => a.timestamp_sec - b.timestamp_sec || a.frame_idx - b.frame_idx);
  return events;
}

function getPlaybackDurationSec() {
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    return videoEl.duration;
  }
  if (frameByTime.length) {
    const last = frameByTime[frameByTime.length - 1];
    const tail = last?.t || 0;
    const fps = poseData?.fps || 15;
    return Math.max(tail + 1 / fps, tail);
  }
  return 0;
}

function formatEventTokens(tokens) {
  const list = (tokens || []).filter(Boolean);
  if (!list.length) return "—";
  if (list.length <= 2) return list.join(", ");
  return `${list.slice(0, 2).join(", ")} +${list.length - 2}`;
}

function isEventVerified(ev) {
  if (!ev) return false;
  const key = eventRowKey(ev);
  if (verifiedTrueKeys.has(key)) return true;
  if (ev.verified_true) {
    verifiedTrueKeys.add(key);
    return true;
  }
  return false;
}

function countVerifiedEvents() {
  return playbackEventStats.verified;
}

function eventTypeLabel(ev) {
  if (ev?.event_type === "alarm") return "告警";
  if (ev?.event_type === "collision") return "碰撞";
  return "帧";
}

/** 按 activeEventKey 在完整事件列表中定位（不受筛选影响） */
function getActiveEvent() {
  if (!activeEventKey || !playbackEvents.length) return null;
  return playbackEventsByKey.get(activeEventKey) ?? null;
}

function refreshEventCountLabel() {
  if (!eventCountLabel) return;
  if (!playbackEvents.length) return;
  let alarmN = playbackEventStats.alarm;
  let collN = playbackEventStats.collision;
  let verifiedN = countVerifiedEvents();
  const list = filteredPlaybackEvents();
  const rtHint = playbackEventsFromRealtime ? " · 回放实时计算" : "";
  const filterHint = list.length !== playbackEvents.length ? ` · 队列 ${list.length}` : "";
  let accuracyHint = "";
  let sourceHint = "";

  const evalCounts =
    typeof getPlaybackAccuracyEvalCounts === "function" ? getPlaybackAccuracyEvalCounts() : null;
  if (evalCounts) {
    alarmN = evalCounts.alarms ?? alarmN;
    collN = evalCounts.collisions ?? collN;
    if (evalCounts.verified > 0) verifiedN = evalCounts.verified;
    accuracyHint = ` · 漏报段 ${evalCounts.missed_segments ?? 0} · 误报 ${evalCounts.false_alarms ?? 0}`;
    if (evalCounts.sourceLabel) {
      sourceHint = ` · 来源：${evalCounts.sourceLabel}`;
    }
  } else if (typeof countPlaybackMissEvents === "function") {
    const missN = countPlaybackMissEvents();
    const falseN = countPlaybackFalseAlarmEvents();
    if (missN > 0 || falseN > 0) {
      accuracyHint = ` · 漏报段 ${missN} · 误报 ${falseN}`;
    }
  }

  eventCountLabel.textContent = `帧 ${playbackEvents.length} · 告警 ${alarmN} · 碰撞 ${collN} · 标真 ${verifiedN}${accuracyHint}${sourceHint}${rtHint}${filterHint}`;
}

function syncVerifiedKeysFromEvents(events, reviewPayload = null) {
  verifiedTrueKeys.clear();
  const reviewList = reviewPayload?.verified_true;
  if (Array.isArray(reviewList)) {
    const eventsByFrame = new Map();
    (events || []).forEach((ev) => {
      ev.verified_true = false;
      const frameIdx = parseInt(ev?.frame_idx, 10) || 0;
      if (!eventsByFrame.has(frameIdx)) eventsByFrame.set(frameIdx, []);
      eventsByFrame.get(frameIdx).push(ev);
    });
    for (const item of reviewList) {
      if (!item || typeof item !== "object") continue;
      verifiedTrueKeys.add(eventRowKey(item));
      const frameIdx = parseInt(item.frame_idx, 10) || 0;
      for (const ev of eventsByFrame.get(frameIdx) || []) {
        verifiedTrueKeys.add(eventRowKey(ev));
        ev.verified_true = true;
      }
    }
  } else {
    (events || []).forEach((ev) => {
      if (ev?.verified_true) verifiedTrueKeys.add(eventRowKey(ev));
    });
  }
  syncConfirmedBoxFromReview(reviewPayload, events);
}

function applyVerifiedFlagsToEvents() {
  playbackEvents.forEach((ev) => {
    ev.verified_true = !!(ev.verified_true || isEventVerified(ev));
  });
}

function setEventVerified(ev, verified) {
  const key = eventRowKey(ev);
  if (verified) {
    verifiedTrueKeys.add(key);
    if (pendingReviewBindingsByKey.has(key)) {
      const bindings = normalizeReviewBindings(
        pendingReviewBindingsByKey.get(key)
      );
      if (bindings.length) {
        ev.bindings = bindings.map((binding) => ({
          ...binding,
          confirmed_box_tokens: [...binding.confirmed_box_tokens],
        }));
        ev.confirmed_box_tokens = unionReviewBindingBoxes(bindings);
        if (bindings.length === 1 && bindings[0].person_id != null) {
          ev.person_id = bindings[0].person_id;
        } else {
          delete ev.person_id;
        }
      }
      pendingReviewBindingsByKey.delete(key);
      pendingConfirmedBoxesByKey.delete(key);
    }
    applyAutoConfirmedBoxOnVerify(ev);
    applyAutoPersonIdOnVerify(ev);
    if (pendingPersonIdByKey.has(key)) {
      const bindings = getEventEffectiveBindings(ev);
      if (bindings.length <= 1) {
        setEventPersonId(ev, pendingPersonIdByKey.get(key), { commitToEvent: true });
      } else {
        pendingPersonIdByKey.delete(key);
        delete ev.person_id;
      }
    }
  } else {
    verifiedTrueKeys.delete(key);
    delete ev.confirmed_box_tokens;
    delete ev.confirmed_box_token;
    delete ev.person_id;
    delete ev.bindings;
    pendingConfirmedBoxesByKey.delete(key);
    pendingReviewBindingsByKey.delete(key);
    pendingPersonIdByKey.delete(key);
    boxAnnotationTouchedKeys.delete(key);
    personIdTouchedKeys.delete(key);
  }
  ev.verified_true = !!verified;
  if (typeof refreshPlaybackEventVerificationIndex === "function") {
    refreshPlaybackEventVerificationIndex(ev);
  }
  if (!verified && isReviewTerminalStatus(currentEventReviewStatus)) {
    currentEventReviewStatus = "in_progress";
    patchPlaybackRecordReviewStatus(currentRecordId, "in_progress", "复核中");
  }
  refreshEventCountLabel();
}

async function flushSaveEventReview() {
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  await saveEventReviewNow();
}

function setEventReviewSaveStatus(text, kind = "") {
  const el = $("#event-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = `event-save-status hint${kind ? ` is-${kind}` : ""}`;
}

function scheduleSaveEventReview() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("仅已保存记录可写入复核", "error");
    return;
  }
  setEventReviewSaveStatus("保存中…", "pending");
  if (eventReviewSaveTimer) clearTimeout(eventReviewSaveTimer);
  eventReviewSaveTimer = setTimeout(() => void saveEventReviewNow(), 450);
}

function buildVerifiedTruePayload() {
  return playbackEvents.filter((e) => isEventVerified(e)).map((e) => eventToReviewPayload(e));
}

function applyEventReviewResponse(body, seq, forRecordId = currentRecordId, options = {}) {
  if (seq !== eventReviewSaveSeq) return false;
  if (typeof clearEventReviewRetryAction === "function") {
    clearEventReviewRetryAction();
  }
  const savedFor = String(body?.record_id || forRecordId || "").trim();
  const applyUi = !!savedFor && savedFor === currentRecordId;

  if (applyUi && Array.isArray(body.events)) {
    const prevKey = activeEventKey;
    playbackEvents = body.events;
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
    if (prevKey && playbackEvents.some((e) => eventRowKey(e) === prevKey)) {
      activeEventKey = prevKey;
    }
  } else if (applyUi && (body.light || body.event_review)) {
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
  }

  if (applyUi && !options.skipAutoConfirmBoxes) {
    playbackEvents.forEach((ev) => {
      if (isEventVerified(ev)) {
        applyAutoConfirmedBoxOnVerify(ev);
        applyAutoPersonIdOnVerify(ev);
      }
    });
  }

  // 磁盘状态刚变，草稿要在这里对账：否则区间标真与按 Y 全量写入留下的草稿
  // 永远消化不掉，「有未保存修改」会一直挂着。
  if (applyUi) pruneSettledEventReviewDrafts();

  if (savedFor) {
    const st =
      body.event_review_status ||
      body.event_review?.status ||
      (body.event_review?.verified_true?.length || body.event_review?.updated_at
        ? "in_progress"
        : null);
    if (st) {
      applyEventReviewPatchFromBody(body, savedFor);
    }
  }

  if (!applyUi) {
    return true;
  }

  currentEventReviewStatus =
    body.event_review_status || body.event_review?.status || currentEventReviewStatus || "in_progress";
  const n =
    typeof body.verified_true_count === "number" ? body.verified_true_count : countVerifiedEvents();
  setEventReviewSaveStatus(options.statusMessage || `已保存 · 标真 ${n} 条`, "saved");
  refreshEventCountLabel();
  updateReviewDock();
  if (!options.skipTable && $("#event-review-list-details")?.open) {
    if (options.patchTableOnly) patchEventReviewTableVerifiedStates();
    else renderEventReviewTable();
  }
  if (!options.skipMarkers) {
    if (options.patchMarkersOnly) patchEventMarkersVerifiedStates();
    else renderEventMarkers();
  }
  if (applyUi && typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  return true;
}

/** 单条标真/取消：服务端 toggle，避免全量 verified_true 覆盖误删 */
async function persistEventReviewConfirmedBoxes(ev, confirmedBoxTokens) {
  const recordId = currentRecordId;
  if (!recordId || !ev) return false;
  const tokens = normalizeBoxTokenList(confirmedBoxTokens);
  const eventPayload = eventToReviewPayload(ev);
  delete eventPayload.confirmed_box_tokens;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    const showUi = recordId === currentRecordId;
    if (showUi) setEventReviewSaveStatus("保存货框确认…", "pending");
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "set_confirmed_box",
          event: eventPayload,
          confirmed_box_tokens: tokens,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      const msg = tokens.length
        ? `已确认货框 ${formatConfirmedBoxes(tokens)}`
        : "已清空货框标注";
      return applyEventReviewResponse(body, seq, recordId, {
        statusMessage: msg,
        skipAutoConfirmBoxes: true,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        if (typeof setEventReviewRetryAction === "function") {
          setEventReviewRetryAction(
            () => persistEventReviewConfirmedBoxes(ev, tokens),
            "重试货框保存"
          );
        }
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function setConfirmedBoxesForEvent(ev, tokens) {
  if (!ev) return;
  const list = normalizeBoxTokenList(tokens);
  setEventConfirmedBoxes(ev, list);
  updateReviewDock();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  redrawCurrentFrame();
  if (typeof updateRangeAnnotTemplateBoxesFromEvent === "function") {
    updateRangeAnnotTemplateBoxesFromEvent(ev, list);
  }
  const detN = normalizeBoxTokenList(ev.box_tokens).length;
  setEventReviewSaveStatus(
    `货框 ${formatConfirmedBoxes(list) || "（无）"}${buildBoxPickStatusHint(ev, list, detN)}`,
    ""
  );
}

async function setPersonIdForEvent(ev, personId) {
  if (!ev) return;
  const ids = getFramePersonIds(ev.frame_idx);
  const normalized =
    personId == null || personId === "" ? null : Number.isFinite(Number(personId)) ? Number(personId) : null;
  if (normalized != null && ids.length && !ids.includes(normalized)) {
    setEventReviewSaveStatus(`person_id ${normalized} 不在当前帧画面中`, "error");
    return;
  }
  if (typeof recordEventReviewUndo === "function") {
    recordEventReviewUndo("人物选择");
  }
  setEventPersonId(ev, normalized);
  const rangePersonConfirmed =
    normalized != null &&
    typeof confirmRangePersonSelection === "function" &&
    confirmRangePersonSelection(ev, normalized);
  updateReviewDock();
  redrawCurrentFrame();
  if (typeof updateRangeAnnotTemplatePersonFromEvent === "function") {
    updateRangeAnnotTemplatePersonFromEvent(ev, normalized);
  }
  const pendingNote = hasPendingPersonIdAnnotation(ev) ? " · 暂选未落盘，按 Y 写入" : "";
  setEventReviewSaveStatus(
    rangePersonConfirmed
      ? `已确认变化点帧 ${parseInt(ev.frame_idx, 10) || 0} 为 P${normalized} · 正在定位下一处…`
      : normalized != null
      ? `已选 person_id ${normalized}${pendingNote}`
      : `已清空 person_id${pendingNote}`,
    ""
  );
  if (
    rangePersonConfirmed &&
    typeof continueRangeIdentityReviewAfterSelection === "function"
  ) {
    continueRangeIdentityReviewAfterSelection();
  }
}

async function resetActiveEventPersonAnnotation() {
  const ev = getPinnedPlaybackEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先选择一条事件", "");
    return;
  }
  setEventPersonId(ev, null);
  if (typeof updateRangeAnnotTemplatePersonFromEvent === "function") {
    updateRangeAnnotTemplatePersonFromEvent(ev, null);
  }
  updateReviewDock();
  redrawCurrentFrame();
  setEventReviewSaveStatus("已重置 person_id 暂选，按 Y 标为真后写入 event_review", "");
}

async function toggleConfirmedBoxForEvent(ev, token) {
  if (!ev || !token) return;
  const hit = String(token).trim();
  if (!isAnnotationBoxToken(hit)) {
    setEventReviewSaveStatus(`货框 ${hit} 不在标注列表中`, "error");
    return;
  }
  const canonical = canonicalBoxToken(hit);
  const current = getEventConfirmedBoxes(ev);
  const hasToken = current.some((t) => canonicalBoxToken(t) === canonical);
  const next = hasToken
    ? current.filter((t) => canonicalBoxToken(t) !== canonical)
    : [...current, hit];
  if (typeof recordEventReviewUndo === "function") {
    recordEventReviewUndo(hasToken ? "解除货框配对" : "绑定货框");
  }
  if (isEventVerified(ev) && currentRecordId) {
    setEventConfirmedBoxes(ev, next, { commitToEvent: true });
    updateReviewDock();
    if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
    redrawCurrentFrame();
    if (typeof updateRangeAnnotTemplateBoxesFromEvent === "function") {
      updateRangeAnnotTemplateBoxesFromEvent(ev, next);
    }
    await persistEventReviewConfirmedBoxes(ev, next);
    return;
  }
  await setConfirmedBoxesForEvent(ev, next);
}

async function resetActiveEventBoxAnnotation() {
  const ev = getPinnedPlaybackEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先选择一条事件", "");
    return;
  }
  setEventConfirmedBoxes(ev, []);
  if (typeof updateRangeAnnotTemplateBoxesFromEvent === "function") {
    updateRangeAnnotTemplateBoxesFromEvent(ev, []);
  }
  updateReviewDock();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  redrawCurrentFrame();
  setEventReviewSaveStatus("已重置 box 暂选，按 Y 标为真后写入 event_review", "");
}

/** 切换事件时清除上一条的货框点选提示/错误（保存中状态保留） */
let eventReviewStatusEventKey = null;

function clearEventReviewPickStatusOnEventChange() {
  const key = activeEventKey || "";
  if (key === eventReviewStatusEventKey) return;
  const prevKey = eventReviewStatusEventKey;
  eventReviewStatusEventKey = key;
  if (prevKey) {
    // 草稿按事件 key 保留；切换事件后再次回来仍可继续，避免静默吞掉标注。
    if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  }
  const el = $("#event-save-status");
  if (el?.classList.contains("is-pending")) return;
  setEventReviewSaveStatus("");
}

/** @deprecated 兼容旧调用 */
async function persistEventReviewConfirmedBox(ev, confirmedBoxToken) {
  const token = String(confirmedBoxToken || "").trim();
  if (!token) return persistEventReviewConfirmedBoxes(ev, []);
  const merged = normalizeBoxTokenList([...getEventConfirmedBoxes(ev), token]);
  return persistEventReviewConfirmedBoxes(ev, merged);
}

/** @deprecated 兼容旧调用 */
async function selectConfirmedBoxForEvent(ev, token) {
  await toggleConfirmedBoxForEvent(ev, token);
}

async function persistEventReviewToggle(ev, wantVerified, eventPayloadOverride = null) {
  const recordId = currentRecordId;
  if (!recordId || !ev) return false;
  const eventPayload = eventPayloadOverride || eventToReviewPayload(ev);
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    const showUi = recordId === currentRecordId;
    if (showUi) {
      setEventReviewSaveStatus(`标真 ${countVerifiedEvents()} 条 · 保存中…`, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "toggle",
          event: eventPayload,
          verified_true: !!wantVerified,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId);
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        if (typeof setEventReviewRetryAction === "function") {
          setEventReviewRetryAction(
            () => persistEventReviewToggle(ev, wantVerified, eventPayload),
            "重试标真保存"
          );
        }
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function persistEventReviewVerifiedList(verified_true, statusMessage = "保存中…") {
  const recordId = currentRecordId;
  if (!recordId) return false;
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
          verified_true,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId);
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        if (typeof setEventReviewRetryAction === "function") {
          setEventReviewRetryAction(
            () => persistEventReviewVerifiedList(verified_true, statusMessage),
            "重试保存"
          );
        }
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

/** 全部标真/取消：轻量 PATCH，服务端构建 verified_true，响应不含 events */
async function persistEventReviewBulkAll(markAll, statusMessage) {
  const recordId = currentRecordId;
  if (!recordId) return false;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  const doneMessage = markAll ? `已全部标真 · 共 ${eventTotal} 条` : "已取消全部标真";
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus(statusMessage, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "set_all_verified",
          mark_all: !!markAll,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId, {
        patchTableOnly: true,
        patchMarkersOnly: true,
        statusMessage: doneMessage,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        if (typeof setEventReviewRetryAction === "function") {
          setEventReviewRetryAction(
            () => persistEventReviewBulkAll(markAll, statusMessage),
            "重试批量保存"
          );
        }
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function saveEventReviewNow() {
  const verified_true = buildVerifiedTruePayload();
  await persistEventReviewVerifiedList(verified_true);
}

function restoreVerifiedSnapshot(snapshot) {
  verifiedTrueKeys.clear();
  snapshot.forEach((key) => verifiedTrueKeys.add(key));
  applyVerifiedFlagsToEvents();
}

async function markAllEventsVerified(verified) {
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  const total = playbackEvents.length;
  if (!total) {
    setEventReviewSaveStatus("无事件可操作", "");
    return;
  }

  const verifiedN = countVerifiedEvents();
  if (verified) {
    if (verifiedN >= total) {
      setEventReviewSaveStatus("全部事件已标真", "");
      return;
    }
    if (
      !window.confirm(
        `确定将本记录全部 ${total} 条事件标为真？\n\n确认后将一次性写入服务端；之后仍可用「全部取消标真」或逐条调整。`
      )
    ) {
      return;
    }
  } else {
    if (verifiedN <= 0) {
      setEventReviewSaveStatus("暂无已标真事件", "");
      return;
    }
    if (
      !window.confirm(
        `确定取消本记录全部 ${verifiedN} 条已标真？\n\n确认后将清空标真状态，需重新标真。`
      )
    ) {
      return;
    }
  }

  const snapshot = new Set(verifiedTrueKeys);
  if (typeof recordEventReviewUndo === "function") {
    recordEventReviewUndo(verified ? "全部标真" : "全部取消标真");
  }
  playbackEvents.forEach((ev) => setEventVerified(ev, verified));
  updateReviewDock();
  patchEventReviewVerifiedUi();

  const statusMessage = verified ? `全部标真 ${total} 条 · 保存中…` : "取消全部标真 · 保存中…";
  const ok = await persistEventReviewBulkAll(verified, statusMessage);
  if (!ok) {
    restoreVerifiedSnapshot(snapshot);
    updateReviewDock();
    patchEventReviewVerifiedUi();
  }
}

async function markEventReviewCompleted() {
  const recordId = currentRecordId;
  if (!recordId) {
    setEventReviewSaveStatus("请从记录列表打开回放后再完成复核", "error");
    return;
  }
  const verified_true = buildVerifiedTruePayload();
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus("正在标记已复核…", "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: "completed",
          event_total: eventTotal,
          verified_true,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `操作失败 (${res.status})`);
      }
      const body = await res.json();
      applyEventReviewResponse(body, seq, recordId);
      if (recordId === currentRecordId) {
        currentEventReviewStatus = body.event_review_status || "completed";
        setEventReviewSaveStatus("已标记为复核完成");
        updateReviewDock();
      }
    } catch (err) {
      setEventReviewSaveStatus(err.message || "操作失败", "error");
    }
  });
}

function filteredPlaybackEvents() {
  const mode = eventFilterSelect?.value || "all";
  if (mode === "all") return playbackEvents;
  if (
    ["verified", "unreviewed", "needs_box", "alarm", "collision"].includes(mode) &&
    filteredPlaybackEventsCache.mode === mode &&
    filteredPlaybackEventsCache.version === playbackEventsIndexVersion
  ) {
    return filteredPlaybackEventsCache.list;
  }
  if (["verified", "unreviewed", "needs_box", "alarm", "collision"].includes(mode)) {
    let list = [];
    if (mode === "verified") list = playbackEvents.filter((e) => isEventVerified(e));
    else if (mode === "unreviewed") list = playbackEvents.filter((e) => !isEventVerified(e));
    else if (mode === "needs_box") {
      list = playbackEvents.filter(
        (e) => isEventVerified(e) && !getEventConfirmedBoxes(e).length
      );
    } else {
      list = playbackEvents.filter((e) => e.event_type === mode);
    }
    list.sort(
      (a, b) =>
        (Number(a.timestamp_sec) || 0) - (Number(b.timestamp_sec) || 0) ||
        (Number(a.frame_idx) || 0) - (Number(b.frame_idx) || 0)
    );
    filteredPlaybackEventsCache = {
      mode,
      version: playbackEventsIndexVersion,
      list,
      set: new Set(list),
      positions: new Map(list.map((ev, idx) => [eventRowKey(ev), idx])),
    };
    return list;
  }
  if (mode === "miss") {
    if (
      typeof externalPlaybackAccuracyOverlay !== "undefined" &&
      externalPlaybackAccuracyOverlay &&
      typeof buildMissSegmentQueueEvents === "function"
    ) {
      const fromOverlay = buildMissSegmentQueueEvents();
      if (fromOverlay.length) return fromOverlay;
    }
    const matched =
      typeof isPlaybackEventInMissSegment === "function"
        ? playbackEvents.filter((e) => isPlaybackEventInMissSegment(e))
        : typeof isPlaybackEventMiss === "function"
          ? playbackEvents.filter((e) => isPlaybackEventMiss(e))
          : [];
    if (matched.length) return matched;
    // 无时间线事件命中时，按漏报段起点生成可导航占位事件
    const missSegments =
      typeof getAccuracyGroundTruthSegments === "function"
        ? getAccuracyGroundTruthSegments().filter((seg) => !seg.detected)
        : [];
    if (missSegments.length) {
      return missSegments
        .map((seg) => {
          const fi = Number(seg.frame_start) || 0;
          const existing = playbackEvents.find((e) => (parseInt(e.frame_idx, 10) || 0) === fi);
          if (existing) return existing;
          const row = frameByTime?.find((r) => Number(r.frameIdx) === fi);
          return {
            event_type: "alarm",
            frame_idx: fi,
            timestamp_sec: row?.t ?? (fi - 1) / (Number(poseData?.fps) || 25),
            box_tokens: seg.tokens || [],
          };
        })
        .filter((e) => (parseInt(e.frame_idx, 10) || 0) > 0);
    }
    return matched;
  }
  if (mode === "false_alarm") {
    if (
      typeof externalPlaybackAccuracyOverlay !== "undefined" &&
      externalPlaybackAccuracyOverlay?.allAlarms?.length &&
      typeof buildFalseAlarmQueueEvents === "function"
    ) {
      const fromOverlay = buildFalseAlarmQueueEvents();
      if (fromOverlay.length) return fromOverlay;
    }
    return typeof isPlaybackEventFalseAlarm === "function"
      ? playbackEvents.filter((e) => isPlaybackEventFalseAlarm(e))
      : [];
  }
  return playbackEvents;
}

function getActiveFilteredEvent() {
  const list = filteredPlaybackEvents();
  if (!list.length) return null;
  if (!activeEventKey) return list[0];
  if (list === playbackEvents) return playbackEventsByKey.get(activeEventKey) ?? null;
  if (list === filteredPlaybackEventsCache.list) {
    const idx = filteredPlaybackEventsCache.positions.get(activeEventKey);
    return idx == null ? null : list[idx] ?? null;
  }
  return list.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

/** 钉住/复核当前事件：完整列表优先，误报/漏报队列占位事件回落到筛选列表 */
function getPinnedPlaybackEvent() {
  return getActiveEvent() ?? getActiveFilteredEvent();
}

function getActiveFilteredIndex() {
  const list = filteredPlaybackEvents();
  if (!list.length) return -1;
  const ev = getActiveFilteredEvent();
  if (!ev) return -1;
  const key = eventRowKey(ev);
  if (list === playbackEvents) return playbackEventPositionByKey.get(key) ?? -1;
  if (list === filteredPlaybackEventsCache.list) {
    return filteredPlaybackEventsCache.positions.get(key) ?? -1;
  }
  return list.findIndex((e) => eventRowKey(e) === key);
}

function getActiveGlobalIndex() {
  if (!playbackEvents.length) return -1;
  if (!activeEventKey) return 0;
  const idx = playbackEventPositionByKey.get(activeEventKey) ?? -1;
  return idx >= 0 ? idx : 0;
}

function globalIndexForEventKey(key) {
  if (!key || !playbackEvents.length) return -1;
  return playbackEventPositionByKey.get(key) ?? -1;
}

/** 按时间线全局顺序切换事件（不受筛选队列影响） */
function navigateReviewEventGlobal(delta, baseGlobalIdx = null) {
  if (!playbackEvents.length) return;
  const cur = baseGlobalIdx != null ? baseGlobalIdx : getActiveGlobalIndex();
  const idx = Math.max(0, Math.min(playbackEvents.length - 1, cur + delta));
  reviewBackKey = null;
  void seekToEvent(playbackEvents[idx]);
}

function navigateReviewEvent(delta) {
  if (!playbackEvents.length) return;

  const cur = getActiveEvent();
  const curKey = cur ? eventRowKey(cur) : "";

  // 标真并下一条后，上一条优先回到刚标真的事件（该事件已不在「未标真」队列）
  if (delta < 0 && reviewBackKey && curKey && curKey !== reviewBackKey) {
    const backEv = playbackEvents.find((e) => eventRowKey(e) === reviewBackKey);
    if (backEv) {
      void seekToEvent(backEv);
      return;
    }
  }

  const filterMode = eventFilterSelect?.value || "all";
  const useFiltered = filterMode !== "all";
  const list = useFiltered ? filteredPlaybackEvents() : playbackEvents;
  if (!list.length) return;

  let idx;
  if (useFiltered) {
    idx = getActiveFilteredIndex();
    if (idx < 0 && curKey) {
      idx = list.findIndex((e) => eventRowKey(e) === curKey);
    }
    if (idx < 0) {
      const globalIdx = globalIndexForEventKey(curKey);
      if (globalIdx < 0) {
        idx = delta > 0 ? 0 : list.length - 1;
      } else if (delta > 0) {
        const next = list.find((e) => globalIndexForEventKey(eventRowKey(e)) > globalIdx);
        reviewBackKey = null;
        if (next) void seekToEvent(next);
        return;
      } else {
        let prev = null;
        for (let i = list.length - 1; i >= 0; i -= 1) {
          if (globalIndexForEventKey(eventRowKey(list[i])) < globalIdx) {
            prev = list[i];
            break;
          }
        }
        reviewBackKey = null;
        if (prev) void seekToEvent(prev);
        return;
      }
    }
    const nextIdx = idx + delta;
    if (nextIdx < 0 || nextIdx >= list.length) return;
    reviewBackKey = null;
    void seekToEvent(list[nextIdx]);
    return;
  } else {
    idx = getActiveGlobalIndex();
    if (idx < 0) idx = 0;
    idx = Math.max(0, Math.min(playbackEvents.length - 1, idx + delta));
  }

  reviewBackKey = null;
  void seekToEvent(playbackEvents[idx]);
}

function scrollActiveEventRowIntoView() {
  if (!eventJumpList || !activeEventKey) return;
  const row = eventJumpList.querySelector(`tr[data-event-key="${CSS.escape(activeEventKey)}"]`);
  if (row) {
    row.scrollIntoView({ block: "nearest" });
    return;
  }
  const idx = eventReviewVirtualList.findIndex((ev) => eventRowKey(ev) === activeEventKey);
  const wrap = $("#event-review-list-scroll");
  if (idx >= 0 && wrap) {
    wrap.scrollTop = idx * EVENT_REVIEW_VIRTUAL_ROW_HEIGHT;
    renderEventReviewTable(eventReviewVirtualList, { fromScroll: true });
  }
}

/** 播放中轻量更新事件表高亮行，避免整表重绘 */
function patchEventReviewTableActiveState() {
  if (!eventJumpList) return;
  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    row.classList.toggle("active", row.dataset.eventKey === activeEventKey);
  });
}

/** 播放中更新右侧 meta 中的画面帧号 */
function updatePlaybackReviewFrameMeta(frameIdx = null) {
  const metaEl = $("#event-review-meta");
  const ev = getPinnedPlaybackEvent();
  if (!metaEl || !ev) return;
  const eventFi = parseInt(ev.frame_idx, 10) || 0;
  const playbackFi =
    frameIdx != null && Number(frameIdx) > 0
      ? Number(frameIdx)
      : typeof getResolvedPlaybackFrameIdx === "function"
        ? getResolvedPlaybackFrameIdx()
        : null;
  let accuracyNote = "";
  if (typeof isPlaybackEventFalseAlarm === "function" && isPlaybackEventFalseAlarm(ev)) {
    accuracyNote = " · 误报";
  } else if (
    (typeof isPlaybackEventInMissSegment === "function" && isPlaybackEventInMissSegment(ev)) ||
    (typeof isPlaybackEventMiss === "function" && isPlaybackEventMiss(ev))
  ) {
    accuracyNote = " · 漏报段内";
  }
  const frameNote =
    playbackFi && eventFi && playbackFi !== eventFi
      ? `画面 帧 ${playbackFi} · 事件 帧 ${eventFi}`
      : playbackFi
        ? `帧 ${playbackFi}`
        : `帧 ${eventFi}`;
  metaEl.textContent = `${formatTime(ev.timestamp_sec)} · ${frameNote}${accuracyNote}`;
}

/** 播放中更新「第 N / M 条」位置文案 */
function updatePlaybackReviewPositionUi({ linkNearest = false } = {}) {
  const posEl = $("#event-review-position");
  const ev = getPinnedPlaybackEvent();
  if (!posEl || !ev || !playbackEvents.length) return;
  const list = filteredPlaybackEvents();
  const key = eventRowKey(ev);
  const globalIdx = playbackEventPositionByKey.get(key) ?? -1;
  const filteredIdx =
    list === playbackEvents
      ? globalIdx
      : list === filteredPlaybackEventsCache.list
        ? filteredPlaybackEventsCache.positions.get(key) ?? -1
        : list.findIndex((item) => eventRowKey(item) === key);
  const evInFilter = filteredIdx >= 0;
  const globalNote =
    globalIdx >= 0 ? ` · 总序 ${globalIdx + 1}/${playbackEvents.length}` : "";
  const linkNote = linkNearest || !playbackEventLinkExact ? " · 最近" : "";
  if (evInFilter) {
    posEl.textContent = `第 ${filteredIdx + 1} / ${list.length} 条${linkNote}${list.length !== playbackEvents.length ? `（队列）${globalNote}` : globalNote}`;
  } else {
    posEl.textContent = `已标真 / 不在当前队列${linkNote}${globalNote}`;
  }
}

/** 按右栏面板剩余高度限制「全部事件列表」滚动区（details 无法可靠参与 flex 限高） */
let eventReviewListScrollSyncRaf = 0;

function syncEventReviewListScrollHeight() {
  const panel = eventsPanel;
  const details = $("#event-review-list-details");
  const wrap = $("#event-review-list-scroll");
  if (!wrap) return;

  if (!panel || panel.classList.contains("hidden") || !details?.open) {
    wrap.style.removeProperty("max-height");
    wrap.style.removeProperty("overflow-y");
    return;
  }

  const panelRect = panel.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const available = panelRect.bottom - wrapRect.top - 8;
  wrap.style.maxHeight = `${Math.max(120, Math.floor(available))}px`;
  wrap.style.overflowY = "auto";
}

function scheduleEventReviewListScrollHeight() {
  if (eventReviewListScrollSyncRaf) {
    cancelAnimationFrame(eventReviewListScrollSyncRaf);
  }
  eventReviewListScrollSyncRaf = requestAnimationFrame(() => {
    eventReviewListScrollSyncRaf = requestAnimationFrame(() => {
      eventReviewListScrollSyncRaf = 0;
      syncEventReviewListScrollHeight();
    });
  });
}

function bindEventReviewListScrollSync() {
  const panel = eventsPanel;
  const details = $("#event-review-list-details");
  if (!panel || !details || details.dataset.scrollSyncBound) return;
  details.dataset.scrollSyncBound = "1";

  details.addEventListener("toggle", () => scheduleEventReviewListScrollHeight());
  window.addEventListener("resize", () => scheduleEventReviewListScrollHeight());

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => scheduleEventReviewListScrollHeight());
    ro.observe(panel);
    const dock = $("#event-review-dock");
    if (dock) ro.observe(dock);
  }
}

function updateReviewDock(options = {}) {
  clearEventReviewPickStatusOnEventChange();
  const list = filteredPlaybackEvents();
  const ev = getPinnedPlaybackEvent();
  const evKey = ev ? eventRowKey(ev) : "";
  const globalIdx = evKey ? playbackEventPositionByKey.get(evKey) ?? -1 : -1;
  const filteredIdx =
    !evKey
      ? -1
      : list === playbackEvents
        ? globalIdx
        : list === filteredPlaybackEventsCache.list
          ? filteredPlaybackEventsCache.positions.get(evKey) ?? -1
          : list.findIndex((item) => eventRowKey(item) === evKey);
  const evInFilter = filteredIdx >= 0;
  const posEl = $("#event-review-position");
  const badgeEl = $("#event-review-badge");
  const metaEl = $("#event-review-meta");
  const tokensEl = $("#event-review-tokens");
  const verifiedTag = $("#event-review-verified-tag");
  const summaryEl = $("#event-review-list-summary");
  const verifiedN = countVerifiedEvents();
  refreshEventCountLabel();

  if (summaryEl) {
    const reviewNote =
      currentEventReviewStatus === "completed"
        ? " · 记录已复核"
        : currentEventReviewStatus === "no_collision"
          ? " · 无碰撞"
          : currentEventReviewStatus === "in_progress"
            ? " · 复核中"
            : "";
    summaryEl.textContent = `全部事件列表（${playbackEvents.length} 条，已标真 ${verifiedN}${reviewNote}）`;
  }

  const markAllBtn = $("#event-mark-all-true-btn");
  const unmarkAllBtn = $("#event-unmark-all-btn");
  const totalEvents = playbackEvents.length;
  const verifiedCount = countVerifiedEvents();
  const canBulkSave = !!currentRecordId && totalEvents > 0;
  if (markAllBtn) {
    markAllBtn.disabled = !canBulkSave || verifiedCount >= totalEvents;
  }
  if (unmarkAllBtn) {
    unmarkAllBtn.disabled = !canBulkSave || verifiedCount <= 0;
  }

  const completeBtn = $("#event-review-complete-btn");
  if (completeBtn) {
    const reviewDone = isReviewTerminalStatus(currentEventReviewStatus);
    completeBtn.disabled = reviewDone || !currentRecordId;
    completeBtn.classList.toggle("is-done", reviewDone);
    if (currentEventReviewStatus === "no_collision") {
      completeBtn.textContent = "无碰撞（已复核）";
    } else {
      completeBtn.textContent = reviewDone ? "✓ 已复核完成" : "标记复核完成";
    }
  }

  if (!playbackEvents.length) {
    if (posEl) {
      posEl.textContent = isReviewTerminalStatus(currentEventReviewStatus) ? "无碰撞事件" : "无事件";
    }
    if (metaEl) {
      metaEl.textContent = isReviewTerminalStatus(currentEventReviewStatus)
        ? "无需人工复核"
        : "—";
    }
    if (tokensEl) {
      tokensEl.textContent = "\u00a0";
      tokensEl.setAttribute("aria-hidden", "true");
    }
    verifiedTag?.classList.add("hidden");
    finishUpdateReviewDock();
    return;
  }

  if (!list.length) {
    if (posEl) posEl.textContent = "队列已清空";
    const mode = eventFilterSelect?.value || "all";
    const emptyHint =
      mode === "miss"
        ? "无漏报段（需有标真范本且段内无匹配告警）"
        : mode === "false_alarm"
          ? "无误报事件（告警均落在标真范本段内）"
          : "当前筛选下无待复核事件";
    if (metaEl) metaEl.textContent = emptyHint;
    if (tokensEl) {
      tokensEl.textContent = "\u00a0";
      tokensEl.setAttribute("aria-hidden", "true");
    }
    verifiedTag?.classList.add("hidden");
    finishUpdateReviewDock();
    return;
  }

  if (posEl) {
    const globalNote =
      globalIdx >= 0 ? ` · 总序 ${globalIdx + 1}/${playbackEvents.length}` : "";
    const linkNote = playbackEventLinkExact ? "" : " · 最近";
    if (evInFilter) {
      posEl.textContent = `第 ${filteredIdx + 1} / ${list.length} 条${linkNote}${list.length !== playbackEvents.length ? `（队列）${globalNote}` : globalNote}`;
    } else {
      posEl.textContent = `已标真 / 不在当前队列${linkNote}${globalNote}`;
    }
  }

  if (!ev) {
    finishUpdateReviewDock();
    return;
  }
  const typeLabel = eventTypeLabel(ev);
  if (badgeEl) {
    badgeEl.textContent = typeLabel;
    badgeEl.className = `event-badge ${ev.event_type}`;
  }
  if (metaEl) {
    let accuracyNote = "";
    if (typeof isPlaybackEventFalseAlarm === "function" && isPlaybackEventFalseAlarm(ev)) {
      accuracyNote = " · 误报";
    } else if (
      (typeof isPlaybackEventInMissSegment === "function" && isPlaybackEventInMissSegment(ev)) ||
      (typeof isPlaybackEventMiss === "function" && isPlaybackEventMiss(ev))
    ) {
      accuracyNote = " · 漏报段内";
    }
    const eventFi = parseInt(ev.frame_idx, 10) || 0;
    const playbackFi =
      typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
    const frameNote =
      playbackFi && eventFi && playbackFi !== eventFi
        ? `画面 帧 ${playbackFi} · 事件 帧 ${eventFi}`
        : `帧 ${eventFi}`;
    metaEl.textContent = `${formatTime(ev.timestamp_sec)} · ${frameNote}${accuracyNote}`;
  }
  if (tokensEl) {
    const tokenText = formatEventTokens(ev.box_tokens);
    const confirmed = getEventConfirmedBoxes(ev);
    const persisted = getEventPersistedConfirmedBoxes(ev);
    let displayText = tokenText;
    if (persisted.length) {
      displayText = `${tokenText} → 已保存 ${formatConfirmedBoxes(persisted)}`;
    }
    if (hasPendingBoxAnnotation(ev)) {
      const pickNote = `暂选 ${formatConfirmedBoxes(confirmed) || "（无）"}`;
      displayText = displayText === tokenText ? pickNote : `${displayText} · ${pickNote}`;
    } else if (!persisted.length && confirmed.length) {
      displayText = `${tokenText} → 已确认 ${formatConfirmedBoxes(confirmed)}`;
    }
    const bindingSummary = formatReviewBindingSummary(ev);
    if (bindingSummary) {
      displayText = `${displayText || tokenText} · 配对：${bindingSummary}`;
    }
    tokensEl.textContent = displayText || "\u00a0";
    tokensEl.setAttribute("aria-hidden", displayText ? "false" : "true");
    if (displayText) tokensEl.title = displayText;
    else tokensEl.removeAttribute("title");
    tokensEl.classList.toggle("is-pending", hasPendingBoxAnnotation(ev));
    tokensEl.classList.toggle("is-confirmed", confirmed.length > 0 && !hasPendingBoxAnnotation(ev));
  }
  if (verifiedTag) {
    verifiedTag.classList.toggle("hidden", !isEventVerified(ev));
  }
  renderEventReviewPersonUi(ev);
  finishUpdateReviewDock(options);
}

function renderEventReviewPersonUi(ev) {
  const wrap = $("#event-review-person-select");
  const optionsEl = $("#event-review-person-options");
  const hintEl = $("#event-review-person-hint");
  const cycleBtn = $("#event-review-person-cycle-btn");
  if (!wrap || !optionsEl || !hintEl) return;

  if (!ev) {
    wrap.classList.add("hidden");
    optionsEl.innerHTML = "";
    hintEl.textContent = "";
    cycleBtn?.classList.add("hidden");
    return;
  }

  const frameIds = getFramePersonIds(ev.frame_idx);
  const rangeConfirmationRequired =
    typeof isRangePersonConfirmationRequired === "function" &&
    isRangePersonConfirmationRequired(ev.frame_idx);
  const selected = rangeConfirmationRequired ? null : getEventPersonId(ev);
  const persisted = getEventPersistedPersonId(ev);

  if (!frameIds.length) {
    wrap.classList.add("hidden");
    optionsEl.innerHTML = "";
    cycleBtn?.classList.add("hidden");
    hintEl.textContent = "当前帧无骨架人员，标真时可不写 person_id";
    return;
  }

  wrap.classList.remove("hidden");
  const key = eventRowKey(ev);
  const stableOptions = getStableReviewPersonOptions(ev);
  cycleBtn?.classList.toggle("hidden", stableOptions.length < 2);
  if (cycleBtn && stableOptions.length >= 2) {
    const currentIndex = stableOptions.findIndex(
      (option) => Number(option.pid) === Number(selected)
    );
    const next = stableOptions[(currentIndex + 1 + stableOptions.length) % stableOptions.length];
    cycleBtn.innerHTML = `→ 人物 ${escapeReviewHtml(next?.stableLabel ?? "A")} <kbd>Alt</kbd>`;
  }
  const effectiveBindings = getEventEffectiveBindings(ev);
  // 单人帧不需要手动点人物卡：这里先把唯一人物勾上，落盘仍由 applyAutoPersonIdOnVerify 兜底，
  // 因此不写 pending 草稿状态，保存出去的字段和以前完全一致。
  const autoSinglePid =
    !rangeConfirmationRequired && frameIds.length === 1 && selected == null ? frameIds[0] : null;
  optionsEl.innerHTML = stableOptions
    .map((stable) => {
      const pid = stable.pid;
      const checked = selected === pid || pid === autoSinglePid ? " checked" : "";
      const assigned = bindingConfirmedBoxesForPerson(effectiveBindings, pid);
      const assignmentClass = assigned.length ? " has-assignment" : "";
      const accentClass = ` person-accent-${reviewPersonAccentIndex(stable.stableId)}`;
      const label = escapeReviewHtml(stable.stableLabel);
      const assignmentHtml = assigned.length
        ? `<span class="event-review-binding-chips">${assigned
            .map(
              (token) =>
                `<button type="button" class="event-review-binding-chip" data-person-id="${pid}" data-box-token="${escapeReviewHtml(token)}" title="解除人物 ${escapeReviewHtml(stable.stableLabel)} 与 ${escapeReviewHtml(token)} 的配对">${escapeReviewHtml(token)} <span aria-hidden="true">×</span></button>`
            )
            .join("")}</span>`
        : `<span class="event-review-person-empty">未配对</span>`;
      return `<label class="event-review-person-option${assignmentClass}${accentClass}" title="人物 ${label} · 本帧原始编号 P${pid}">
        <input type="radio" name="event-person-${CSS.escape(key)}" value="${pid}" data-stable-id="${stable.stableId}"${checked} />
        <span class="event-review-person-badge" aria-hidden="true">${label}</span>
        <span class="event-review-person-text">
          <span class="event-review-person-stable">人物 ${label}</span>
          <small>P${pid}</small>
        </span>
        <span class="event-review-person-assignment">${assignmentHtml}</span>
      </label>`;
    })
    .join("");

  optionsEl.querySelectorAll('input[type="radio"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      void setPersonIdForEvent(ev, Number(input.value));
    });
  });
  optionsEl.querySelectorAll(".event-review-binding-chip").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const personId = Number(button.dataset.personId);
      const token = String(button.dataset.boxToken || "").trim();
      if (!Number.isFinite(personId) || !token) return;
      setEventPersonId(ev, personId);
      void toggleConfirmedBoxForEvent(ev, token);
    });
  });

  const pendingNote = hasPendingPersonIdAnnotation(ev) ? " · 未落盘" : "";
  const savedNote =
    persisted != null && !hasPendingPersonIdAnnotation(ev) ? ` · 已保存 P${persisted}` : "";
  const pairedN = effectiveBindings.filter(
    (binding) => binding.person_id != null && binding.confirmed_box_tokens.length
  ).length;
  const progressEl = $("#event-review-person-progress");
  if (progressEl) {
    progressEl.textContent = frameIds.length >= 2 ? `${pairedN}/${frameIds.length} 已配对` : "";
    progressEl.classList.toggle("is-done", frameIds.length >= 2 && pairedN >= frameIds.length);
  }
  const stepsEl = wrap.querySelector(".event-review-person-steps");
  if (stepsEl) {
    // 三步引导只在需要人工判断时提示，单人帧不占地方。
    stepsEl.classList.toggle("hidden", frameIds.length < 2);
    stepsEl.dataset.step = selected == null ? "1" : pairedN > 0 ? "3" : "2";
  }

  if (rangeConfirmationRequired) {
    const suggested =
      typeof getRangePersonConfirmationSuggestion === "function"
        ? getRangePersonConfirmationSuggestion(ev.frame_idx)
        : null;
    const suggestedInfo =
      suggested != null &&
      typeof getStablePersonDisplayInfoByRawId === "function"
        ? getStablePersonDisplayInfoByRawId(ev.frame_idx, suggested)
        : null;
    hintEl.textContent =
      suggested != null
        ? `身份低置信度 · 建议人物 ${suggestedInfo?.stableLabel ?? Number(suggested) + 1}，按 1/2 确认`
        : "身份低置信度 · 请按 1/2 选择人物";
    hintEl.classList.add("is-required");
  } else if (frameIds.length >= 2) {
    const selectedInfo = stableOptions.find(
      (item) => Number(item.pid) === Number(selected)
    );
    hintEl.textContent = selected != null
      ? `正在为人物 ${selectedInfo?.stableLabel ?? "?"} 指定货框 · 切换人物不会丢失已配对${savedNote}${pendingNote}`
      : `本帧 ${frameIds.length} 人 · 先选人物再点画面货框${pendingNote}`;
    hintEl.classList.toggle("is-required", selected == null);
  } else {
    const only = stableOptions[0];
    const boxDone = getEventConfirmedBoxes(ev).length > 0 || effectiveBindings.length > 0;
    hintEl.textContent = boxDone
      ? `本帧仅人物 ${only?.stableLabel ?? "A"} · 已自动选中，货框已确认，可按 Y${savedNote}${pendingNote}`
      : `本帧仅人物 ${only?.stableLabel ?? "A"} · 已自动选中，点画面货框后按 Y${savedNote}${pendingNote}`;
    hintEl.classList.toggle("is-required", !boxDone);
  }
}

function finishUpdateReviewDock(options = {}) {
  if (typeof updateRangeAnnotUi === "function") updateRangeAnnotUi();
  if (typeof invalidatePlaybackAccuracyOverlay === "function") invalidatePlaybackAccuracyOverlay();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
  if (!options.skipRedraw && typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  const filterMode = eventFilterSelect?.value || "all";
  if (filterMode === "miss" || filterMode === "false_alarm") {
    refreshEventCountLabel();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
  }
  scheduleEventReviewListScrollHeight();
  if (typeof updateEventReviewWorkspaceUi === "function") {
    updateEventReviewWorkspaceUi();
  }
}

function patchEventReviewTableVerifiedStates() {
  if (!eventJumpList) return;
  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    const key = row.dataset.eventKey;
    if (!key) return;
    const isVerified = verifiedTrueKeys.has(key);
    row.classList.toggle("verified-true", isVerified);
    const input = row.querySelector(".event-verify-check");
    if (input) input.checked = isVerified;
  });
}

function patchEventMarkersVerifiedStates() {
  renderEventMarkers();
}

/** During playback update only cheap text/badge state; defer person controls and list scrolling. */
function updatePlaybackReviewUiDuringPlay(frameIdx = null, timeSec = null) {
  const ev = getPinnedPlaybackEvent();
  if (!ev) return;
  const badgeEl = $("#event-review-badge");
  const tokensEl = $("#event-review-tokens");
  const verifiedTag = $("#event-review-verified-tag");
  if (badgeEl) {
    badgeEl.textContent = ev.event_type === "alarm" ? "告警" : "碰撞";
    badgeEl.className = `event-badge ${ev.event_type}`;
  }
  if (tokensEl) {
    const text = formatEventTokens(ev.box_tokens);
    if (tokensEl.textContent !== text) tokensEl.textContent = text || "\u00a0";
  }
  verifiedTag?.classList.toggle("hidden", !isEventVerified(ev));
  updatePlaybackReviewFrameMeta(frameIdx);
  updatePlaybackReviewPositionUi({ linkNearest: true });
  updateEventReviewFrameNavUi();
}

function patchEventReviewVerifiedUi() {
  if ($("#event-review-list-details")?.open) patchEventReviewTableVerifiedStates();
  patchEventMarkersVerifiedStates();
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
}

const EVENT_REVIEW_VIRTUAL_ROW_HEIGHT = 36;
const EVENT_REVIEW_VIRTUAL_WINDOW = 80;
const EVENT_REVIEW_VIRTUAL_OVERSCAN = 12;
let eventReviewVirtualList = [];
let eventReviewVirtualScrollRaf = 0;
let eventReviewVirtualLoggedSize = -1;

function renderEventReviewTable(list = null, options = {}) {
  if (!eventJumpList) return;
  const allRows = list ?? filteredPlaybackEvents();
  if (list != null && typeof resetEventReviewWindow === "function") {
    resetEventReviewWindow();
  }
  const rows =
    typeof eventReviewWindowForRows === "function"
      ? eventReviewWindowForRows(allRows)
      : allRows;
  const canSave = !!currentRecordId;
  eventReviewVirtualList = rows;
  const wrap = $("#event-review-list-scroll");
  let start = 0;
  if (options.fromScroll && wrap) {
    start = Math.max(
      0,
      Math.floor(wrap.scrollTop / EVENT_REVIEW_VIRTUAL_ROW_HEIGHT) -
        EVENT_REVIEW_VIRTUAL_OVERSCAN
    );
  } else if (activeEventKey) {
    const activeIdx = rows.findIndex((ev) => eventRowKey(ev) === activeEventKey);
    if (activeIdx >= 0) start = Math.max(0, activeIdx - Math.floor(EVENT_REVIEW_VIRTUAL_WINDOW / 2));
  }
  start = Math.min(start, Math.max(0, rows.length - EVENT_REVIEW_VIRTUAL_WINDOW));
  const end = Math.min(rows.length, start + EVENT_REVIEW_VIRTUAL_WINDOW);
  const visibleRows = rows.slice(start, end);
  if (!options.fromScroll && eventReviewVirtualLoggedSize !== rows.length) {
    eventReviewVirtualLoggedSize = rows.length;
    if (typeof playbackDebugLog === "function") {
      playbackDebugLog("review-list-virtualized", {
        totalRows: rows.length,
        renderedRows: visibleRows.length,
        windowSize: EVENT_REVIEW_VIRTUAL_WINDOW,
        overscan: EVENT_REVIEW_VIRTUAL_OVERSCAN,
      });
    }
  }
  const spacer = (height) =>
    height > 0
      ? `<tr class="event-review-virtual-spacer" aria-hidden="true"><td colspan="5" style="height:${height}px"></td></tr>`
      : "";

  eventJumpList.innerHTML =
    spacer(start * EVENT_REVIEW_VIRTUAL_ROW_HEIGHT) +
    visibleRows
    .map((ev) => {
      const key = eventRowKey(ev);
      const typeLabel = eventTypeLabel(ev);
      const active = key === activeEventKey ? " active" : "";
      const verified = isEventVerified(ev);
      const verifiedCls = verified ? " verified-true" : "";
      const checked = verified ? " checked" : "";
      const disabled = canSave ? "" : " disabled";
      let accuracyTag = "";
      if (typeof isPlaybackEventFalseAlarm === "function" && isPlaybackEventFalseAlarm(ev)) {
        accuracyTag = '<span class="event-accuracy-tag false-alarm" title="误报">误</span>';
      } else if (
        (typeof isPlaybackEventInMissSegment === "function" && isPlaybackEventInMissSegment(ev)) ||
        (typeof isPlaybackEventMiss === "function" && isPlaybackEventMiss(ev))
      ) {
        accuracyTag = '<span class="event-accuracy-tag miss" title="落在漏报标真段内">漏</span>';
      }
      return `<tr class="event-review-row${active}${verifiedCls}" data-event-key="${key}">
        <td class="col-verify"><input type="checkbox" class="event-verify-check" data-event-key="${key}"${checked}${disabled} aria-label="标为真实碰撞" /></td>
        <td class="col-type"><span class="event-badge ${ev.event_type}">${typeLabel}</span>${accuracyTag}</td>
        <td class="col-time">${formatTime(ev.timestamp_sec)}</td>
        <td class="col-frame">${ev.frame_idx}</td>
        <td class="col-tokens" title="${formatEventTokens(ev.box_tokens)}">${formatEventTokens(ev.box_tokens)}${getEventConfirmedBoxes(ev).length ? ` → ${formatConfirmedBoxes(getEventConfirmedBoxes(ev))}` : ""}</td>
      </tr>`;
    })
    .join("") +
    spacer((rows.length - end) * EVENT_REVIEW_VIRTUAL_ROW_HEIGHT);

  if (wrap && !wrap.dataset.virtualScrollBound) {
    wrap.dataset.virtualScrollBound = "1";
    wrap.addEventListener("scroll", () => {
      if (eventReviewVirtualScrollRaf) return;
      eventReviewVirtualScrollRaf = requestAnimationFrame(() => {
        eventReviewVirtualScrollRaf = 0;
        renderEventReviewTable(eventReviewVirtualList, { fromScroll: true });
      });
    });
  }

  eventJumpList.querySelectorAll(".event-verify-check").forEach((input) => {
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("change", () => {
      void (async () => {
        const key = input.dataset.eventKey;
        const item = playbackEvents.find((row) => eventRowKey(row) === key);
        if (!item || !currentRecordId) return;
        const want = input.checked;
        if (want) {
          const check = validatePersonIdBeforeVerify(item);
          if (!check.ok) {
            input.checked = false;
            setEventReviewSaveStatus(check.message, "error");
            updateReviewDock();
            return;
          }
        }
        if (typeof recordEventReviewUndo === "function") {
          recordEventReviewUndo(want ? "标真事件" : "取消事件标真");
        }
        setEventVerified(item, want);
        updateReviewDock();
        const ok = await persistEventReviewToggle(item, want);
        if (!ok) {
          setEventVerified(item, !want);
          input.checked = !want;
          updateReviewDock();
        }
        renderEventReviewTable();
        renderEventMarkers();
      })();
    });
  });

  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    row.addEventListener("click", () => {
      const key = row.dataset.eventKey;
      const item = playbackEvents.find((rowEv) => eventRowKey(rowEv) === key);
      if (item && typeof selectReviewEventWithoutPlaybackNavigation === "function") {
        selectReviewEventWithoutPlaybackNavigation(item);
      }
    });
  });
  if (!options.fromScroll) scrollActiveEventRowIntoView();
  scheduleEventReviewListScrollHeight();
}

async function markActiveEventVerified(verified) {
  const ev = getActiveEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先在列表或进度条上选择一条事件", "");
    return;
  }
  if (!verified && !isEventVerified(ev)) {
    setEventReviewSaveStatus("当前选中事件未标真", "");
    return;
  }
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  if (verified) {
    const check = validatePersonIdBeforeVerify(ev);
    if (!check.ok) {
      setEventReviewSaveStatus(check.message, "error");
      updateReviewDock();
      return;
    }
    // 与 Y 键同一条规则：算法检测框只是参考，货框必须人工点过才能标真。
    if (!getEventConfirmedBoxes(ev).length && !getEventEffectiveBindings(ev).length) {
      setEventReviewSaveStatus(
        "请先在画面上点击货框确认（虚线为算法检测框，仅供参考），再标真",
        "error"
      );
      updateReviewDock();
      return;
    }
  }
  if (typeof recordEventReviewUndo === "function") {
    recordEventReviewUndo(verified ? "标真当前帧" : "取消当前帧标真");
  }
  setEventVerified(ev, verified);
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  const ok = await persistEventReviewToggle(ev, verified);
  if (!ok) {
    setEventVerified(ev, !verified);
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    return;
  }
  if (!verified && eventRowKey(ev) === reviewBackKey) {
    reviewBackKey = null;
  }
}

/** Y 键乐观推进期间的重入锁：连按时避免两次标真落在同一帧上。 */
let confirmTrueNavigating = false;

async function confirmTrueAndNextFrame() {
  if (confirmTrueNavigating) return;
  const ev = getActiveFilteredEvent();
  if (!ev) return;
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  const check = validatePersonIdBeforeVerify(ev);
  if (!check.ok) {
    setEventReviewSaveStatus(check.message, "error");
    updateReviewDock();
    return;
  }
  // 算法检测框误差较大，不能顶替人工确认：任何类型的事件都要先点选货框再标真。
  const hasConfirmedBox =
    getEventConfirmedBoxes(ev).length > 0 || getEventEffectiveBindings(ev).length > 0;
  if (!hasConfirmedBox) {
    setEventReviewSaveStatus(
      "请先在画面上点击货框确认（虚线为算法检测框，仅供参考），再按 Y 标真",
      "error"
    );
    updateReviewDock();
    return;
  }
  const rowKey = eventRowKey(ev);
  reviewBackKey = rowKey;
  if (typeof recordEventReviewUndo === "function") {
    recordEventReviewUndo("标真当前帧");
  }
  setEventVerified(ev, true);
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();

  // 乐观推进：先跳下一帧，PATCH 在后台串行排队；失败再回滚这一帧并提示重试。
  const savePromise = persistEventReviewToggle(ev, true).then((ok) => {
    if (ok) return true;
    // 保存返回时 playbackEvents 可能已被整体替换，按 key 重新定位这一帧。
    const target = playbackEvents.find((e) => eventRowKey(e) === rowKey) || ev;
    setEventVerified(target, false);
    if (reviewBackKey === rowKey) reviewBackKey = null;
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    setEventReviewSaveStatus(
      `帧 ${target.frame_idx ?? "?"} 标真未保存已回滚，请用「重试标真保存」`,
      "error"
    );
    return false;
  });

  confirmTrueNavigating = true;
  try {
    await navigatePlaybackFrame(1);
  } finally {
    confirmTrueNavigating = false;
  }
  await savePromise;
}

async function unmarkTrueAndNextFrame() {
  const ev = getActiveEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先选择一帧", "");
    return;
  }
  if (isEventVerified(ev)) {
    if (!currentRecordId) {
      setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
      return;
    }
    // 取消会清除前端事件上的货框/person_id；须先保留完整帧身份供 PATCH，
    // 否则无检测框的普通帧会被提交成空 event，后端无法定位要取消的帧。
    const previousPayload = eventToReviewPayload(ev);
    const previousConfirmed = getEventConfirmedBoxes(ev);
    const previousPersonId = getEventPersonId(ev);
    if (typeof recordEventReviewUndo === "function") {
      recordEventReviewUndo("取消当前帧标真");
    }
    setEventVerified(ev, false);
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    const ok = await persistEventReviewToggle(ev, false, previousPayload);
    if (!ok) {
      setEventVerified(ev, true);
      setEventConfirmedBoxes(ev, previousConfirmed, { commitToEvent: true });
      if (previousPersonId != null) {
        setEventPersonId(ev, previousPersonId, { commitToEvent: true });
      }
      updateReviewDock();
      if ($("#event-review-list-details")?.open) renderEventReviewTable();
      renderEventMarkers();
      return;
    }
    reviewBackKey = null;
  } else {
    setEventReviewSaveStatus("本帧保持未标真", "");
  }
  await navigatePlaybackFrame(1);
}

async function skipToNextEvent() {
  navigateReviewEvent(1);
}

async function beginEventReview() {
  if (!playbackEvents.length) return;
  const first = filteredPlaybackEvents()[0];
  if (first && typeof selectReviewEventWithoutPlaybackNavigation === "function") {
    selectReviewEventWithoutPlaybackNavigation(first, { scroll: false });
  }
  else updateReviewDock();
}

/** 时间轴标记按像素桶聚合后的代表事件，供点击与高亮复用。 */
const reviewTimelineBucketEvents = new Map();
/** 事件 key → 所属像素桶，用于 O(1) 定位当前高亮标记。 */
const reviewTimelineBucketByKey = new Map();
/** 每个像素桶占多少 CSS 像素；桶越大 DOM 越少，上千事件也不掉帧。 */
const REVIEW_TIMELINE_BUCKET_PX = 4;

function reviewTimelineTrackWidth() {
  const width = eventMarkersEl?.getBoundingClientRect().width || 0;
  return width > 1 ? width : 640;
}

/** 标记容器只绑一次委托监听，避免每个标记各自挂 listener。 */
function bindReviewTimelineDelegation(container) {
  if (!container || container.dataset.delegated === "1") return;
  container.dataset.delegated = "1";
  container.addEventListener("click", (event) => {
    const dot = event.target.closest("[data-bucket]");
    if (!dot) return;
    event.stopPropagation();
    const ev = reviewTimelineBucketEvents.get(dot.dataset.bucket);
    if (ev && typeof selectReviewEventWithoutPlaybackNavigation === "function") {
      selectReviewEventWithoutPlaybackNavigation(ev);
    }
  });
}

function updateReviewTimelineSummary(total, verified, attention) {
  const el = document.getElementById("review-timeline-summary");
  if (!el) return;
  if (!total) {
    el.textContent = "";
    return;
  }
  // 待确认是罕见的区间跟踪停顿点，为 0 时不占位。
  el.textContent = attention
    ? `${total} 条 · 已标真 ${verified} · 待确认 ${attention}`
    : `${total} 条 · 已标真 ${verified}`;
}

let eventMarkerRenderList = [];
let eventMarkerBinsCache = { key: "", bins: new Map() };
let reviewTimelineResizeRaf = 0;
let reviewTimelineObservedBucketCount = 0;

function reviewTimelineBucketCount() {
  return Math.max(1, Math.ceil(reviewTimelineTrackWidth() / REVIEW_TIMELINE_BUCKET_PX));
}

function ensureReviewTimelineResizeObserver() {
  if (!eventMarkersEl || typeof ResizeObserver === "undefined") return;
  if (eventMarkersEl._eventMarkerResizeObserver) return;
  const observer = new ResizeObserver(() => {
    if (reviewTimelineResizeRaf) return;
    reviewTimelineResizeRaf = requestAnimationFrame(() => {
      reviewTimelineResizeRaf = 0;
      const nextCount = reviewTimelineBucketCount();
      if (nextCount === reviewTimelineObservedBucketCount) return;
      reviewTimelineObservedBucketCount = nextCount;
      eventMarkerBinsCache = { key: "", bins: new Map() };
      renderEventMarkers();
    });
  });
  observer.observe(eventMarkersEl);
  eventMarkersEl._eventMarkerResizeObserver = observer;
}

function timelineBucketState(bucket) {
  let attention = false;
  let attentionCount = 0;
  let verified = false;
  let unreviewed = false;
  for (const ev of bucket?.events || []) {
    const needsAttention =
      typeof eventNeedsIdentityAttention === "function" && eventNeedsIdentityAttention(ev);
    if (needsAttention) {
      attention = true;
      attentionCount += 1;
    }
    else if (isEventVerified(ev)) verified = true;
    else unreviewed = true;
  }
  return { attention, attentionCount, verified, unreviewed };
}

function patchTimelineBucketForEvent(ev) {
  if (!ev || !eventMarkerBinsCache.bins.size) return false;
  const bucketKey = reviewTimelineBucketByKey.get(eventRowKey(ev));
  const bucket = bucketKey == null ? null : eventMarkerBinsCache.bins.get(bucketKey);
  if (!bucket) return false;
  const previousAttentionCount = Number(bucket.attentionCount) || 0;
  const state = timelineBucketState(bucket);
  Object.assign(bucket, state);
  eventMarkerBinsCache.attentionTotal = Math.max(
    0,
    (Number(eventMarkerBinsCache.attentionTotal) || 0) +
      state.attentionCount -
      previousAttentionCount
  );
  const reviewDot = reviewMarkersEl?.querySelector(`[data-bucket="${bucketKey}"]`);
  if (reviewDot) {
    reviewDot.classList.toggle("attention", state.attention);
    reviewDot.classList.toggle("unreviewed", !state.attention && state.unreviewed);
    reviewDot.classList.toggle("verified", !state.attention && !state.unreviewed);
    const stateText = state.attention
      ? "身份待确认"
      : state.unreviewed
        ? "未处理"
        : "已标真";
    const groupNote = bucket.count > 1 ? ` · 共 ${bucket.count} 条` : "";
    reviewDot.title = `${formatTime(bucket.firstEvent.timestamp_sec)} · ${stateText}${groupNote}`;
  }
  updateReviewTimelineSummary(
    eventMarkerRenderList.length,
    playbackEventStats.verified,
    eventMarkerBinsCache.attentionTotal
  );
  return true;
}

function renderEventMarkerCursor() {
  if (!eventMarkersEl) return;
  let cursor = eventMarkersEl.querySelector(".event-marker-cursor");
  if (!cursor) {
    cursor = document.createElement("span");
    cursor.className = "event-marker-cursor";
    eventMarkersEl.appendChild(cursor);
  }
  const active = playbackEventsByKey.get(activeEventKey);
  const dur = getPlaybackDurationSec();
  if (!active || !dur) {
    cursor.classList.add("hidden");
    return;
  }
  const ratio = Math.min(1, Math.max(0, (Number(active.timestamp_sec) || 0) / dur));
  cursor.style.transform = `translateX(${ratio * Math.max(0, eventMarkersEl.clientWidth - 1)}px)`;
  cursor.classList.remove("hidden");
}

function renderEventMarkers() {
  if (!eventMarkersEl) return;
  ensureReviewTimelineResizeObserver();
  bindReviewTimelineDelegation(eventMarkersEl);
  bindReviewTimelineDelegation(reviewMarkersEl);

  const dur = getPlaybackDurationSec();
  if (!dur || !playbackEvents.length) {
    eventMarkersEl.innerHTML = "";
    if (reviewMarkersEl) reviewMarkersEl.innerHTML = "";
    eventMarkerBinsCache = { key: "", bins: new Map() };
    updateReviewTimelineSummary(0, 0, 0);
    return;
  }

  const rows = filteredPlaybackEvents();
  const bucketCount = reviewTimelineBucketCount();
  reviewTimelineObservedBucketCount = bucketCount;
  const filterMode = eventFilterSelect?.value || "all";
  const cacheKey = `${playbackEventsStructureVersion}|${filterMode}|${bucketCount}|${dur}|${rows.length}`;
  if (eventMarkerBinsCache.key === cacheKey) {
    patchTimelineBucketForEvent(getActiveEvent());
    updateEventMarkerActiveState();
    return;
  }

  eventMarkersEl.innerHTML = "";
  if (reviewMarkersEl) reviewMarkersEl.innerHTML = "";
  reviewTimelineBucketEvents.clear();
  reviewTimelineBucketByKey.clear();
  eventMarkerRenderList = rows;
  const buckets = new Map();
  let verifiedTotal = 0;
  let attentionTotal = 0;

  rows.forEach((ev) => {
    const key = eventRowKey(ev);
    const ratio = Math.min(1, Math.max(0, (Number(ev.timestamp_sec) || 0) / dur));
    const bucketIdx = Math.round(ratio * (bucketCount - 1));
    const bucketKey = String(bucketIdx);
    reviewTimelineBucketByKey.set(key, bucketKey);

    const attention =
      typeof eventNeedsIdentityAttention === "function" &&
      eventNeedsIdentityAttention(ev);
    const verified = isEventVerified(ev);
    if (attention) attentionTotal += 1;
    if (verified) verifiedTotal += 1;

    let bucket = buckets.get(bucketKey);
    if (!bucket) {
      bucket = {
        bucketKey,
        pct: (bucketIdx / Math.max(1, bucketCount - 1)) * 100,
        firstEvent: ev,
        count: 0,
        alarm: false,
        attention: false,
        attentionCount: 0,
        verified: false,
        unreviewed: false,
        events: [],
      };
      buckets.set(bucketKey, bucket);
      reviewTimelineBucketEvents.set(bucketKey, ev);
    }
    bucket.count += 1;
    bucket.events.push(ev);
    if (ev.event_type === "alarm") bucket.alarm = true;
    if (attention) {
      bucket.attention = true;
      bucket.attentionCount += 1;
    }
    else if (verified) bucket.verified = true;
    else bucket.unreviewed = true;
  });

  const activeBucketKey = reviewTimelineBucketByKey.get(activeEventKey) || null;
  const eventFrag = document.createDocumentFragment();
  const reviewFrag = document.createDocumentFragment();

  buckets.forEach((bucket) => {
    const ev = bucket.firstEvent;
    const activeCls = bucket.bucketKey === activeBucketKey ? " active" : "";
    const groupNote = bucket.count > 1 ? ` · 共 ${bucket.count} 条` : "";

    const dot = document.createElement("button");
    dot.type = "button";
    dot.tabIndex = -1;
    dot.className = `event-marker ${bucket.alarm ? "alarm" : "collision"}${activeCls}`;
    dot.dataset.bucket = bucket.bucketKey;
    dot.style.left = `${bucket.pct}%`;
    dot.title = `${eventTypeLabel(ev)} ${formatTime(ev.timestamp_sec)} · ${formatEventTokens(ev.box_tokens)}${groupNote}`;
    eventFrag.appendChild(dot);

    if (reviewMarkersEl) {
      // 一个桶里混合状态时按「待确认 > 未处理 > 已标真」取最需要关注的颜色。
      const stateClass = bucket.attention
        ? "attention"
        : bucket.unreviewed
          ? "unreviewed"
          : "verified";
      const stateText = bucket.attention
        ? "身份待确认"
        : bucket.unreviewed
          ? "未处理"
          : "已标真";
      const reviewDot = document.createElement("button");
      reviewDot.type = "button";
      reviewDot.tabIndex = -1;
      reviewDot.className = `review-marker ${stateClass}${activeCls}`;
      reviewDot.dataset.bucket = bucket.bucketKey;
      reviewDot.style.left = `${bucket.pct}%`;
      reviewDot.title = `${formatTime(ev.timestamp_sec)} · ${stateText}${groupNote}`;
      reviewFrag.appendChild(reviewDot);
    }
  });

  eventMarkersEl.appendChild(eventFrag);
  if (reviewMarkersEl) reviewMarkersEl.appendChild(reviewFrag);
  eventMarkerBinsCache = { key: cacheKey, bins: buckets, attentionTotal };
  updateReviewTimelineSummary(rows.length, verifiedTotal, attentionTotal);
  renderEventMarkerCursor();
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
}

function renderEventReviewList() {
  if (!eventsPanel) return;
  const list = filteredPlaybackEvents();
  const verifiedN = countVerifiedEvents();

  if (!playbackEvents.length) {
    eventsPanel.classList.add("hidden");
    if (eventJumpList) eventJumpList.innerHTML = "";
    if (eventCountLabel) {
      const hint = annotationBoxes.length
        ? "无碰撞事件（已按标注实时扫描）"
        : "无事件（需采集时启用碰撞或加载标注）";
      eventCountLabel.textContent = hint;
    }
    setEventReviewSaveStatus("");
    updateReviewDock();
    return;
  }

  eventsPanel.classList.remove("hidden");
  refreshEventCountLabel();

  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
  }

  updateReviewDock();
  if ($("#event-review-list-details")?.open) {
    renderEventReviewTable(list);
  } else if (eventJumpList) {
    eventJumpList.innerHTML = "";
  }
  renderEventMarkers();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  scheduleEventReviewListScrollHeight();
}

/** @deprecated 兼容旧调用 */
const renderEventJumpList = renderEventReviewList;
