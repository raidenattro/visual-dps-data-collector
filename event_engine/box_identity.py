"""货位唯一标识：统一为 Box_{box_id}（shelf:id 等旧格式在读取时归一化）。"""

from __future__ import annotations


def box_collision_token(box: dict) -> str:
    """碰撞/告警落盘 token：仅使用 box_id，格式 Box_{box_id}。"""
    if not isinstance(box, dict):
        return ""
    box_id = str(box.get("box_id", "") or box.get("id", "") or "").strip()
    if not box_id:
        return ""
    return f"Box_{box_id}"


def canonical_box_token(token: str) -> str:
    """任意碰撞 token 归一为 Box_{box_id}；无法解析则返回空串。"""
    box_id = box_id_from_token(token)
    return f"Box_{box_id}" if box_id else ""


def canonicalize_box_token_list(tokens: list[str] | None) -> list[str]:
    """去重、排序后的规范 token 列表。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tokens or []:
        canon = canonical_box_token(str(raw).strip())
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    out.sort()
    return out


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
