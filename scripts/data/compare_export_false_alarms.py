#!/usr/bin/env python3
"""对比两个 export 目录的误报/漏报差异，生成 Markdown 报告。

数据来源：各目录 accuracy_report.json
  - 误报：clips[].diagnostics.false_alarms
  - 漏报：clips[].diagnostics.missed_segments

用法（项目根目录）:
  python scripts/data/compare_export_false_alarms.py \\
    --baseline localdata/export/rule-baseline-prod-test \\
    --experiment localdata/export/rule-speed-prefilter-prod-test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_report(dir_path: Path) -> dict[str, Any]:
    path = dir_path / "accuracy_report.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 accuracy_report.json: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _fp_key(row: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("upload_file") or row.get("clip") or ""),
        int(row.get("frame_idx") or 0),
        str(row.get("box_token") or "").strip(),
    )


def _fn_key(row: dict[str, Any], clip_row: dict[str, Any]) -> tuple[str, int, int, str]:
    tokens = row.get("gt_tokens") or []
    tok = ",".join(sorted(str(t) for t in tokens))
    return (
        str(clip_row.get("upload_file") or clip_row.get("clip") or ""),
        int(row.get("frame_start") or 0),
        int(row.get("frame_end") or 0),
        tok,
    )


def _collect_fps(report: dict[str, Any]) -> dict[tuple[str, int, str], dict[str, Any]]:
    out: dict[tuple[str, int, str], dict[str, Any]] = {}
    for clip in report.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        cam = str(clip.get("camera_slug") or "")
        upload = str(clip.get("upload_file") or clip.get("clip") or "")
        for fa in (clip.get("diagnostics") or {}).get("false_alarms") or []:
            if not isinstance(fa, dict):
                continue
            row = {
                "upload_file": upload,
                "camera_slug": cam,
                "frame_idx": int(fa.get("frame_idx") or 0),
                "box_token": str(fa.get("box_token") or "").strip(),
                "seek_frame": fa.get("seek_frame"),
                "label": fa.get("label"),
            }
            out[_fp_key(row)] = row
    return out


def _collect_fns(report: dict[str, Any]) -> dict[tuple[str, int, int, str], dict[str, Any]]:
    out: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for clip in report.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        cam = str(clip.get("camera_slug") or "")
        upload = str(clip.get("upload_file") or clip.get("clip") or "")
        for ms in (clip.get("diagnostics") or {}).get("missed_segments") or []:
            if not isinstance(ms, dict):
                continue
            row = {
                "upload_file": upload,
                "camera_slug": cam,
                "frame_start": int(ms.get("frame_start") or 0),
                "frame_end": int(ms.get("frame_end") or 0),
                "seek_frame": ms.get("seek_frame"),
                "gt_tokens": list(ms.get("gt_tokens") or []),
                "label": ms.get("label"),
            }
            out[_fn_key(row, clip)] = row
    return out


def _fmt_tokens(tokens: list[str]) -> str:
    items = [str(t).strip() for t in tokens if str(t).strip()]
    return ", ".join(f"`{t}`" for t in items) if items else "—"


def _group_fp_summary(rows: list[dict[str, Any]]) -> list[str]:
    by_clip: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_clip[str(r.get("upload_file") or "")].append(int(r.get("frame_idx") or 0))
    lines: list[str] = []
    for clip in sorted(by_clip.keys()):
        frames = sorted(set(by_clip[clip]))
        frame_txt = ", ".join(str(f) for f in frames)
        lines.append(f"- `{clip}`：{len(frames)} 次 · 帧 {frame_txt}")
    return lines


def _group_fn_summary(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for r in sorted(rows, key=lambda x: (x.get("upload_file"), x.get("frame_start"))):
        clip = r.get("upload_file")
        a = r.get("frame_start")
        b = r.get("frame_end")
        toks = _fmt_tokens(r.get("gt_tokens") or [])
        lines.append(f"- `{clip}`：1 段 · 帧 {a}–{b} ({toks})")
    return lines


def render_compare_markdown(
    baseline_dir: Path,
    experiment_dir: Path,
    *,
    baseline_report: dict[str, Any],
    experiment_report: dict[str, Any],
) -> str:
    b_sum = baseline_report.get("summary") or {}
    e_sum = experiment_report.get("summary") or {}

    b_fps = _collect_fps(baseline_report)
    e_fps = _collect_fps(experiment_report)
    b_fns = _collect_fns(baseline_report)
    e_fns = _collect_fns(experiment_report)

    removed_fp = [b_fps[k] for k in b_fps if k not in e_fps]
    added_fp = [e_fps[k] for k in e_fps if k not in b_fps]
    removed_fn = [b_fns[k] for k in b_fns if k not in e_fns]
    added_fn = [e_fns[k] for k in e_fns if k not in b_fns]

    removed_fp.sort(key=lambda r: (r.get("upload_file"), r.get("frame_idx")))
    added_fp.sort(key=lambda r: (r.get("upload_file"), r.get("frame_idx")))
    added_fn.sort(key=lambda r: (r.get("upload_file"), r.get("frame_start")))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    exp_name = experiment_dir.name
    out_name = f"compare_{exp_name}_vs_{baseline_dir.name}.md"

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
        f"| 误报总数（FP） | {b_sum.get('false_alarms', 0)} | {e_sum.get('false_alarms', 0)} | "
        f"{int(e_sum.get('false_alarms') or 0) - int(b_sum.get('false_alarms') or 0):+d} |",
        f"| 减少的误报（仅基准有） | — | — | {len(removed_fp)} |",
        f"| 新增的误报（仅实验有） | — | — | {len(added_fp)} |",
        f"| 漏报段总数（FN） | {b_sum.get('missed', 0)} | {e_sum.get('missed', 0)} | "
        f"{int(e_sum.get('missed') or 0) - int(b_sum.get('missed') or 0):+d} |",
        f"| 减少的漏报（仅基准有） | — | — | {len(removed_fn)} |",
        f"| 新增的漏报（仅实验有） | — | — | {len(added_fn)} |",
        f"| 召回率 | {b_sum.get('recall')} | {e_sum.get('recall')} | — |",
        f"| 基准 eval_id | `{b_sum.get('eval_id')}` | — | — |",
        f"| 实验 eval_id | — | `{e_sum.get('eval_id')}` | — |",
        "",
        f"- 误报净变化 {int(e_sum.get('false_alarms') or 0) - int(b_sum.get('false_alarms') or 0):+d} 次"
        f"（{b_sum.get('false_alarms')} → {e_sum.get('false_alarms')}）。",
        f"- 漏报净变化 {int(e_sum.get('missed') or 0) - int(b_sum.get('missed') or 0):+d} 段"
        f"（{b_sum.get('missed')} → {e_sum.get('missed')}）。",
        "",
        "## 减少的误报（基准有、实验无）",
        "",
    ]

    if removed_fp:
        lines += [
            "| 记录 | 机位 | 帧 | 货框 |",
            "|------|------|-----|------|",
        ]
        for r in removed_fp:
            lines.append(
                f"| `{r.get('upload_file')}` | {r.get('camera_slug') or '—'} | "
                f"{r.get('frame_idx')} | `{r.get('box_token')}` |"
            )
        lines += ["", "### 按记录汇总", ""] + _group_fp_summary(removed_fp)
    else:
        lines.append("_无_")

    lines += ["", "## 新增的误报（实验有、基准无）", ""]
    if added_fp:
        lines += [
            "| 记录 | 机位 | 帧 | 货框 |",
            "|------|------|-----|------|",
        ]
        for r in added_fp:
            lines.append(
                f"| `{r.get('upload_file')}` | {r.get('camera_slug') or '—'} | "
                f"{r.get('frame_idx')} | `{r.get('box_token')}` |"
            )
        lines += ["", "### 按记录汇总", ""] + _group_fp_summary(added_fp)
    else:
        lines.append("_无_")

    lines += ["", "## 减少的漏报（基准有、实验无）", ""]
    if removed_fn:
        lines += [
            "| 记录 | 机位 | 帧区间 | seek_frame | 标真货框 |",
            "|------|------|--------|------------|----------|",
        ]
        for r in removed_fn:
            lines.append(
                f"| `{r.get('upload_file')}` | {r.get('camera_slug') or '—'} | "
                f"{r.get('frame_start')}–{r.get('frame_end')} | {r.get('seek_frame')} | "
                f"{_fmt_tokens(r.get('gt_tokens') or [])} |"
            )
        lines += ["", "### 按记录汇总", ""] + _group_fn_summary(removed_fn)
    else:
        lines.append("_无_")

    lines += ["", "## 新增的漏报（实验有、基准无）", ""]
    if added_fn:
        lines += [
            "| 记录 | 机位 | 帧区间 | seek_frame | 标真货框 |",
            "|------|------|--------|------------|----------|",
        ]
        for r in added_fn:
            label = str(r.get("label") or "")
            extra = ""
            if "(" in label:
                m = re.search(r"\(([^)]+)\)", label)
                if m:
                    extra = f"({m.group(1)})"
            lines.append(
                f"| `{r.get('upload_file')}` | {r.get('camera_slug') or '—'} | "
                f"{r.get('frame_start')}–{r.get('frame_end')}{extra} | {r.get('seek_frame')} | "
                f"{_fmt_tokens(r.get('gt_tokens') or [])} |"
            )
        lines += ["", "### 按记录汇总", ""] + _group_fn_summary(added_fn)
    else:
        lines.append("_无_")

    lines += [
        "",
        "## 方法说明",
        "",
        "- 数据来源：各目录内 `accuracy_report.json`",
        "  - 误报：`clips[].diagnostics.false_alarms`",
        "  - 漏报：`clips[].diagnostics.missed_segments`",
        f"- 脚本：`scripts/data/compare_export_false_alarms.py`",
        f"- 输出文件：`{out_name}`",
    ]
    return "\n".join(lines) + "\n"


def _ordinal_map(report: dict[str, Any]) -> dict[str, dict[tuple[int, str], int]]:
    """每条 clip 的 (frame, box) → 误报序号（1-based，按帧排序）。"""
    out: dict[str, dict[tuple[int, str], int]] = {}
    for clip in report.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        upload = str(clip.get("upload_file") or clip.get("clip") or "")
        fps = list((clip.get("diagnostics") or {}).get("false_alarms") or [])
        fps.sort(key=lambda x: int(x.get("frame_idx") or 0))
        mapping: dict[tuple[int, str], int] = {}
        for i, fa in enumerate(fps, start=1):
            mapping[(int(fa.get("frame_idx") or 0), str(fa.get("box_token") or "").strip())] = i
        out[upload] = mapping
    return out


def _strike_numbers_in_cell(cell: str, strike_ordinals: set[int]) -> str:
    """将单元格中的序号加上删除线。"""
    if not cell or not strike_ordinals:
        return cell

    # 先去掉旧删除线，统一按数字处理
    text = re.sub(r"~~(\d+)~~", r"\1", cell)

    def _fmt_num(n: int) -> str:
        return f"~~{n}~~" if n in strike_ordinals else str(n)

    def _replace_range(m: re.Match) -> str:
        a, b = int(m.group(1)), int(m.group(2))
        suffix = m.group(3) or ""
        if a > b:
            a, b = b, a
        if b - a >= 6:
            # 长区间：若全消除则整体删除线，否则保留原文
            if all(n in strike_ordinals for n in range(a, b + 1)):
                return f"~~{a}-{b}~~{suffix}"
            return m.group(0)
        parts = [_fmt_num(n) for n in range(a, b + 1)]
        return "，".join(parts) + suffix

    text = re.sub(r"(\d+)-(\d+)([^，\d]|$)", _replace_range, text)

    def _replace_single(m: re.Match) -> str:
        n = int(m.group(1))
        return _fmt_num(n)

    # 不匹配区间内的数字（如 4-62 中的 4 和 62）
    text = re.sub(r"(?<![\d-])(\d+)(?![\d-])", _replace_single, text)
    return text


def render_fp_fn_situation_markdown(
    template_path: Path,
    baseline_dir: Path,
    experiment_dir: Path,
    *,
    baseline_report: dict[str, Any],
    experiment_report: dict[str, Any],
) -> str:
    """基于 baseline 误报漏报模板，用实验消除的误报序号标注删除线。"""
    template = template_path.read_text(encoding="utf-8") if template_path.is_file() else ""
    # 去掉已有删除线，以 baseline 原始序号为准
    base_clean = re.sub(r"~~(\d+)~~", r"\1", template)
    base_clean = re.sub(
        r"说明：.*?\n\n",
        "",
        base_clean,
        count=1,
        flags=re.DOTALL,
    )

    b_fps = _collect_fps(baseline_report)
    e_fps = _collect_fps(experiment_report)
    b_ord = _ordinal_map(baseline_report)

    strike_by_clip: dict[str, set[int]] = defaultdict(set)
    for key, row in b_fps.items():
        if key in e_fps:
            continue
        upload = str(row.get("upload_file") or "")
        ord_map = b_ord.get(upload) or {}
        ordinal = ord_map.get((int(row.get("frame_idx") or 0), str(row.get("box_token") or "").strip()))
        if ordinal:
            strike_by_clip[upload].add(ordinal)

    exp_name = experiment_dir.name
    header = (
        f"说明：每个表格的数字表明是第几个错误事件；"
        f"~~删除线~~ 表示该误报已被 `{exp_name}` 消除（相对 `{baseline_dir.name}`）。\n\n"
    )

    lines = base_clean.splitlines()
    out_lines: list[str] = [header.rstrip(), ""]
    in_fp_table = False
    for line in lines:
        if line.startswith("## 误报"):
            in_fp_table = True
            out_lines.append(line)
            continue
        if line.startswith("## ") and in_fp_table:
            in_fp_table = False
        if in_fp_table and line.startswith("| rtmpose-m/"):
            parts = line.split("|")
            if len(parts) >= 3:
                record_cell = parts[1].strip()
                clip_guess = None
                if "/" in record_cell:
                    slug = record_cell.split("/")[-1].strip()
                    clip_guess = f"{slug}.json" if not slug.endswith(".json") else slug
                strikes = set()
                if clip_guess:
                    strikes = strike_by_clip.get(clip_guess, set())
                    if not strikes:
                        for k, v in strike_by_clip.items():
                            if slug in k:
                                strikes = v
                                break
                for i in range(2, len(parts) - 1):
                    parts[i] = " " + _strike_numbers_in_cell(parts[i].strip(), strikes) + " "
                line = "|".join(parts)
        out_lines.append(line)

    # 追加：相对实验的新增漏报
    b_fns = _collect_fns(baseline_report)
    e_fns = _collect_fns(experiment_report)
    added_fn = [e_fns[k] for k in e_fns if k not in b_fns]
    if added_fn:
        out_lines += [
            "",
            f"## 补充：{exp_name} 新增漏报（相对 baseline）",
            "",
            "| 记录 | 机位 | 帧区间 | 标真货框 |",
            "|------|------|--------|----------|",
        ]
        for r in sorted(added_fn, key=lambda x: (x.get("upload_file"), x.get("frame_start"))):
            out_lines.append(
                f"| `{r.get('upload_file')}` | {r.get('camera_slug') or '—'} | "
                f"{r.get('frame_start')}–{r.get('frame_end')} | "
                f"{_fmt_tokens(r.get('gt_tokens') or [])} |"
            )

    out_lines += [
        "",
        f"速度：前置过滤 `{exp_name}` 在走过货位类误报上与段级后置过滤思路一致，"
        f"在手腕进框前按帧级 `lower_mean_speed` 门控。",
        "",
        f"对比明细：`compare_{exp_name}_vs_{baseline_dir.name}.md`",
    ]
    return "\n".join(out_lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="对比 export 目录误报/漏报")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument(
        "--template",
        type=Path,
        default=ROOT / "localdata/export/rule-baseline-prod-test误报漏报情况.md",
        help="误报漏报人工分类模板（无删除线或含旧删除线均可）",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    baseline_dir = args.baseline.resolve()
    experiment_dir = args.experiment.resolve()
    output_dir = (args.output_dir or ROOT / "localdata/export").resolve()

    baseline_report = _load_report(baseline_dir)
    experiment_report = _load_report(experiment_dir)

    compare_md = render_compare_markdown(
        baseline_dir,
        experiment_dir,
        baseline_report=baseline_report,
        experiment_report=experiment_report,
    )
    compare_name = f"compare_{experiment_dir.name}_vs_{baseline_dir.name}.md"
    compare_path = output_dir / compare_name
    compare_path.write_text(compare_md, encoding="utf-8")
    print(f"对比报告: {compare_path}")

    situation_name = f"{experiment_dir.name}误报漏报情况.md"
    situation_path = output_dir / situation_name
    situation_md = render_fp_fn_situation_markdown(
        args.template.resolve(),
        baseline_dir,
        experiment_dir,
        baseline_report=baseline_report,
        experiment_report=experiment_report,
    )
    situation_path.write_text(situation_md, encoding="utf-8")
    print(f"误报漏报: {situation_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
