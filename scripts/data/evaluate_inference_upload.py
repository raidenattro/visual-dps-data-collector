#!/usr/bin/env python3
"""评估上传目录中的推测 JSON，与本地 review 标真对比。

规则：is_picking=true 为碰撞告警；货框取 rule_alarm_collisions，无则 rule_collisions；
box_id 兼容匹配（如 85:4017 与 Box_4017）。

用法（项目根目录）:
  python scripts/data/evaluate_inference_upload.py --dry-run
  python scripts/data/evaluate_inference_upload.py \\
    --dir localdata/export/rule-baseline-prod-test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_config_path
from api.inference_eval_service import (
    discover_upload_json_files,
    evaluate_upload_directory,
    load_upload_manifest,
)
from scripts.data.report_paths import DOCS_JSON_DIR, resolve_docs_json


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2%}"


def _render_markdown(result: dict[str, Any], *, dir_path: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s = result.get("summary") or {}
    lines = [
        "# 上传推测结果评估报告",
        "",
        f"> 生成时间：{now}  ",
        f"> 目录：`{dir_path}`  ",
        "> 规则：is_picking=true 为碰撞告警；货框 rule_alarm_collisions → rule_collisions；box_id 兼容匹配",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 上传文件数 | {s.get('clip_count', 0)} |",
        f"| 成功评估 | {s.get('evaluated', 0)} |",
        f"| 跳过 | {s.get('skipped', 0)} |",
        f"| 失败 | {s.get('errors', 0)} |",
        f"| 标真段数 | {s.get('gt_segments', 0)} |",
        f"| 检出（TP） | {s.get('detected', 0)} |",
        f"| 漏报（FN） | {s.get('missed', 0)} |",
        f"| 误报（FP） | {s.get('false_alarms', 0)} |",
        f"| 召回率 | {_pct(s.get('recall'))} |",
        f"| 漏报率 | {_pct(s.get('miss_rate'))} |",
        f"| 精确率（代理） | {_pct(s.get('precision_proxy'))} |",
        "",
        "## 分片明细",
        "",
        "| 文件 | record_id | 状态 | 标真段 | 检出 | 漏报 | 误报 | 召回 |",
        "|------|-----------|------|--------|------|------|------|------|",
    ]
    for row in result.get("clips") or []:
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| {row.get('upload_file', '—')} | `{row.get('record_id', '—')}` | "
            f"{row.get('status', '—')} | {row.get('gt_segments', '—')} | "
            f"{row.get('detected', '—')} | {row.get('missed', '—')} | "
            f"{row.get('false_alarms', '—')} | {_pct(row.get('recall'))} |"
        )
        if row.get("error"):
            lines.append(f"| | | {row.get('error')} | | | | | |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="评估上传推测 JSON 目录（is_picking + 货框）")
    parser.add_argument(
        "--dir",
        default=str(ROOT / "localdata" / "export" / "rule-baseline-prod-test"),
        help="含 clip JSON 与可选 _manifest.json 的目录",
    )
    parser.add_argument("--tags", default="", help="记录标签筛选（逗号分隔，须同时命中）")
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "inference-upload-eval.md"),
        help="Markdown 报告路径",
    )
    parser.add_argument("--json-out", default="", help="JSON 输出路径（默认 docs/json/{报告名}.json）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    dir_path = Path(args.dir)
    if not dir_path.is_dir():
        print(f"目录不存在: {dir_path}", file=sys.stderr)
        return 1

    manifest = load_upload_manifest(dir_path)
    files = discover_upload_json_files(dir_path, manifest=manifest)
    if args.dry_run:
        print(f"将评估 {len(files)} 个 JSON（is_picking + 货框匹配）")
        for p in files:
            print(f"  {p.relative_to(dir_path)}")
        return 0

    tags = [t.strip() for t in str(args.tags or "").replace("，", ",").split(",") if t.strip()]
    result = evaluate_upload_directory(
        dir_path,
        tags=tags or None,
    )

    s = result.get("summary") or {}
    print(
        f"评估完成: {s.get('evaluated', 0)}/{s.get('clip_count', 0)} 片, "
        f"recall={_pct(s.get('recall'))}, FP={s.get('false_alarms', 0)}"
    )

    md = _render_markdown(result, dir_path=dir_path)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"报告: {out_path}")

    json_path = resolve_docs_json(args.out, args.json_out)
    DOCS_JSON_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
