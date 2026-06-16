/** 标注 ROI 准确率评估（review 范本 vs 告警检测） */

function acc$(sel) {
  return document.querySelector(sel);
}

function pct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(Number(v) * 100).toFixed(1)}%`;
}

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setAccuracyStatus(html, isError = false) {
  const el = acc$("#accuracy-status");
  if (!el) return;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
  else el.classList.remove("error");
  el.innerHTML = html;
}

async function readApiError(res) {
  try {
    const body = await res.json();
    const d = body.detail;
    if (Array.isArray(d)) return d.map((x) => x.msg || x).join("; ");
    return d || res.statusText;
  } catch {
    return res.statusText;
  }
}

function updateAccuracyContextHint(ctx) {
  const el = acc$("#accuracy-context-hint");
  if (!el || !ctx) {
    el?.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `机位 <strong>${escHtml(ctx.camera_label)}</strong>（<code>${escHtml(ctx.camera_slug)}</code>）· <strong>已复核</strong>分片 <strong>${ctx.clip_count}</strong> 个 · 模型层 <code>${escHtml(ctx.pose_tier)}</code> 可匹配记录 <strong>${ctx.matched_record_count}</strong> 个`;
}

async function loadAccuracyCameras() {
  const sel = acc$("#accuracy-camera");
  if (!sel) return;
  try {
    const res = await fetch("/api/accuracy/cameras");
    if (!res.ok) throw new Error(await readApiError(res));
    const body = await res.json();
    const cameras = Array.isArray(body.cameras) ? body.cameras : [];
    sel.innerHTML = '<option value="">— 请选择机位 —</option>';
    cameras.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.camera_label;
      opt.textContent = `${c.camera_label}（已复核 ${c.clip_count} 片）`;
      sel.appendChild(opt);
    });
    if (!cameras.length) {
      sel.innerHTML = '<option value="">— 无 review 范本数据 —</option>';
    }
  } catch (err) {
    sel.innerHTML = '<option value="">— 加载失败 —</option>';
    setAccuracyStatus(`❌ 机位列表加载失败：${escHtml(err.message)}`, true);
  }
}

async function refreshAccuracyContext() {
  const poseTier = acc$("#accuracy-pose-tier")?.value || "rtmpose-m";
  const camera = acc$("#accuracy-camera")?.value || "";
  if (!camera) {
    acc$("#accuracy-context-hint")?.classList.add("hidden");
    return;
  }
  try {
    const qs = new URLSearchParams({ pose_tier: poseTier, camera });
    const res = await fetch(`/api/accuracy/context?${qs}`);
    if (!res.ok) throw new Error(await readApiError(res));
    updateAccuracyContextHint(await res.json());
  } catch (err) {
    setAccuracyStatus(`❌ ${escHtml(err.message)}`, true);
  }
}

function renderAccuracySummary(result) {
  const el = acc$("#accuracy-summary");
  if (!el) return;
  const s = result.summary || {};
  el.classList.remove("hidden");
  el.innerHTML = `
    <h2 class="accuracy-summary-heading">汇总 · ${escHtml(result.camera_label)} · ${escHtml(result.pose_tier)}</h2>
    <div class="accuracy-metrics-grid">
      <div class="accuracy-metric"><span class="accuracy-metric-label">评估分片</span><span class="accuracy-metric-value">${s.evaluated ?? 0} / ${s.clip_count ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">范本取货段</span><span class="accuracy-metric-value">${s.gt_segments ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">检出成功</span><span class="accuracy-metric-value accuracy-ok">${s.detected ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">漏报</span><span class="accuracy-metric-value accuracy-warn">${s.missed ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">误报</span><span class="accuracy-metric-value accuracy-bad">${s.false_alarms ?? 0}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">召回率</span><span class="accuracy-metric-value">${pct(s.recall)}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">漏报率</span><span class="accuracy-metric-value">${pct(s.miss_rate)}</span></div>
      <div class="accuracy-metric"><span class="accuracy-metric-label">精确率（代理）</span><span class="accuracy-metric-value">${pct(s.precision_proxy)}</span></div>
    </div>
    <p class="hint accuracy-rules-hint">精确率（代理）= 检出成功 / (检出成功 + 误报)。跳过 ${s.skipped ?? 0} 片，失败 ${s.errors ?? 0} 片。</p>
  `;
}

function renderAccuracyClips(clips) {
  const wrap = acc$("#accuracy-clips-wrap");
  const body = acc$("#accuracy-clips-body");
  if (!wrap || !body) return;
  const list = Array.isArray(clips) ? clips : [];
  if (!list.length) {
    wrap.classList.add("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  body.innerHTML = list
    .map((c) => {
      const rk = c.review_key || "";
      const shortKey = rk.includes("/") ? rk.split("/").slice(1).join("/") : rk;
      const st = c.status || "";
      const stLabel =
        st === "ok" ? "完成" : st === "skipped" ? "跳过" : st === "error" ? "失败" : st;
      const stClass =
        st === "ok" ? "accuracy-st-ok" : st === "skipped" ? "accuracy-st-skip" : "accuracy-st-err";
      if (st !== "ok") {
        return `<tr>
          <td title="${escHtml(rk)}">${escHtml(shortKey.slice(0, 48))}${shortKey.length > 48 ? "…" : ""}</td>
          <td colspan="6" class="hint">${escHtml(c.error || "—")}</td>
          <td><span class="${stClass}">${stLabel}</span></td>
        </tr>`;
      }
      return `<tr>
        <td title="${escHtml(rk)}">${escHtml(shortKey.slice(0, 40))}${shortKey.length > 40 ? "…" : ""}</td>
        <td title="${escHtml(c.record_id || "")}"><code>${escHtml((c.record_id || "").split("/").pop() || "—")}</code></td>
        <td>${c.gt_segments ?? 0}</td>
        <td class="accuracy-ok">${c.detected ?? 0}</td>
        <td class="accuracy-warn">${c.missed ?? 0}</td>
        <td class="accuracy-bad">${c.false_alarms ?? 0}</td>
        <td>${pct(c.recall)}</td>
        <td><span class="${stClass}">${stLabel}</span></td>
      </tr>`;
    })
    .join("");
}

async function runAccuracyEvaluation() {
  const poseTier = acc$("#accuracy-pose-tier")?.value || "rtmpose-m";
  const camera = acc$("#accuracy-camera")?.value || "";
  if (!camera) {
    setAccuracyStatus("请选择机位", true);
    return;
  }
  setAccuracyStatus("正在批量评估，请稍候…");
  acc$("#accuracy-summary")?.classList.add("hidden");
  acc$("#accuracy-clips-wrap")?.classList.add("hidden");
  try {
    const res = await fetch("/api/accuracy/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pose_tier: poseTier, camera }),
    });
    if (!res.ok) throw new Error(await readApiError(res));
    const result = await res.json();
    renderAccuracySummary(result);
    renderAccuracyClips(result.clips);
    const s = result.summary || {};
    setAccuracyStatus(
      `✅ 评估完成：${s.evaluated ?? 0} 个分片，召回率 ${pct(s.recall)}，误报 ${s.false_alarms ?? 0} 次`
    );
  } catch (err) {
    setAccuracyStatus(`❌ ${escHtml(err.message)}`, true);
  }
}

let accuracyPanelInited = false;

function initAccuracyPanel() {
  if (accuracyPanelInited) return;
  accuracyPanelInited = true;
  loadAccuracyCameras();

  acc$("#accuracy-pose-tier")?.addEventListener("change", () => {
    void refreshAccuracyContext();
  });
  acc$("#accuracy-camera")?.addEventListener("change", () => {
    void refreshAccuracyContext();
  });
  acc$("#accuracy-run")?.addEventListener("click", () => {
    void runAccuracyEvaluation();
  });
}

window.initAccuracyPanel = initAccuracyPanel;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initAccuracyPanel);
} else {
  initAccuracyPanel();
}
