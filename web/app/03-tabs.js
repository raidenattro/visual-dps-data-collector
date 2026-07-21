/** 页签切换与采集表单联动 */
tabs.forEach((btn) => {
  btn.addEventListener("click", () => {
    const leavingPlayback = panels.playback.classList.contains("active") && btn.dataset.tab !== "playback";
    if (leavingPlayback) {
      suspendPlaybackOnTabLeave();
    }
    tabs.forEach((b) => b.classList.toggle("active", b === btn));
    Object.values(panels).forEach((p) => p.classList.remove("active"));
    panels[btn.dataset.tab].classList.add("active");
    if (btn.dataset.tab === "collect") {
      void loadInferenceConfigDefaults();
    }
    if (btn.dataset.tab === "annotate") {
      if (typeof window.initAnnotatePanel === "function") window.initAnnotatePanel();
    }
    if (btn.dataset.tab === "spatial") {
      if (typeof window.initSpatialCalibratePanel === "function") window.initSpatialCalibratePanel();
    }
    if (btn.dataset.tab === "accuracy") {
      if (typeof window.initAccuracyPanel === "function") window.initAccuracyPanel();
    }
    if (btn.dataset.tab === "playback") {
      void loadRecords({ quiet: playbackRecordsByTier.has(playbackPoseTier || "rtmpose-t") });
      restorePlaybackPanelUi();
    }
  });
});

$("#collect-fps")?.addEventListener("input", () => {
  $("#collect-fps").dataset.userTouched = "1";
});
$("#collect-alarm-min")?.addEventListener("change", () => {
  saveCollisionConfigToStorage(readCollisionConfigFromForm());
  resetPlaybackCollisionTracker();
});
$("#collect-alarm-cooldown")?.addEventListener("change", () => {
  saveCollisionConfigToStorage(readCollisionConfigFromForm());
  resetPlaybackCollisionTracker();
});
