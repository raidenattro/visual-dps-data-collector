/**
 * 标注交互（与 visual-dps useAnnotateTool 对齐）：货架角点 + 货位四边形拖拽
 */
(function initAnnotateVisualMode(global) {
  const MAP_W = 600;
  const MAP_H = 600;
  const CELL_POLYGON_INSET = 0.08;
  const geo = () => global.annotateGeometry;

  let gridRows = 4;
  let gridCols = 4;
  let shelfPoints = [];
  let M_fwd = null;
  let M_inv = null;
  let layerYs = [];
  let layerColXs = [];
  /** @type {Map<string, number[][]>} */
  let cellPolygons = new Map();
  let deletedCells = new Set();
  let boxIdOverrides = {};
  let selectedCell = null;
  let dragTarget = null;
  let gridReady = false;
  let onSelectionChange = null;
  /** 是否绘制并可拖拽货架四角（沙盒加载已有标注时可关闭） */
  let shelfOutlineVisible = true;
  /** 沙盒微调模式：提高货位描边/编号对比度 */
  let cellRenderHighContrast = false;

  function getCellDrawStyle(isSelected) {
    if (cellRenderHighContrast) {
      if (isSelected) {
        return {
          stroke: "#22d3ee",
          lineWidth: 3.5,
          fill: "rgba(34, 211, 238, 0.3)",
          handle: "#fb923c",
          handleR: 6,
          labelFill: "#ffffff",
          labelStroke: "rgba(0, 0, 0, 0.85)",
          labelWidth: 4,
          font: "bold 18px Arial",
        };
      }
      return {
        stroke: "rgba(250, 204, 21, 0.95)",
        lineWidth: 2.25,
        fill: "rgba(250, 204, 21, 0.16)",
        handle: null,
        handleR: 0,
        labelFill: null,
        labelStroke: null,
        labelWidth: 0,
        font: "bold 16px Arial",
      };
    }
    if (isSelected) {
      return {
        stroke: "#00d4aa",
        lineWidth: 2.5,
        fill: "rgba(0, 212, 170, 0.12)",
        handle: "#e67e22",
        handleR: 5,
        labelFill: "#00ffcc",
        labelStroke: null,
        labelWidth: 0,
        font: "bold 16px Arial",
      };
    }
    return {
      stroke: "rgba(241, 196, 15, 0.28)",
      lineWidth: 1,
      fill: null,
      handle: null,
      handleR: 0,
      labelFill: null,
      labelStroke: null,
      labelWidth: 0,
      font: "bold 16px Arial",
    };
  }

  function drawCellLabel(ctx, text, x, y, style) {
    ctx.font = style.font;
    if (style.labelStroke) {
      ctx.lineWidth = style.labelWidth;
      ctx.strokeStyle = style.labelStroke;
      ctx.strokeText(text, x, y);
    }
    ctx.fillStyle = style.labelFill || "#00ffcc";
    ctx.fillText(text, x, y);
  }

  function getCellKey(rowIdx, colIdx) {
    return `${rowIdx + 1}-${colIdx + 1}`;
  }

  function clonePoly(poly) {
    return poly.map(([x, y]) => [Number(x), Number(y)]);
  }

  function averageArray(values) {
    let sum = 0;
    for (const val of values) sum += val;
    return sum / values.length;
  }

  function createDefaultShelfCorners(frameW, frameH, marginRatio = 0.12) {
    const w = Math.max(1, Number(frameW) || 1);
    const h = Math.max(1, Number(frameH) || 1);
    const mx = w * marginRatio;
    const my = h * marginRatio;
    return [
      [mx, my],
      [w - mx, my],
      [w - mx, h - my],
      [mx, h - my],
    ];
  }

  function initGrid(rows, cols) {
    gridRows = rows;
    gridCols = cols;
    layerYs = [];
    layerColXs = [];
    for (let i = 0; i <= rows; i += 1) layerYs.push(i * (MAP_H / rows));
    for (let i = 0; i < rows; i += 1) {
      const colLine = [];
      for (let j = 0; j <= cols; j += 1) colLine.push(j * (MAP_W / cols));
      layerColXs.push(colLine);
    }
  }

  function getGridExtent() {
    let divR = layerYs.length > 1 ? layerYs.length - 1 : 0;
    let divC = 0;
    for (const row of layerColXs) {
      if (row?.length > 1) divC = Math.max(divC, row.length - 1);
    }
    return {
      rows: Math.max(gridRows, divR),
      cols: Math.max(gridCols, divC),
    };
  }

  function refreshPerspectiveFromShelf() {
    if (shelfPoints.length !== 4) return;
    const dst = [
      [0, 0],
      [MAP_W, 0],
      [MAP_W, MAP_H],
      [0, MAP_H],
    ];
    try {
      M_fwd = geo().getPerspectiveTransform(shelfPoints, dst);
      M_inv = geo().getPerspectiveTransform(dst, shelfPoints);
    } catch {
      M_fwd = null;
      M_inv = null;
    }
  }

  function computeCellPolyFromGrid(rowIdx, colIdx) {
    if (!M_inv || !layerColXs[rowIdx] || layerColXs[rowIdx].length <= colIdx + 1) return null;
    const p0 = geo().perspectiveTransform([layerColXs[rowIdx][colIdx], layerYs[rowIdx]], M_inv);
    const p1 = geo().perspectiveTransform([layerColXs[rowIdx][colIdx + 1], layerYs[rowIdx]], M_inv);
    const p2 = geo().perspectiveTransform([layerColXs[rowIdx][colIdx + 1], layerYs[rowIdx + 1]], M_inv);
    const p3 = geo().perspectiveTransform([layerColXs[rowIdx][colIdx], layerYs[rowIdx + 1]], M_inv);
    return geo().insetConvexQuad(clonePoly([p0, p1, p2, p3]), CELL_POLYGON_INSET);
  }

  function getCellPoly(rowIdx, colIdx) {
    const key = getCellKey(rowIdx, colIdx);
    if (deletedCells.has(key)) return null;
    const existing = cellPolygons.get(key);
    if (existing && existing.length >= 4) return existing;
    const computed = computeCellPolyFromGrid(rowIdx, colIdx);
    if (computed) cellPolygons.set(key, computed);
    return computed;
  }

  function fillMissingCellPolygons() {
    const { rows, cols } = getGridExtent();
    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        const key = getCellKey(i, j);
        if (deletedCells.has(key)) continue;
        if (!cellPolygons.has(key)) {
          const poly = computeCellPolyFromGrid(i, j);
          if (poly) cellPolygons.set(key, poly);
        }
      }
    }
  }

  function loadCellPolygonsFromBoxes(boxes) {
    cellPolygons = new Map();
    deletedCells = new Set();
    if (!Array.isArray(boxes)) return;
    for (const box of boxes) {
      const rowIdx = Number(box.layer) - 1;
      const colIdx = Number(box.column) - 1;
      if (rowIdx < 0 || colIdx < 0) continue;
      const poly = box.video_polygon;
      if (!Array.isArray(poly) || poly.length < 4) continue;
      cellPolygons.set(getCellKey(rowIdx, colIdx), clonePoly(poly.slice(0, 4)));
    }
  }

  function markUnmappedCellsDeleted() {
    deletedCells = new Set();
    const { rows, cols } = getGridExtent();
    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        if (!cellPolygons.has(getCellKey(i, j))) deletedCells.add(getCellKey(i, j));
      }
    }
  }

  function applyAnnotationToGrid(boxes, rowCount, colCount) {
    if (!Array.isArray(boxes) || !M_fwd) return;
    const ySamples = Array.from({ length: rowCount + 1 }, () => []);
    const xSamples = Array.from({ length: rowCount }, () =>
      Array.from({ length: colCount + 1 }, () => [])
    );
    const overrides = {};
    for (const box of boxes) {
      const rowIdx = Number(box.layer) - 1;
      const colIdx = Number(box.column) - 1;
      if (rowIdx < 0 || colIdx < 0 || rowIdx >= rowCount || colIdx >= colCount) continue;
      const poly = Array.isArray(box.video_polygon) ? box.video_polygon : [];
      if (poly.length < 4) continue;
      const flat = poly.map((pt) => geo().perspectiveTransform(pt, M_fwd));
      const topY = (flat[0][1] + flat[1][1]) / 2;
      const bottomY = (flat[2][1] + flat[3][1]) / 2;
      const leftX = (flat[0][0] + flat[3][0]) / 2;
      const rightX = (flat[1][0] + flat[2][0]) / 2;
      ySamples[rowIdx].push(topY);
      ySamples[rowIdx + 1].push(bottomY);
      xSamples[rowIdx][colIdx].push(leftX);
      xSamples[rowIdx][colIdx + 1].push(rightX);
      const boxId = box.box_id !== undefined ? String(box.box_id) : "";
      const defaultId = String(rowIdx * colCount + colIdx + 1);
      if (boxId && boxId !== defaultId) overrides[getCellKey(rowIdx, colIdx)] = boxId;
    }
    const newYs = [...layerYs];
    const newColXs = layerColXs.map((row) => [...row]);
    for (let i = 0; i <= rowCount; i += 1) {
      if (ySamples[i].length) newYs[i] = averageArray(ySamples[i]);
    }
    for (let i = 0; i < rowCount; i += 1) {
      for (let j = 0; j <= colCount; j += 1) {
        if (xSamples[i][j].length) newColXs[i][j] = averageArray(xSamples[i][j]);
      }
    }
    layerYs = newYs;
    layerColXs = newColXs;
    boxIdOverrides = overrides;
  }

  function getDefaultBoxId(rowIdx, colIdx) {
    return String(rowIdx * gridCols + colIdx + 1);
  }

  function getEffectiveBoxId(rowIdx, colIdx) {
    const key = getCellKey(rowIdx, colIdx);
    const raw = boxIdOverrides[key];
    if (raw === undefined || raw === null || raw === "") return getDefaultBoxId(rowIdx, colIdx);
    const text = String(raw).trim();
    return text || getDefaultBoxId(rowIdx, colIdx);
  }

  function clientToCanvas(canvas, clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return [(clientX - rect.left) * scaleX, (clientY - rect.top) * scaleY];
  }

  function canvasHitRadius(canvas, basePx) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / (rect.width || 1);
    const scaleY = canvas.height / (rect.height || 1);
    return basePx * Math.max(scaleX, scaleY, 1);
  }

  /** 命中四边形角点则返回角索引，否则 -1 */
  function hitCellCornerIndex(poly, x, y, cornerHit) {
    if (!poly || poly.length < 4) return -1;
    let nearestCi = 0;
    let nearestDist = Infinity;
    for (let ci = 0; ci < poly.length; ci += 1) {
      const d = geo().getPointDist([x, y], poly[ci]);
      if (d < nearestDist) {
        nearestDist = d;
        nearestCi = ci;
      }
    }
    return nearestDist < cornerHit ? nearestCi : -1;
  }

  function beginCellDrag(i, j, poly, x, y, cornerHit) {
    const ci = hitCellCornerIndex(poly, x, y, cornerHit);
    if (ci >= 0) {
      dragTarget = { type: "cell-corner", row: i, col: j, ci };
      return;
    }
    dragTarget = {
      type: "cell-body",
      row: i,
      col: j,
      startX: x,
      startY: y,
      startPoly: clonePoly(poly),
      moved: false,
    };
  }

  function tryBeginCellInteraction(i, j, x, y, cornerHit, { allowOutsideCorner = false } = {}) {
    const poly = getCellPoly(i, j);
    if (!poly || poly.length < 4) return false;
    const inside = geo().pointInPolygon([x, y], poly);
    const cornerCi = hitCellCornerIndex(poly, x, y, cornerHit);
    if (inside || (allowOutsideCorner && cornerCi >= 0)) {
      beginCellDrag(i, j, poly, x, y, cornerHit);
      return true;
    }
    return false;
  }

  function findTopCellAtPoint(rows, cols, x, y) {
    for (let i = rows - 1; i >= 0; i -= 1) {
      for (let j = cols - 1; j >= 0; j -= 1) {
        const poly = getCellPoly(i, j);
        if (!poly || poly.length < 4) continue;
        if (geo().pointInPolygon([x, y], poly)) return { row: i, col: j, poly };
      }
    }
    return null;
  }

  function drawShelfOutline(ctx) {
    if (!shelfPoints.length || !shelfOutlineVisible) return;
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#2ecc71";
    ctx.beginPath();
    ctx.moveTo(shelfPoints[0][0], shelfPoints[0][1]);
    for (let i = 1; i < shelfPoints.length; i += 1) ctx.lineTo(shelfPoints[i][0], shelfPoints[i][1]);
    if (shelfPoints.length === 4) ctx.closePath();
    ctx.stroke();
    ctx.fillStyle = "#e74c3c";
    for (const p of shelfPoints) {
      ctx.beginPath();
      ctx.arc(p[0], p[1], 6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawEditableGridBoxes(ctx) {
    if (!M_inv) return;
    const { rows, cols } = getGridExtent();
    const selRow = selectedCell?.rowIdx;
    const selCol = selectedCell?.colIdx;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        const poly = getCellPoly(i, j);
        if (!poly || poly.length < 4) continue;
        const isSelected =
          (selRow === i && selCol === j) ||
          (dragTarget &&
            (dragTarget.type === "cell-body" || dragTarget.type === "cell-corner") &&
            dragTarget.row === i &&
            dragTarget.col === j);
        const style = getCellDrawStyle(isSelected);
        ctx.strokeStyle = style.stroke;
        ctx.lineWidth = style.lineWidth;
        if (style.fill) {
          ctx.fillStyle = style.fill;
          ctx.beginPath();
          ctx.moveTo(poly[0][0], poly[0][1]);
          for (let k = 1; k < poly.length; k += 1) ctx.lineTo(poly[k][0], poly[k][1]);
          ctx.closePath();
          ctx.fill();
        }
        ctx.beginPath();
        ctx.moveTo(poly[0][0], poly[0][1]);
        for (let k = 1; k < poly.length; k += 1) ctx.lineTo(poly[k][0], poly[k][1]);
        ctx.closePath();
        ctx.stroke();
        if (!isSelected) continue;
        if (style.handle) {
          ctx.fillStyle = style.handle;
          for (const pt of poly) {
            ctx.beginPath();
            ctx.arc(pt[0], pt[1], style.handleR, 0, Math.PI * 2);
            ctx.fill();
          }
        }
        const centerX = poly.reduce((s, p) => s + p[0], 0) / poly.length;
        const centerY = poly.reduce((s, p) => s + p[1], 0) / poly.length;
        drawCellLabel(ctx, getEffectiveBoxId(i, j), centerX, centerY, style);
      }
    }
  }

  const api = {
    reset() {
      shelfPoints = [];
      M_fwd = null;
      M_inv = null;
      layerYs = [];
      layerColXs = [];
      cellPolygons = new Map();
      deletedCells = new Set();
      boxIdOverrides = {};
      selectedCell = null;
      dragTarget = null;
      gridReady = false;
      gridRows = 4;
      gridCols = 4;
      shelfOutlineVisible = true;
      cellRenderHighContrast = false;
    },

    setShelfOutlineVisible(visible) {
      shelfOutlineVisible = !!visible;
    },

    isShelfOutlineVisible() {
      return shelfOutlineVisible;
    },

    setCellRenderHighContrast(enabled) {
      cellRenderHighContrast = !!enabled;
    },

    setGridSize(rows, cols) {
      gridRows = Math.max(1, Math.min(8, Number(rows) || 4));
      gridCols = Math.max(1, Math.min(8, Number(cols) || 4));
    },

    getGridSize() {
      return { rows: gridRows, cols: gridCols };
    },

    isGridReady() {
      return gridReady && !!M_inv;
    },

    shelfCornersReady() {
      return shelfPoints.length === 4 && !!M_inv;
    },

    ensureDefaultShelf(frameW, frameH) {
      if (shelfPoints.length !== 4) {
        shelfPoints = createDefaultShelfCorners(frameW, frameH);
        refreshPerspectiveFromShelf();
      }
    },

    loadShelf(shelf, options = {}) {
      const mapPts = options.mapPointsToFrame;
      const annSize = options.annotationSize;
      const frameW = Math.max(1, options.frameWidth || 1);
      const frameH = Math.max(1, options.frameHeight || 1);

      let shapeRows = gridRows;
      let shapeCols = gridCols;
      if (Array.isArray(shelf?.grid_shape) && shelf.grid_shape.length === 2) {
        const r = Number(shelf.grid_shape[0]);
        const c = Number(shelf.grid_shape[1]);
        if (r > 0 && c > 0) {
          shapeRows = r;
          shapeCols = c;
          gridRows = r;
          gridCols = c;
        }
      }

      let corners = Array.isArray(shelf?.shelf_corners)
        ? shelf.shelf_corners.map((p) => [Number(p[0]), Number(p[1])])
        : [];
      if (corners.length !== 4) {
        corners = createDefaultShelfCorners(frameW, frameH);
      } else if (annSize && mapPts) {
        corners = mapPts(corners, null);
      }
      shelfPoints = corners;
      refreshPerspectiveFromShelf();

      const boxes = (Array.isArray(shelf?.boxes) ? shelf.boxes : []).map((box) => {
        const norm =
          options.isNormValid?.(box.video_polygon_norm) ? box.video_polygon_norm : null;
        const poly = Array.isArray(box.video_polygon) ? box.video_polygon : [];
        if (!poly.length || !annSize || !mapPts) return box;
        return { ...box, video_polygon: mapPts(poly, norm) };
      });

      boxIdOverrides = {};
      selectedCell = null;
      gridReady = false;

      if (M_fwd && boxes.length) {
        initGrid(shapeRows, shapeCols);
        applyAnnotationToGrid(boxes, shapeRows, shapeCols);
        loadCellPolygonsFromBoxes(boxes);
        markUnmappedCellsDeleted();
        gridReady = true;
      } else if (M_inv) {
        layerYs = [];
        layerColXs = [];
        cellPolygons = new Map();
        deletedCells = new Set();
      }
    },

    confirmGenerateGrid() {
      if (shelfPoints.length < 4 || !M_inv) {
        return { ok: false, message: "请先标定货架四角（拖动红色角点）" };
      }
      const rows = Math.max(1, Math.min(8, gridRows));
      const cols = Math.max(1, Math.min(8, gridCols));
      gridRows = rows;
      gridCols = cols;
      initGrid(rows, cols);
      cellPolygons = new Map();
      deletedCells = new Set();
      fillMissingCellPolygons();
      selectedCell = null;
      gridReady = true;
      if (onSelectionChange) onSelectionChange(null);
      return { ok: true, message: `已生成 ${rows}×${cols} 个货位，点击货位可编辑编号与形状` };
    },

    buildBoxes(frameW, frameH) {
      const fw = Math.max(1, frameW || 1);
      const fh = Math.max(1, frameH || 1);
      const boxes = [];
      const { rows, cols } = getGridExtent();
      for (let i = 0; i < rows; i += 1) {
        for (let j = 0; j < cols; j += 1) {
          const poly = getCellPoly(i, j);
          if (!poly || poly.length < 4) continue;
          boxes.push({
            box_id: getEffectiveBoxId(i, j),
            layer: i + 1,
            column: j + 1,
            video_polygon: clonePoly(poly),
            video_polygon_norm: poly.map(([x, y]) => [x / fw, y / fh]),
          });
        }
      }
      return boxes;
    },

    getShelfCorners() {
      return shelfPoints.map((p) => [p[0], p[1]]);
    },

    getSelectedCellPanel() {
      if (!selectedCell) return null;
      const { rowIdx, colIdx } = selectedCell;
      return {
        rowIdx,
        colIdx,
        row: rowIdx + 1,
        col: colIdx + 1,
        value: boxIdOverrides[getCellKey(rowIdx, colIdx)] ?? "",
        defaultId: getDefaultBoxId(rowIdx, colIdx),
      };
    },

    setBoxId(rowIdx, colIdx, raw) {
      const key = getCellKey(rowIdx, colIdx);
      const text = String(raw || "").trim();
      if (!text) delete boxIdOverrides[key];
      else boxIdOverrides[key] = text;
    },

    deleteSelectedCell() {
      if (!selectedCell) return false;
      const { rowIdx, colIdx } = selectedCell;
      const key = getCellKey(rowIdx, colIdx);
      if (!getCellPoly(rowIdx, colIdx)) return false;
      deletedCells.add(key);
      cellPolygons.delete(key);
      delete boxIdOverrides[key];
      selectedCell = null;
      if (onSelectionChange) onSelectionChange(null);
      return true;
    },

    render(ctx, bgImage) {
      if (!ctx) return;
      if (bgImage?.complete) ctx.drawImage(bgImage, 0, 0);
      if (shelfPoints.length) drawShelfOutline(ctx);
      if (M_inv && gridReady) drawEditableGridBoxes(ctx);
    },

    bindCanvas(canvas, hooks = {}) {
      onSelectionChange = hooks.onSelectionChange || null;
      const onRender = hooks.onRender || null;
      const bump = () => {
        if (onRender) onRender();
      };

      const onDown = (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        const pt = clientToCanvas(canvas, e.clientX, e.clientY);
        if (!pt) return;
        const [x, y] = pt;
        const cornerHit = canvasHitRadius(canvas, 10);
        const shelfHit = canvasHitRadius(canvas, 12);

        if (shelfOutlineVisible && shelfPoints.length === 4) {
          for (let i = 0; i < shelfPoints.length; i += 1) {
            if (geo().getPointDist([x, y], shelfPoints[i]) < shelfHit) {
              dragTarget = { type: "shelf-corner", i };
              bump();
              return;
            }
          }
        }

        if (!gridReady) return;
        const { rows, cols } = getGridExtent();

        // 已选中货位：仅操作该货位（角点可在多边形外少量延伸），其它货位不误触
        if (selectedCell) {
          const { rowIdx, colIdx } = selectedCell;
          if (
            tryBeginCellInteraction(rowIdx, colIdx, x, y, cornerHit, {
              allowOutsideCorner: true,
            })
          ) {
            bump();
            return;
          }

          const hitOther = findTopCellAtPoint(rows, cols, x, y);
          if (hitOther && (hitOther.row !== rowIdx || hitOther.col !== colIdx)) {
            beginCellDrag(hitOther.row, hitOther.col, hitOther.poly, x, y, cornerHit);
            bump();
            return;
          }

          selectedCell = null;
          if (onSelectionChange) onSelectionChange(null);
          bump();
          return;
        }

        const hit = findTopCellAtPoint(rows, cols, x, y);
        if (hit) {
          beginCellDrag(hit.row, hit.col, hit.poly, x, y, cornerHit);
          bump();
          return;
        }

        if (onSelectionChange) onSelectionChange(null);
        bump();
      };

      const onMove = (e) => {
        if (!dragTarget) return;
        const canvasPt = clientToCanvas(canvas, e.clientX, e.clientY);
        if (!canvasPt) return;

        if (dragTarget.type === "shelf-corner") {
          shelfPoints[dragTarget.i] = [canvasPt[0], canvasPt[1]];
          refreshPerspectiveFromShelf();
          if (gridReady) fillMissingCellPolygons();
          bump();
          return;
        }

        if (dragTarget.type === "cell-corner") {
          const key = getCellKey(dragTarget.row, dragTarget.col);
          const poly = cellPolygons.get(key) || getCellPoly(dragTarget.row, dragTarget.col);
          if (!poly) return;
          const next = clonePoly(poly);
          next[dragTarget.ci] = [canvasPt[0], canvasPt[1]];
          cellPolygons.set(key, next);
          bump();
          return;
        }

        if (dragTarget.type === "cell-body") {
          const dx = canvasPt[0] - dragTarget.startX;
          const dy = canvasPt[1] - dragTarget.startY;
          if (!dragTarget.moved) {
            if (dx * dx + dy * dy < 16) return;
            dragTarget.moved = true;
          }
          const key = getCellKey(dragTarget.row, dragTarget.col);
          const next = dragTarget.startPoly.map(([px, py]) => [px + dx, py + dy]);
          cellPolygons.set(key, next);
          bump();
        }
      };

      const onUp = () => {
        const ended = dragTarget;
        dragTarget = null;
        if (
          ended &&
          (ended.type === "cell-body" || ended.type === "cell-corner") &&
          Number.isInteger(ended.row) &&
          Number.isInteger(ended.col)
        ) {
          if (ended.type === "cell-body" && !ended.moved) {
            cellPolygons.set(getCellKey(ended.row, ended.col), clonePoly(ended.startPoly));
          }
          selectedCell = { rowIdx: ended.row, colIdx: ended.col };
          if (onSelectionChange) onSelectionChange(api.getSelectedCellPanel());
        }
        bump();
      };

      canvas.addEventListener("mousedown", onDown);
      canvas.addEventListener("mousemove", onMove);
      canvas.addEventListener("mouseup", onUp);
      canvas.addEventListener("mouseleave", onUp);

      return () => {
        canvas.removeEventListener("mousedown", onDown);
        canvas.removeEventListener("mousemove", onMove);
        canvas.removeEventListener("mouseup", onUp);
        canvas.removeEventListener("mouseleave", onUp);
      };
    },
  };

  global.AnnotateVisualMode = api;
})(window);
