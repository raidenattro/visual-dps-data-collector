#!/usr/bin/env python3
"""对 rule-baseline-prod-test manifest 中的 28 条记录提取全骨骼速度特征。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_config_path
from api.record_service import locate_record_by_id
from api.skeleton_features_service import extract_skeleton_features_for_record

MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"


def main() -> int:
    resolve_config_path(None)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [r["record_id"] for r in manifest.get("records") or []]
    ok = skip = fail = 0
    for rid in ids:
        loc = locate_record_by_id(rid)
        if not loc:
            print(f"{rid}: FAIL 记录不存在")
            fail += 1
            continue
        try:
            res = extract_skeleton_features_for_record(loc, skip_if_exists=True)
            st = res.get("status")
            if st == "skipped":
                skip += 1
                print(f"{rid}: skip")
            else:
                ok += 1
                print(
                    f"{rid}: ok vel={res.get('velocity_count')} "
                    f"motion_seg={res.get('motion_segment_count')}"
                )
        except Exception as exc:
            fail += 1
            print(f"{rid}: FAIL {exc}")
    print(f"\nDONE ok={ok} skip={skip} fail={fail} total={len(ids)}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
