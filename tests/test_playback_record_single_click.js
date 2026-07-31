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
    "await prefetchFrameChunksParallel(1, 1)"
  );
  const videoLoad = recordsSource.indexOf(
    "await prepareAndLoadRecordVideo(recordId"
  );
  const backgroundPrefetch = recordsSource.indexOf(
    "void prefetchAllPlaybackChunksInBackground(recordId)"
  );
  assert.ok(firstChunk >= 0 && firstChunk < videoLoad);
  assert.ok(backgroundPrefetch > videoLoad);

  console.log("playback record single-click tests passed");
})().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
