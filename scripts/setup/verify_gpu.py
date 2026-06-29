#!/usr/bin/env python3
"""验证 ONNX Runtime GPU 是否可用（Linux / Windows 通用）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    p = argparse.ArgumentParser(description="验证 GPU 推理环境")
    p.add_argument(
        "--models-dir",
        default="localdata/models/onnx",
        help="ONNX 根目录（默认 localdata/models/onnx）",
    )
    p.add_argument(
        "--backend",
        default="t",
        help="姿态档 t/s/m（默认 t）",
    )
    p.add_argument(
        "--det",
        default="m",
        help="检测档 nano/m（默认 m；t 为 nano 旧别名）",
    )
    p.add_argument(
        "--skip-infer",
        action="store_true",
        help="仅检查 EP，不跑 dummy 推理",
    )
    args = p.parse_args()

    from ort_cuda_setup import nvidia_dll_dirs_available, prepare_ort_cuda_dll_path
    from rtmpose_infer import RTMPosePipeline, ort_available_providers

    dirs = nvidia_dll_dirs_available()
    print(f"ℹ️ NVIDIA 库目录: {len(dirs)} 个")
    for d in dirs[:4]:
        print(f"   - {d}")
    if len(dirs) > 4:
        print(f"   … 共 {len(dirs)} 个")

    prepare_ort_cuda_dll_path()
    providers = ort_available_providers()
    print(f"ℹ️ ORT 可用 EP: {providers}")

    if "CUDAExecutionProvider" not in providers:
        print("❌ 未检测到 CUDAExecutionProvider", file=sys.stderr)
        return 1

    if args.skip_infer:
        print("✅ CUDAExecutionProvider 可用")
        return 0

    import numpy as np

    pipe = RTMPosePipeline(
        variant=args.backend,
        det_variant=args.det,
        models_dir=args.models_dir,
        device="cuda",
    )
    pipe.load()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    batch = pipe.infer(frame)
    print(f"✅ GPU 推理就绪 device={pipe.device} persons={batch.num_persons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
