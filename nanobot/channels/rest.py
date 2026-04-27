"""REST channel — synchronous HTTP interface for programmatic message sending (evals, testing)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    408: "Request Timeout",
    500: "Internal Server Error",
}


class RestChannel(BaseChannel):
    """
    HTTP channel for synchronous request/response messaging.

    POST /chat  {"message": "...", "session_id": "..."}  → {"response": "...", "session_id": "..."}
    GET  /health                                          → {"ok": true}

    Auth: X-API-Key header matched against allow_from list. Use ["*"] for open access.
    """

    name = "rest"
    display_name = "REST"

    def __init__(self, config: Any, bus: MessageBus) -> None:
        super().__init__(config, bus)
        self._server: asyncio.base_events.Server | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}

    async def start(self) -> None:
        self._running = True
        host = str(getattr(self.config, "host", "0.0.0.0"))
        port = int(getattr(self.config, "port", 8765))
        self._server = await asyncio.start_server(self._handle_connection, host, port)
        logger.info("REST channel listening on {}:{}", host, port)
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def send(self, msg: OutboundMessage) -> None:
        future = self._pending.get(msg.chat_id)
        if future and not future.done():
            future.set_result(msg.content)

    # ── connection handler ───────────────────────────────────────────────���─────

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            req = await self._read_request(reader)
            if req is None:
                await self._write_json(writer, 400, {"error": "invalid request"})
                return

            method, path, _query, headers, body = req

            if method == "POST" and path == "/chat":
                await self._route_chat(writer, headers, body)
                return

            if method == "GET" and path == "/health":
                await self._write_json(writer, 200, {"ok": True})
                return

            await self._write_json(writer, 404, {"error": "not found"})
        except Exception as e:
            logger.debug("REST handler error: {}", e)
            try:
                await self._write_json(writer, 500, {"error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route_chat(
        self,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        api_key = headers.get("x-api-key", "")
        if not self.is_allowed(api_key):
            await self._write_json(writer, 401, {"error": "unauthorized"})
            return

        try:
            payload = json.loads(body) if body else {}
        except Exception:
            await self._write_json(writer, 400, {"error": "invalid JSON"})
            return

        message = str(payload.get("message") or "").strip()
        if not message:
            await self._write_json(writer, 400, {"error": "missing message"})
            return

        session_id = str(payload.get("session_id") or uuid.uuid4())

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[session_id] = future

        try:
            await self.bus.publish_inbound(InboundMessage(
                channel=self.name,
                sender_id="rest",
                chat_id=session_id,
                content=message,
                session_key_override=f"{self.name}:{session_id}",
            ))
            timeout = float(getattr(self.config, "timeout", 120.0))
            response = await asyncio.wait_for(future, timeout=timeout)
            await self._write_json(writer, 200, {"response": response, "session_id": session_id})
        except asyncio.TimeoutError:
            await self._write_json(writer, 408, {"error": "timeout"})
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(session_id, None)

    # ── HTTP primitives ────────────────────────────────────────────────────────

    async def _read_request(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[str, str, dict[str, list[str]], dict[str, str], bytes] | None:
        line = await reader.readline()
        if not line:
            return None
        try:
            method, raw_target, _version = line.decode("utf-8").strip().split(" ", 2)
        except Exception:
            return None

        headers: dict[str, str] = {}
        while True:
            h = await reader.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            try:
                k, v = h.decode("utf-8").strip().split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except Exception:
                continue

        body = b""
        try:
            content_len = int(headers.get("content-length", "0"))
            if content_len > 0:
                body = await reader.readexactly(content_len)
        except Exception:
            pass

        target = urlsplit(raw_target)
        return method.upper(), target.path or "/", parse_qs(target.query), headers, body

    async def _write_json(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        payload: dict[str, Any],
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status = _STATUS_TEXT.get(code, "OK")
        writer.write(
            f"HTTP/1.1 {code} {status}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Connection: close\r\n"
            f"\r\n".encode("utf-8")
        )
        writer.write(data)
        await writer.drain()
