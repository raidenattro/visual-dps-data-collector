"""货位唯一标识：多货架场景下 shelf_code + box_id（与 visual-dps 一致）。"""

from __future__ import annotations


def box_collision_token(box: dict) -> str:
    if not isinstance(box, dict):
        return ""
    shelf = str(box.get("shelf_code", "") or "").strip()
    box_id = str(box.get("box_id", "") or box.get("id", "") or "").strip()
    if not box_id:
        return ""
    if shelf:
        return f"{shelf}:{box_id}"
    return f"Box_{box_id}"


def parse_collision_token(token: str) -> tuple[str, str]:
    text = str(token or "").strip()
    if not text:
        return "", ""
    if text.startswith("Box_"):
        return "", text[4:].strip()
    if ":" in text:
        shelf, _, box_id = text.partition(":")
        return shelf.strip(), box_id.strip()
    return "", text
