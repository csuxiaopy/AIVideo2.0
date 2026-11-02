"""Web-only ASGI app. Never constructs MonitoringRuntime or loads GPU models."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.routing import Match

from backend.api.context import context
from backend.api.auth import router as auth_router
from backend.api.logs import router as logs_router
from backend.api.cameras import router as cameras_router
from backend.api.monitoring import router as monitoring_router
from backend.api.settings import router as settings_router
from backend.audit import audit_http_request
from backend.database import engine
from backend.main import _mount_frontend
from backend.runtime_proxy import RuntimeProxy


REMOTE_SETTINGS = {"put_models", "test_models", "put_detectors"}
REMOTE_MONITORING = {"send_alerts_to_webhooks", "delete_alerts", "delete_selected_alerts",
                     "events_socket", "system_monitor"}


def runtime_owned(route):
    module = route.endpoint.__module__
    return (module == "backend.api.cameras"
            or module == "backend.api.settings" and route.name in REMOTE_SETTINGS
            or module == "backend.api.monitoring" and route.name in REMOTE_MONITORING)


@asynccontextmanager
async def lifespan(app):
    # Schema ownership stays with analysis. Web replicas only check connectivity.
    def check_database():
        with engine.connect() as connection:
            from sqlalchemy import text
            connection.execute(text("SELECT 1"))
    await run_in_threadpool(check_database)
    proxy = RuntimeProxy()
    app.state.runtime_proxy = proxy
    context.runtime = proxy
    try:
        yield
    finally:
        context.runtime = None
        await proxy.close()


def create_web_app():
    app = FastAPI(title="AI Video Web", lifespan=lifespan)
    app.add_middleware(CORSMiddleware,
        allow_origins=[item.strip() for item in context.settings.allowed_origins.split(",") if item.strip()],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    remote_routes = []
    for router in (auth_router, cameras_router, monitoring_router, logs_router, settings_router):
        for route in router.routes:
            if runtime_owned(route):
                remote_routes.append(route)
                if hasattr(route, "methods"):
                    async def forward(request):
                        response = await request.app.state.runtime_proxy.http(request)
                        if request.url.path == "/api/system-monitor" and response.status_code == 200:
                            import asyncio, json
                            from backend.gpu_monitor import gpu_snapshot
                            body = b"".join([chunk async for chunk in response.body_iterator])
                            if response.headers.get("content-encoding") == "gzip":
                                import gzip
                                body = gzip.decompress(body)
                            data = json.loads(body)
                            data["gpu"] = await asyncio.to_thread(gpu_snapshot)
                            from starlette.responses import JSONResponse
                            enriched = JSONResponse(data, background=response.background)
                            enriched.raw_headers += [(k,v) for k,v in response.raw_headers if k.lower() not in {b"content-length",b"content-type",b"content-encoding"}]
                            return enriched
                        return response
                    app.add_route(route.path, forward, methods=list(route.methods))
                else:
                    async def forward_socket(websocket):
                        await websocket.app.state.runtime_proxy.websocket(websocket)
                    app.add_websocket_route(route.path, forward_socket)
            else:
                local = APIRouter()
                local.routes.append(route)
                app.include_router(local)

    @app.middleware("http")
    async def audit(request, call_next):
        # Runtime-owned mutations are authenticated and audited once, by analysis.
        if any(route.matches(request.scope)[0] == Match.FULL for route in remote_routes):
            return await call_next(request)
        return await audit_http_request(request, call_next)

    @app.get("/health/web")
    def web_health():
        return {"status": "ok", "role": "web"}

    @app.get("/metrics")
    async def metrics(request: Request):
        return await request.app.state.runtime_proxy.http(request)

    # Evidence still uses the original auth-protected file route.
    from backend.main import create_app
    source = create_app()
    for route in source.routes:
        if getattr(route, "path", "") == "/evidence/{filename}":
            local = APIRouter()
            local.routes.append(route)
            app.include_router(local)
    _mount_frontend(app)
    return app


app = create_web_app()
