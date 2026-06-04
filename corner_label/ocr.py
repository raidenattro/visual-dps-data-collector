"""从视频 ROI 识别机位标签（如 2-1组-3）。同一环境内默认 CPU 版 PaddleOCR。"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

from corner_label.reflection import normalize_corner_label

CORNER_LABEL_RE = re.compile(r"\d+-\d+组-\d+")

# 与主环境共存：CPU paddle；未安装时 auto 回退 easyocr
_DEFAULT_ENGINE = "paddle"


def _ocr_log(msg: str) -> None:
    print(f"[corner-ocr] {msg}", flush=True)


def default_ocr_engine() -> str:
    try:
        cfg = __import__("config_loader", fromlist=["load_config_file", "resolve_config_path"])
        data = cfg.load_config_file(cfg.resolve_config_path(None))
        eng = str((data.get("ocr") or {}).get("engine") or "").strip().lower()
        if eng in ("paddle", "easy", "auto"):
            return eng
    except Exception:
        pass
    return _DEFAULT_ENGINE


@dataclass(frozen=True)
class CornerRoi:
    """画面比例 ROI：x0,y0,x1,y1 ∈ [0,1]，优先读 config.json → ocr.roi。"""

    x0: float = 0.5
    y0: float = 0.25
    x1: float = 1.0
    y1: float = 0.75


def default_corner_roi() -> CornerRoi:
    """从 config.json 读取 ocr.roi: [x0, y0, x1, y1]，便于手动调 ROI。"""
    try:
        cfg = __import__("config_loader", fromlist=["load_config_file", "resolve_config_path"])
        data = cfg.load_config_file(cfg.resolve_config_path(None))
        ocr = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
        box = ocr.get("roi")
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            vals = [float(box[i]) for i in range(4)]
            return CornerRoi(
                x0=max(0.0, min(1.0, vals[0])),
                y0=max(0.0, min(1.0, vals[1])),
                x1=max(0.0, min(1.0, vals[2])),
                y1=max(0.0, min(1.0, vals[3])),
            )
    except Exception:
        pass
    return CornerRoi()


def _normalize_for_label_extract(text: str) -> str:
    """纠正常见 OCR 误识后再做正则匹配。"""
    s = str(text or "")
    s = s.replace("＃", "#").replace("－", "-").replace("—", "-")
    s = re.sub(r"(?i)#g", "组", s)
    s = re.sub(r"\s+", "", s)
    return s


def extract_corner_label_candidates(text: str) -> list[str]:
    found = CORNER_LABEL_RE.findall(_normalize_for_label_extract(text))
    return [normalize_corner_label(x) for x in found]


def _crop_roi(frame, roi: CornerRoi):
    h, w = frame.shape[:2]
    x0 = max(0, int(w * roi.x0))
    y0 = max(0, int(h * roi.y0))
    x1 = min(w, int(w * roi.x1))
    y1 = min(h, int(h * roi.y1))
    if x1 <= x0 or y1 <= y0:
        return frame
    return frame[y0:y1, x0:x1]


def _scale_min_side(bgr, min_side: int = 160):
    h, w = bgr.shape[:2]
    factor = max(2.0, float(min_side) / max(1, min(h, w)))
    return cv2.resize(bgr, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


def _preprocess_variants_for_osd(bgr) -> list[tuple[str, object]]:
    """
    监控 OSD 白字：原图放大、CLAHE、亮区掩膜、Otsu 等多路，供 Paddle 逐路识别。
    """
    scaled = _scale_min_side(bgr, min_side=160)
    if len(scaled.shape) == 2:
        gray = scaled
        color = cv2.cvtColor(scaled, cv2.COLOR_GRAY2BGR)
    else:
        color = scaled
        gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)

    variants: list[tuple[str, object]] = [("color", color)]

    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(4, 4))
    enhanced = clahe.apply(gray)
    variants.append(("clahe", cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)))

    # 白字：高亮像素 → 黑字白底（OCR 更稳）
    _, bright = cv2.threshold(enhanced, 165, 255, cv2.THRESH_BINARY)
    white_fg = 255 - bright
    variants.append(("white_fg", cv2.cvtColor(white_fg, cv2.COLOR_GRAY2BGR)))

    _, th_otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(("otsu", cv2.cvtColor(th_otsu, cv2.COLOR_GRAY2BGR)))
    variants.append(("otsu_inv", cv2.cvtColor(255 - th_otsu, cv2.COLOR_GRAY2BGR)))

    return variants


_EASYOCR_READER = None
_PADDLE_OCR = None
_PADDLE_INIT_ERROR: str | None = None


def _paddle_env_setup() -> None:
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")


def _get_easyocr_reader():
    global _EASYOCR_READER
    if _EASYOCR_READER is None:
        import easyocr

        _EASYOCR_READER = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
    return _EASYOCR_READER


def _ocr_easy(image_bgr) -> str:
    reader = _get_easyocr_reader()
    rgb = (
        cv2.cvtColor(image_bgr, cv2.COLOR_GRAY2RGB)
        if len(image_bgr.shape) == 2
        else cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    )
    lines = reader.readtext(rgb, detail=0, paragraph=True)
    return " ".join(str(x) for x in lines)


def _get_paddle_ocr():
    global _PADDLE_OCR, _PADDLE_INIT_ERROR
    if _PADDLE_OCR is not None:
        return _PADDLE_OCR
    if _PADDLE_INIT_ERROR:
        raise RuntimeError(_PADDLE_INIT_ERROR)
    _paddle_env_setup()
    try:
        from paddleocr import PaddleOCR

        try:
            _PADDLE_OCR = PaddleOCR(
                use_angle_cls=False,
                lang="ch",
                show_log=False,
                det_db_thresh=0.2,
                det_limit_side_len=1280,
            )
        except TypeError:
            try:
                _PADDLE_OCR = PaddleOCR(lang="ch")
            except TypeError:
                _PADDLE_OCR = PaddleOCR(use_angle_cls=False, lang="ch")
    except Exception as exc:
        _PADDLE_INIT_ERROR = f"PaddleOCR 初始化失败: {exc}"
        raise RuntimeError(_PADDLE_INIT_ERROR) from exc
    return _PADDLE_OCR


def _collect_text_from_paddle_result(result) -> list[str]:
    parts: list[str] = []
    if result is None:
        return parts
    if isinstance(result, list):
        if result and all(isinstance(x, str) for x in result):
            parts.extend(str(x).strip() for x in result if str(x).strip())
            return parts
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                if isinstance(item[0], str) and isinstance(item[1], (int, float)):
                    parts.append(str(item[0]).strip())
                    continue
                if isinstance(item[1], (list, tuple)) and item[1]:
                    parts.append(str(item[1][0]).strip())
                    continue
            parts.extend(_collect_text_from_paddle_result(item))
        return parts
    if isinstance(result, dict):
        for key in ("rec_text", "text", "transcription"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str) and v.strip():
                        parts.append(v.strip())
        res = result.get("res") or result.get("result")
        if res is not None:
            parts.extend(_collect_text_from_paddle_result(res))
        return parts
    if isinstance(result, tuple) and len(result) >= 2:
        text_part = result[1]
        if isinstance(text_part, (list, tuple)) and text_part:
            parts.append(str(text_part[0]))
        elif isinstance(text_part, str):
            parts.append(text_part)
    return parts


def _ocr_paddle_once(image_bgr, *, det: bool = True) -> str:
    ocr = _get_paddle_ocr()
    if len(image_bgr.shape) == 2:
        img = cv2.cvtColor(image_bgr, cv2.COLOR_GRAY2BGR)
    else:
        img = image_bgr

    parts: list[str] = []
    if hasattr(ocr, "ocr"):
        try:
            legacy = ocr.ocr(img, det=det, rec=True, cls=False)
        except TypeError:
            try:
                legacy = ocr.ocr(img, cls=False)
            except TypeError:
                legacy = ocr.ocr(img)
        parts.extend(_collect_text_from_paddle_result(legacy))
    if not parts and hasattr(ocr, "predict"):
        try:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            for batch in ocr.predict(rgb):
                parts.extend(_collect_text_from_paddle_result(batch))
        except NotImplementedError as exc:
            raise RuntimeError(
                "Paddle 3.x 在 Windows 上 oneDNN 报错，请: pip install paddlepaddle==2.6.2 paddleocr==2.7.3"
            ) from exc
    return " ".join(parts)


def _ocr_paddle_multi(image_bgr, *, debug_tag: str = "") -> tuple[str, list[dict]]:
    """多路预处理 + det / det=False 整图识别，合并文本。"""
    chunks: list[str] = []
    steps: list[dict] = []
    for name, variant in _preprocess_variants_for_osd(image_bgr):
        for use_det in (True, False):
            try:
                text = _ocr_paddle_once(variant, det=use_det)
            except Exception as exc:
                steps.append({"variant": name, "det": use_det, "error": str(exc)})
                continue
            t = str(text or "").strip()
            steps.append({"variant": name, "det": use_det, "text": t})
            if t:
                chunks.append(t)
            if debug_tag:
                dbg = os.environ.get("CORNER_OCR_DEBUG_DIR", "").strip()
                if dbg:
                    p = Path(dbg)
                    p.mkdir(parents=True, exist_ok=True)
                    suffix = f"{debug_tag}_{name}_{'det' if use_det else 'rec'}.png"
                    cv2.imwrite(str(p / suffix), variant)
    merged = " ".join(chunks).strip()
    return merged, steps


def ocr_image_corner(image_bgr, *, engine: str = "auto", debug_tag: str = "") -> tuple[str, list[dict]]:
    eng = str(engine or "auto").strip().lower()
    if eng == "auto":
        eng = default_ocr_engine()

    errors: list[str] = []
    steps: list[dict] = []
    if eng in ("paddle", "auto"):
        try:
            text, steps = _ocr_paddle_multi(image_bgr, debug_tag=debug_tag)
            if text:
                return text, steps
        except ImportError:
            errors.append("未安装 paddleocr（见 requirements-ocr.txt）")
        except Exception as exc:
            errors.append(f"paddle: {exc}")
            if eng == "paddle":
                raise

    if eng in ("easy", "auto"):
        try:
            parts: list[str] = []
            for name, variant in _preprocess_variants_for_osd(image_bgr):
                t = _ocr_easy(variant).strip()
                steps.append({"variant": name, "engine": "easy", "text": t})
                if t:
                    parts.append(t)
            text = " ".join(parts).strip()
            if text:
                return text, steps
        except ImportError:
            errors.append("未安装 easyocr")
        except Exception as exc:
            errors.append(f"easyocr: {exc}")

    hint = "；".join(errors) if errors else f"未知引擎 {engine}"
    raise RuntimeError(f"OCR 失败（{hint}）")


def read_frames_at_indices(
    video_path: str | Path,
    indices: tuple[int, ...],
) -> list[tuple[int, object]]:
    path = Path(video_path)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    out: list[tuple[int, object]] = []
    try:
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
            ret, frame = cap.read()
            if ret and frame is not None:
                out.append((idx, frame))
    finally:
        cap.release()
    return out


def read_corner_label_from_video(
    video_path: str | Path,
    *,
    roi: CornerRoi | None = None,
    sample_frame_indices: tuple[int, ...] = (0, 30, 60, 90),
    engine: str = "auto",
) -> tuple[str | None, dict]:
    roi = roi or default_corner_roi()
    path = Path(video_path)
    eng = str(engine or "auto").strip().lower() or default_ocr_engine()
    votes: dict[str, int] = {}
    meta: dict = {"video": str(path), "engine": eng, "frames": []}

    _ocr_log(f"开始 OCR: {path.name} engine={eng} roi=({roi.x0},{roi.y0},{roi.x1},{roi.y1})")

    frames = read_frames_at_indices(path, sample_frame_indices)
    if not frames:
        cap = cv2.VideoCapture(str(path))
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return None, {**meta, "error": "无法读取视频帧"}
        frames = [(0, frame)]

    last_error = ""
    for frame_idx, frame in frames:
        crop = _crop_roi(frame, roi)
        h, w = crop.shape[:2]
        dbg_dir = os.environ.get("CORNER_OCR_DEBUG_DIR", "").strip()
        if dbg_dir:
            Path(dbg_dir).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(Path(dbg_dir) / f"crop_f{frame_idx}.png"), crop)
        try:
            raw, ocr_steps = ocr_image_corner(crop, engine=eng, debug_tag=f"f{frame_idx}")
        except Exception as exc:
            last_error = str(exc)
            _ocr_log(f"帧 {frame_idx} 失败: {last_error}")
            meta["frames"].append({"frame": frame_idx, "error": last_error})
            continue
        cands = extract_corner_label_candidates(raw)
        norm = _normalize_for_label_extract(raw)
        _ocr_log(
            f"帧 {frame_idx} crop={w}x{h} raw={raw!r} norm={norm!r} candidates={cands!r}"
        )
        for step in ocr_steps:
            if step.get("text"):
                _ocr_log(f"  · {step.get('variant')} det={step.get('det')} → {step.get('text')!r}")
        meta["frames"].append(
            {
                "frame": frame_idx,
                "raw": raw,
                "normalized": norm,
                "candidates": cands,
                "ocr_steps": ocr_steps,
            }
        )
        for c in cands:
            votes[c] = votes.get(c, 0) + 1

    if not votes:
        err = last_error or "未识别到符合 数字-数字组-数字 的标签"
        _ocr_log(f"未匹配: {err}")
        meta["error"] = err
        return None, meta

    best = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
    meta["votes"] = votes
    meta["chosen"] = best
    _ocr_log(f"选用机位: {best!r} votes={votes}")
    return best, meta
