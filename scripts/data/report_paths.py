"""docs 报告输出路径约定。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
DOCS_JSON_DIR = DOCS_DIR / "json"
DOCS_VIEW_DIR = DOCS_DIR / "view"
DOCS_HAND_PROBE_DIR = DOCS_DIR / "hand-probe"
DOCS_ALARM_MIN_DIR = DOCS_DIR / "alarm-min"
DOCS_SEGMENT_FILTER_DIR = DOCS_DIR / "segment-filter"
DOCS_FEATURES_DIR = DOCS_DIR / "features"
DOCS_GUIDES_DIR = DOCS_DIR / "guides"


def resolve_docs_json(out: str | Path, json_out: str = "") -> Path:
    """由 Markdown 报告路径推导 JSON 路径：docs/json/{stem}.json。"""
    if json_out:
        return Path(json_out)
    md = Path(out)
    stem = md.stem if md.suffix else md.name
    return DOCS_JSON_DIR / f"{stem}.json"
