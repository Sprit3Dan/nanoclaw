"""A2A channel implementation over HTTP-only transport (push, async, and SSE modes)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.a2a_protocol import (
    A2AEnvelopePolicy,
    MESSAGE_TYPE_DELEGATION_REQUEST,
    PROTOCOL,
    join_url,
    make_auth,
    normalize_base_url,
    should_sign,
    validate_inbound_envelope,
)
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

_STATUS_TEXT = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class A2AConfig(Base):
    """A2A HTTP channel configuration."""

    enabled: bool = False
    agent_id: str = "agent"

    # Listener
    listen_host: str = "0.0.0.0"
    listen_port: int = 19100

    # HTTP paths
    inbox_path: str = "/inbox"          # push mode ingress
    async_path: str = "/async"          # async submit endpoint
    tasks_prefix: str = "/tasks"        # async polling endpoint: /tasks/{task_id}
    events_prefix: str = "/events"      # SSE endpoint: /events/{task_id}

    # Dynamic routing/discovery
    advertised_base_url: str = ""  # Optional externally reachable base URL for this agent
    allow_metadata_route_hint: bool = True
    route_cache_ttl_seconds: int = 900

    # Delivery mode policy
    delivery_mode: Literal["push", "async", "sse", "auto"] = "push"

    # Timeouts
    connect_timeout_s: float = 10.0
    request_timeout_s: float = 20.0
    sse_timeout_s: float = 60.0
    sse_heartbeat_seconds: int = 10

    # Security
    require_auth: bool = False
    shared_secret: str = ""
    clock_skew_seconds: int = 300
    nonce_ttl_seconds: int = 600


@dataclass
class A2ATask:
    task_id: str
    status: Literal["queued", "running", "done", "failed"]
    created_at: float
    updated_at: float
    envelope: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class A2AChannel(BaseChannel):
    """Built-in A2A channel over HTTP transport."""

    name = "a2a"
    display_name = "A2A"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return A2AConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = A2AConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: A2AConfig = config

        self._server: asyncio.base_events.Server | None = None
        self._client: httpx.AsyncClient | None = None

        self._seen_nonces: dict[str, float] = {}
        self._nonce_gc_task: asyncio.Task | None = None

        self._tasks: dict[str, A2ATask] = {}
        self._task_workers: dict[str, asyncio.Task] = {}
        self._task_watchers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

        # Dynamic peer routing cache: agent_id -> (base_url, expires_at)
        self._peer_routes: dict[str, tuple[str, float]] = {}

    def is_allowed(self, sender_id: str) -> bool:
        """A2A channel trusts authenticated/validated envelopes, not allowlists."""
        return True

    async def start(self) -> None:
        """Start HTTP listener and block until stopped."""
        self._running = True

        if self.config.require_auth and not self.config.shared_secret:
            logger.error("A2A auth is required but shared_secret is empty")
            self._running = False
            return

        await self._ensure_client()

        self._nonce_gc_task = asyncio.create_task(self._nonce_gc_loop())

        self._server = await asyncio.start_server(
            self._handle_client,
            self.config.listen_host,
            self.config.listen_port,
        )

        logger.info(
            "Starting A2A channel as '{}' on http://{}:{}",
            self.config.agent_id,
            self.config.listen_host,
            self.config.listen_port,
        )

        while self._running:
            await asyncio.sleep(0.5)

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def stop(self) -> None:
        """Stop A2A channel."""
        self._running = False

        for worker in list(self._task_workers.values()):
            worker.cancel()
        self._task_workers.clear()

        if self._nonce_gc_task:
            self._nonce_gc_task.cancel()
            try:
                await self._nonce_gc_task
            except asyncio.CancelledError:
                pass
            self._nonce_gc_task = None

        if self._client:
            await self._client.aclose()
            self._client = None

    async def send(self, msg: OutboundMessage) -> None:
        """
        Send A2A message to peer agent.

        Supported modes:
        - push: fire-and-ack over POST /inbox
        - async: submit over POST /async and return once task is accepted
        - sse: submit async + consume progress stream from GET /events/{task_id}
        """
        target_agent = msg.chat_id
        metadata = msg.metadata or {}
        peer_base = await self._resolve_peer_base(target_agent, metadata)
        if not peer_base:
            logger.warning("A2A peer '{}' could not be resolved dynamically", target_agent)
            return

        mode = self._resolve_send_mode(metadata)
        envelope = self._build_outbound_envelope(target_agent, msg, mode=mode)

        try:
            if mode == "push":
                await self._send_push(peer_base, envelope)
                return
            if mode == "async":
                await self._send_async(peer_base, envelope)
                return
            if mode == "sse":
                await self._send_sse(peer_base, envelope)
                return
            logger.warning("Unknown A2A mode '{}', falling back to push", mode)
            await self._send_push(peer_base, envelope)
        except Exception as e:
            logger.error("A2A send failed to '{}': {}", target_agent, e)

    async def _send_push(self, peer_base: str, envelope: dict[str, Any]) -> None:
        client = await self._ensure_client()
        url = self._join_url(peer_base, self.config.inbox_path)
        resp = await client.post(url, json=envelope)
        if resp.status_code >= 400:
            logger.warning("A2A push failed [{}]: {}", resp.status_code, resp.text[:300])

    async def _send_async(self, peer_base: str, envelope: dict[str, Any]) -> None:
        client = await self._ensure_client()
        url = self._join_url(peer_base, self.config.async_path)
        resp = await client.post(url, json={"envelope": envelope, "stream": False})
        if resp.status_code >= 400:
            logger.warning("A2A async submit failed [{}]: {}", resp.status_code, resp.text[:300])
            return

        try:
            payload = resp.json()
        except Exception:
            logger.warning("A2A async submit returned non-JSON response")
            return

        remote_task_id = str(payload.get("task_id", "")).strip() or None
        status_url, events_url = self._extract_submit_urls(peer_base, payload)

        logger.debug(
            "A2A async submit accepted: to_agent='{}' local_task_id='{}' remote_task_id='{}' status_url='{}' events_url='{}'",
            envelope.get("to_agent"),
            envelope.get("task_id"),
            remote_task_id,
            status_url,
            events_url,
        )

        self._record_delegation_submit_tracking(
            envelope,
            remote_task_id=remote_task_id,
            status_url=status_url,
            events_url=events_url,
        )

    async def _send_sse(self, peer_base: str, envelope: dict[str, Any]) -> None:
        client = await self._ensure_client()
        submit_url = self._join_url(peer_base, self.config.async_path)
        resp = await client.post(submit_url, json={"envelope": envelope, "stream": True})
        if resp.status_code >= 400:
            logger.warning("A2A sse submit failed [{}]: {}", resp.status_code, resp.text[:300])
            return

        try:
            payload = resp.json()
        except Exception:
            logger.warning("A2A sse submit returned non-JSON response")
            return

        task_id = str(payload.get("task_id", "")).strip()
        if not task_id:
            logger.warning("A2A sse submit did not return task_id")
            return

        status_url, events_url = self._extract_submit_urls(peer_base, payload)
        if not events_url:
            events_url = self._join_url(peer_base, f"{self.config.events_prefix}/{task_id}")

        logger.debug(
            "A2A sse submit accepted: to_agent='{}' local_task_id='{}' remote_task_id='{}' status_url='{}' events_url='{}'",
            envelope.get("to_agent"),
            envelope.get("task_id"),
            task_id,
            status_url,
            events_url,
        )

        self._record_delegation_submit_tracking(
            envelope,
            remote_task_id=task_id,
            status_url=status_url,
            events_url=events_url,
        )

        # Consume events until completion/failure or timeout.
        try:
            async with client.stream("GET", events_url) as stream:
                start = time.time()
                async for line in stream.aiter_lines():
                    if time.time() - start > self.config.sse_timeout_s:
                        logger.warning("A2A SSE timed out for task {}", task_id)
                        break
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype in {"done", "failed"}:
                        break
        except Exception as e:
            logger.warning("A2A SSE stream failed for task {}: {}", task_id, e)

    def _extract_submit_urls(self, peer_base: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        def _abs(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            raw = value.strip()
            if not raw:
                return None
            try:
                parsed = urlsplit(raw)
            except Exception:
                return None
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return raw.rstrip("/")
            path = raw if raw.startswith("/") else f"/{raw}"
            return self._join_url(peer_base, path)

        status_url = _abs(payload.get("status_url"))
        events_url = _abs(payload.get("events_url"))
        return status_url, events_url

    def _record_delegation_submit_tracking(
        self,
        envelope: dict[str, Any],
        *,
        remote_task_id: str | None,
        status_url: str | None,
        events_url: str | None,
    ) -> None:
        raw_metadata = envelope.get("metadata")
        if not isinstance(raw_metadata, dict):
            return

        metadata = raw_metadata
        raw_delegation = metadata.get("_delegation")
        delegation_meta = raw_delegation if isinstance(raw_delegation, dict) else None

        if remote_task_id:
            metadata["a2a_remote_task_id"] = remote_task_id
            if delegation_meta is not None:
                delegation_meta["remote_task_id"] = remote_task_id

        if status_url:
            metadata["a2a_remote_status_url"] = status_url
            if delegation_meta is not None:
                delegation_meta["remote_status_url"] = status_url

        if events_url:
            metadata["a2a_remote_events_url"] = events_url
            if delegation_meta is not None:
                delegation_meta["remote_events_url"] = events_url

        correlation_id_raw = metadata.get("correlation_id")
        if not correlation_id_raw and delegation_meta is not None:
            correlation_id_raw = delegation_meta.get("correlation_id")

        correlation_id = (
            str(correlation_id_raw).strip()
            if isinstance(correlation_id_raw, str)
            else ""
        )
        if not correlation_id:
            logger.debug(
                "A2A delegation submit tracking skipped: missing correlation_id for remote_task_id='{}'",
                remote_task_id,
            )
            return

        delegated_agent_id_raw = envelope.get("to_agent")
        delegated_agent_id = (
            str(delegated_agent_id_raw).strip()
            if isinstance(delegated_agent_id_raw, str)
            else None
        ) or None

        if remote_task_id:
            self.bus.delegation.bind_remote_task_id(
                correlation_id=correlation_id,
                delegated_task_id=remote_task_id,
                delegated_agent_id=delegated_agent_id,
            )

        local_task = self.bus.delegation.resolve({"correlation_id": correlation_id})
        if local_task is None:
            logger.debug(
                "A2A delegation submit tracking skipped: local delegation task not found correlation_id='{}' remote_task_id='{}'",
                correlation_id,
                remote_task_id,
            )
            return

        if remote_task_id:
            local_task.metadata["remote_task_id"] = remote_task_id
        if status_url:
            local_task.metadata["remote_status_url"] = status_url
        if events_url:
            local_task.metadata["remote_events_url"] = events_url
        local_task.updated_at = time.time()

        logger.debug(
            "A2A delegation submit tracking updated: correlation_id='{}' local_task_id='{}' remote_task_id='{}' status_url='{}' events_url='{}'",
            correlation_id,
            local_task.id,
            remote_task_id,
            status_url,
            events_url,
        )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Minimal HTTP request handler (single request per connection)."""
        try:
            req = await self._read_request(reader)
            if req is None:
                await self._write_json(writer, 400, {"ok": False, "error": "invalid request"})
                return

            method, path, query, _headers, body = req
            if method == "POST" and path == self.config.inbox_path:
                await self._route_inbox(writer, body)
                return

            if method == "POST" and path == self.config.async_path:
                await self._route_async_submit(writer, body)
                return

            if method == "GET" and path.startswith(f"{self.config.tasks_prefix}/"):
                task_id = path.removeprefix(f"{self.config.tasks_prefix}/").strip("/")
                await self._route_task_status(writer, task_id)
                return

            if method == "GET" and path.startswith(f"{self.config.events_prefix}/"):
                task_id = path.removeprefix(f"{self.config.events_prefix}/").strip("/")
                await self._route_sse(writer, task_id, query)
                return

            await self._write_json(writer, 404, {"ok": False, "error": "not found"})
        except Exception as e:
            logger.debug("A2A HTTP handler error: {}", e)
            try:
                await self._write_json(writer, 500, {"ok": False, "error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route_inbox(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        envelope = self._parse_json_bytes(body)
        if not isinstance(envelope, dict):
            await self._write_json(writer, 400, {"ok": False, "error": "invalid JSON"})
            return

        ok, result = await self._ingest_envelope(envelope)
        if ok:
            await self._write_json(writer, 200, {"ok": True, "result": result})
        else:
            await self._write_json(writer, 422, {"ok": False, "error": result.get("error", "ingest failed")})

    async def _route_async_submit(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        payload = self._parse_json_bytes(body)
        if not isinstance(payload, dict):
            await self._write_json(writer, 400, {"ok": False, "error": "invalid JSON"})
            return

        envelope = payload.get("envelope")
        if not isinstance(envelope, dict):
            await self._write_json(writer, 400, {"ok": False, "error": "missing envelope"})
            return

        envelope_task_id = envelope.get("task_id")
        if isinstance(envelope_task_id, str) and envelope_task_id.strip():
            task_id = envelope_task_id.strip()
        else:
            task_id = uuid.uuid4().hex[:12]

        stream_url = f"{self.config.events_prefix}/{task_id}"

        if task_id in self._tasks:
            logger.debug(
                "A2A async submit deduplicated: task_id='{}' from_agent='{}' to_agent='{}'",
                task_id,
                envelope.get("from_agent"),
                envelope.get("to_agent"),
            )
            await self._write_json(
                writer,
                202,
                {
                    "ok": True,
                    "task_id": task_id,
                    "status_url": f"{self.config.tasks_prefix}/{task_id}",
                    "events_url": stream_url,
                },
            )
            return

        now = time.time()
        task = A2ATask(
            task_id=task_id,
            status="queued",
            created_at=now,
            updated_at=now,
            envelope=envelope,
        )
        self._tasks[task_id] = task
        logger.debug(
            "A2A async task created: task_id='{}' from_agent='{}' to_agent='{}' mode='{}'",
            task_id,
            envelope.get("from_agent"),
            envelope.get("to_agent"),
            envelope.get("mode"),
        )
        self._task_watchers.setdefault(task_id, set())

        worker = asyncio.create_task(self._run_async_task(task_id))
        self._task_workers[task_id] = worker
        worker.add_done_callback(lambda _fut, tid=task_id: self._task_workers.pop(tid, None))

        await self._write_json(
            writer,
            202,
            {
                "ok": True,
                "task_id": task_id,
                "status_url": f"{self.config.tasks_prefix}/{task_id}",
                "events_url": stream_url,
            },
        )

    async def _route_task_status(self, writer: asyncio.StreamWriter, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            await self._write_json(writer, 404, {"ok": False, "error": "task not found"})
            return
        await self._write_json(
            writer,
            200,
            {
                "ok": True,
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            },
        )

    async def _route_sse(
        self,
        writer: asyncio.StreamWriter,
        task_id: str,
        query: dict[str, list[str]],
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            await self._write_json(writer, 404, {"ok": False, "error": "task not found"})
            return

        # SSE headers
        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        await self._write_head(writer, 200, headers)

        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        watchers = self._task_watchers.setdefault(task_id, set())
        watchers.add(q)

        # Replay current state quickly
        await self._sse_send(writer, {"type": "state", "task_id": task_id, "status": task.status})

        heartbeat = max(1, int(self.config.sse_heartbeat_seconds))
        follow = query.get("follow", ["1"])[0] != "0"

        try:
            while self._running:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=heartbeat)
                    await self._sse_send(writer, evt)
                    if evt.get("type") in {"done", "failed"} and not follow:
                        break
                    if evt.get("type") in {"done", "failed"} and follow:
                        # Send final event and close by default after completion.
                        break
                except asyncio.TimeoutError:
                    await self._sse_comment(writer, "heartbeat")
                    if task.status in {"done", "failed"}:
                        break
        finally:
            watchers.discard(q)

    async def _run_async_task(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status = "running"
        task.updated_at = time.time()
        await self._publish_task_event(task_id, {"type": "running", "task_id": task_id})

        ok, result = await self._ingest_envelope(task.envelope)
        task.updated_at = time.time()

        if ok:
            task.status = "done"
            task.result = result
            await self._publish_task_event(
                task_id,
                {"type": "done", "task_id": task_id, "result": result},
            )
        else:
            task.status = "failed"
            task.error = result.get("error", "unknown error")
            await self._publish_task_event(
                task_id,
                {"type": "failed", "task_id": task_id, "error": task.error},
            )

    async def _ingest_envelope(self, envelope: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        ok, err = self._verify_inbound_envelope(envelope)
        if not ok:
            return False, {"error": err or "verification failed"}

        sender = str(envelope.get("from_agent", "unknown"))
        content = str(envelope.get("content", ""))

        if not content.strip():
            return False, {"error": "empty content"}

        raw_metadata = envelope.get("metadata")
        metadata: dict[str, Any] = {}
        if isinstance(raw_metadata, dict):
            metadata = {str(k): v for k, v in raw_metadata.items()}

        reply_to_base = envelope.get("reply_to_base")
        metadata["_a2a"] = {
            "message_id": envelope.get("message_id"),
            "message_type": envelope.get("message_type"),
            "correlation_id": envelope.get("correlation_id"),
            "task_id": envelope.get("task_id"),
            "from_agent": sender,
            "to_agent": envelope.get("to_agent"),
            "intent": envelope.get("intent"),
            "timestamp": envelope.get("timestamp"),
            "mode": envelope.get("mode"),
            "reply_to_base": reply_to_base,
        }

        if isinstance(reply_to_base, str) and reply_to_base.strip():
            self._remember_peer_route(sender, reply_to_base)

        chat_id = str(envelope.get("chat_id") or sender)
        media = envelope.get("media")
        media_list = media if isinstance(media, list) else []
        session_key = envelope.get("session_key")
        session_key_str = str(session_key) if isinstance(session_key, str) and session_key else None

        await self._handle_message(
            sender_id=sender,
            chat_id=chat_id,
            content=content,
            media=media_list,
            metadata=metadata,
            session_key=session_key_str,
        )

        return True, {"accepted": True, "message_id": envelope.get("message_id")}

    def _build_outbound_envelope(
        self,
        target_agent: str,
        msg: OutboundMessage,
        *,
        mode: Literal["push", "async", "sse"],
    ) -> dict[str, Any]:
        now = int(time.time())
        message_id = str(uuid.uuid4())
        nonce = uuid.uuid4().hex

        metadata = dict(msg.metadata or {})
        session_key = metadata.pop("session_key", None)
        task_id = metadata.get("task_id")
        intent = metadata.get("intent")
        message_type = metadata.get("message_type") or MESSAGE_TYPE_DELEGATION_REQUEST
        correlation_id = metadata.get("correlation_id")

        envelope: dict[str, Any] = {
            "protocol": PROTOCOL,
            "message_id": message_id,
            "message_type": message_type,
            "correlation_id": correlation_id,
            "task_id": task_id,
            "from_agent": self.config.agent_id,
            "to_agent": target_agent,
            "chat_id": metadata.pop("chat_id", self.config.agent_id),
            "session_key": session_key,
            "intent": intent,
            "mode": mode,
            "content": msg.content or "",
            "media": msg.media or [],
            "metadata": metadata,
            "timestamp": now,
            "nonce": nonce,
        }

        advertised = self._advertised_base_url()
        if advertised:
            envelope["reply_to_base"] = advertised

        if should_sign(self.config.shared_secret):
            envelope["auth"] = make_auth(envelope, self.config.shared_secret)

        return envelope

    def _verify_inbound_envelope(self, envelope: dict[str, Any]) -> tuple[bool, str | None]:
        policy = A2AEnvelopePolicy(
            agent_id=self.config.agent_id,
            shared_secret=self.config.shared_secret,
            require_auth=self.config.require_auth,
            clock_skew_seconds=self.config.clock_skew_seconds,
            protocol=PROTOCOL,
        )
        return validate_inbound_envelope(
            envelope,
            policy=policy,
            seen_nonces=self._seen_nonces,
        )



    def _resolve_send_mode(self, metadata: dict[str, Any]) -> Literal["push", "async", "sse"]:
        raw = metadata.get("a2a_mode")
        if isinstance(raw, str):
            m = raw.strip().lower()
            if m == "push":
                return "push"
            if m == "async":
                return "async"
            if m == "sse":
                return "sse"

        cfg = self.config.delivery_mode
        if cfg == "push":
            return "push"
        if cfg == "async":
            return "async"
        if cfg == "sse":
            return "sse"
        # default and "auto" path
        return "push"

    async def _resolve_peer_base(self, target_agent: str, metadata: dict[str, Any]) -> str | None:
        # 1) Explicit per-message hint
        if self.config.allow_metadata_route_hint:
            for key in ("a2a_peer_base", "peer_base", "reply_to_base"):
                hint = metadata.get(key)
                if isinstance(hint, str) and hint.strip():
                    normalized = self._normalize_base_url(hint)
                    if normalized:
                        self._remember_peer_route(target_agent, normalized)
                        return normalized

            # 1b) Structured hint coming from inbound `_a2a` metadata
            raw_a2a = metadata.get("_a2a")
            if isinstance(raw_a2a, dict):
                hint = raw_a2a.get("reply_to_base")
                if isinstance(hint, str) and hint.strip():
                    normalized = self._normalize_base_url(hint)
                    if normalized:
                        self._remember_peer_route(target_agent, normalized)
                        return normalized

        # 2) Cache
        cached = self._get_cached_peer_route(target_agent)
        if cached:
            return cached

        return None



    def _remember_peer_route(self, agent_id: str, base_url: str) -> None:
        normalized = self._normalize_base_url(base_url)
        if not normalized:
            return
        ttl = max(30, int(self.config.route_cache_ttl_seconds))
        self._peer_routes[str(agent_id)] = (normalized, time.time() + ttl)

    def _get_cached_peer_route(self, agent_id: str) -> str | None:
        entry = self._peer_routes.get(str(agent_id))
        if not entry:
            return None
        base, expires_at = entry
        if expires_at < time.time():
            self._peer_routes.pop(str(agent_id), None)
            return None
        return base

    def _advertised_base_url(self) -> str | None:
        explicit = (self.config.advertised_base_url or "").strip()
        if explicit:
            return self._normalize_base_url(explicit)

        host = str(self.config.listen_host).strip()
        if host in {"", "0.0.0.0", "::"}:
            return None
        return self._normalize_base_url(f"http://{host}:{self.config.listen_port}")

    @staticmethod
    def _normalize_base_url(value: str) -> str | None:
        return normalize_base_url(value)

    async def _publish_task_event(self, task_id: str, event: dict[str, Any]) -> None:
        for q in list(self._task_watchers.get(task_id, set())):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    async def _nonce_gc_loop(self) -> None:
        ttl = max(60, int(self.config.nonce_ttl_seconds))
        while True:
            await asyncio.sleep(30)
            cutoff = time.time() - ttl
            stale = [n for n, ts in self._seen_nonces.items() if ts < cutoff]
            for n in stale:
                self._seen_nonces.pop(n, None)

            now_ts = time.time()
            stale_routes = [aid for aid, (_base, exp) in self._peer_routes.items() if exp < now_ts]
            for aid in stale_routes:
                self._peer_routes.pop(aid, None)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self.config.connect_timeout_s,
                read=self.config.request_timeout_s,
                write=self.config.request_timeout_s,
                pool=self.config.request_timeout_s,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    async def _read_request(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[str, str, dict[str, list[str]], dict[str, str], bytes] | None:
        """
        Parse one HTTP/1.1 request (minimal implementation).

        Returns:
            (method, path, query, headers, body)
        """
        line = await reader.readline()
        if not line:
            return None

        try:
            request_line = line.decode("utf-8").strip()
            method, raw_target, _version = request_line.split(" ", 2)
        except Exception:
            return None

        headers: dict[str, str] = {}
        while True:
            h = await reader.readline()
            if not h:
                break
            if h in (b"\r\n", b"\n"):
                break
            try:
                hs = h.decode("utf-8").strip()
                k, v = hs.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except Exception:
                continue

        content_len = 0
        try:
            content_len = int(headers.get("content-length", "0"))
        except ValueError:
            content_len = 0

        body = b""
        if content_len > 0:
            body = await reader.readexactly(content_len)

        target = urlsplit(raw_target)
        path = target.path or "/"
        query = parse_qs(target.query)
        return method.upper(), path, query, headers, body

    async def _write_json(self, writer: asyncio.StreamWriter, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(data)),
            "Connection": "close",
        }
        await self._write_head(writer, code, headers)
        writer.write(data)
        await writer.drain()

    async def _write_head(self, writer: asyncio.StreamWriter, code: int, headers: dict[str, str]) -> None:
        text = _STATUS_TEXT.get(code, "OK")
        writer.write(f"HTTP/1.1 {code} {text}\r\n".encode("utf-8"))
        for k, v in headers.items():
            writer.write(f"{k}: {v}\r\n".encode("utf-8"))
        writer.write(b"\r\n")
        await writer.drain()

    async def _sse_send(self, writer: asyncio.StreamWriter, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        writer.write(f"data: {payload}\n\n".encode("utf-8"))
        await writer.drain()

    async def _sse_comment(self, writer: asyncio.StreamWriter, text: str) -> None:
        writer.write(f": {text}\n\n".encode("utf-8"))
        await writer.drain()

    @staticmethod
    def _parse_json_bytes(data: bytes) -> Any:
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return None



    @staticmethod
    def _join_url(base: str, path: str) -> str:
        return join_url(base, path)
