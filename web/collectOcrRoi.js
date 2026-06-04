/**
 * 采集页：在视频预览上手动拖拽/缩放 OCR 区域（比例 0~1，相对视频画面）。
 */
(function () {
  const wrap = document.getElementById("collect-ocr-roi-wrap");
  const stage = document.getElementById("collect-ocr-roi-stage");
  const video = document.getElementById("collect-ocr-preview");
  const box = document.getElementById("collect-ocr-roi-box");
  const label = document.getElementById("collect-ocr-roi-label");
  const resetBtn = document.getElementById("collect-ocr-roi-reset");

  if (!wrap || !stage || !video || !box) return;

  let objectUrl = null;
  let norm = { x0: 0.72, y0: 0.86, x1: 1.0, y1: 0.98 };
  let drag = null;

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

  function getLayout() {
    const rect = stage.getBoundingClientRect();
    const vw = video.videoWidth || 640;
    const vh = video.videoHeight || 360;
    return computeContainLayout(rect.width, rect.height, vw, vh);
  }

  function clampNorm(n) {
    return {
      x0: Math.max(0, Math.min(1, n.x0)),
      y0: Math.max(0, Math.min(1, n.y0)),
      x1: Math.max(0, Math.min(1, n.x1)),
      y1: Math.max(0, Math.min(1, n.y1)),
    };
  }

  function normToPx(n, layout) {
    const nn = clampNorm(n);
    const x0 = layout.offsetX + nn.x0 * layout.drawW;
    const y0 = layout.offsetY + nn.y0 * layout.drawH;
    const x1 = layout.offsetX + nn.x1 * layout.drawW;
    const y1 = layout.offsetY + nn.y1 * layout.drawH;
    return { left: x0, top: y0, width: Math.max(8, x1 - x0), height: Math.max(8, y1 - y0) };
  }

  function pxToNorm(left, top, width, height, layout) {
    const x0 = (left - layout.offsetX) / layout.drawW;
    const y0 = (top - layout.offsetY) / layout.drawH;
    const x1 = (left + width - layout.offsetX) / layout.drawW;
    const y1 = (top + height - layout.offsetY) / layout.drawH;
    return clampNorm({
      x0: Math.min(x0, x1),
      y0: Math.min(y0, y1),
      x1: Math.max(x0, x1),
      y1: Math.max(y0, y1),
    });
  }

  function formatNorm(n) {
    const f = (v) => Number(v).toFixed(2);
    return `x0=${f(n.x0)}, y0=${f(n.y0)}, x1=${f(n.x1)}, y1=${f(n.y1)}`;
  }

  function syncBoxFromNorm() {
    const layout = getLayout();
    const px = normToPx(norm, layout);
    box.style.left = `${px.left}px`;
    box.style.top = `${px.top}px`;
    box.style.width = `${px.width}px`;
    box.style.height = `${px.height}px`;
    if (label) label.textContent = `OCR 区域（画面比例）：${formatNorm(norm)}`;
  }

  function readNormFromBox() {
    const layout = getLayout();
    const left = parseFloat(box.style.left) || 0;
    const top = parseFloat(box.style.top) || 0;
    const width = parseFloat(box.style.width) || 0;
    const height = parseFloat(box.style.height) || 0;
    norm = pxToNorm(left, top, width, height, layout);
    if (label) label.textContent = `OCR 区域（画面比例）：${formatNorm(norm)}`;
  }

  function stagePointFromEvent(e) {
    const rect = stage.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  box.addEventListener("pointerdown", (e) => {
    if (e.target.classList.contains("collect-ocr-roi-handle")) return;
    e.preventDefault();
    const pt = stagePointFromEvent(e);
    const left = parseFloat(box.style.left) || 0;
    const top = parseFloat(box.style.top) || 0;
    drag = { mode: "move", startX: pt.x, startY: pt.y, origLeft: left, origTop: top };
    box.setPointerCapture(e.pointerId);
  });

  const handle = box.querySelector(".collect-ocr-roi-handle");
  handle?.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const pt = stagePointFromEvent(e);
    drag = {
      mode: "resize",
      startX: pt.x,
      startY: pt.y,
      origLeft: parseFloat(box.style.left) || 0,
      origTop: parseFloat(box.style.top) || 0,
      origW: parseFloat(box.style.width) || 40,
      origH: parseFloat(box.style.height) || 24,
    };
    handle.setPointerCapture(e.pointerId);
  });

  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const pt = stagePointFromEvent(e);
    const dx = pt.x - drag.startX;
    const dy = pt.y - drag.startY;
    const layout = getLayout();
    const maxW = layout.drawW;
    const maxH = layout.drawH;

    if (drag.mode === "move") {
      let left = drag.origLeft + dx;
      let top = drag.origTop + dy;
      const w = parseFloat(box.style.width) || 40;
      const h = parseFloat(box.style.height) || 24;
      left = Math.max(layout.offsetX, Math.min(layout.offsetX + maxW - w, left));
      top = Math.max(layout.offsetY, Math.min(layout.offsetY + maxH - h, top));
      box.style.left = `${left}px`;
      box.style.top = `${top}px`;
    } else {
      let w = Math.max(24, drag.origW + dx);
      let h = Math.max(16, drag.origH + dy);
      w = Math.min(w, layout.offsetX + maxW - drag.origLeft);
      h = Math.min(h, layout.offsetY + maxH - drag.origTop);
      box.style.width = `${w}px`;
      box.style.height = `${h}px`;
    }
    readNormFromBox();
  });

  box.addEventListener("pointerup", (e) => {
    if (!drag) return;
    drag = null;
    readNormFromBox();
    try {
      box.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  });

  box.addEventListener("pointercancel", () => {
    drag = null;
    readNormFromBox();
  });

  window.addEventListener("resize", () => {
    if (!wrap.classList.contains("hidden")) syncBoxFromNorm();
  });

  resetBtn?.addEventListener("click", () => {
    norm = { x0: 0.72, y0: 0.86, x1: 1.0, y1: 0.98 };
    syncBoxFromNorm();
  });

  function revokeUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  window.setCollectOcrRoiNorm = function (arr) {
    if (!Array.isArray(arr) || arr.length < 4) return;
    norm = clampNorm({
      x0: Number(arr[0]),
      y0: Number(arr[1]),
      x1: Number(arr[2]),
      y1: Number(arr[3]),
    });
    if (!wrap.classList.contains("hidden")) syncBoxFromNorm();
  };

  window.getCollectOcrRoiNorm = function () {
    readNormFromBox();
    return { ...norm };
  };

  window.initCollectOcrRoiPreview = function (file) {
    revokeUrl();
    if (!file) {
      wrap.classList.add("hidden");
      video.removeAttribute("src");
      video.load();
      return;
    }
    objectUrl = URL.createObjectURL(file);
    video.src = objectUrl;
    wrap.classList.remove("hidden");
    const onReady = () => {
      video.removeEventListener("loadedmetadata", onReady);
      video.currentTime = 0;
      syncBoxFromNorm();
    };
    video.addEventListener("loadedmetadata", onReady);
  };

  window.destroyCollectOcrRoiPreview = revokeUrl;
})();
