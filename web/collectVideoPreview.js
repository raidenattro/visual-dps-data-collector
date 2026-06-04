/**
 * 采集页：选择视频后展示首帧预览（仅画面，无 OCR）。
 */
(function () {
  const wrap = document.getElementById("collect-preview-wrap");
  const video = document.getElementById("collect-preview-video");
  if (!wrap || !video) return;

  let objectUrl = null;

  function revokeUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  function showWrap(show) {
    wrap.classList.toggle("hidden", !show);
  }

  window.initCollectVideoPreview = function initCollectVideoPreview(file) {
    revokeUrl();
    video.removeAttribute("src");
    video.load();
    if (!file) {
      showWrap(false);
      return;
    }
    objectUrl = URL.createObjectURL(file);
    video.src = objectUrl;
    showWrap(true);
    video.onloadeddata = () => {
      try {
        video.currentTime = 0;
      } catch {
        /* ignore */
      }
    };
  };
})();
