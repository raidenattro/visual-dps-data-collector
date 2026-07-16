#!/usr/bin/env python3
"""baseline + sklearn_hist_gradient_boosting 否决式融合，导出两版 upload 目录并生成实验报告。

A 版 fullfeat：skeleton.parquet 全帧 frame_index，仅在 baseline export 帧融合写出。
B 版 interval2：skeleton 仅保留 export 帧（与 pose_frame_interval=2 对齐）。

用法（项目根目录）:
  python scripts/data/export_ml_veto_upload.py
  python scripts/data/export_ml_veto_upload.py --skip-eval
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_config_path
from event_engine.ml_picking_predict import (
    MlFeatureMode,
    load_sklearn_picking_model,
    make_registry_for_model,
    predict_and_fuse_clip,
)
from api.inference_eval_service import load_inference_json_file
from api.record_service import locate_record_by_id
from api.wrist_features_service import (
    _infer_size_from_frames,
    load_annotation_config_for_export,
)
from pose_store import load_all_frames, load_manifest
from scripts.data.upload_export_common import (
    MANIFEST_NAME,
    POSE_FRAME_INTERVAL,
    build_output_manifest,
    export_indices_for_record,
    load_baseline_manifest,
    resolve_baseline_clip_path,
)

DEFAULT_BASELINE_DIR = ROOT / "localdata/export/rule-baseline-local-prod-test"
DEFAULT_SHELF_ROOT = Path(r"D:\work\workspace\git-repo\ShelfPickSense")
DEFAULT_MODEL_DIR = (
    DEFAULT_SHELF_ROOT
    / "models/feature_benchmark/run_2026-07-16_13-04-14/skeleton/sklearn_hist_gradient_boosting"
)
OUT_FULLFEAT = ROOT / "localdata/export/ml-hgb-veto-fullfeat-local-prod-test"
OUT_INTERVAL2 = ROOT / "localdata/export/ml-hgb-veto-interval2-local-prod-test"
EXPERIMENT_MD = ROOT / "localdata/export/ml-hgb-veto-experiment.md"
EXPERIMENT_JSON = ROOT / "localdata/export/ml-hgb-veto-experiment.json"

MODE_CONFIG: dict[str, dict[str, Any]] = {
    "fullfeat": {
        "output_dir": OUT_FULLFEAT,
        "ml_feature_mode": "full_skeleton_index",
        "label": "A 全量 skeleton frame_index",
    },
    "interval2": {
        "output_dir": OUT_INTERVAL2,
        "ml_feature_mode": "interval2_only_index",
        "label": "B 仅 interval=2 export 帧 skeleton",
    },
}


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2%}"


def _process_one_mode(
    *,
    mode_key: str,
    baseline_manifest: dict[str, Any],
    baseline_dir: Path,
    output_dir: Path,
    model: Any,
    registry: Any,
    shelf_root: Path,
    pose_frame_interval: int,
) -> list[dict[str, Any]]:
    feature_mode: MlFeatureMode = MODE_CONFIG[mode_key]["ml_feature_mode"]
    results: list[dict[str, Any]] = []
    records = list(baseline_manifest.get("records") or [])

    output_dir.mkdir(parents=True, exist_ok=True)

    for entry in records:
        record_id = str(entry.get("record_id") or "").strip()
        loc = locate_record_by_id(record_id)
        if not loc:
            results.append({"record_id": record_id, "status": "error", "error": "本地记录不存在"})
            continue

        clip_path = resolve_baseline_clip_path(baseline_dir, entry)
        if clip_path is None or not clip_path.is_file():
            results.append({"record_id": record_id, "status": "error", "error": "缺少 baseline clip"})
            continue

        baseline_frames = load_inference_json_file(clip_path)
        export_indices = export_indices_for_record(
            entry,
            baseline_dir=baseline_dir,
            timeline_frames=load_all_frames(loc),
            pose_frame_interval=pose_frame_interval,
        )
        if not export_indices:
            results.append({"record_id": record_id, "status": "error", "error": "无法确定 export 帧"})
            continue

        manifest = load_manifest(loc)
        timeline_frames = load_all_frames(loc)
        infer_w, infer_h = _infer_size_from_frames(
            timeline_frames,
            manifest,
        )
        if entry.get("infer_width") and entry.get("infer_height"):
            infer_w = float(entry["infer_width"])
            infer_h = float(entry["infer_height"])

        ann = load_annotation_config_for_export(loc, manifest)
        if not ann:
            results.append({"record_id": record_id, "status": "error", "error": "缺少 annotation"})
            continue

        try:
            fused = predict_and_fuse_clip(
                baseline_frames,
                locator=loc,
                record_id=record_id,
                annotation=ann,
                infer_width=infer_w,
                infer_height=infer_h,
                model=model,
                registry=registry,
                feature_mode=feature_mode,
                export_indices=export_indices,
                shelf_root=shelf_root,
            )
        except Exception as exc:
            results.append({"record_id": record_id, "status": "error", "error": str(exc)})
            continue

        if not fused:
            results.append({"record_id": record_id, "status": "error", "error": "融合无有效帧"})
            continue

        out_name = str(entry.get("file") or f"{entry.get('clip_name') or record_id}.json")
        if not out_name.endswith(".json"):
            out_name = f"{out_name}.json"
        out_path = output_dir / out_name
        out_path.write_text(json.dumps(fused, ensure_ascii=False, indent=2), encoding="utf-8")

        veto_n = sum(1 for r in fused if r.get("ml_vetoed"))
        baseline_pick_n = sum(1 for r in fused if r.get("baseline_is_picking"))
        pick_n = sum(1 for r in fused if r.get("is_picking"))
        results.append(
            {
                "status": "ok",
                "record_id": record_id,
                "clip_name": entry.get("clip_name") or out_path.stem,
                "camera_slug": entry.get("camera_slug"),
                "file": out_name,
                "path": str(out_path.resolve()),
                "frame_count_exported": len(fused),
                "picking_frame_count": pick_n,
                "baseline_picking_frame_count": baseline_pick_n,
                "ml_veto_count": veto_n,
                "annotation_file": entry.get("annotation_file"),
                "infer_width": infer_w,
                "infer_height": infer_h,
                **{k: entry[k] for k in (
                    "frame_count_timeline",
                    "frame_count_skeleton",
                    "frame_range_min",
                    "frame_range_max",
                    "stored_pose_frame_interval",
                    "infer_size_record_dir",
                ) if k in entry},
            }
        )
        print(
            f"[{mode_key}] {record_id}: export={len(fused)} "
            f"baseline_pick={baseline_pick_n} final_pick={pick_n} veto={veto_n}"
        )
    return results


def _write_mode_manifest(
    *,
    baseline_manifest: dict[str, Any],
    baseline_dir: Path,
    output_dir: Path,
    results: list[dict[str, Any]],
    mode_key: str,
    model_dir: Path,
    shelf_root: Path,
    pose_frame_interval: int,
) -> dict[str, Any]:
    cfg = MODE_CONFIG[mode_key]
    manifest = build_output_manifest(
        baseline_manifest,
        baseline_dir=baseline_dir,
        results=results,
        params_patch={
            "baseline_type": "ml_sklearn_hist_gradient_boosting_veto",
            "fusion": "baseline_and_ml_veto",
            "ml_veto_rule": "ml_is_picking == false",
            "ml_feature_mode": cfg["ml_feature_mode"],
            "ml_feature_mode_label": cfg["label"],
            "model_name": "sklearn_hist_gradient_boosting",
            "model_dir": str(model_dir.resolve()),
            "shelf_picksense_root": str(shelf_root.resolve()),
            "source_baseline_manifest": str((baseline_dir / MANIFEST_NAME).resolve()),
            "pose_frame_interval": pose_frame_interval,
            "fail_open_empty_skeleton": True,
            "writeback_timeline": False,
        },
    )
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _load_accuracy_summary(dir_path: Path) -> dict[str, Any] | None:
    path = dir_path / "accuracy_report.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("summary") if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _run_eval(dirs: list[Path]) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/data/evaluate_inference_upload.py"),
        "--dirs",
        *[str(d) for d in dirs],
        "--in-place",
    ]
    print("\n运行评估:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def _run_compare(baseline_dir: Path, experiment_dir: Path) -> Path | None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts/data/compare_export_false_alarms.py"),
        "--baseline",
        str(baseline_dir),
        "--experiment",
        str(experiment_dir),
        "--output-dir",
        str(ROOT / "localdata/export"),
    ]
    print("运行对比:", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return None
    if proc.stdout.strip():
        print(proc.stdout.strip())
    name = f"compare_{experiment_dir.name}_vs_{baseline_dir.name}.md"
    return ROOT / "localdata/export" / name


def render_experiment_md(
    *,
    baseline_dir: Path,
    model_dir: Path,
    manifests: dict[str, dict[str, Any]],
    summaries: dict[str, dict[str, Any] | None],
    compare_paths: dict[str, Path | None],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base_sum = summaries.get("baseline")
    lines = [
        "# ML 否决式辅助过滤实验（sklearn_hist_gradient_boosting）",
        "",
        f"> 生成时间：{now}",
        "",
        "## 实验设计",
        "",
        "| 项 | 说明 |",
        "|----|------|",
        f"| baseline 对照 | `{baseline_dir}` |",
        f"| 模型目录 | `{model_dir}` |",
        "| 融合逻辑 | `final_is_picking = baseline_is_picking AND ml_is_picking` |",
        "| ML 否决 | `ml_is_picking == False` → 不告警 |",
        "| `rule_collisions` | 保持 baseline 原值 |",
        "| `rule_alarm_collisions` | baseline 告警货框，仅 final 为真时保留 |",
        "| `picking_prob` | 每 export 帧写入 ML 概率 |",
        "| `predicted_box_tokens` | ML 货框，仅 `ml_is_picking` 时填写 |",
        "| 无骨架帧 | fail-open（不否决 baseline） |",
        "",
        "## 两版差异",
        "",
        "| 版本 | 目录 | ML 特征 frame_index |",
        "|------|------|---------------------|",
        f"| A fullfeat | `{OUT_FULLFEAT.name}` | skeleton.parquet **全帧**（与训练一致） |",
        f"| B interval2 | `{OUT_INTERVAL2.name}` | skeleton **仅 export 帧**（与 baseline 采样对齐） |",
        "",
        "## 导出统计",
        "",
        "| 版本 | 记录数 | export 帧告警合计 | baseline 告警 | ML 否决帧数 |",
        "|------|--------|-------------------|---------------|-------------|",
    ]

    for key in ("fullfeat", "interval2"):
        m = manifests.get(key) or {}
        ok = [r for r in (m.get("records") or []) if r.get("status") == "ok"]
        lines.append(
            f"| {MODE_CONFIG[key]['label']} | {m.get('exported_count', 0)} | "
            f"{(m.get('summary') or {}).get('picking_frames', 0)} | "
            f"{sum(int(r.get('baseline_picking_frame_count') or 0) for r in ok)} | "
            f"{sum(int(r.get('ml_veto_count') or 0) for r in ok)} |"
        )

    lines += [
        "",
        "## 准确率评估（相对 event_review 标真）",
        "",
        "| 目录 | TP | FP | FN | 召回率 | 精确率(代理) |",
        "|------|-----|-----|-----|--------|-------------|",
    ]

    if base_sum:
        lines.append(
            f"| baseline | {base_sum.get('detected', '—')} | {base_sum.get('false_alarms', '—')} | "
            f"{base_sum.get('missed', '—')} | {_pct(base_sum.get('recall'))} | "
            f"{_pct(base_sum.get('precision_proxy'))} |"
        )

    for key, out_dir in (("fullfeat", OUT_FULLFEAT), ("interval2", OUT_INTERVAL2)):
        s = summaries.get(key)
        if s:
            lines.append(
                f"| {out_dir.name} | {s.get('detected', '—')} | {s.get('false_alarms', '—')} | "
                f"{s.get('missed', '—')} | {_pct(s.get('recall'))} | "
                f"{_pct(s.get('precision_proxy'))} |"
            )

    lines += ["", "## 对比报告", ""]
    for key in ("fullfeat", "interval2"):
        p = compare_paths.get(key)
        if p and p.is_file():
            lines.append(f"- [{p.name}]({p.name})")
        else:
            lines.append(f"- `{MODE_CONFIG[key]['output_dir'].name}` 对比报告未生成")

    lines += [
        "",
        "## 复现命令",
        "",
        "```bash",
        "python scripts/data/export_ml_veto_upload.py",
        "python scripts/data/evaluate_inference_upload.py \\",
        f"  --dirs {baseline_dir} {OUT_FULLFEAT.name} {OUT_INTERVAL2.name} \\",
        "  --in-place",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 ML 否决融合 upload 目录（两版）")
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--shelf-root", type=Path, default=DEFAULT_SHELF_ROOT)
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--skip-eval", action="store_true", help="跳过 accuracy 评估与对比")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    baseline_dir = args.baseline_dir.resolve()
    model_dir = args.model_dir.resolve()
    shelf_root = args.shelf_root.resolve()

    manifest_path = baseline_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"缺少 baseline manifest: {manifest_path}", file=sys.stderr)
        return 2
    if not model_dir.is_dir():
        print(f"缺少模型目录: {model_dir}", file=sys.stderr)
        return 2

    baseline_manifest = load_baseline_manifest(manifest_path)

    if args.dry_run:
        n = len(baseline_manifest.get("records") or [])
        print(f"baseline: {baseline_dir}")
        print(f"model: {model_dir}")
        print(f"将处理 {n} 条记录 × 2 版本")
        print(f"输出: {OUT_FULLFEAT}")
        print(f"      {OUT_INTERVAL2}")
        return 0

    print("加载模型...")
    model = load_sklearn_picking_model(model_dir, shelf_root=shelf_root)
    registry = make_registry_for_model(model, shelf_root=shelf_root)
    print(f"模型: {model.name}, frame_features={len(model.frame_feature_names)}")

    mode_manifests: dict[str, dict[str, Any]] = {}
    for mode_key in ("fullfeat", "interval2"):
        out_dir = MODE_CONFIG[mode_key]["output_dir"]
        print(f"\n=== {MODE_CONFIG[mode_key]['label']} ===")
        results = _process_one_mode(
            mode_key=mode_key,
            baseline_manifest=baseline_manifest,
            baseline_dir=baseline_dir,
            output_dir=out_dir,
            model=model,
            registry=registry,
            shelf_root=shelf_root,
            pose_frame_interval=args.pose_frame_interval,
        )
        mode_manifests[mode_key] = _write_mode_manifest(
            baseline_manifest=baseline_manifest,
            baseline_dir=baseline_dir,
            output_dir=out_dir,
            results=results,
            mode_key=mode_key,
            model_dir=model_dir,
            shelf_root=shelf_root,
            pose_frame_interval=args.pose_frame_interval,
        )
        err_n = mode_manifests[mode_key].get("error_count", 0)
        if err_n:
            print(f"警告: {mode_key} 有 {err_n} 条错误")

    compare_paths: dict[str, Path | None] = {}
    summaries: dict[str, dict[str, Any] | None] = {"baseline": None, "fullfeat": None, "interval2": None}

    if not args.skip_eval:
        eval_dirs = [baseline_dir, OUT_FULLFEAT, OUT_INTERVAL2]
        try:
            _run_eval(eval_dirs)
        except subprocess.CalledProcessError as exc:
            print(f"评估失败: {exc}", file=sys.stderr)

        summaries["baseline"] = _load_accuracy_summary(baseline_dir)
        summaries["fullfeat"] = _load_accuracy_summary(OUT_FULLFEAT)
        summaries["interval2"] = _load_accuracy_summary(OUT_INTERVAL2)

        for key, out_dir in (("fullfeat", OUT_FULLFEAT), ("interval2", OUT_INTERVAL2)):
            compare_paths[key] = _run_compare(baseline_dir, out_dir)

    md = render_experiment_md(
        baseline_dir=baseline_dir,
        model_dir=model_dir,
        manifests=mode_manifests,
        summaries=summaries,
        compare_paths=compare_paths,
    )
    EXPERIMENT_MD.write_text(md, encoding="utf-8")
    EXPERIMENT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "baseline_dir": str(baseline_dir),
                "model_dir": str(model_dir),
                "outputs": {
                    "fullfeat": str(OUT_FULLFEAT),
                    "interval2": str(OUT_INTERVAL2),
                },
                "manifests": mode_manifests,
                "accuracy_summaries": summaries,
                "compare_reports": {k: str(v) if v else None for k, v in compare_paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n实验报告: {EXPERIMENT_MD}")
    print(f"实验 JSON: {EXPERIMENT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
