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


def box_id_from_token(token: str) -> str:
    """从碰撞 token 提取货位 id（Box_3100 与 MAP_19:3100 → 3100）。"""
    _, box_id = parse_collision_token(token)
    return box_id


def collision_tokens_equivalent(left: str, right: str) -> bool:
    """同一货位不同写法视为等价（仅比较 box_id）。"""
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    a_id = box_id_from_token(a)
    b_id = box_id_from_token(b)
    return bool(a_id and a_id == b_id)


def token_matches_any(token: str, candidates: set[str] | frozenset[str] | list[str]) -> bool:
    return any(collision_tokens_equivalent(token, c) for c in candidates)
