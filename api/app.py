"""FastAPI 应用工厂与启动入口。"""

from __future__ import annotations

import argparse
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api.routes import router as http_router
from config_loader import (
    build_settings,
    default_save_video,
    load_config_file,
    project_root,
    resolve_app_paths,
    resolve_config_path,
)
from pose_store import migrate_v1_json_dir


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    from api.sandbox_service import cleanup_expired_sandbox_sessions

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            result = cleanup_expired_sandbox_sessions()
            removed = int(result.get("removed_count") or 0)
            if removed:
                print(f"🧪 沙盒：已清理过期 session {removed} 个")
        except Exception as exc:
            print(f"⚠ 沙盒清理跳过: {exc}")
        yield

    application = FastAPI(title="visual-dps-datacollect", version="0.2.0", lifespan=lifespan)
    application.include_router(http_router)
    web_dir = project_root() / "web"
    if web_dir.is_dir():
        application.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return application


app = create_app()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="visual-dps 数据采集 Web 服务")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="监听端口（覆盖 config.json 的 server.port，默认 8765）",
    )
    p.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help="监听地址（覆盖 config.json 的 server.host，默认 127.0.0.1）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    args = build_arg_parser().parse_args(argv)
    cfg = load_config_file(resolve_config_path(None))
    server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
    host = str(args.host or server.get("host") or "127.0.0.1")
    port = int(args.port if args.port is not None else server.get("port") or 8765)
    paths = resolve_app_paths(cfg)
    for p in (
        paths.json_dir,
        paths.video_dir,
        paths.upload_dir,
        paths.playback_temp_dir,
        paths.annotation_dir,
    ):
        p.mkdir(parents=True, exist_ok=True)
    migrated = migrate_v1_json_dir(paths.json_dir)
    if migrated:
        print(f"📦 已迁移 {len(migrated)} 条 v1 JSON → Parquet 包")
    settings = build_settings(config_path=resolve_config_path(None), cli={})
    print(f"🌐 Web UI: http://{host}:{port}")
    print(f"📁 JSON 目录: {paths.json_dir}")
    print(f"🎬 视频目录: {paths.video_dir}（默认保存: {default_save_video()})")
    print(f"📦 ONNX 目录: {paths.models_onnx_dir}")
    print(f"   ├─ detection: {paths.models_detection_dir}")
    print(f"   └─ pose: {paths.models_pose_dir}")
    print(f"🏷 标注目录: {paths.annotation_dir}（每视频一份，新保存覆盖旧文件）")
    print(f"🧠 推理设备: {settings.device}（models.use_gpu / INFERENCE_USE_GPU）")
    if settings.device == "cuda":
        try:
            from rtmpose_infer import assert_cuda_ort_available, ort_available_providers

            assert_cuda_ort_available()
            print(f"✅ ORT GPU 就绪: {ort_available_providers()}")
        except RuntimeError as exc:
            print(f"❌ {exc}")
    print(f"🧪 沙盒目录: {(paths.upload_dir / 'sandbox').resolve()}（临时碰撞实验，不写正式库）")
    uvicorn.run("api.app:app", host=host, port=port, reload=False)
    return 0
