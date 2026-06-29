#!/usr/bin/env python3
"""预下载 ONNX 权重到 localdata/models/onnx（支持 OPENMMLAB_MIRROR_BASE 镜像）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config_loader import load_config_file, resolve_app_paths
from model_assets import (
    RTMDET_VARIANT_ASSETS,
    RTMPOSE_VARIANT_ASSETS,
    describe_models_layout,
    ensure_detection_onnx,
    ensure_pose_onnx,
    openmmlab_sdk_base,
    resolve_det_assets,
)


def main() -> int:
    p = argparse.ArgumentParser(description="下载 RTMDet + RTMPose ONNX 到标准目录")
    p.add_argument(
        "--models-dir",
        default="",
        help="ONNX 根目录（默认 config paths.models_onnx_dir）",
    )
    p.add_argument(
        "--det",
        default="nano,m",
        help="检测档，逗号分隔：nano,m（默认 nano,m；t 为 nano 旧别名）",
    )
    p.add_argument(
        "--pose",
        default="t,s,m",
        help="姿态档，逗号分隔：t,s,m（默认全部）",
    )
    args = p.parse_args()

    cfg = load_config_file()
    paths = resolve_app_paths(cfg, base=_ROOT)
    models_dir = args.models_dir.strip() or str(paths.models_onnx_dir)
    models_dir = str(Path(models_dir).resolve())

    print(f"📦 ONNX 根目录: {models_dir}")
    print(f"🌐 OpenMMLab: {openmmlab_sdk_base()}")
    print(describe_models_layout(models_dir))

    det_keys = [x.strip() for x in args.det.split(",") if x.strip()]
    pose_keys = [x.strip() for x in args.pose.split(",") if x.strip()]

    for d in det_keys:
        try:
            _, assets = resolve_det_assets(d)
        except ValueError:
            print(f"⚠️ 跳过未知 det 档: {d}", file=sys.stderr)
            continue
        path = ensure_detection_onnx(models_dir, d)
        print(f"✅ detection/{assets['det_dir']}: {path}")

    for v in pose_keys:
        if v not in RTMPOSE_VARIANT_ASSETS:
            print(f"⚠️ 跳过未知 pose 档: {v}", file=sys.stderr)
            continue
        path = ensure_pose_onnx(models_dir, v)
        print(f"✅ pose/{RTMPOSE_VARIANT_ASSETS[v]['pose_dir']}: {path}")

    print("==> 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
