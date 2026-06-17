"""机位标识（camera）→ reflection → localdata/json/annotations/{编号}.json。"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from corner_label.reflection import (
    ReflectionMap,
    merge_annotation_files,
    normalize_corner_label,
)


@dataclass
class ResolveResult:
    camera_label: str
    annotation_path: Path
    source_annotation_paths: list[Path]
    annotation_ids: list[str]


def _write_merged_temp(data: dict, camera_label: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=f"_{normalize_corner_label(camera_label).replace('-', '_')}.json",
        delete=False,
        encoding="utf-8",
    )
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return Path(tmp.name)


def collect_annotation_paths_for_camera(
    camera_label: str,
    reflection: ReflectionMap,
    annotations_dir: Path,
    *fallback_dirs: Path,
) -> list[Path]:
    """在 primary / fallback 目录查找机位对应的全部标注文件（reflection 多编号）。"""
    from corner_label.reflection import annotation_json_path

    label = normalize_corner_label(camera_label)
    ann_ids = reflection.annotations_for_camera(label)
    if not ann_ids:
        raise FileNotFoundError(f"机位 {label!r} 在 reflection 中无 annotation")
    out: list[Path] = []
    searched: list[str] = []
    for aid in ann_ids:
        found: Path | None = None
        for d in (annotations_dir, *fallback_dirs):
            if d is None:
                continue
            root = Path(d)
            searched.append(str(root))
            p = annotation_json_path(aid, root)
            if p.is_file():
                found = p
                break
        if not found:
            raise FileNotFoundError(
                f"机位 {label!r} 缺少标注 {aid}（目录: {', '.join(searched)})"
            )
        out.append(found)
    return out


def materialize_annotation_paths(paths: list[Path], camera_label: str) -> Path:
    """单文件直接返回；多文件合并为临时 JSON。"""
    if len(paths) == 1:
        return paths[0]
    merged = merge_annotation_files(paths)
    return _write_merged_temp(merged, camera_label)


def resolve_annotation_for_camera(
    camera_label: str,
    *,
    reflection: ReflectionMap,
    annotations_dir: Path,
) -> ResolveResult:
    """按手动输入的机位标识装配标注 JSON。"""
    label = normalize_corner_label(camera_label)
    if not label:
        raise ValueError("机位标识不能为空")
    if not reflection.has_camera(label):
        known = ", ".join(reflection.cameras[:12])
        extra = "…" if len(reflection.cameras) > 12 else ""
        raise ValueError(
            f"机位 {label!r} 不在 reflection.json 中"
            + (f"（示例: {known}{extra}）" if known else "")
        )

    ann_ids = reflection.annotations_for_camera(label)
    src_paths = collect_annotation_paths_for_camera(
        label, reflection, Path(annotations_dir)
    )
    out_path = materialize_annotation_paths(src_paths, label)

    return ResolveResult(
        camera_label=label,
        annotation_path=out_path,
        source_annotation_paths=src_paths,
        annotation_ids=ann_ids,
    )
