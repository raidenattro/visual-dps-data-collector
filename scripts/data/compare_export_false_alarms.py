#!/usr/bin/env python3
"""对比两个 export 目录 accuracy_report.json 的误报/漏报差异。

用法（项目根目录）:
  python scripts/data/compare_export_false_alarms.py \\
    --baseline localdata/export/rule-baseline-prod-test \\
    --experiment localdata/export/rule-speed-lower60-prod-test
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REPORT_NAME = "accuracy_report.json"


def _load_report(dir_path: Path) -> dict[str, Any]:
    report_path = dir_path / DEFAULT_REPORT_NAME
    if not report_path.is_file():
        raise FileNotFoundError(f"缺少 {report_path}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _clip_meta(clip: dict[str, Any]) -> dict[str, str]:
    return {
        "record_id": str(clip.get("record_id") or "").strip(),
        "upload_file": str(clip.get("upload_file") or clip.get("clip") or "").strip(),
        "camera_slug": str(clip.get("camera_slug") or "").strip(),
    }


def _extract_false_alarms(report: dict[str, Any]) -> dict[tuple[str, int, str], dict[str, Any]]:
    """提取误报集合，键为 (record_id, frame_idx, box_token)。"""
    out: dict[tuple[str, int, str], dict[str, Any]] = {}
    for clip in report.get("clips") or []:
        if not isinstance(clip, dict) or clip.get("status") != "ok":
            continue
        meta = _clip_meta(clip)
        record_id = meta["record_id"]
        if not record_id:
            continue
        diag = clip.get("diagnostics") or {}
        for row in diag.get("false_alarms") or []:
            if not isinstance(row, dict):
                continue
            frame_idx = int(row.get("frame_idx") or row.get("seek_frame") or 0)
            box_token = str(row.get("box_token") or "").strip()
            if frame_idx <= 0:
                continue
            key = (record_id, frame_idx, box_token)
            out[key] = {
                **meta,
                "frame_idx": frame_idx,
                "box_token": box_token,
                "label": str(row.get("label") or f"误报 · {box_token} · 帧 {frame_idx}"),
            }
    return out


def _missed_segment_key(record_id: str, row: dict[str, Any]) -> tuple[str, int, int, tuple[str, ...]]:
    frame_start = int(row.get("frame_start") or row.get("seek_frame") or 0)
    frame_end = int(row.get("frame_end") or frame_start)
    gt_tokens = tuple(sorted(str(t).strip() for t in (row.get("gt_tokens") or []) if str(t).strip()))
    return record_id, frame_start, frame_end, gt_tokens


def _extract_missed_segments(report: dict[str, Any]) -> dict[tuple[str, int, int, tuple[str, ...]], dict[str, Any]]:
    """提取漏报段集合，键为 (record_id, frame_start, frame_end, gt_tokens)。"""
    out: dict[tuple[str, int, int, tuple[str, ...]], dict[str, Any]] = {}
    for clip in report.get("clips") or []:
        if not isinstance(clip, dict) or clip.get("status") != "ok":
            continue
        meta = _clip_meta(clip)
        record_id = meta["record_id"]
        if not record_id:
            continue
        diag = clip.get("diagnostics") or {}
        for row in diag.get("missed_segments") or []:
            if not isinstance(row, dict):
                continue
            frame_start = int(row.get("frame_start") or row.get("seek_frame") or 0)
            frame_end = int(row.get("frame_end") or frame_start)
            if frame_start <= 0:
                continue
            gt_tokens = [str(t).strip() for t in (row.get("gt_tokens") or []) if str(t).strip()]
            key = _missed_segment_key(record_id, row)
            out[key] = {
                **meta,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "seek_frame": int(row.get("seek_frame") or frame_start),
                "gt_tokens": gt_tokens,
                "label": str(
                    row.get("label")
                    or f"漏报 · {', '.join(gt_tokens) or '—'} · 帧 {frame_start}–{frame_end}"
                ),
            }
    return out


def _sort_fp_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            str(r.get("record_id") or ""),
            int(r.get("frame_idx") or 0),
            str(r.get("box_token") or ""),
        ),
    )


def _sort_fn_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            str(r.get("record_id") or ""),
            int(r.get("frame_start") or 0),
            int(r.get("frame_end") or 0),
            ",".join(r.get("gt_tokens") or []),
        ),
    )


def _group_by_record_fp(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("record_id") or "")].append(row)
    for rid in grouped:
        grouped[rid] = sorted(grouped[rid], key=lambda r: int(r.get("frame_idx") or 0))
    return dict(grouped)


def _group_by_record_fn(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("record_id") or "")].append(row)
    for rid in grouped:
        grouped[rid] = sorted(grouped[rid], key=lambda r: int(r.get("frame_start") or 0))
    return dict(grouped)


def _short_record_id(record_id: str) -> str:
    return record_id.split("/")[-1] if record_id else record_id


def _format_gt_tokens(tokens: list[str] | tuple[str, ...] | None) -> str:
    items = [str(t).strip() for t in (tokens or []) if str(t).strip()]
    return ", ".join(f"`{t}`" for t in items) if items else "—"


def _render_fp_section(title: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("_无_")
        return lines
    grouped = _group_by_record_fp(rows)
    lines.extend(["| 记录 | 机位 | 帧 | 货框 |", "|------|------|-----|------|"])
    for rid in sorted(grouped):
        group = grouped[rid]
        upload_file = group[0].get("upload_file") or _short_record_id(rid)
        camera = group[0].get("camera_slug") or "—"
        for row in group:
            lines.append(
                f"| `{upload_file}` | {camera} | {row['frame_idx']} | `{row['box_token']}` |"
            )
    lines.extend(["", "### 按记录汇总", ""])
    for rid in sorted(grouped):
        group = grouped[rid]
        frames = ", ".join(str(r["frame_idx"]) for r in group)
        lines.append(
            f"- `{group[0].get('upload_file') or _short_record_id(rid)}`：{len(group)} 次 · 帧 {frames}"
        )
    return lines


def _render_fn_section(title: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("_无_")
        return lines
    grouped = _group_by_record_fn(rows)
    lines.extend(
        [
            "| 记录 | 机位 | 帧区间 | seek_frame | 标真货框 |",
            "|------|------|--------|------------|----------|",
        ]
    )
    for rid in sorted(grouped):
        group = grouped[rid]
        upload_file = group[0].get("upload_file") or _short_record_id(rid)
        camera = group[0].get("camera_slug") or "—"
        for row in group:
            frame_range = (
                str(row["frame_start"])
                if row["frame_start"] == row["frame_end"]
                else f"{row['frame_start']}–{row['frame_end']}"
            )
            lines.append(
                f"| `{upload_file}` | {camera} | {frame_range} | {row['seek_frame']} | "
                f"{_format_gt_tokens(row.get('gt_tokens'))} |"
            )
    lines.extend(["", "### 按记录汇总", ""])
    for rid in sorted(grouped):
        group = grouped[rid]
        parts = []
        for row in group:
            if row["frame_start"] == row["frame_end"]:
                parts.append(f"帧 {row['frame_start']} ({', '.join(row.get('gt_tokens') or [])})")
            else:
                parts.append(
                    f"帧 {row['frame_start']}–{row['frame_end']} ({', '.join(row.get('gt_tokens') or [])})"
                )
        lines.append(
            f"- `{group[0].get('upload_file') or _short_record_id(rid)}`：{len(group)} 段 · {'; '.join(parts)}"
        )
    return lines


def _render_markdown(
    *,
    baseline_dir: Path,
    experiment_dir: Path,
    baseline_report: dict[str, Any],
    experiment_report: dict[str, Any],
    reduced_fp: list[dict[str, Any]],
    added_fp: list[dict[str, Any]],
    reduced_fn: list[dict[str, Any]],
    added_fn: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    b_sum = baseline_report.get("summary") or {}
    e_sum = experiment_report.get("summary") or {}
    b_fp = int(b_sum.get("false_alarms") or 0)
    e_fp = int(e_sum.get("false_alarms") or 0)
    b_fn = int(b_sum.get("missed") or 0)
    e_fn = int(e_sum.get("missed") or 0)
    fp_delta = e_fp - b_fp
    fn_delta = e_fn - b_fn

    lines = [
        "# 误报 / 漏报对比报告",
        "",
        f"> 生成时间：{now}  ",
        f"> 基准（baseline）：`{baseline_dir}`  ",
        f"> 实验（experiment）：`{experiment_dir}`  ",
        "> 误报维度：单条事件 = `record_id` + `frame_idx` + `box_token`  ",
        "> 漏报维度：单段事件 = `record_id` + `frame_start` + `frame_end` + `gt_tokens`",
        "",
        "## 汇总",
        "",
        "| 指标 | 基准 | 实验 | 变化 (实验−基准) |",
        "|------|------|------|------------------|",
        f"| 误报总数（FP） | {b_fp} | {e_fp} | {fp_delta:+d} |",
        f"| 减少的误报（仅基准有） | — | — | {len(reduced_fp)} |",
        f"| 新增的误报（仅实验有） | — | — | {len(added_fp)} |",
        f"| 漏报段总数（FN） | {b_fn} | {e_fn} | {fn_delta:+d} |",
        f"| 减少的漏报（仅基准有） | — | — | {len(reduced_fn)} |",
        f"| 新增的漏报（仅实验有） | — | — | {len(added_fn)} |",
        f"| 召回率 | {b_sum.get('recall', '—')} | {e_sum.get('recall', '—')} | — |",
        f"| 基准 eval_id | `{b_sum.get('eval_id', '')}` | — | — |",
        f"| 实验 eval_id | — | `{e_sum.get('eval_id', '')}` | — |",
        "",
    ]

    if fp_delta < 0:
        lines.append(f"- 误报净减少 {abs(fp_delta)} 次（{b_fp} → {e_fp}）。")
    elif fp_delta > 0:
        lines.append(f"- 误报净增加 {fp_delta} 次（{b_fp} → {e_fp}）。")
    else:
        lines.append(f"- 误报总数不变（{b_fp}），帧级替换 {len(reduced_fp)} 消失 / {len(added_fp)} 新增。")

    if fn_delta < 0:
        lines.append(f"- 漏报净减少 {abs(fn_delta)} 段（{b_fn} → {e_fn}）。")
    elif fn_delta > 0:
        lines.append(f"- 漏报净增加 {fn_delta} 段（{b_fn} → {e_fn}）。")
    else:
        lines.append(f"- 漏报段数不变（{b_fn}），段级替换 {len(reduced_fn)} 消失 / {len(added_fn)} 新增。")
    lines.append("")

    lines.extend(_render_fp_section("减少的误报（基准有、实验无）", reduced_fp))
    lines.append("")
    lines.extend(_render_fp_section("新增的误报（实验有、基准无）", added_fp))
    lines.append("")
    lines.extend(_render_fn_section("减少的漏报（基准有、实验无）", reduced_fn))
    lines.append("")
    lines.extend(_render_fn_section("新增的漏报（实验有、基准无）", added_fn))
    lines.extend(
        [
            "",
            "## 方法说明",
            "",
            "- 数据来源：各目录内 `accuracy_report.json`",
            "  - 误报：`clips[].diagnostics.false_alarms`",
            "  - 漏报：`clips[].diagnostics.missed_segments`",
            "- 脚本：`scripts/data/compare_export_false_alarms.py`",
            "",
        ]
    )
    return "\n".join(lines)


def compare_reports(baseline_dir: Path, experiment_dir: Path) -> dict[str, Any]:
    baseline_report = _load_report(baseline_dir)
    experiment_report = _load_report(experiment_dir)

    baseline_fps = _extract_false_alarms(baseline_report)
    experiment_fps = _extract_false_alarms(experiment_report)
    reduced_fp = _sort_fp_rows([baseline_fps[k] for k in set(baseline_fps) - set(experiment_fps)])
    added_fp = _sort_fp_rows([experiment_fps[k] for k in set(experiment_fps) - set(baseline_fps)])

    baseline_fns = _extract_missed_segments(baseline_report)
    experiment_fns = _extract_missed_segments(experiment_report)
    reduced_fn = _sort_fn_rows([baseline_fns[k] for k in set(baseline_fns) - set(experiment_fns)])
    added_fn = _sort_fn_rows([experiment_fns[k] for k in set(experiment_fns) - set(baseline_fns)])

    return {
        "baseline_dir": str(baseline_dir),
        "experiment_dir": str(experiment_dir),
        "baseline_summary": baseline_report.get("summary") or {},
        "experiment_summary": experiment_report.get("summary") or {},
        "reduced_false_alarms": reduced_fp,
        "added_false_alarms": added_fp,
        "reduced_missed_segments": reduced_fn,
        "added_missed_segments": added_fn,
        "counts": {
            "baseline_fp": len(baseline_fps),
            "experiment_fp": len(experiment_fps),
            "reduced_fp": len(reduced_fp),
            "added_fp": len(added_fp),
            "net_fp_delta": len(experiment_fps) - len(baseline_fps),
            "baseline_fn": len(baseline_fns),
            "experiment_fn": len(experiment_fns),
            "reduced_fn": len(reduced_fn),
            "added_fn": len(added_fn),
            "net_fn_delta": len(experiment_fns) - len(baseline_fns),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="对比两个 export 目录的误报/漏报差异")
    parser.add_argument("--baseline", required=True, help="基准目录（含 accuracy_report.json）")
    parser.add_argument("--experiment", required=True, help="实验目录（含 accuracy_report.json）")
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "localdata" / "export" / "compare"),
        help="报告输出目录（默认 localdata/export/compare）",
    )
    parser.add_argument("--stem", default="", help="报告文件名前缀（默认 compare_{实验}_vs_{基准}）")
    args = parser.parse_args()

    baseline_dir = Path(args.baseline)
    experiment_dir = Path(args.experiment)
    if not baseline_dir.is_dir():
        print(f"基准目录不存在: {baseline_dir}", file=sys.stderr)
        return 1
    if not experiment_dir.is_dir():
        print(f"实验目录不存在: {experiment_dir}", file=sys.stderr)
        return 1

    payload = compare_reports(baseline_dir, experiment_dir)
    stem = args.stem.strip() or f"compare_{experiment_dir.name}_vs_{baseline_dir.name}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"

    md_path.write_text(
        _render_markdown(
            baseline_dir=baseline_dir,
            experiment_dir=experiment_dir,
            baseline_report=_load_report(baseline_dir),
            experiment_report=_load_report(experiment_dir),
            reduced_fp=payload["reduced_false_alarms"],
            added_fp=payload["added_false_alarms"],
            reduced_fn=payload["reduced_missed_segments"],
            added_fn=payload["added_missed_segments"],
        ),
        encoding="utf-8",
    )
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    c = payload["counts"]
    print(
        f"误报：基准 {c['baseline_fp']} → 实验 {c['experiment_fp']} "
        f"(净变化 {c['net_fp_delta']:+d})，减少 {c['reduced_fp']} / 新增 {c['added_fp']}"
    )
    print(
        f"漏报：基准 {c['baseline_fn']} → 实验 {c['experiment_fn']} "
        f"(净变化 {c['net_fn_delta']:+d})，减少 {c['reduced_fn']} / 新增 {c['added_fn']}"
    )
    print(f"报告: {md_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
