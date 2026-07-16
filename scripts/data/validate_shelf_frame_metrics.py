#!/usr/bin/env python3
"""用 ShelfPickSense 帧级指标（compute_picking_metrics）评估 export 目录，并与原仓库 benchmark 对照。

评测口径与 ShelfPickSense analysis/evaluation.py 一致：
- GT：event_review.verified_true 逐帧 is_picking
- 遍历 skeleton.parquet 全部帧
- export 仅含 interval=2 子集时，未导出帧 pred=False（与 rule_collision interval=2 一致）

用法（项目根目录）:
  python scripts/data/validate_shelf_frame_metrics.py
  python scripts/data/validate_shelf_frame_metrics.py --export-dir localdata/export/ml-hgb-only-fullfeat-local-prod-test
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHELF_ROOT = Path(r"D:\work\workspace\git-repo\ShelfPickSense")
DEFAULT_MODEL_DIR = (
    DEFAULT_SHELF_ROOT
    / "models/feature_benchmark/run_2026-07-16_13-04-14/skeleton/sklearn_hist_gradient_boosting"
)
DEFAULT_BASELINE_EXPORT = ROOT / "localdata/export/rule-baseline-local-prod-test"
DEFAULT_ML_ONLY_EXPORT = ROOT / "localdata/export/ml-hgb-only-fullfeat-local-prod-test"
DEFAULT_ML_VETO_EXPORT = ROOT / "localdata/export/ml-hgb-veto-fullfeat-local-prod-test"
OUT_JSON = ROOT / "localdata/export/shelf-frame-metrics-alignment.json"
OUT_MD = ROOT / "localdata/export/shelf-frame-metrics-alignment.md"

TEST_RECORDS = ["record_002", "record_006", "record_013", "record_017", "record_026"]


def _ensure_shelf_import(shelf_root: Path) -> None:
    src = shelf_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _load_data28_clip_map(shelf_root: Path) -> dict[str, str]:
    """folder -> clip 文件名（无扩展名）。"""
    manifest_path = shelf_root / "data/data28/manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for item in data.get("records") or []:
        if not isinstance(item, dict):
            continue
        folder = str(item.get("folder") or "").strip()
        clip = str(item.get("clip") or "").strip()
        if folder and clip:
            out[folder] = clip
    return out


def _load_export_preds(export_dir: Path) -> dict[str, dict[int, dict[str, Any]]]:
    """shelf_record_folder -> frame_idx -> row。"""
    manifest_path = export_dir / "_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"缺少 export manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    preds: dict[str, dict[int, dict[str, Any]]] = {}

    for rec in manifest.get("records") or []:
        if not isinstance(rec, dict) or rec.get("status") != "ok":
            continue
        infer_dir = str(rec.get("infer_size_record_dir") or "").strip()
        if not infer_dir:
            continue
        folder = Path(infer_dir).name
        clip_file = str(rec.get("file") or "").strip()
        if not clip_file:
            continue
        clip_path = export_dir / clip_file
        if not clip_path.is_file():
            continue
        rows = json.loads(clip_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            continue
        by_frame: dict[int, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                fi = int(row.get("frame_idx") or 0)
            except (TypeError, ValueError):
                continue
            by_frame[fi] = row
        preds[folder] = by_frame
    return preds


def _discover_all_record_dirs(shelf_root: Path) -> dict[str, Path]:
    """record_folder -> 含 skeleton 的目录路径。"""
    merged_train = shelf_root / "data/data28-merged/Train"
    mapping: dict[str, Path] = {}
    if merged_train.is_dir():
        for child in sorted(merged_train.iterdir()):
            if child.is_dir() and (child / "skeleton.parquet").is_file():
                mapping[child.name] = child.resolve()
    # 补齐 merged 中可能缺失的 Test 专项目录
    for split in ("Train", "Test"):
        split_dir = shelf_root / "data/data28" / split
        if not split_dir.is_dir():
            continue
        for child in sorted(split_dir.iterdir()):
            if child.is_dir() and (child / "skeleton.parquet").is_file():
                mapping.setdefault(child.name, child.resolve())
    return mapping


def evaluate_export_frame_level(
    *,
    export_dir: Path,
    shelf_root: Path,
    record_folders: list[str] | None = None,
    export_frames_only: bool = False,
) -> dict[str, Any]:
    _ensure_shelf_import(shelf_root)
    from analysis.evaluation import compute_box_metrics, compute_picking_metrics
    from analysis.records import load_record

    export_preds = _load_export_preds(export_dir)
    record_dirs = _discover_all_record_dirs(shelf_root)
    folders = record_folders or sorted(export_preds.keys())

    y_true: list[bool] = []
    y_pred: list[bool] = []
    true_boxes: list[set[str]] = []
    pred_boxes: list[set[str]] = []
    per_record: list[dict[str, Any]] = []

    for folder in folders:
        record_dir = record_dirs.get(folder)
        if record_dir is None:
            per_record.append({"record_id": folder, "status": "missing_record_dir"})
            continue
        pred_by_frame = export_preds.get(folder)
        if not pred_by_frame:
            per_record.append({"record_id": folder, "status": "missing_export"})
            continue

        record = load_record(record_dir)
        r_true: list[bool] = []
        r_pred: list[bool] = []
        export_frame_count = 0

        for frame in record.frames():
            label = record.labels.label_for(frame.frame_idx)
            row = pred_by_frame.get(frame.frame_idx)
            if export_frames_only and row is None:
                continue
            if row is not None:
                export_frame_count += 1
                pred_is_picking = bool(row.get("is_picking"))
                pred_box_tokens = list(row.get("predicted_box_tokens") or [])
                if not pred_box_tokens and pred_is_picking:
                    pred_box_tokens = list(row.get("rule_alarm_collisions") or [])
            else:
                pred_is_picking = False
                pred_box_tokens = []

            true_is_picking = label.is_picking
            y_true.append(true_is_picking)
            y_pred.append(pred_is_picking)
            r_true.append(true_is_picking)
            r_pred.append(pred_is_picking)

            if label.is_picking and label.confirmed_box_tokens:
                true_boxes.append(set(label.confirmed_box_tokens))
                pred_boxes.append(set(pred_box_tokens))

        picking = compute_picking_metrics(r_true, r_pred)
        per_record.append(
            {
                "record_id": folder,
                "status": "ok",
                "frame_count": len(r_true),
                "export_frame_count": export_frame_count,
                "positive_frames": sum(r_true),
                "tp": picking.tp,
                "fp": picking.fp,
                "fn": picking.fn,
                "precision": picking.precision,
                "recall": picking.recall,
                "f1": picking.f1,
                "macro_f1": picking.macro_f1,
            }
        )

    picking = compute_picking_metrics(y_true, y_pred)
    box = compute_box_metrics(true_boxes, pred_boxes)
    return {
        "export_dir": str(export_dir.resolve()),
        "record_ids": folders,
        "export_frames_only": export_frames_only,
        "picking": asdict(picking),
        "box": asdict(box),
        "extra": {
            "frame_count": len(y_true),
            "positive_frames": sum(y_true),
            "box_eval_frames": len(true_boxes),
        },
        "per_record": per_record,
    }


def evaluate_native_shelf(
    *,
    shelf_root: Path,
    data_dir: Path,
    model_dir: Path | None = None,
    rule_collision: bool = False,
) -> dict[str, Any]:
    _ensure_shelf_import(shelf_root)
    from analysis.evaluation import evaluate_model
    from analysis.records import load_all_records
    from analysis.rule_baseline import run_external_collision_baseline

    records = load_all_records(data_dir)
    if rule_collision:
        report = run_external_collision_baseline(
            records,
            data_dir=str(data_dir.resolve()),
            output_dir=ROOT / "localdata/export/_tmp_shelf_rule_collision_eval",
            video_fps=15.0,
            alarm_min_consecutive_frames=3,
            alarm_cooldown_frames=0,
            pose_frame_interval=2,
            model_name="rule_collision",
            predictions_filename="eval_predictions.json",
        )
    else:
        if model_dir is None:
            raise ValueError("model_dir required for ML native eval")
        report = evaluate_model(
            model_dir,
            data_dir,
            predictions_output_path=ROOT / "localdata/export/_tmp_shelf_ml_eval/predictions.json",
        )
    return report.to_dict()


def _load_saved_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_metrics(p: dict[str, Any]) -> str:
    return (
        f"Macro-F1={p.get('macro_f1', 0):.4f}, "
        f"取货P={p.get('precision', 0):.4f}, "
        f"取货R={p.get('recall', 0):.4f}, "
        f"取货F1={p.get('f1', 0):.4f}, "
        f"tp={p.get('tp')}, fp={p.get('fp')}, fn={p.get('fn')}"
    )


def _render_md(results: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# ShelfPickSense 帧级指标对齐报告",
        "",
        f"> 生成时间：{now}",
        "> 口径：ShelfPickSense `compute_picking_metrics`（逐 skeleton 帧；export 未覆盖帧 pred=False）",
        "",
        "## Test 5 条对比",
        "",
        "| 来源 | Macro-F1 | 取货 Precision | 取货 Recall | 取货 F1 | TP | FP | FN | 帧数 |",
        "|------|----------|----------------|-------------|---------|----|----|-----|------|",
    ]

    test = results.get("test5") or {}
    for key, label in [
        ("saved_ml_report", "原仓库 ML eval_report（存档）"),
        ("native_ml_rerun", "本地重跑 evaluate_model"),
        ("export_ml_only", "export ml-hgb-only-fullfeat"),
        ("saved_rule_report", "原仓库 rule_collision eval_report（存档）"),
        ("native_rule_rerun", "本地重跑 rule_collision"),
        ("export_rule_baseline", "export rule-baseline-local-prod-test"),
        ("export_ml_veto", "export ml-hgb-veto-fullfeat"),
        ("export_ml_only_interval2", "export ml-only（仅 interval=2 帧）"),
    ]:
        row = test.get(key)
        if not row:
            continue
        p = row.get("picking") or row
        lines.append(
            f"| {label} | {p.get('macro_f1', 0):.4f} | {p.get('precision', 0):.4f} | "
            f"{p.get('recall', 0):.4f} | {p.get('f1', 0):.4f} | "
            f"{p.get('tp')} | {p.get('fp')} | {p.get('fn')} | "
            f"{(row.get('extra') or {}).get('frame_count', '—')} |"
        )

    lines.extend(
        [
            "",
            "## manifest28 全量（28 条）",
            "",
            "| export 目录 | Macro-F1 | 取货 P | 取货 R | TP | FP | FN | 帧数 | 正帧 |",
            "|-------------|----------|--------|--------|----|----|-----|------|------|",
        ]
    )
    for item in results.get("all28") or []:
        p = item.get("picking") or {}
        ex = item.get("extra") or {}
        name = Path(str(item.get("export_dir", ""))).name
        lines.append(
            f"| {name} | {p.get('macro_f1', 0):.4f} | {p.get('precision', 0):.4f} | "
            f"{p.get('recall', 0):.4f} | {p.get('tp')} | {p.get('fp')} | {p.get('fn')} | "
            f"{ex.get('frame_count')} | {ex.get('positive_frames')} |"
        )

    lines.extend(
        [
            "",
            "## 结论提示",
            "",
            "- **原仓库 benchmark** 与 **本地重跑** 应非常接近（验证环境一致）。",
            "- **export 帧级指标** 仅在 interval=2 子集有预测；若对全 skeleton 帧计分（未导出帧 pred=False），取货 Recall 会显著低于 benchmark。",
            "- **仅 interval=2 帧计分** 时，export ml-only 应与 ShelfPickSense 原生 ML 在该子集上完全一致。",
            "- 段级 `evaluate_inference_upload` 的 FP/召回与上表数值量级不同，属正常。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="ShelfPickSense 帧级指标对齐验证")
    parser.add_argument("--shelf-root", type=Path, default=DEFAULT_SHELF_ROOT)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--skip-native", action="store_true", help="跳过重跑 ShelfPickSense 原生评测")
    args = parser.parse_args()

    shelf_root = args.shelf_root.resolve()
    test_data_dir = shelf_root / "data/data28/Test"
    all28_data_dir = shelf_root / "data/data28-merged/Train"

    results: dict[str, Any] = {"test5": {}, "all28": []}

    saved_ml = _load_saved_report(
        shelf_root
        / "models/feature_benchmark/run_2026-07-16_13-04-14/skeleton/sklearn_hist_gradient_boosting/eval_report.json"
    )
    saved_rule = _load_saved_report(
        shelf_root / "models/feature_benchmark/run_2026-07-16_13-04-14/rule_collision/eval_report.json"
    )
    if saved_ml:
        results["test5"]["saved_ml_report"] = saved_ml
    if saved_rule:
        results["test5"]["saved_rule_report"] = saved_rule

    if not args.skip_native:
        print("重跑 ShelfPickSense evaluate_model (Test 5)...")
        results["test5"]["native_ml_rerun"] = evaluate_native_shelf(
            shelf_root=shelf_root,
            data_dir=test_data_dir,
            model_dir=args.model_dir,
        )
        print("重跑 rule_collision baseline (Test 5)...")
        try:
            results["test5"]["native_rule_rerun"] = evaluate_native_shelf(
                shelf_root=shelf_root,
                data_dir=test_data_dir,
                rule_collision=True,
            )
        except Exception as exc:
            print(f"rule_collision 重跑失败（沿用存档对比）: {exc}")
            results["test5"]["native_rule_rerun_error"] = str(exc)

    export_specs = [
        ("export_ml_only", DEFAULT_ML_ONLY_EXPORT),
        ("export_rule_baseline", DEFAULT_BASELINE_EXPORT),
        ("export_ml_veto", DEFAULT_ML_VETO_EXPORT),
    ]
    for key, export_dir in export_specs:
        if not export_dir.is_dir():
            print(f"跳过缺失目录: {export_dir}")
            continue
        print(f"帧级评估 Test5: {export_dir.name}")
        results["test5"][key] = evaluate_export_frame_level(
            export_dir=export_dir,
            shelf_root=shelf_root,
            record_folders=TEST_RECORDS,
        )

    if DEFAULT_ML_ONLY_EXPORT.is_dir():
        print("帧级评估 Test5 ml-only（仅 interval=2 帧）")
        results["test5"]["export_ml_only_interval2"] = evaluate_export_frame_level(
            export_dir=DEFAULT_ML_ONLY_EXPORT,
            shelf_root=shelf_root,
            record_folders=TEST_RECORDS,
            export_frames_only=True,
        )

    # 若存在本地重跑结果，写入报告
    tmp_native = ROOT / "localdata/export/_tmp_shelf_ml_eval/predictions.json"
    if tmp_native.is_file():
        _ensure_shelf_import(shelf_root)
        from analysis.evaluation import compute_picking_metrics

        rows = json.loads(tmp_native.read_text(encoding="utf-8")).get("predictions") or []
        y_true, y_pred = [], []
        for row in rows:
            if row.get("record_id") not in TEST_RECORDS:
                continue
            y_true.append(bool(row.get("true_is_picking")))
            y_pred.append(bool(row.get("pred_is_picking")))
        picking = compute_picking_metrics(y_true, y_pred)
        results["test5"]["native_ml_rerun"] = {
            "picking": asdict(picking),
            "extra": {"frame_count": len(y_true), "positive_frames": sum(y_true)},
        }

    for export_dir in [DEFAULT_BASELINE_EXPORT, DEFAULT_ML_ONLY_EXPORT, DEFAULT_ML_VETO_EXPORT]:
        if not export_dir.is_dir():
            continue
        print(f"帧级评估 manifest28: {export_dir.name}")
        item = evaluate_export_frame_level(
            export_dir=export_dir,
            shelf_root=shelf_root,
            record_folders=None,
        )
        results["all28"].append(item)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_render_md(results), encoding="utf-8")
    print(f"已写入: {OUT_JSON}")
    print(f"已写入: {OUT_MD}")

    test5 = results["test5"]
    if test5.get("saved_ml_report") and test5.get("native_ml_rerun"):
        s = test5["saved_ml_report"]["picking"]
        n = test5["native_ml_rerun"]["picking"]
        print("\n=== ML Test5 对齐 ===")
        print(f"存档: {_fmt_metrics(s)}")
        print(f"重跑: {_fmt_metrics(n)}")
    if test5.get("export_ml_only"):
        print(f"export ml-only Test5: {_fmt_metrics(test5['export_ml_only']['picking'])}")
    if test5.get("export_rule_baseline") and test5.get("native_rule_rerun"):
        e = test5["export_rule_baseline"]["picking"]
        r = test5["native_rule_rerun"]["picking"]
        print("\n=== rule_collision Test5 对齐 ===")
        print(f"export baseline: {_fmt_metrics(e)}")
        print(f"native rerun:    {_fmt_metrics(r)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
