const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const selectionSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "02-playback-selection.js"),
  "utf8"
);
const recordsSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "06-records.js"),
  "utf8"
);

const loadButton = { disabled: true, textContent: "" };
const listClasses = new Set();
const sessionList = {
  classList: {
    add: (name) => listClasses.add(name),
    remove: (name) => listClasses.delete(name),
  },
};
let resolveOpen;
let openCalls = 0;
const context = vm.createContext({
  console,
  CSS: { escape: (value) => String(value) },
  document: {
    getElementById(id) {
      if (id === "playback-load-record") return loadButton;
      if (id === "session-list") return sessionList;
      return null;
    },
    querySelectorAll: () => [],
    querySelector: () => null,
  },
  setPlaybackInfo: () => {},
  openRecordReplay: async () => {
    openCalls += 1;
    await new Promise((resolve) => {
      resolveOpen = resolve;
    });
  },
});
vm.runInContext(selectionSource, context);

(async () => {
  const li = {
    dataset: {
      recordId: "rtmpose-m/camera/example",
      displayName: "example",
      poseFile: "manifest.json",
      hasVideo: "1",
    },
  };
  context.selectPlaybackRecordItem(li);
  const firstOpen = context.startPlaybackFromSelectedRecord();
  const duplicateOpen = context.startPlaybackFromSelectedRecord();

  assert.equal(openCalls, 1, "重复点击不能创建第二个加载请求");
  assert.equal(context.isPlaybackRecordOpening(), true);
  assert.equal(loadButton.disabled, true);
  assert.equal(loadButton.textContent, "正在加载…");
  assert.equal(listClasses.has("playback-record-list-loading"), true);

  resolveOpen();
  await Promise.all([firstOpen, duplicateOpen]);
  assert.equal(context.isPlaybackRecordOpening(), false);
  assert.equal(loadButton.disabled, false);
  assert.equal(loadButton.textContent, "加载并回放");
  assert.equal(listClasses.has("playback-record-list-loading"), false);

  assert.match(
    recordsSource,
    /selectPlaybackRecordItem\(li\);\s*startPlaybackFromSelectedRecord\(\)/,
    "记录行单击后应立即启动加载"
  );
  assert.doesNotMatch(
    recordsSource,
    /li\.addEventListener\("dblclick"/,
    "不应再依赖双击加载"
  );

  const firstChunk = recordsSource.indexOf(
    "const initialFramesPromise = prefetchFrameChunksParallel(1, FRAME_CHUNK_PREFETCH_INITIAL)"
  );
  const videoLoad = recordsSource.indexOf(
    "const videoPromise = prepareAndLoadRecordVideo(recordId"
  );
  assert.ok(firstChunk >= 0 && videoLoad >= 0, "首批骨架和视频应并行启动");
  assert.doesNotMatch(
    recordsSource,
    /prefetchAllPlaybackChunksInBackground/,
    "长记录不得后台预取全部骨架分块"
  );
  assert.doesNotMatch(
    recordsSource,
    /await eventsPromise/,
    "事件与复核状态不得阻塞视频首开"
  );

  console.log("playback record single-click tests passed");
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
