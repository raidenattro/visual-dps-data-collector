# 问题记录

现场观察到的现象、原因与建议方案；**持续追加**，未写明「已修复」的条目均**未在代码中实现**。

格式建议：`## 编号 · 标题` → 现象 / 原因 / 方案 / 相关代码（各 1～3 句）。

---

## 001 · 骨架飞线（框外关键点）

**现象**：回放时关键点远离人体，连线穿过货架、推车等。

**原因**：RTMPose 在遮挡、蹲姿等情况下仍会给出框外且 score 偏高的错点；`infer()` 结果原样写入 JSON，无框外裁剪。回放 `SCORE_MIN=0.3` 即连线。与是否 GPU、仅换 `rtmdet_m` **无关**。

**方案**：采集后处理（框外点 score 置 0、可配置 `pose_score_min`、双肩门控）；或仅改回放（连线前校验 bbox）。参考 `box_human_det/services/inference_service.py`。

**代码**：`rtmpose_infer.py`、`collect_core.py`、`web/app.js`
