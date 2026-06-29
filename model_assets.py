"""RTMPose / RTMDet ONNX 模型资产（目录与 visual-dps 对齐，det 与 pose 分目录存放）。"""

from __future__ import annotations

import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

# 目录布局（相对 models_onnx_dir）：
#   detection/rtmdet_nano/end2end.onnx
#   detection/rtmdet_m/end2end.onnx
#   pose/rtmpose_t/end2end.onnx
#   pose/rtmpose_s|m/end2end.onnx
ONNX_DETECTION_DIR = "detection"
ONNX_POSE_DIR = "pose"
# 旧版扁平根目录名（兼容 visual-dps 与历史数据）
LEGACY_ONNX_FLAT_DIR = "rtmpose_onnx"

_OPENMMLAB_OFFICIAL = "https://download.openmmlab.com"
_ONNX_SDK_PATH = "/mmpose/v1/projects/rtmposev1/onnx_sdk"
_DET_NANO_ZIP = "rtmdet_nano_8xb32-100e_coco-obj365-person-05d8511e.zip"
_DET_M_ZIP = "rtmdet_m_8xb32-100e_coco-obj365-person-235e8209.zip"

RTMPOSE_VARIANTS = ("t", "s", "m")
RTMDET_VARIANTS = ("nano", "s", "m", "l")

RTMDET_VARIANT_ASSETS: dict[str, dict] = {
    "nano": {
        "det_dir": "rtmdet_nano",
        "det_zip": _DET_NANO_ZIP,
        "det_size": (320, 320),
        "label": "RTMDet-nano 320×320",
    },
    "m": {
        "det_dir": "rtmdet_m",
        "det_zip": _DET_M_ZIP,
        "det_size": (640, 640),
        "label": "RTMDet-m 640×640",
    },
}

RTMPOSE_VARIANT_ASSETS: dict[str, dict] = {
    "t": {
        "pose_dir": "rtmpose_t",
        "pose_zip": "rtmpose-t_simcc-body7_pt-body7_420e-256x192-026a1439_20230504.zip",
        "pose_size": (192, 256),
    },
    "s": {
        "pose_dir": "rtmpose_s",
        "pose_zip": "rtmpose-s_simcc-body7_pt-body7_420e-256x192-acd4a1ef_20230504.zip",
        "pose_size": (192, 256),
    },
    "m": {
        "pose_dir": "rtmpose_m",
        "pose_zip": "rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip",
        "pose_size": (192, 256),
    },
}

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".ts"}
)

COCO17_KEYPOINT_NAMES = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)


def openmmlab_sdk_base() -> str:
    """OpenMMLab 下载根 URL；可用环境变量 OPENMMLAB_MIRROR_BASE 指定镜像。"""
    mirror = os.environ.get("OPENMMLAB_MIRROR_BASE", "").strip().rstrip("/")
    if mirror:
        return mirror
    return _OPENMMLAB_OFFICIAL


def openmmlab_onnx_zip_url(zip_name: str) -> str:
    return f"{openmmlab_sdk_base()}{_ONNX_SDK_PATH}/{zip_name}"


def det_zip_url(det_variant: str) -> str:
    _, assets = resolve_det_assets(det_variant)
    return openmmlab_onnx_zip_url(str(assets["det_zip"]))


def pose_zip_url(pose_variant: str) -> str:
    v = str(pose_variant or "t").strip().lower()
    if v not in RTMPOSE_VARIANT_ASSETS:
        raise ValueError(f"pose variant 必须是 {RTMPOSE_VARIANTS} 之一")
    return openmmlab_onnx_zip_url(str(RTMPOSE_VARIANT_ASSETS[v]["pose_zip"]))


def default_models_onnx_dir() -> str:
    return str(
        Path(__file__).resolve().parent / "localdata" / "models" / "onnx"
    )


def models_onnx_detection_dir(models_onnx_dir: str) -> str:
    return os.path.join(models_onnx_dir, ONNX_DETECTION_DIR)


def models_onnx_pose_dir(models_onnx_dir: str) -> str:
    return os.path.join(models_onnx_dir, ONNX_POSE_DIR)


def _onnx_end2end_path(root: str, category: str, model_name: str) -> str:
    return os.path.join(root, category, model_name, "end2end.onnx")


def _legacy_flat_onnx_path(models_root: str, model_name: str) -> str:
    return os.path.join(models_root, model_name, "end2end.onnx")


def _legacy_nested_onnx_path(localdata_models: str, model_name: str) -> str:
    return os.path.join(
        localdata_models, LEGACY_ONNX_FLAT_DIR, model_name, "end2end.onnx"
    )


def resolve_detection_onnx_path(models_onnx_dir: str, det_dir_name: str) -> str:
    """解析检测 ONNX 路径；优先新目录 detection/，兼容旧扁平 rtmpose_onnx。"""
    models_onnx_dir = os.path.abspath(models_onnx_dir)
    det_dir_name = str(det_dir_name).strip()
    candidates = [
        _onnx_end2end_path(models_onnx_dir, ONNX_DETECTION_DIR, det_dir_name),
        _legacy_flat_onnx_path(models_onnx_dir, det_dir_name),
    ]
    parent = Path(models_onnx_dir).parent
    if parent.name == "models":
        candidates.append(
            _legacy_nested_onnx_path(str(parent), det_dir_name)
        )
    elif (parent / LEGACY_ONNX_FLAT_DIR).is_dir():
        candidates.append(
            _legacy_nested_onnx_path(str(parent), det_dir_name)
        )

    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return os.path.abspath(candidates[0])


