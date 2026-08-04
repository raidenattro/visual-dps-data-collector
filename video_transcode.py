"""回放预览视频转码（高分辨率源视频降为配置高度，减轻浏览器解码压力）。"""

from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2

from config_loader import load_config_file, project_root, resolve_config_path

logger = logging.getLogger("uvicorn.error")

ProgressCallback = Callable[[int, int], None]

# 小于该字节数视为损坏（典型空 MP4 壳约 200–300 字节）
MIN_PREVIEW_ABS_BYTES = 50_000
# 预览体积至少为原片的 0.5%
MIN_PREVIEW_SIZE_RATIO = 0.005


@dataclass
class TranscodeJobState:
    status: str = "idle"  # idle | ready | transcoding | error
    progress: int = 0
    total_frames: int = 0
    message: str = ""
    error: str = ""
    source_height: int = 0
    preview_height: int = 0
    use_original: bool = False


_jobs_lock = threading.Lock()
_jobs: dict[str, TranscodeJobState] = {}
_preview_executor: ThreadPoolExecutor | None = None
_nvenc_available_cache: bool | None = None
_PREVIEW_CACHE_VERSION = "v3-gop1s-zero-pts"


def probe_video_timing(video_path: Path) -> dict[str, float]:
    """用 ffprobe 读取容器时长与首帧 PTS 偏移（秒）。"""
    out: dict[str, float] = {}
    ff = shutil.which("ffprobe")
    if not ff or not video_path.is_file():
        return out
    cmd = [
        ff,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration,start_time",
        "-show_entries",
        "stream=duration,start_time",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-read_intervals",
        "%+#1",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        payload = json.loads(subprocess.check_output(cmd, text=True))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return out
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    for raw in (fmt.get("duration"), stream.get("duration")):
        if raw is None:
            continue
        try:
            dur = float(raw)
        except (TypeError, ValueError):
            continue
        if dur > 0:
            out["duration_sec"] = dur
            break
    for raw in (fmt.get("start_time"), stream.get("start_time")):
        if raw is None:
            continue
        try:
            st = float(raw)
        except (TypeError, ValueError):
            continue
        if st >= 0:
            out["start_pts_sec"] = st
            break
    frames = payload.get("frames") or []
    if frames:
        raw = frames[0].get("best_effort_timestamp_time")
        if raw is not None:
            try:
                out["first_pts_sec"] = float(raw)
            except (TypeError, ValueError):
                pass
    return out


def probe_video_duration_sec(video_path: Path) -> float | None:
    timing = probe_video_timing(video_path)
    dur = timing.get("duration_sec")
    return dur if dur and dur > 0 else None


def default_playback_transcode_height() -> int:
    cfg = load_config_file(resolve_config_path(None))
    raw = (cfg.get("video") or {}).get("transcode_height", 480)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 480


def default_playback_transcode_min_frames() -> int:
    cfg = load_config_file(resolve_config_path(None))
    raw = (cfg.get("video") or {}).get("preview_min_frames", 10_000)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 10_000


def preview_video_path(src: Path, target_height: int) -> Path:
    h = max(1, int(target_height))
    stat = src.stat()
    identity = (
        f"{_PREVIEW_CACHE_VERSION}|{src.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{h}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    cfg = load_config_file(resolve_config_path(None))
    video_cfg = cfg.get("video") if isinstance(cfg.get("video"), dict) else {}
    raw_dir = str(video_cfg.get("preview_cache_dir") or "localdata/playback-cache/video")
    cache_dir = Path(raw_dir)
    if not cache_dir.is_absolute():
        cache_dir = project_root() / cache_dir
    return cache_dir.resolve() / digest[:2] / f"{src.stem}_{digest}_h{h}.mp4"


def _job_key(src: Path, target_height: int) -> str:
    return f"{src.resolve()}|{max(1, int(target_height))}"


def read_video_height(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 0
    try:
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if h <= 0:
            ret, frame = cap.read()
            if ret and frame is not None:
                h = int(frame.shape[0])
        return max(0, h)
    finally:
        cap.release()


def read_video_frame_count(path: Path) -> int:
    """Return the container's decoded frame count, or zero when it cannot be read."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 0
    try:
        return max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
    finally:
        cap.release()


def read_video_fps(path: Path) -> float:
    """Return the source frame rate used to derive a one-second GOP."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 0.0
    try:
        return max(0.0, float(cap.get(cv2.CAP_PROP_FPS) or 0.0))
    finally:
        cap.release()


def is_usable_playback_video(path: Path, src: Path | None = None) -> bool:
    """校验视频可被 OpenCV 读帧且体积合理（排除 258B 空壳 MP4）。"""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < MIN_PREVIEW_ABS_BYTES:
        return False
    if src is not None and Path(src).is_file():
        try:
            src_size = Path(src).stat().st_size
            if src_size > 0 and size < int(src_size * MIN_PREVIEW_SIZE_RATIO):
                return False
        except OSError:
            pass

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return False
    try:
        preview_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        ret, frame = cap.read()
        if not ret or frame is None:
            return False
        h = int(frame.shape[0])
        w = int(frame.shape[1])
        if w <= 0 or h <= 0 or preview_frames <= 0:
            return False
    finally:
        cap.release()

    if src is not None and Path(src).is_file():
        source_frames = read_video_frame_count(Path(src))
        if source_frames <= 0 or preview_frames != source_frames:
            logger.warning(
                "preview frame count mismatch: %s=%s, %s=%s",
                path.name,
                preview_frames,
                Path(src).name,
                source_frames,
            )
            return False
    return True


def purge_invalid_preview(preview: Path, src: Path) -> bool:
    """删除无效预览文件；返回是否已删除。"""
    preview = Path(preview)
    if not preview.is_file():
        return False
    if is_usable_playback_video(preview, src):
        return False
    try:
        bad_size = preview.stat().st_size
        preview.unlink(missing_ok=True)
        logger.warning("已删除无效预览视频 %s（%s 字节）", preview.name, bad_size)
        return True
    except OSError as exc:
        logger.warning("删除无效预览失败 %s: %s", preview, exc)
        return False


def _even_dim(n: int) -> int:
    v = max(2, int(n))
    return v - (v % 2)


def _find_ffmpeg() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        return bundled if bundled and Path(bundled).is_file() else None
    except (ImportError, OSError, RuntimeError):
        return None


def _ffmpeg_nvenc_available(ffmpeg: str) -> bool:
    global _nvenc_available_cache
    if _nvenc_available_cache is not None:
        return _nvenc_available_cache
    probe = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=size=64x64:rate=1:duration=1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "NUL" if os.name == "nt" else "/dev/null",
    ]
    try:
        _nvenc_available_cache = subprocess.run(
            probe,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        _nvenc_available_cache = False
    return _nvenc_available_cache


def _ffmpeg_duration_sec(src: Path) -> float:
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return 0.0
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps > 0 and frames > 0:
            return frames / fps
        return 0.0
    finally:
        cap.release()


def transcode_with_ffmpeg(
    src: Path,
    dest: Path,
    target_height: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """优先用 ffmpeg 生成浏览器可播的 H.264 MP4。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False

    src = Path(src)
    dest = Path(dest)
    th = max(2, int(target_height))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        dest.unlink()

    duration_sec = _ffmpeg_duration_sec(src)
    source_fps = read_video_fps(src)
    gop_frames = max(1, int(round(source_fps))) if source_fps > 0 else 25
    use_nvenc = _ffmpeg_nvenc_available(ffmpeg)
    encoder_args = (
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-b:v", "0"]
        if use_nvenc
        else ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]
    )
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(src),
        "-vf",
        f"scale=-2:{th},setpts=PTS-STARTPTS",
        *encoder_args,
        "-g",
        str(gop_frames),
        "-keyint_min",
        str(gop_frames),
        "-sc_threshold",
        "0",
        "-fps_mode",
        "passthrough",
        "-movflags",
        "+faststart",
        "-an",
        str(dest),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        logger.warning("ffmpeg 启动失败: %s", exc)
        return False

    time_re = re.compile(r"out_time_us=(\d+)")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not on_progress or duration_sec <= 0:
                continue
            m = time_re.search(line)
            if not m:
                continue
            us = int(m.group(1))
            done = max(1, int(round(us / 1_000_000)))
            total = max(1, int(round(duration_sec)))
            on_progress(min(done, total), total)
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.warning("ffmpeg 转码超时 %s", src)
        if dest.is_file():
            dest.unlink(missing_ok=True)
        return False
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            err = proc.stderr.read()
            if proc.returncode != 0 and err:
                logger.warning("ffmpeg  stderr: %s", err.strip()[:500])

    if proc.returncode != 0 or not is_usable_playback_video(dest, src):
        if dest.is_file():
            dest.unlink(missing_ok=True)
        return False
    logger.info("ffmpeg 预览转码完成 %s → %s", src.name, dest.name)
    return True


def _open_video_writer(dest: Path, fps: float, size: tuple[int, int]) -> cv2.VideoWriter | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 优先 mp4v：Windows 上 avc1/H264 常返回 isOpened 但写不出有效流
    fourcc_candidates = ("mp4v", "avc1", "H264", "XVID")
    for tag in fourcc_candidates:
        writer = cv2.VideoWriter(
            str(dest),
            cv2.VideoWriter_fourcc(*tag),
            fps,
            size,
        )
        if writer.isOpened():
            return writer
        writer.release()
    return None


def transcode_with_opencv(
    src: Path,
    dest: Path,
    target_height: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """OpenCV 转码兜底（写入后严格校验，不合格则删除）。"""
    src = Path(src)
    dest = Path(dest)
    th = max(2, int(target_height))
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        logger.warning("OpenCV 转码失败：无法打开 %s", src)
        return False

    try:
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0:
            fps = 25.0
        if src_w <= 0 or src_h <= 0:
            ret, probe = cap.read()
            if not ret or probe is None:
                return False
            src_h, src_w = probe.shape[:2]
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        out_h = _even_dim(th)
        out_w = _even_dim(int(round(src_w * (out_h / float(src_h)))))
        writer = _open_video_writer(dest, fps, (out_w, out_h))
        if writer is None:
            return False

        if dest.is_file():
            dest.unlink()

        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            if frame.shape[0] != out_h or frame.shape[1] != out_w:
                frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
            writer.write(frame)
            frame_idx += 1
            if on_progress and (frame_idx == 1 or frame_idx % 25 == 0):
                on_progress(frame_idx, max(frame_idx, total_frames))

        writer.release()
        if on_progress:
            on_progress(frame_idx, max(frame_idx, total_frames))

        if not is_usable_playback_video(dest, src):
            if dest.is_file():
                dest.unlink(missing_ok=True)
            logger.warning("OpenCV 转码产出无效 %s（帧数=%s）", dest.name, frame_idx)
            return False
        logger.info("OpenCV 预览转码完成 %s → %s (%dx%d)", src.name, dest.name, out_w, out_h)
        return True
    except OSError as exc:
        logger.warning("OpenCV 转码异常 %s: %s", src, exc)
        if dest.is_file():
            dest.unlink(missing_ok=True)
        return False
    finally:
        cap.release()


def transcode_preview_video(
    src: Path,
    dest: Path,
    target_height: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """ffmpeg 优先，OpenCV 兜底；任一产出无效则视为失败。"""
    if transcode_with_ffmpeg(src, dest, target_height, on_progress=on_progress):
        return True
    return transcode_with_opencv(src, dest, target_height, on_progress=on_progress)


def _preview_plan(src: Path, target_height: int | None = None) -> dict:
    src = Path(src)
    th = default_playback_transcode_height() if target_height is None else max(0, int(target_height))
    min_frames = default_playback_transcode_min_frames()
    if not src.is_file():
        return {
            "needs_transcode": False,
            "ready": False,
            "preview": None,
            "target_height": th,
            "source_height": 0,
            "source_frames": 0,
            "preview_min_frames": min_frames,
            "use_original": False,
        }
    src_h = read_video_height(src)
    src_frames = read_video_frame_count(src)
    # Preview eligibility is frame-count based. Long videos are re-encoded even
    # when their source height is already at or below the configured target so
    # they still gain fast-start and the one-second GOP used for responsive seek.
    if th <= 0 or src_frames < min_frames:
        return {
            "needs_transcode": False,
            "ready": True,
            "preview": src,
            "target_height": th,
            "source_height": src_h,
            "source_frames": src_frames,
            "preview_min_frames": min_frames,
            "use_original": True,
        }
    preview = preview_video_path(src, th)
    if preview.is_file():
        if is_usable_playback_video(preview, src) and preview.stat().st_mtime >= src.stat().st_mtime:
            return {
                "needs_transcode": False,
                "ready": True,
                "preview": preview,
                "target_height": th,
                "source_height": src_h,
                "source_frames": src_frames,
                "preview_min_frames": min_frames,
                "use_original": False,
            }
        purge_invalid_preview(preview, src)
    return {
        "needs_transcode": True,
        "ready": False,
        "preview": preview,
        "target_height": th,
        "source_height": src_h,
        "source_frames": src_frames,
        "preview_min_frames": min_frames,
        "use_original": False,
    }


def resolve_playback_serve_path(src: Path, *, target_height: int | None = None) -> Path:
    """返回可安全提供给浏览器的视频路径（无效预览自动跳过）。"""
    src = Path(src)
    if not src.is_file():
        return src
    plan = _preview_plan(src, target_height=target_height)
    preview = plan.get("preview")
    if (
        plan.get("needs_transcode") is False
        and isinstance(preview, Path)
        and preview.is_file()
        and preview.resolve() != src.resolve()
        and is_usable_playback_video(preview, src)
    ):
        return preview
    if isinstance(preview, Path) and preview.is_file() and is_usable_playback_video(preview, src):
        return preview
    return src


def build_preview_video_sync(
    src: Path,
    *,
    target_height: int | None = None,
    force: bool = False,
) -> dict:
    """Build one derived playback preview synchronously without modifying the source video."""
    src = Path(src)
    plan = _preview_plan(src, target_height=target_height)
    preview = plan.get("preview")
    if not src.is_file():
        return _status_dict_from_plan(plan, status="missing", progress=0, message="source missing")
    if not plan.get("needs_transcode") and not force:
        return _status_dict_from_plan(plan, status="ready", message="video ready")
    if not isinstance(preview, Path) or preview.resolve() == src.resolve():
        return _status_dict_from_plan(plan, status="ready", message="preview not required")
    if force and preview.is_file():
        preview.unlink(missing_ok=True)
    ok = transcode_preview_video(src, preview, int(plan["target_height"]))
    refreshed = _preview_plan(src, target_height=int(plan["target_height"]))
    if ok and refreshed.get("ready") and not refreshed.get("use_original"):
        return _status_dict_from_plan(refreshed, status="ready", message="preview ready")
    return {
        **_status_dict_from_plan(refreshed, status="error", progress=0, message="preview failed"),
        "error": "preview transcoding failed",
    }


def _run_transcode_job(src: Path, preview: Path, th: int, key: str) -> None:
    def on_progress(done: int, total: int) -> None:
        pct = min(99, int(round((done / max(1, total)) * 100)))
        with _jobs_lock:
            job = _jobs.get(key)
            if not job:
                return
            job.progress = pct
            job.total_frames = total
            job.message = f"转码中 {pct}%"

    logger.info(
        "preview transcode started source=%s preview=%s target_height=%d source_frames=%d",
        src,
        preview,
        th,
        read_video_frame_count(src),
    )
    try:
        ok = transcode_preview_video(src, preview, th, on_progress=on_progress)
    except Exception:
        logger.exception("preview transcode crashed source=%s preview=%s", src, preview)
        ok = False
    with _jobs_lock:
        job = _jobs.get(key)
        if not job:
            return
        if ok and is_usable_playback_video(preview, src):
            job.status = "ready"
            job.progress = 100
            job.use_original = False
            job.message = "预览视频已就绪"
            logger.info(
                "preview transcode ready source=%s preview=%s frames=%d",
                src,
                preview,
                read_video_frame_count(preview),
            )
        else:
            purge_invalid_preview(preview, src)
            job.status = "error"
            job.use_original = True
            job.error = "预览转码失败，将使用原视频"
            job.message = job.error
            logger.error(
                "preview transcode failed source=%s preview=%s; playback will use original",
                src,
                preview,
            )


def _status_dict_from_plan(plan: dict, *, status: str, progress: int = 100, message: str = "") -> dict:
    th = int(plan["target_height"])
    src_h = int(plan["source_height"])
    return {
        "status": status,
        "progress": progress,
        "needs_transcode": bool(plan.get("needs_transcode")),
        "source_height": src_h,
        "source_frames": int(plan.get("source_frames") or 0),
        "preview_min_frames": int(plan.get("preview_min_frames") or 0),
        "preview_height": (
            src_h
            if bool(plan.get("use_original")) or th <= 0 or src_h <= 0
            else th
        ),
        "message": message,
        "error": "",
        "use_original": bool(plan.get("use_original")),
        "cache_hit": bool(
            isinstance(plan.get("preview"), Path)
            and plan.get("preview") != plan.get("source")
            and plan.get("ready")
            and not plan.get("use_original")
        ),
        "cache_path_type": "local_preview" if not plan.get("use_original") else "original",
    }


def _get_preview_executor() -> ThreadPoolExecutor:
    global _preview_executor
    with _jobs_lock:
        if _preview_executor is None:
            cfg = load_config_file(resolve_config_path(None))
            video_cfg = cfg.get("video") if isinstance(cfg.get("video"), dict) else {}
            try:
                workers = max(1, int(video_cfg.get("preview_workers") or 1))
            except (TypeError, ValueError):
                workers = 1
            _preview_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="preview-transcode",
            )
            logger.info("preview transcode executor initialized workers=%d", workers)
        return _preview_executor


def ensure_preview_transcode_async(src: Path, *, target_height: int | None = None) -> dict:
    """确保预览转码已启动或完成；返回状态字典。"""
    plan = _preview_plan(src, target_height=target_height)
    th = int(plan["target_height"])
    src_h = int(plan["source_height"])
    preview: Path | None = plan["preview"]

    if not plan["needs_transcode"]:
        logger.info(
            "preview plan uses %s source=%s height=%d frames=%d threshold_frames=%d target_height=%d",
            "cached preview" if not plan.get("use_original") else "original",
            src,
            src_h,
            int(plan.get("source_frames") or 0),
            int(plan.get("preview_min_frames") or 0),
            th,
        )
        return _status_dict_from_plan(
            plan,
            status="ready" if plan["ready"] else "missing",
            message="视频已就绪" if plan["ready"] else "配套视频不存在",
        )

    key = _job_key(src, th)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job.status == "transcoding":
            return {
                "status": "transcoding",
                "progress": job.progress,
                "needs_transcode": True,
                "source_height": src_h,
                "source_frames": int(plan.get("source_frames") or 0),
                "preview_min_frames": int(plan.get("preview_min_frames") or 0),
                "preview_height": th,
                "message": job.message or "正在生成预览视频…",
                "error": "",
                "use_original": False,
                "cache_hit": False,
                "cache_path_type": "original",
            }
        if job and job.status == "ready":
            if isinstance(preview, Path) and is_usable_playback_video(preview, src):
                return {
                    "status": "ready",
                    "progress": 100,
                    "needs_transcode": True,
                    "source_height": src_h,
                    "source_frames": int(plan.get("source_frames") or 0),
                    "preview_min_frames": int(plan.get("preview_min_frames") or 0),
                    "preview_height": th,
                    "message": "预览视频已就绪",
                    "error": "",
                    "use_original": False,
                    "cache_hit": True,
                    "cache_path_type": "local_preview",
                }
            _jobs.pop(key, None)
            job = None
        if job and job.status == "error":
            # 预览已删除或仍无效时允许重新转码
            if not (isinstance(preview, Path) and is_usable_playback_video(preview, src)):
                _jobs.pop(key, None)
            else:
                return {
                    "status": "error",
                    "progress": job.progress,
                    "needs_transcode": True,
                    "source_height": src_h,
                    "source_frames": int(plan.get("source_frames") or 0),
                    "preview_min_frames": int(plan.get("preview_min_frames") or 0),
                    "preview_height": th,
                    "message": job.message,
                    "error": job.error,
                    "use_original": True,
                    "cache_hit": False,
                    "cache_path_type": "original",
                }

        job = TranscodeJobState(
            status="transcoding",
            progress=0,
            message="正在生成预览视频…",
            source_height=src_h,
            preview_height=th,
        )
        _jobs[key] = job

    assert preview is not None
    logger.info(
        "preview transcode queued source=%s preview=%s height=%d frames=%d threshold_frames=%d",
        src,
        preview,
        th,
        int(plan.get("source_frames") or 0),
        int(plan.get("preview_min_frames") or 0),
    )
    try:
        _get_preview_executor().submit(_run_transcode_job, Path(src), preview, th, key)
    except Exception:
        logger.exception("preview transcode enqueue failed source=%s preview=%s", src, preview)
        with _jobs_lock:
            failed = _jobs.get(key)
            if failed:
                failed.status = "error"
                failed.use_original = True
                failed.error = "preview transcode could not be scheduled"
                failed.message = failed.error
        return {
            **_status_dict_from_plan(
                plan,
                status="error",
                progress=0,
                message="preview scheduling failed",
            ),
            "error": "preview transcode could not be scheduled",
            "use_original": True,
        }
    return {
        "status": "transcoding",
        "progress": 0,
        "needs_transcode": True,
        "source_height": src_h,
        "source_frames": int(plan.get("source_frames") or 0),
        "preview_min_frames": int(plan.get("preview_min_frames") or 0),
        "preview_height": th,
        "message": "正在生成预览视频…",
        "error": "",
        "use_original": False,
        "cache_hit": False,
        "cache_path_type": "original",
    }


# 兼容旧调用
def transcode_video_to_height(
    src: Path,
    dest: Path,
    target_height: int,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    return transcode_preview_video(src, dest, target_height, on_progress=on_progress)


def resolve_playback_video_path(
    src: Path,
    *,
    target_height: int | None = None,
    allow_transcode: bool = True,
) -> Path:
    src = Path(src)
    if not src.is_file():
        return src
    serve = resolve_playback_serve_path(src, target_height=target_height)
    if serve.resolve() != src.resolve():
        return serve
    plan = _preview_plan(src, target_height=target_height)
    if plan["needs_transcode"] and allow_transcode:
        ensure_preview_transcode_async(src, target_height=target_height)
    return src
