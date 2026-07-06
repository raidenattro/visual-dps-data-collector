"""cv2.pointPolygonTest 兜底（部分环境 cv2 为空命名空间）。"""

from __future__ import annotations

import sys


def ensure_cv2_point_polygon_test() -> None:
    existing = sys.modules.get("cv2")
    if existing is not None and hasattr(existing, "pointPolygonTest"):
        return

    def _ray_point_in_contour(x: float, y: float, contour) -> bool:
        try:
            import numpy as np

            arr = np.asarray(contour, dtype=float)
            if arr.ndim == 3:
                arr = arr.reshape(-1, 2)
            elif arr.ndim == 2 and arr.shape[1] != 2:
                arr = arr.reshape(-1, 2)
            poly = [(float(px), float(py)) for px, py in arr]
        except Exception:
            poly = []
            for pt in contour or []:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    poly.append((float(pt[0]), float(pt[1])))
        if len(poly) < 3:
            return False
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
                inside = not inside
        return inside

    class _Cv2Shim:
        @staticmethod
        def pointPolygonTest(contour, pt, measure_dist):  # noqa: N802
            x, y = float(pt[0]), float(pt[1])
            return 1.0 if _ray_point_in_contour(x, y, contour) else -1.0

    sys.modules["cv2"] = _Cv2Shim()
