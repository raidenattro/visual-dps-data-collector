#!/usr/bin/env python3
"""纯 ML（sklearn_hist_gradient_boosting）拣货判断，导出 upload 目录并评估。

与 baseline 使用相同 export 帧（pose_frame_interval=2），特征用全量 skeleton frame_index。

用法（项目根目录）:
  python scripts/data/export_ml_only_upload.py
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
    load_sklearn_picking_model,
    make_registry_for_model,
    predict_ml_only_clip,
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
OUT_DIR = ROOT / "localdata/export/ml-hgb-only-fullfeat-local-prod-test"
EXPERIMENT_MD = ROOT / "localdata/export/ml-hgb-only-experiment.md"
EXPERIMENT_JSON = ROOT / "localdata/export/ml-hgb-only-experiment.json"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2%}"


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
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return None
    if proc.stdout.strip():
        print(proc.stdout.strip())
    return ROOT / "localdata/export" / f"compare_{experiment_dir.name}_vs_{baseline_dir.name}.md"


def render_experiment_md(
    *,
    baseline_dir: Path,
    model_dir: Path,
    out_manifest: dict[str, Any],
    baseline_sum: dict[str, Any] | None,
    ml_sum: dict[str, Any] | None,
    compare_path: Path | None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok = [r for r in (out_manifest.get("records") or []) if r.get("status") == "ok"]
    lines = [
        "# 纯 ML 拣货判断实验（sklearn_hist_gradient_boosting）",
        "",
        f"> 生成时间：{now}",
        "",
        "## 实验设计",
        "",
        "| 项 | 说明 |",
        "|----|------|",
        f"| baseline 对照 | `{baseline_dir}` |",
        f"| 输出目录 | `{OUT_DIR}` |",
        f"| 模型目录 | `{model_dir}` |",
        "| 判定逻辑 | **仅** `ml_is_picking`（无 baseline 融合） |",
        "| ML 特征 | skeleton.parquet 全帧 frame_index（与训练一致） |",
        "| export 帧 | 与 baseline 相同（`pose_frame_interval=2`） |",
        "| `is_picking` | `ml_is_picking` |",
        "| `rule_alarm_collisions` | `predicted_box_tokens`（告警时） |",
        "| `rule_collisions` | 空（无规则引擎） |",
        "| 无骨架帧 | fail-closed → 不告警 |",
        "",
        "## 导出统计",
        "",
        f"- 记录数：{out_manifest.get('exported_count', 0)}",
        f"- export 帧告警合计：{(out_manifest.get('summary') or {}).get('picking_frames', 0)}",
        "",
        "## 准确率评估（相对 event_review 标真）",
        "",
        "| 目录 | TP | FP | FN | 召回率 | 精确率(代理) |",
        "|------|-----|-----|-----|--------|-------------|",
    ]
    if baseline_sum:
        lines.append(
            f"| baseline | {baseline_sum.get('detected', '—')} | {baseline_sum.get('false_alarms', '—')} | "
            f"{baseline_sum.get('missed', '—')} | {_pct(baseline_sum.get('recall'))} | "
            f"{_pct(baseline_sum.get('precision_proxy'))} |"
        )
    if ml_sum:
        lines.append(
            f"| {OUT_DIR.name} | {ml_sum.get('detected', '—')} | {ml_sum.get('false_alarms', '—')} | "
            f"{ml_sum.get('missed', '—')} | {_pct(ml_sum.get('recall'))} | "
            f"{_pct(ml_sum.get('precision_proxy'))} |"
        )
    lines += ["", "## 对比报告", ""]
    if compare_path and compare_path.is_file():
        lines.append(f"- [{compare_path.name}]({compare_path.name})")
    lines += [
        "",
        "## 复现命令",
        "",
        "```bash",
        "python scripts/data/export_ml_only_upload.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出纯 ML 拣货判断 upload 目录")
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--shelf-root", type=Path, default=DEFAULT_SHELF_ROOT)
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    baseline_dir = args.baseline_dir.resolve()
    output_dir = args.output_dir.resolve()
    model_dir = args.model_dir.resolve()
    shelf_root = args.shelf_root.resolve()

    manifest_path = baseline_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"缺少 baseline manifest: {manifest_path}", file=sys.stderr)
        return 2

    baseline_manifest = load_baseline_manifest(manifest_path)
    records = list(baseline_manifest.get("records") or [])

    if args.dry_run:
        print(f"baseline: {baseline_dir}")
        print(f"output: {output_dir}")
        print(f"model: {model_dir}")
        print(f"将处理 {len(records)} 条记录（纯 ML）")
        return 0

    print("加载模型...")
    model = load_sklearn_picking_model(model_dir, shelf_root=shelf_root)
    registry = make_registry_for_model(model, shelf_root=shelf_root)
    print(f"模型: {model.name}, frame_features={len(model.frame_feature_names)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for entry in records:
        record_id = str(entry.get("record_id") or "").strip()
        loc = locate_record_by_id(record_id)
        if not loc:
            results.append({"record_id": record_id, "status": "error", "error": "本地记录不存在"})
            continue

        clip_path = resolve_baseline_clip_path(baseline_dir, entry)
        if clip_path is None or not clip_path.is_file():
            results.append({"record_id": record_id, "status": "error", "error": "缺少 baseline clip（仅取 export 帧）"})
            continue

        export_indices = export_indices_for_record(
            entry,
            baseline_dir=baseline_dir,
            timeline_frames=load_all_frames(loc),
            pose_frame_interval=args.pose_frame_interval,
        )
        if not export_indices:
            results.append({"record_id": record_id, "status": "error", "error": "无法确定 export 帧"})
            continue

        manifest = load_manifest(loc)
        timeline_frames = load_all_frames(loc)
        infer_w, infer_h = _infer_size_from_frames(timeline_frames, manifest)
        if entry.get("infer_width") and entry.get("infer_height"):
            infer_w = float(entry["infer_width"])
            infer_h = float(entry["infer_height"])

        ann = load_annotation_config_for_export(loc, manifest)
        if not ann:
            results.append({"record_id": record_id, "status": "error", "error": "缺少 annotation"})
            continue

        try:
            rows = predict_ml_only_clip(
                locator=loc,
                record_id=record_id,
                annotation=ann,
                infer_width=infer_w,
                infer_height=infer_h,
                model=model,
                registry=registry,
                feature_mode="full_skeleton_index",
                export_indices=export_indices,
                shelf_root=shelf_root,
            )
        except Exception as exc:
            results.append({"record_id": record_id, "status": "error", "error": str(exc)})
            continue

        if not rows:
            results.append({"record_id": record_id, "status": "error", "error": "ML 推理无有效帧"})
            continue

        out_name = str(entry.get("file") or f"{entry.get('clip_name') or record_id}.json")
        if not out_name.endswith(".json"):
            out_name = f"{out_name}.json"
        out_path = output_dir / out_name
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

        pick_n = sum(1 for r in rows if r.get("is_picking"))
        results.append(
            {
                "status": "ok",
                "record_id": record_id,
                "clip_name": entry.get("clip_name") or out_path.stem,
                "camera_slug": entry.get("camera_slug"),
                "file": out_name,
                "path": str(out_path.resolve()),
                "frame_count_exported": len(rows),
                "picking_frame_count": pick_n,
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
        print(f"[ml_only] {record_id}: export={len(rows)} ml_pick={pick_n}")

    out_manifest = build_output_manifest(
        baseline_manifest,
        baseline_dir=baseline_dir,
        results=results,
        params_patch={
            "baseline_type": "ml_sklearn_hist_gradient_boosting_only",
            "fusion": "ml_only",
            "ml_feature_mode": "full_skeleton_index",
            "model_name": "sklearn_hist_gradient_boosting",
            "model_dir": str(model_dir.resolve()),
            "shelf_picksense_root": str(shelf_root.resolve()),
            "source_baseline_manifest": str(manifest_path.resolve()),
            "pose_frame_interval": args.pose_frame_interval,
            "fail_open_empty_skeleton": False,
            "writeback_timeline": False,
        },
    )
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(out_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    baseline_sum: dict[str, Any] | None = None
    ml_sum: dict[str, Any] | None = None
    compare_path: Path | None = None

    if not args.skip_eval:
        try:
            _run_eval([baseline_dir, output_dir])
        except subprocess.CalledProcessError as exc:
            print(f"评估失败: {exc}", file=sys.stderr)
        baseline_sum = _load_accuracy_summary(baseline_dir)
        ml_sum = _load_accuracy_summary(output_dir)
        compare_path = _run_compare(baseline_dir, output_dir)

    md = render_experiment_md(
        baseline_dir=baseline_dir,
        model_dir=model_dir,
        out_manifest=out_manifest,
        baseline_sum=baseline_sum,
        ml_sum=ml_sum,
        compare_path=compare_path,
    )
    EXPERIMENT_MD.write_text(md, encoding="utf-8")
    EXPERIMENT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "baseline_dir": str(baseline_dir),
                "output_dir": str(output_dir),
                "model_dir": str(model_dir),
                "manifest": out_manifest,
                "accuracy_summaries": {"baseline": baseline_sum, "ml_only": ml_sum},
                "compare_report": str(compare_path) if compare_path else None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    err_n = out_manifest.get("error_count", 0)
    print(
        f"\n完成: {out_manifest.get('exported_count', 0)}/{len(records)} ok, {err_n} errors\n"
        f"picking帧合计: {(out_manifest.get('summary') or {}).get('picking_frames')}\n"
        f"实验报告: {EXPERIMENT_MD}"
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
