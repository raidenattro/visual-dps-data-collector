"""Web API 包：路由、采集服务、记录服务。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.app import FastAPI

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in __all__:
        from api.app import app, create_app

        return {"app": app, "create_app": create_app}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
