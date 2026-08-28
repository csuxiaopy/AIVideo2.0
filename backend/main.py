from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend.api.cameras import router as cameras_router
from backend.api.context import context
from backend.api.monitoring import router as monitoring_router
from backend.api.settings import router as settings_router
from backend.database import upgrade_schema
from backend.pipeline import MonitoringRuntime


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    upgrade_schema()
    context.runtime = MonitoringRuntime(context.settings, context.repository, context.cipher)
    await context.runtime.start()
    try:
        yield
    finally:
        await context.runtime.close()
        context.runtime = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="YOLO + 视觉大模型监控平台",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(cameras_router)
    app.include_router(monitoring_router)
    app.include_router(settings_router)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/evidence/{filename}")
    async def evidence(filename: str):
        safe_name = filename.replace("\\", "/").split("/")[-1]
        path = context.settings.evidence_dir / safe_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="证据图片不存在")
        return FileResponse(path)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    web_dist_dir = context.settings.web_dist_dir
    if web_dist_dir.exists():
        assets = web_dist_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", response_class=HTMLResponse)
        async def spa(path: str):
            candidate = web_dist_dir / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist_dir / "index.html")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return "<h1>YOLO + 视觉大模型监控平台</h1><p>前端尚未构建，请运行 frontend 的 npm run build。</p>"


app = create_app()