def resolve_pose_onnx_path(models_onnx_dir: str, pose_dir_name: str) -> str:
    """解析姿态 ONNX 路径；优先新目录 pose/，兼容旧扁平 rtmpose_onnx。"""
    models_onnx_dir = os.path.abspath(models_onnx_dir)
    pose_dir_name = str(pose_dir_name).strip()
    candidates = [
        _onnx_end2end_path(models_onnx_dir, ONNX_POSE_DIR, pose_dir_name),
        _legacy_flat_onnx_path(models_onnx_dir, pose_dir_name),
    ]
    parent = Path(models_onnx_dir).parent
    if parent.name == "models":
        candidates.append(
            _legacy_nested_onnx_path(str(parent), pose_dir_name)
        )
    elif (parent / LEGACY_ONNX_FLAT_DIR).is_dir():
        candidates.append(
            _legacy_nested_onnx_path(str(parent), pose_dir_name)
        )

    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)
    return os.path.abspath(candidates[0])


def resolve_onnx_path(models_onnx_dir: str, subdir: str, *, category: str) -> str:
    """统一入口：category 为 detection 或 pose。"""
    if category == ONNX_DETECTION_DIR:
        return resolve_detection_onnx_path(models_onnx_dir, subdir)
    if category == ONNX_POSE_DIR:
        return resolve_pose_onnx_path(models_onnx_dir, subdir)
    raise ValueError(f"未知 ONNX 类别: {category}")


def ensure_onnx_from_zip(model_path: str, zip_url: str) -> str:
    """若 model_path 不存在则从 zip_url 下载并解压 end2end.onnx。"""
    model_path = os.path.abspath(model_path)
    if os.path.isfile(model_path):
        return model_path

    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
    tmp_zip = model_path + ".zip"
    print(f"⬇️ 正在下载 ONNX: {zip_url}")
    urllib.request.urlretrieve(zip_url, tmp_zip)
    try:
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            members = [n for n in zf.namelist() if n.endswith("end2end.onnx")]
            if not members:
                raise RuntimeError(f"ZIP 中未找到 end2end.onnx: {zip_url}")
            with zf.open(members[0]) as src, open(model_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    finally:
        if os.path.isfile(tmp_zip):
            os.remove(tmp_zip)

    if not os.path.isfile(model_path):
        raise RuntimeError(f"ONNX 准备失败: {model_path}")
    return model_path


def ensure_detection_onnx(models_onnx_dir: str, det_variant: str) -> str:
    key, assets = resolve_det_assets(det_variant)
    path = resolve_detection_onnx_path(models_onnx_dir, str(assets["det_dir"]))
    if os.path.isfile(path):
        return path
    target = _onnx_end2end_path(
        os.path.abspath(models_onnx_dir), ONNX_DETECTION_DIR, str(assets["det_dir"])
    )
    return ensure_onnx_from_zip(target, det_zip_url(key))


def ensure_pose_onnx(models_onnx_dir: str, pose_variant: str) -> str:
    v = str(pose_variant or "t").strip().lower()
    assets = RTMPOSE_VARIANT_ASSETS[v]
    path = resolve_pose_onnx_path(models_onnx_dir, str(assets["pose_dir"]))
    if os.path.isfile(path):
        return path
    target = _onnx_end2end_path(
        os.path.abspath(models_onnx_dir), ONNX_POSE_DIR, str(assets["pose_dir"])
    )
    return ensure_onnx_from_zip(target, pose_zip_url(v))


def parse_det_variant(raw: str) -> str:
    v = str(raw or "nano").strip().lower()
    if v.startswith("rtmdet_"):
        v = v.removeprefix("rtmdet_")
    if v == "t":
        v = "nano"
    if v not in RTMDET_VARIANTS:
        raise ValueError(f"det_variant 必须是 {RTMDET_VARIANTS} 之一")
    return v


def resolve_det_assets(det_variant: str) -> tuple[str, dict]:
    requested = parse_det_variant(det_variant)
    if requested in RTMDET_VARIANT_ASSETS:
        return requested, RTMDET_VARIANT_ASSETS[requested]
    if requested == "s":
        print("⚠️ RTMDet-s 暂无官方 person ONNX，回退为 RTMDet-nano")
        return "nano", RTMDET_VARIANT_ASSETS["nano"]
    if requested == "l":
        print("⚠️ RTMDet-l 暂无官方 person ONNX，回退为 RTMDet-m (m)")
        return "m", RTMDET_VARIANT_ASSETS["m"]
    return "nano", RTMDET_VARIANT_ASSETS["nano"]


def det_variant_to_backend(det_variant: str) -> str:
    key, _ = resolve_det_assets(det_variant)
    return f"rtmdet_{key}"


def describe_models_layout(models_onnx_dir: str | None = None) -> str:
    root = models_onnx_dir or default_models_onnx_dir()
    return (
        f"{root}/\n"
        f"  {ONNX_DETECTION_DIR}/rtmdet_nano/end2end.onnx\n"
        f"  {ONNX_DETECTION_DIR}/rtmdet_m/end2end.onnx\n"
        f"  {ONNX_POSE_DIR}/rtmpose_t|s|m/end2end.onnx"
    )
