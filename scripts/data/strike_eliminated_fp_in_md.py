#!/usr/bin/env python3
"""在 baseline 误报漏报情况 MD 中，对被 speed-lower60 消除的误报序号加删除线。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _expand_range(a: int, b: int) -> list[int]:
    return list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))


def _build_eliminated_indices(
    baseline_report: dict,
    reduced_false_alarms: list[dict],
) -> dict[str, set[int]]:
    reduced = {
        (r["record_id"], r["frame_idx"], r["box_token"])
        for r in reduced_false_alarms
    }
    out: dict[str, set[int]] = {}
    for clip in baseline_report.get("clips") or []:
        if clip.get("status") != "ok":
            continue
        rid = str(clip.get("record_id") or "").strip()
        fps = (clip.get("diagnostics") or {}).get("false_alarms") or []
        out[rid] = {
            i
            for i, fp in enumerate(fps, 1)
            if (rid, int(fp["frame_idx"]), fp["box_token"]) in reduced
        }
    return out


def strike_cell(cell: str, eliminated: set[int]) -> str:
    if not cell.strip() or not eliminated:
        return cell
    if cell.strip() in {"SS"}:
        return cell

    token_re = re.compile(r"(\d+)\s*[-–]\s*(\d+)|(\d+)")
    pos = 0
    chunks: list[tuple] = []
    for m in token_re.finditer(cell):
        if m.start() > pos:
            chunks.append(("text", cell[pos : m.start()]))
        if m.group(1) and m.group(2):
            a, b = int(m.group(1)), int(m.group(2))
            chunks.append(("range", a, b, _expand_range(a, b), m.group(0)))
        else:
            chunks.append(("num", int(m.group(3)), m.group(0)))
        pos = m.end()
    if pos < len(cell):
        chunks.append(("text", cell[pos:]))
    if not chunks:
        return cell

    out: list[str] = []
    for ch in chunks:
        if ch[0] == "text":
            out.append(ch[1])
            continue
        if ch[0] == "num":
            n, raw = ch[1], ch[2]
            out.append(f"~~{raw}~~" if n in eliminated else raw)
            continue
        _a, _b, nums, raw = ch[1], ch[2], ch[3], ch[4]
        flags = [n in eliminated for n in nums]
        if all(flags):
            out.append(f"~~{raw}~~")
        elif not any(flags):
            out.append(raw)
        else:
            segs: list[tuple[int, int, bool]] = []
            start = nums[0]
            prev = flags[0]
            for idx in range(1, len(nums)):
                if flags[idx] != prev:
                    segs.append((start, nums[idx - 1], prev))
                    start = nums[idx]
                    prev = flags[idx]
            segs.append((start, nums[-1], prev))
            rendered = []
            for sa, sb, is_elim in segs:
                piece = str(sa) if sa == sb else f"{sa}-{sb}"
                rendered.append(f"~~{piece}~~" if is_elim else piece)
            out.append("，".join(rendered))
    return "".join(out)


def update_markdown(
    md_path: Path,
    eliminated_by_record: dict[str, set[int]],
) -> str:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    in_fp_table = False
    for line in lines:
        if line.startswith("## 误报"):
            in_fp_table = True
            out_lines.append(line)
            continue
        if in_fp_table and line.startswith("## "):
            in_fp_table = False
        if in_fp_table and line.startswith("| rtmpose-m/"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rid = cells[0].strip()
            eliminated = eliminated_by_record.get(rid, set())
            new_cells = [cells[0]] + [strike_cell(c, eliminated) for c in cells[1:]]
            out_lines.append("| " + " | ".join(new_cells) + " |")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="对 MD 中被消除的误报序号加删除线")
    parser.add_argument(
        "--md",
        default=str(ROOT / "localdata" / "export" / "rule-baseline-prod-test误报漏报情况.md"),
    )
    parser.add_argument(
        "--baseline-report",
        default=str(ROOT / "localdata" / "export" / "rule-baseline-prod-test" / "accuracy_report.json"),
    )
    parser.add_argument(
        "--compare-json",
        default=str(
            ROOT
            / "localdata"
            / "export"
            / "compare_rule-speed-lower60-prod-test_vs_rule-baseline-prod-test.json"
        ),
    )
    args = parser.parse_args()

    md_path = Path(args.md)
    baseline = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
    compare = json.loads(Path(args.compare_json).read_text(encoding="utf-8"))
    eliminated = _build_eliminated_indices(baseline, compare.get("reduced_false_alarms") or [])
    content = update_markdown(md_path, eliminated)
    md_path.write_text(content, encoding="utf-8")
    total = sum(len(v) for v in eliminated.values())
    print(f"已更新: {md_path}")
    print(f"共 {total} 个误报序号已加删除线（speed-lower60 已消除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
