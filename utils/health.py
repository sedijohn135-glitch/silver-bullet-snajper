"""Tiny stdlib HTTP health endpoint for Railway.

Railway healthchecks (and the platform's "is this thing alive?" probes) want an
HTTP port.  A full web framework would be absurd for two JSON routes, so this is
a bare asyncio server: no extra dependency, no request parsing beyond the request
line.  It binds only when ``PORT`` is present, so worker-style deploys skip it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

from utils.logging import get_logger

log = get_logger("health")


class HealthServer:
    def __init__(self, port: int, snapshot: Callable[[], dict]) -> None:
        self._port = port
        self._snapshot = snapshot
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)
        log.info("Health endpoint listening on :%s (/health, /status)", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover - shutdown best effort
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            path = "/"
            parts = request_line.decode("latin-1").split()
            if len(parts) >= 2:
                path = parts[1].split("?", 1)[0]
            # Drain headers so the client doesn't see a reset.
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            if path in ("/status", "/"):
                body = json.dumps(self._snapshot(), default=str, indent=2).encode()
                status, ctype = "200 OK", "application/json"
            elif path == "/health":
                snapshot = self._snapshot()
                healthy = bool(snapshot.get("healthy", True))
                body = json.dumps({"status": "ok" if healthy else "degraded"}).encode()
                status = "200 OK" if healthy else "503 Service Unavailable"
                ctype = "application/json"
            else:
                body, status, ctype = b'{"error":"not found"}', "404 Not Found", "application/json"

            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - a probe must never crash the bot
            log.debug("Health request failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
