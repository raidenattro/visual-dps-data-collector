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
    resolve_annotation_paths_for_camera,
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
    src_paths = resolve_annotation_paths_for_camera(label, reflection, Path(annotations_dir))
    merged = merge_annotation_files(src_paths)
    out_path = _write_merged_temp(merged, label) if len(src_paths) > 1 else src_paths[0]

    return ResolveResult(
        camera_label=label,
        annotation_path=out_path,
        source_annotation_paths=src_paths,
        annotation_ids=ann_ids,
    )
