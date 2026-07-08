#!/usr/bin/env python3
"""评估上传目录中的推测 JSON，与本地 review 标真对比。

规则：is_picking=true 为碰撞告警；货框取 rule_alarm_collisions，无则 rule_collisions；
box_id 兼容匹配（如 85:4017 与 Box_4017）。

用法（项目根目录）:
  python scripts/data/evaluate_inference_upload.py --dry-run
  python scripts/data/evaluate_inference_upload.py \\
    --dir localdata/export/rule-baseline-prod-test --in-place
  python scripts/data/evaluate_inference_upload.py \\
    --dirs localdata/export/rule-baseline-prod-test localdata/export/rule-hand-ext-alpha010-prod-test \\
    --in-place
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

DEFAULT_REPORT_STEM = "accuracy_report"
ACCURACY_EVAL_INDEX = "_accuracy_eval.json"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2%}"


def _format_tokens(tokens: list[str] | None) -> str:
    items = [str(t).strip() for t in (tokens or []) if str(t).strip()]
    return ", ".join(items) if items else "—"


def _render_markdown(result: dict[str, Any], *, dir_path: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s = result.get("summary") or {}
    eval_id = result.get("eval_id") or s.get("eval_id") or ""
    lines = [
        "# 上传推测结果评估报告",
        "",
        f"> 生成时间：{now}  ",
        f"> 目录：`{dir_path}`  ",
        f"> eval_id：`{eval_id}`（前端诊断/回放 overlay）  " if eval_id else "",
        "> 规则：is_picking=true 为碰撞告警；货框 rule_alarm_collisions → rule_collisions；box_id 兼容匹配",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 上传文件数 | {s.get('clip_count', 0)} |",
        f"| 成功评估 | {s.get('evaluated', 0)} |",
        f"| 跳过 | {s.get('skipped', 0)} |",
        f"| 排除 | {s.get('excluded', 0)} |",
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

    ok_clips = [
        row
        for row in (result.get("clips") or [])
        if isinstance(row, dict) and row.get("status") == "ok" and row.get("diagnostics")
    ]
    if ok_clips:
        lines.extend(["", "## 诊断明细（漏报 / 误报）", ""])
        for row in ok_clips:
            diag = row.get("diagnostics") or {}
            upload_file = row.get("upload_file") or row.get("record_id") or "—"
            lines.append(f"### {upload_file}")
            lines.append("")
            missed = diag.get("missed_segments") or []
            false_alarms = diag.get("false_alarms") or []
            if missed:
                lines.append("**漏报（FN）**")
                lines.append("")
                lines.append("| seek_frame | frame_start | frame_end | gt_tokens |")
                lines.append("|------------|-------------|-----------|-----------|")
                for item in missed:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        f"| {item.get('seek_frame', item.get('frame_start', '—'))} | "
                        f"{item.get('frame_start', '—')} | {item.get('frame_end', '—')} | "
                        f"{_format_tokens(item.get('gt_tokens'))} |"
                    )
                lines.append("")
            if false_alarms:
                lines.append("**误报（FP）**")
                lines.append("")
                lines.append("| frame_idx | box_token |")
                lines.append("|-----------|-----------|")
                for item in false_alarms:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        f"| {item.get('frame_idx', item.get('seek_frame', '—'))} | "
                        f"{item.get('box_token', '—')} |"
                    )
                lines.append("")
            if not missed and not false_alarms:
                lines.append("（无漏报/误报）")
                lines.append("")

    return "\n".join(line for line in lines if line is not None) + "\n"


def resolve_report_paths(
    dir_path: Path,
    *,
    in_place: bool,
    out: str,
    json_out: str,
    report_stem: str,
) -> tuple[Path, Path]:
    stem = report_stem or DEFAULT_REPORT_STEM
    if in_place:
        return dir_path / f"{stem}.md", dir_path / f"{stem}.json"
    md_path = Path(out)
    json_path = Path(json_out) if json_out else resolve_docs_json(out, "")
    return md_path, json_path


def write_accuracy_eval_index(dir_path: Path, *, eval_id: str, summary: dict[str, Any]) -> Path:
    index_path = dir_path / ACCURACY_EVAL_INDEX
    payload = {
        "eval_id": eval_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    }
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path


def evaluate_dir(
    dir_path: Path,
    *,
    tags: list[str] | None,
    in_place: bool,
    out: str,
    json_out: str,
    report_stem: str,
) -> dict[str, Any]:
    result = evaluate_upload_directory(dir_path, tags=tags or None)
    s = result.get("summary") or {}
    eval_id = result.get("eval_id") or s.get("eval_id") or ""

    md_path, json_path = resolve_report_paths(
        dir_path,
        in_place=in_place,
        out=out,
        json_out=json_out,
        report_stem=report_stem,
    )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown(result, dir_path=dir_path), encoding="utf-8")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = None
    if in_place and eval_id:
        index_path = write_accuracy_eval_index(dir_path, eval_id=eval_id, summary=s)

    return {
        "dir_path": dir_path,
        "result": result,
        "md_path": md_path,
        "json_path": json_path,
        "index_path": index_path,
        "eval_id": eval_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="评估上传推测 JSON 目录（is_picking + 货框）")
    parser.add_argument(
        "--dir",
        default="",
        help="含 clip JSON 与可选 _manifest.json 的目录",
    )
    parser.add_argument(
        "--dirs",
        nargs="*",
        default=None,
        help="批量评估多个目录（与 --dir 二选一）",
    )
    parser.add_argument("--tags", default="", help="记录标签筛选（逗号分隔，须同时命中）")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help=f"报告写到评估目录内：{{dir}}/{DEFAULT_REPORT_STEM}.md 与 .json",
    )
    parser.add_argument(
        "--report-stem",
        default=DEFAULT_REPORT_STEM,
        help=f"in-place 时报告文件名前缀（默认 {DEFAULT_REPORT_STEM}）",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "inference-upload-eval.md"),
        help="Markdown 报告路径（非 in-place 时）",
    )
    parser.add_argument("--json-out", default="", help="JSON 输出路径（非 in-place 时）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)

    if args.dirs:
        dir_paths = [Path(p) for p in args.dirs]
    elif args.dir:
        dir_paths = [Path(args.dir)]
    else:
        dir_paths = [ROOT / "localdata" / "export" / "rule-baseline-prod-test"]

    missing = [p for p in dir_paths if not p.is_dir()]
    if missing:
        for p in missing:
            print(f"目录不存在: {p}", file=sys.stderr)
        return 1

    tags = [t.strip() for t in str(args.tags or "").replace("，", ",").split(",") if t.strip()]

    if args.dry_run:
        for dir_path in dir_paths:
            manifest = load_upload_manifest(dir_path)
            files = discover_upload_json_files(dir_path, manifest=manifest)
            print(f"[{dir_path.name}] 将评估 {len(files)} 个 JSON")
            for p in files:
                print(f"  {p.relative_to(dir_path)}")
        return 0

    exit_code = 0
    for dir_path in dir_paths:
        try:
            written = evaluate_dir(
                dir_path,
                tags=tags or None,
                in_place=args.in_place,
                out=args.out,
                json_out=args.json_out,
                report_stem=args.report_stem,
            )
        except Exception as exc:
            print(f"[{dir_path.name}] 评估失败: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        s = written["result"].get("summary") or {}
        eval_id = written["eval_id"]
        print(
            f"[{dir_path.name}] 评估完成: {s.get('evaluated', 0)}/{s.get('clip_count', 0)} 片, "
            f"recall={_pct(s.get('recall'))}, FP={s.get('false_alarms', 0)}"
        )
        if eval_id:
            print(f"  eval_runs: localdata/eval_runs/{eval_id}")
        print(f"  报告: {written['md_path']}")
        print(f"  JSON: {written['json_path']}")
        if written.get("index_path"):
            print(f"  索引: {written['index_path']}")

    if len(dir_paths) > 1 and not args.in_place:
        print("提示：批量模式建议加 --in-place，将报告写到各 export 子目录。", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
