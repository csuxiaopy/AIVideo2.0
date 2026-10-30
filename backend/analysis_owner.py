"""Singleton ownership and a private transport boundary for split deployments."""
from __future__ import annotations

import os
import secrets
from sqlalchemy import text
from starlette.responses import JSONResponse

from backend.database import engine


class AnalysisOwner:
    def __init__(self):
        self.connection = None

    def acquire(self):
        connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("Split analysis ownership requires PostgreSQL")
            if not connection.scalar(text("SELECT pg_try_advisory_lock(17216, 166229)")):
                raise RuntimeError("Another analysis service already owns video capture")
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    def release(self):
        if self.connection is not None:
            try:
                self.connection.execute(text("SELECT pg_advisory_unlock(17216, 166229)"))
            finally:
                self.connection.close()
                self.connection = None


class AnalysisBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = os.environ.get("ANALYSIS_INTERNAL_TOKEN", "")
        if token and scope["type"] in {"http", "websocket"} and scope.get("path") != "/health":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"x-analysis-token", b"")
            if not secrets.compare_digest(supplied, token.encode()):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                else:
                    await JSONResponse({"detail": "Private analysis service"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
