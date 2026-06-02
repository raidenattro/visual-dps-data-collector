"""RTMDet-nano + RTMPose（ONNX）同步推理，仅人体检测与姿态估计。"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from model_assets import (
    RTMPOSE_VARIANT_ASSETS,
    RTMPOSE_VARIANTS,
    ensure_detection_onnx,
    ensure_pose_onnx,
    resolve_det_assets,
    resolve_detection_onnx_path,
    resolve_pose_onnx_path,
)


def ort_available_providers() -> list[str]:
    from ort_cuda_setup import prepare_ort_cuda_dll_path

    prepare_ort_cuda_dll_path()
    import onnxruntime as ort

    return list(ort.get_available_providers())


def assert_cuda_ort_available() -> None:
    """配置为 GPU 时验证 CUDA Session 能创建（避免仅有 EP 名但缺 cuDNN DLL）。"""
    from ort_cuda_setup import nvidia_dll_dirs_available, prepare_ort_cuda_dll_path

    dirs = nvidia_dll_dirs_available()
    if not dirs:
        raise RuntimeError(
            "已请求 GPU 推理，但未找到 nvidia-cudnn-cu12。"
            " 请在当前 conda 环境执行: pip install nvidia-cudnn-cu12"
            " -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn"
        )
    prepare_ort_cuda_dll_path()
    providers = ort_available_providers()
    if "CUDAExecutionProvider" not in providers:
        raise RuntimeError(
            "已请求 GPU 推理，但 ONNX Runtime 无 CUDAExecutionProvider。"
            f" 当前 EP: {providers}。"
            " 请执行: pip uninstall onnxruntime -y && pip install onnxruntime-gpu"
        )
    import onnxruntime as ort
    from pathlib import Path

    probe = Path(
        resolve_detection_onnx_path(
            str(Path(__file__).resolve().parent / "localdata/models/onnx"),
            "rtmdet_nano",
        )
    )
    if probe.is_file():
        sess = ort.InferenceSession(str(probe), providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        if sess.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError(
                "CUDAExecutionProvider 不可用（常见原因: 缺 cudnn64_9.dll）。"
                f" 当前 EP: {sess.get_providers()}。"
                f" 已加入 DLL 路径: {dirs[:2]}…"
            )


def resolve_runtime_device(requested: str) -> str:
    """解析实际推理设备；config 已设 cuda 时不再被静默覆盖为 cpu。"""
    env = os.environ.get("INFERENCE_USE_GPU", "").strip().lower()
    if env in ("0", "false", "no"):
        return "cpu"
    if env in ("1", "true", "yes"):
        return "cuda"
    dev = str(requested or "cuda").strip().lower()
    if dev in ("cpu", "cuda", "gpu"):
        return "cuda" if dev == "gpu" else dev
    return "cuda"


def _log_ort_provider(onnx_path: str, *, expect_cuda: bool) -> str:
    from ort_cuda_setup import prepare_ort_cuda_dll_path

    if expect_cuda:
        dirs = prepare_ort_cuda_dll_path()
        if dirs:
            print(f"ℹ️ NVIDIA DLL 路径: {dirs[0]} …（共 {len(dirs)} 个）")
    import onnxruntime as ort

    available = ort.get_available_providers()
    print(f"ℹ️ ORT 可用 EP: {available}")
    providers = (
        ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if expect_cuda
        else ["CPUExecutionProvider"]
    )
    sess = ort.InferenceSession(onnx_path, providers=providers)
    active = sess.get_providers()[0]
    print(f"ℹ️ ORT 实际 EP: {active}")
    if expect_cuda and active != "CUDAExecutionProvider":
        print("⚠️ 期望 GPU 但未启用 CUDAExecutionProvider，请确认已安装 onnxruntime-gpu 且驱动正常")
    return active


@dataclass
class PoseBatch:
    keypoints: np.ndarray
    keypoint_scores: np.ndarray
    bboxes: np.ndarray

    @property
    def num_persons(self) -> int:
        if self.keypoints.size == 0:
            return 0
        return int(self.keypoints.shape[0])

    @classmethod
    def empty(cls) -> PoseBatch:
        return cls(
            keypoints=np.empty((0, 17, 2), dtype=np.float32),
            keypoint_scores=np.empty((0, 17), dtype=np.float32),
            bboxes=np.empty((0, 4), dtype=np.float32),
        )


class RTMPosePipeline:
    def __init__(
        self,
        *,
        variant: str = "t",
        det_variant: str = "t",
        models_dir: str,
        device: str = "cuda",
        backend: str = "onnxruntime",
    ):
        self.variant = str(variant or "t").lower()
        if self.variant not in RTMPOSE_VARIANTS:
            raise ValueError(f"pose variant 必须是 {RTMPOSE_VARIANTS} 之一")
        self.det_variant, self._det_assets = resolve_det_assets(det_variant)
        self.models_dir = models_dir
        self.device = resolve_runtime_device(device)
        self.backend = str(backend or "onnxruntime").strip()
        self._det = None
        self._pose = None
        self._loaded_key: tuple[str, str, str] | None = None

    def load(self) -> None:
        load_key = (self.det_variant, self.variant, self.device)
        if self._det is not None and self._pose is not None and self._loaded_key == load_key:
            return

        from rtmlib.tools.object_detection.rtmdet import RTMDet
        from rtmlib.tools.pose_estimation.rtmpose import RTMPose

        det_assets = self._det_assets
        pose_assets = RTMPOSE_VARIANT_ASSETS[self.variant]
        ensure_detection_onnx(self.models_dir, self.det_variant)
        ensure_pose_onnx(self.models_dir, self.variant)
        det_path = resolve_detection_onnx_path(
            self.models_dir, str(det_assets["det_dir"])
        )
        pose_path = resolve_pose_onnx_path(
            self.models_dir, str(pose_assets["pose_dir"])
        )

        det_size = det_assets["det_size"]
        pose_size = pose_assets["pose_size"]
        det_input_size = (int(det_size[0]), int(det_size[1]))
        pose_input_size = (int(pose_size[0]), int(pose_size[1]))

        dev = self.device
        expect_cuda = dev == "cuda"
        if expect_cuda:
            assert_cuda_ort_available()

        det_label = det_assets.get("label", self.det_variant)
        print(
            f"🚀 加载 {det_label} + RTMPose-{self.variant.upper()} device={dev}"
        )
        print(f"ℹ️ ORT 可用 EP: {ort_available_providers()}")
        try:
            _log_ort_provider(det_path, expect_cuda=expect_cuda)
            self._det = RTMDet(
                onnx_model=det_path,
                model_input_size=det_input_size,
                backend=self.backend,
                device=dev,
            )
            self._pose = RTMPose(
                onnx_model=pose_path,
                model_input_size=pose_input_size,
                backend=self.backend,
                device=dev,
            )
            self._loaded_key = (self.det_variant, self.variant, dev)
        except Exception as exc:
            if not expect_cuda:
                raise
            print(f"⚠️ CUDA 加载失败（{exc!r}），回退 CPU（请检查驱动/CUDA 与 onnxruntime-gpu）")
            dev = "cpu"
            self.device = dev
            self._det = RTMDet(
                onnx_model=det_path,
                model_input_size=det_input_size,
                backend=self.backend,
                device=dev,
            )
            self._pose = RTMPose(
                onnx_model=pose_path,
                model_input_size=pose_input_size,
                backend=self.backend,
                device=dev,
            )
            self._loaded_key = (self.det_variant, self.variant, dev)

        print(
            f"✅ RTMDet-{self.det_variant} + RTMPose-{self.variant.upper()} 就绪 device={dev}"
        )

    def infer(self, frame: np.ndarray) -> PoseBatch:
        self.load()
        boxes = self._det(frame)
        if boxes is None or len(boxes) == 0:
            return PoseBatch.empty()

        bboxes = np.asarray(boxes, dtype=np.float32)
        if bboxes.ndim == 1:
            bboxes = bboxes.reshape(1, -1)
        bboxes = bboxes[:, :4]

        bbox_list = bboxes[:, :4].tolist()
        keypoints, scores = self._pose(frame, bboxes=bbox_list)
        if keypoints is None or len(keypoints) == 0:
            return PoseBatch.empty()

        kpts = np.asarray(keypoints, dtype=np.float32)
        sc = np.asarray(scores, dtype=np.float32)
        if kpts.ndim == 2:
            kpts = kpts.reshape(1, -1, 2)
        if sc.ndim == 1:
            sc = sc.reshape(1, -1)
        return PoseBatch(keypoints=kpts, keypoint_scores=sc, bboxes=bboxes)
