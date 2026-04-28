"""REST channel — synchronous HTTP interface for programmatic message sending (evals, testing)."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

class RestChannelConfig(Base):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8765
    allow_from: list[str] = []
    timeout: float = 3600.0


_STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    408: "Request Timeout",
    500: "Internal Server Error",
}


@dataclass
class _PendingResponse:
    future: asyncio.Future[str]
    files: list[str] = field(default_factory=list)  # download URLs registered during this turn


class RestChannel(BaseChannel):
    """
    HTTP channel for synchronous request/response messaging.

    POST /chat          {"message": "...", "session_id": "..."}
                        → {"response": "...", "session_id": "...", "files": ["http://.../files/<token>"]}
    GET  /files/<token> → raw file bytes (served once; token expires after download)
    GET  /health        → {"ok": true}

    Auth: X-API-Key header matched against allow_from list. Use ["*"] for open access.
    """

    name = "rest"
    display_name = "REST"

    def __init__(self, config: Any, bus: MessageBus) -> None:
        if isinstance(config, dict):
            config = RestChannelConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: RestChannelConfig = config
        self._server: asyncio.base_events.Server | None = None
        self._pending: dict[str, _PendingResponse] = {}
        self._file_tokens: dict[str, Path] = {}  # token → absolute path

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
        for pr in self._pending.values():
            if not pr.future.done():
                pr.future.cancel()
        self._pending.clear()
        self._file_tokens.clear()

    async def send(self, msg: OutboundMessage) -> None:
        pr = self._pending.get(msg.chat_id)
        if not pr or pr.future.done():
            return

        host = str(getattr(self.config, "host", "0.0.0.0"))
        port = int(getattr(self.config, "port", 8765))
        base = f"http://{host}:{port}"

        for media_path in msg.media or []:
            path = Path(media_path)
            if path.is_file():
                token = uuid.uuid4().hex
                self._file_tokens[token] = path.resolve()
                pr.files.append(f"{base}/files/{token}")
                logger.debug("REST: registered file token {} → {}", token, path)

        pr.future.set_result(msg.content)

    # ── connection handler ────────────────────────────────────────────────────

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

            if method == "GET" and path.startswith("/files/"):
                token = path[len("/files/"):]
                await self._route_file(writer, token)
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
        pr = _PendingResponse(future=future)
        self._pending[session_id] = pr

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
            await self._write_json(writer, 200, {
                "response": response,
                "session_id": session_id,
                "files": pr.files,
            })
        except asyncio.TimeoutError:
            await self._write_json(writer, 408, {"error": "timeout"})
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(session_id, None)

    async def _route_file(self, writer: asyncio.StreamWriter, token: str) -> None:
        path = self._file_tokens.pop(token, None)
        if path is None or not path.is_file():
            await self._write_json(writer, 404, {"error": "file not found or already downloaded"})
            return

        try:
            data = path.read_bytes()
        except Exception as e:
            await self._write_json(writer, 500, {"error": f"read error: {e}"})
            return

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        writer.write(
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {mime}\r\n"
            f"Content-Length: {len(data)}\r\n"
            f"Content-Disposition: attachment; filename=\"{path.name}\"\r\n"
            f"Connection: close\r\n"
            f"\r\n".encode("utf-8")
        )
        writer.write(data)
        await writer.drain()
        logger.debug("REST: served file {} ({})", path.name, token)

    # ── HTTP primitives ───────────────────────────────────────────────────────

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
