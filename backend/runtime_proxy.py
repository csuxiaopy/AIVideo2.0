"""Private HTTP/WebSocket boundary between Web and the single analysis owner."""
from __future__ import annotations

import asyncio
import os
import time

import httpx
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import connect

HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
               b"te", b"trailer", b"transfer-encoding", b"upgrade"}


def clean_headers(headers):
    connection_tokens = set()
    for key, value in headers:
        if key.lower() == b"connection":
            connection_tokens.update(token.strip().lower() for token in value.split(b","))
    return [(key, value) for key, value in headers
            if key.lower() not in HOP_HEADERS | connection_tokens | {b"x-analysis-token"}]


class RuntimeProxy:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ.get("ANALYSIS_URL", "http://app:8100")).rstrip("/")
        self.token = token if token is not None else os.environ.get("ANALYSIS_INTERNAL_TOKEN", "")
        if not self.token:
            raise RuntimeError("Split Web requires ANALYSIS_INTERNAL_TOKEN")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=5),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,
        )
        self._status_lock = asyncio.Lock()
        self._status_until = 0.0
        self._status = None

    async def close(self):
        await self.client.aclose()

    async def status(self):
        async with self._status_lock:
            if self._status is None or time.monotonic() >= self._status_until:
                try:
                    response = await self.client.get(self.url + "/health", timeout=3,
                                                     headers={"X-Analysis-Token": self.token})
                    response.raise_for_status()
                    snapshot = response.json()
                    self._status = {key: value for key, value in snapshot.items()
                                    if key not in {"status", "version"}}
                    self._status["analysis_service"] = {"status": snapshot.get("status", "ok")}
                except (httpx.HTTPError, ValueError):
                    self._status = {
                        "analysis_service": {"status": "unavailable"},
                        "queue": {"status": "unavailable"},
                        "postprocessing": {"degraded": True},
                        "detectors": {"general": {}, "fire_smoke": {}},
                    }
                self._status_until = time.monotonic() + 1
            return self._status

    async def http(self, request):
        headers = clean_headers(request.headers.raw)
        headers.append((b"x-analysis-token", self.token.encode()))
        path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
        target = self.url + path
        if request.url.query:
            target += "?" + request.url.query
        upstream = None
        try:
            req = self.client.build_request(request.method, target, headers=headers,
                                            content=request.stream())
            upstream = await self.client.send(req, stream=True)
            response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                         background=BackgroundTask(upstream.aclose))
            response.raw_headers = clean_headers(upstream.headers.raw)
            return response
        except httpx.HTTPError:
            if upstream is not None:
                await upstream.aclose()
            return JSONResponse({"detail": "分析服务暂不可用，请稍后重试"}, status_code=503)

    async def websocket(self, websocket):
        headers = {key: websocket.headers[key] for key in ("cookie", "origin")
                   if key in websocket.headers}
        headers["X-Analysis-Token"] = self.token
        target = self.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        target += websocket.url.path
        tasks = []
        try:
            async with connect(target, extra_headers=headers, open_timeout=5,
                               max_size=4 * 1024 * 1024) as upstream:
                await websocket.accept()
                async def to_client():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                async def to_analysis():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        await upstream.send(message.get("bytes") if message.get("bytes") is not None
                                            else message.get("text", ""))
                tasks = [asyncio.create_task(to_client()), asyncio.create_task(to_analysis())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Before accept, this preserves a failed authenticated handshake.
            try:
                await websocket.close(code=1013)
            except (RuntimeError, OSError):
                pass
