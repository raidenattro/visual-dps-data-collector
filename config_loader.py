"""读取 config.json（字段命名与 visual-dps app_config.json 对齐）。"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.json"

# visual-dps models.backend → RTMPose 规格
_BACKEND_TO_VARIANT = {
    "rtmpose_t": "t",
    "rtmpose_s": "s",
    "rtmpose_m": "m",
    "rtmpose_onnx": "t",
    "lite": "t",
    "default": "t",
}

_DET_BACKEND_TO_VARIANT = {
    "rtmdet_t": "t",
    "rtmdet_s": "s",
    "rtmdet_m": "m",
    "rtmdet_l": "l",
    "rtmdet_nano": "t",
    "nano": "t",
    "default": "t",
}


def project_root() -> Path:
    return _PROJECT_ROOT


def resolve_config_path(path: str | None) -> Path:
    if path and str(path).strip():
        p = Path(path.strip())
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return p
    env_path = os.environ.get("POSE_COLLECT_CONFIG", "").strip()
    if env_path:
        p = Path(env_path)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return p
    return DEFAULT_CONFIG_PATH


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        return {}
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    block = cfg.get(name)
    return block if isinstance(block, dict) else {}


def _resolve_path(raw: str, *, base: Path) -> str:
    p = Path(str(raw or "").strip())
    if not str(p):
        return ""
    if not p.is_absolute():
        p = base / p
    return str(p.resolve())


def _pick_str(cli_val: Any, cfg_val: Any, default: str = "") -> str:
    if cli_val is not None and str(cli_val).strip():
        return str(cli_val).strip()
    if cfg_val is not None and str(cfg_val).strip():
        return str(cfg_val).strip()
    return default


def _pick_int(cli_val: Any, cfg_val: Any, default: int = 0) -> int:
    if cli_val is not None:
        try:
            return int(cli_val)
        except (TypeError, ValueError):
            pass
    if cfg_val is not None:
        try:
            return int(cfg_val)
        except (TypeError, ValueError):
            pass
    return default


def backend_to_variant(backend: str) -> str:
    key = str(backend or "").strip().lower()
    if key in _BACKEND_TO_VARIANT:
        return _BACKEND_TO_VARIANT[key]
    m = re.match(r"rtmpose[_-]?([tsm])", key)
    if m:
        return m.group(1)
    if key in ("t", "s", "m", "ms"):
        return "m" if key == "ms" else key
    return "t"


def variant_to_backend(variant: str) -> str:
    v = backend_to_variant(variant)
    return f"rtmpose_{v}"


def det_backend_to_variant(backend: str) -> str:
    key = str(backend or "").strip().lower()
    if key in _DET_BACKEND_TO_VARIANT:
        return _DET_BACKEND_TO_VARIANT[key]
    if key in ("t", "s", "m", "l"):
        return key
    m = re.match(r"rtmdet[_-]?([tsml])", key)
    if m:
        return m.group(1)
    return "t"


def det_variant_to_backend_name(variant: str) -> str:
    return f"rtmdet_{det_backend_to_variant(variant)}"


@dataclass
class AppPaths:
    base_localdata: Path
    json_dir: Path
    video_dir: Path
    upload_dir: Path
    playback_temp_dir: Path
    annotation_dir: Path
    models_onnx_dir: Path
    models_detection_dir: Path
    models_pose_dir: Path


def resolve_app_paths(cfg: dict[str, Any] | None = None, *, base: Path | None = None) -> AppPaths:
    cfg = cfg or load_config_file()
    root = base or project_root()
    paths = _section(cfg, "paths")
    base_localdata = root / str(paths.get("base_localdata_dir") or "localdata")
    json_dir = Path(_resolve_path(str(paths.get("json_dir") or "localdata/json"), base=root))
    video_dir = Path(_resolve_path(str(paths.get("video_dir") or "localdata/video"), base=root))
    upload_dir = Path(_resolve_path(str(paths.get("upload_dir") or "localdata/upload"), base=root))
    annotation_dir = Path(
        _resolve_path(
            str(paths.get("annotation_dir") or "localdata/json/annotations"),
            base=root,
        )
    )
    models_onnx = Path(
        _resolve_path(
            str(
                paths.get("models_onnx_dir")
                or paths.get("models_rtmpose_onnx_dir")  # 旧配置名
                or "localdata/models/onnx"
            ),
            base=root,
        )
    )
    from model_assets import ONNX_DETECTION_DIR, ONNX_POSE_DIR

    return AppPaths(
        base_localdata=base_localdata.resolve(),
        json_dir=json_dir,
        video_dir=video_dir,
        upload_dir=upload_dir,
        playback_temp_dir=(upload_dir / "playback_temp").resolve(),
        annotation_dir=annotation_dir.resolve(),
        models_onnx_dir=models_onnx.resolve(),
        models_detection_dir=(models_onnx / ONNX_DETECTION_DIR).resolve(),
        models_pose_dir=(models_onnx / ONNX_POSE_DIR).resolve(),
    )


def _pick_bool(cli_val: Any, cfg_val: Any, default: bool = True) -> bool:
    if cli_val is not None:
        if isinstance(cli_val, bool):
            return cli_val
        s = str(cli_val).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    if cfg_val is not None:
        if isinstance(cfg_val, bool):
            return cfg_val
        s = str(cfg_val).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return default


def default_save_video(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or load_config_file()
    storage = _section(cfg, "storage")
    return _pick_bool(None, storage.get("save_video"), True)


def record_video_path(
    paths: AppPaths,
    pose_record_path: Path,
    suffix: str,
    *,
    camera_slug: str | None = None,
) -> Path:
    """与 pose 记录同名的配套视频路径（支持机位子目录）。"""
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    stem = pose_record_path.stem if pose_record_path.is_file() else pose_record_path.name
    slug = camera_slug
    if not slug:
        try:
            rel = pose_record_path.resolve().parent.relative_to(paths.json_dir.resolve())
            if rel.parts and str(rel) != ".":
                slug = rel.parts[0]
        except ValueError:
            slug = None
    base = video_bucket_dir(paths, slug)
    return base / f"{stem}{ext}"


@dataclass
class CollectSettings:
    config_path: Path
    backend: str
    variant: str
    det_variant: str
    det_backend: str
    video: str
    output: str
    models_dir: str
    device: str
    ort_backend: str
    infer_height: int
    infer_width: int
    pose_frame_interval: int
    frame_rate: float
    max_pose_frames: int | None
    save_video: bool
    collision_method: str
    collision_params: dict[str, Any]
    alarm_min_consecutive_frames: int
    alarm_cooldown_frames: int


def sanitize_file_stem(name: str) -> str:
    """与视频主文件名一致的安全前缀（去扩展名、非法字符）。"""
    stem = Path(str(name or "").strip()).stem
    if not stem:
        return "video"
    safe = re.sub(r"[^\w.\-]+", "_", stem, flags=re.UNICODE)
    return safe.strip("._") or "video"


def camera_storage_slug(camera_label: str) -> str:
    """机位标识 → 存储目录名，如 1-6组-2 → 1-6-2。"""
    s = str(camera_label or "").strip()
    s = s.replace("－", "-").replace("—", "-").replace(" ", "")
    s = s.replace("组", "-")
    s = re.sub(r"[^\w.\-]+", "-", s, flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip(".-")
    return s or "camera"


_FOLDER_DUP_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)(?:[-_]?[（(](?P<n>\d+)[)）])$",
    re.UNICODE,
)


def parse_camera_folder_name(folder_name: str) -> tuple[str, int | None]:
    """视频文件夹名 → (机位标识, 重复序号)。

    输入文件夹不能同名时，可用后缀区分同机位多批数据：
      1-2组-1     → (1-2组-1, None)
      1-2组-1(2)  → (1-2组-1, 2)
      1-2组-1-(2) → (1-2组-1, 2)
    """
    s = str(folder_name or "").strip()
    if not s:
        return "", None
    m = _FOLDER_DUP_SUFFIX_RE.match(s)
    if m:
        base = str(m.group("base") or "").strip()
        n = int(m.group("n"))
        if base and n >= 2:
            return base, n
    return s, None


def camera_storage_slug_for_folder(paths: AppPaths, folder_name: str) -> tuple[str, str]:
    """视频文件夹名 → (机位标识, 输出存储 slug)。"""
    base_label, dup_n = parse_camera_folder_name(folder_name)
    if not base_label:
        raise ValueError("文件夹名不能为空")
    base_slug = camera_storage_slug(base_label)
    if dup_n is not None:
        return base_label, f"{base_slug}-({dup_n})"
    return base_label, allocate_camera_storage_slug(paths, base_label)


def _bucket_dir_has_content(bucket_dir: Path) -> bool:
    """机位 json/video 子目录是否已有数据（忽略 . 开头文件）。"""
    if not bucket_dir.is_dir():
        return False
    try:
        for p in bucket_dir.iterdir():
            if p.name.startswith("."):
                continue
            return True
    except OSError:
        return True
    return False


def camera_bucket_exists(paths: AppPaths, camera_slug: str) -> bool:
    """localdata/json 或 localdata/video 下是否已有该机位目录且非空。"""
    slug = str(camera_slug or "").strip()
    if not slug:
        return False
    return _bucket_dir_has_content(paths.json_dir / slug) or _bucket_dir_has_content(
        paths.video_dir / slug
    )


def allocate_camera_storage_slug(paths: AppPaths, camera_label: str) -> str:
    """为机位分配存储目录名；若 base 已占用则依次使用 base-(2)、base-(3)…"""
    base = camera_storage_slug(camera_label)
    if not camera_bucket_exists(paths, base):
        return base
    for n in range(2, 10_000):
        candidate = f"{base}-({n})"
        if not camera_bucket_exists(paths, candidate):
            return candidate
    raise ValueError(f"机位 {camera_label} 可用存储目录过多，请清理 localdata/json 后重试")


def json_bucket_dir(paths: AppPaths, camera_slug: str | None) -> Path:
    """骨架数据根目录；有机位时落在 json_dir/{camera_slug}/。"""
    if camera_slug:
        d = paths.json_dir / camera_slug
        d.mkdir(parents=True, exist_ok=True)
        return d
    paths.json_dir.mkdir(parents=True, exist_ok=True)
    return paths.json_dir


def video_bucket_dir(paths: AppPaths, camera_slug: str | None) -> Path:
    if camera_slug:
        d = paths.video_dir / camera_slug
        d.mkdir(parents=True, exist_ok=True)
        return d
    paths.video_dir.mkdir(parents=True, exist_ok=True)
    return paths.video_dir


def record_id_for_pose_path(json_dir: Path, pose_path: Path) -> str:
    """相对 json_dir 的记录 ID，含机位子目录时形如 1-6-2/foo_rtmpose_t。"""
    jp = json_dir.resolve()
    pp = pose_path.resolve()
    if pp.is_file():
        pp = pp.parent
    try:
        rel = pp.relative_to(jp)
        if str(rel) == ".":
            return pp.name
        return rel.as_posix()
    except ValueError:
        return pp.name


def default_pose_record_path(
    paths: AppPaths,
    *,
    backend: str,
    video_stem: str | None = None,
    job_id: str | None = None,
    camera_slug: str | None = None,
) -> Path:
    """记录目录命名：{机位目录/}{视频主名}_{backend}/（schema v2 Parquet 包）。"""
    base = json_bucket_dir(paths, camera_slug)
    prefix = sanitize_file_stem(video_stem) if video_stem else "video"
    safe_backend = re.sub(r"[^\w.-]", "_", backend)
    candidate = base / f"{prefix}_{safe_backend}"
    if candidate.is_dir() or candidate.with_suffix(".json").is_file():
        suffix = (job_id or time.strftime("%H%M%S"))[:12]
        candidate = base / f"{prefix}_{safe_backend}_{suffix}"
    return candidate


def default_pose_json_path(
    paths: AppPaths,
    *,
    backend: str,
    video_stem: str | None = None,
    job_id: str | None = None,
    camera_slug: str | None = None,
) -> Path:
    """兼容旧名：新采集默认返回 Parquet 包目录。"""
    return default_pose_record_path(
        paths,
        backend=backend,
        video_stem=video_stem,
        job_id=job_id,
        camera_slug=camera_slug,
    )


def build_settings(*, config_path: Path, cli: dict[str, Any]) -> CollectSettings:
    cfg = load_config_file(config_path)
    base = config_path.parent
    paths = resolve_app_paths(cfg, base=base)

    models = _section(cfg, "models")
    inference = _section(cfg, "inference")
    source = _section(cfg, "source")

    # 兼容旧配置 collect.* / models.variant
    legacy_collect = _section(cfg, "collect")

    backend_cli = _pick_str(cli.get("backend"), None)
    variant_cli = _pick_str(cli.get("variant"), None)
    if variant_cli:
        variant = backend_to_variant(variant_cli)
        backend = variant_to_backend(variant)
    elif backend_cli:
        backend = backend_cli if backend_cli.startswith("rtmpose") else variant_to_backend(backend_cli)
        variant = backend_to_variant(backend)
    else:
        raw = str(
            models.get("backend") or legacy_collect.get("variant") or "rtmpose_t"
        ).strip().lower()
        if raw in ("t", "s", "m"):
            variant = raw
            backend = variant_to_backend(variant)
        else:
            backend = raw if raw.startswith("rtmpose") else f"rtmpose_{backend_to_variant(raw)}"
            variant = backend_to_variant(backend)

    det_cli = _pick_str(cli.get("det_variant") or cli.get("det_backend"), None)
    if det_cli:
        det_variant = det_backend_to_variant(det_cli)
    else:
        det_raw = str(
            models.get("det_variant")
            or models.get("det_backend")
            or legacy_collect.get("det_variant")
            or "t"
        ).strip().lower()
        det_variant = det_backend_to_variant(det_raw)
    det_backend = det_variant_to_backend_name(det_variant)

    video_raw = _pick_str(
        cli.get("video") or cli.get("input"),
        source.get("video") or legacy_collect.get("video") or legacy_collect.get("input"),
    )
    if video_raw and not Path(video_raw).is_absolute():
        video_raw = _resolve_path(video_raw, base=base)

    output_raw = _pick_str(cli.get("output"), legacy_collect.get("output"))
    if output_raw:
        out_p = Path(output_raw)
        if not out_p.is_absolute():
            output_raw = str(_resolve_path(output_raw, base=base))
    else:
        video_stem_for_name = Path(video_raw).stem if video_raw else None
        output_raw = str(
            default_pose_json_path(paths, backend=backend, video_stem=video_stem_for_name)
        )

    models_dir = _pick_str(cli.get("models_dir") or cli.get("models_onnx_dir"), None)
    if not models_dir:
        models_dir = str(paths.models_onnx_dir)
    else:
        models_dir = _resolve_path(models_dir, base=base)

    device_cli = _pick_str(cli.get("device"), None)
    if device_cli in ("cpu", "cuda"):
        device = device_cli
    else:
        use_gpu_env = os.environ.get("INFERENCE_USE_GPU", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # 默认 GPU（与 requirements onnxruntime-gpu 一致）；显式 use_gpu:false 或仅 CPU 包时走 cpu
        use_gpu_cfg = models.get("use_gpu")
        if use_gpu_cfg is None:
            use_gpu = True
        else:
            use_gpu = bool(use_gpu_cfg)
        if use_gpu_env or use_gpu or legacy_collect.get("use_gpu"):
            device = str(models.get("rtmpose_onnx_device_gpu") or "cuda").strip().lower()
        else:
            device = str(models.get("rtmpose_onnx_device") or "cpu").strip().lower()
        if device not in ("cpu", "cuda"):
            device = "cpu"

    ort_backend = _pick_str(
        cli.get("ort_backend"),
        models.get("rtmpose_onnx_ort_backend") or models.get("ort_backend"),
        "onnxruntime",
    )

    infer_height = _pick_int(
        cli.get("height"),
        inference.get("height") or legacy_collect.get("height") or _section(cfg, "video").get("capture_height"),
        0,
    )
    infer_width = _pick_int(cli.get("width"), legacy_collect.get("width"), 0)

    pose_frame_interval = max(
        1,
        _pick_int(
            cli.get("frame_interval"),
            inference.get("pose_frame_interval") or legacy_collect.get("frame_interval"),
            1,
        ),
    )

    max_val = _pick_int(
        cli.get("max_frames"),
        inference.get("max_pose_frames") or legacy_collect.get("max_frames"),
        0,
    )
    max_pose_frames = max_val if max_val > 0 else None

    frame_rate_raw = cli.get("frame_rate")
    if frame_rate_raw is not None:
        try:
            frame_rate = float(frame_rate_raw)
        except (TypeError, ValueError):
            try:
                frame_rate = float(
                    inference.get("frame_rate")
                    if inference.get("frame_rate") is not None
                    else 0
                )
            except (TypeError, ValueError):
                frame_rate = 0.0
    else:
        try:
            frame_rate = float(inference.get("frame_rate") if inference.get("frame_rate") is not None else 0)
        except (TypeError, ValueError):
            frame_rate = 0.0
    if frame_rate < 0:
        frame_rate = 0.0

    storage = _section(cfg, "storage")
    save_video = _pick_bool(cli.get("save_video"), storage.get("save_video"), True)

    alarm_min = max(
        1,
        _pick_int(
            cli.get("alarm_min_consecutive_frames"),
            inference.get("alarm_min_consecutive_frames"),
            3,
        ),
    )
    alarm_cooldown = max(
        1,
        _pick_int(
            cli.get("alarm_cooldown_frames"),
            inference.get("alarm_cooldown_frames"),
            12,
        ),
    )
    collision_cfg = inference.get("collision") if isinstance(inference.get("collision"), dict) else {}
    collision_method = str(
        cli.get("collision_method")
        or inference.get("collision_method")
        or inference.get("collision_method_name")
        or collision_cfg.get("method")
        or ""
    ).strip() or "wrist_point"
    collision_params: dict[str, Any] = {}
    if isinstance(collision_cfg, dict):
        collision_params.update(collision_cfg)
    if isinstance(inference.get("collision_params"), dict):
        raw_params = inference.get("collision_params") or {}
        if isinstance(raw_params.get(collision_method), dict):
            collision_params.update(raw_params.get(collision_method) or {})
        else:
            collision_params.update(raw_params)
    if isinstance(cli.get("collision_params"), dict):
        collision_params.update(cli.get("collision_params") or {})
    collision_params["method"] = collision_method
    collision_params["alarm_min_consecutive_frames"] = alarm_min
    collision_params["alarm_cooldown_frames"] = alarm_cooldown

    return CollectSettings(
        config_path=config_path.resolve(),
        backend=backend,
        variant=variant,
        det_variant=det_variant,
        det_backend=det_backend,
        video=video_raw,
        output=output_raw,
        models_dir=models_dir,
        device=device,
        ort_backend=ort_backend,
        infer_height=infer_height,
        infer_width=infer_width,
        pose_frame_interval=pose_frame_interval,
        frame_rate=frame_rate,
        max_pose_frames=max_pose_frames,
        save_video=save_video,
        collision_method=collision_method,
        collision_params=collision_params,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cooldown,
    )
