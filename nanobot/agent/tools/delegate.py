"""Delegate task tool: discovery-only routing + A2A dispatch."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.bus.delegation import DelegationTaskMap
from nanobot.bus.events import OutboundMessage


@dataclass
class _ResolvedRoute:
    agent_id: str | None
    host: str
    port: int
    base_url: str


class DelegateTaskTool(Tool):
    """
    Delegate a task to another agent over A2A using discovery service responses only.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        delegation_queue: DelegationTaskMap | None = None,
        *,
        agent_id: str | None = None,
        listen_host: str | None = None,
        listen_port: int = 19100,
    ):
        self._send_callback = send_callback
        self._delegation_queue = delegation_queue

        self._agent_id = agent_id or os.environ.get("NANOBOT_A2A_AGENT_ID", "agent")
        self._listen_host = listen_host or os.environ.get("NANOBOT_A2A_LISTEN_HOST", "0.0.0.0")
        self._listen_port = int(os.environ.get("NANOBOT_A2A_LISTEN_PORT", str(listen_port)))

        self._register_lock = asyncio.Lock()
        self._registered_at: dict[str, float] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._origin_channel: str = ""
        self._origin_chat_id: str = ""

    def set_context(self, origin_channel: str, origin_chat_id: str) -> None:
        """Set default origin routing context for delegation calls."""
        self._origin_channel = str(origin_channel or "").strip()
        self._origin_chat_id = str(origin_chat_id or "").strip()

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to another agent over A2A. "
            "Route resolution is performed only via discovery service responses."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task content to send to another agent.",
                    "minLength": 1,
                },
                "target_agent": {
                    "type": "string",
                    "description": "Optional explicit target agent id for discovery.",
                },
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._send_callback:
            return "Error: Delegation is not configured (missing send callback)."

        task_raw = kwargs.get("task", "")
        task = str(task_raw).strip() if task_raw is not None else ""
        if not task:
            return "Error: task must be non-empty."

        logger.debug(
            "delegate_task.start agent_id={} listen={}:{} task_len={}",
            self._agent_id,
            self._listen_host,
            self._listen_port,
            len(task),
        )

        discovery_base_url = self._normalize_discovery_base_url(
            str(os.environ.get("NANOBOT_DISCOVERY_BASE_URL", "")).strip()
        )
        if not discovery_base_url:
            return "Error: NANOBOT_DISCOVERY_BASE_URL is required."

        target_agent_raw = kwargs.get("target_agent")
        target_agent = str(target_agent_raw).strip() if isinstance(target_agent_raw, str) else None
        if target_agent == "":
            target_agent = None

        mode_raw = kwargs.get("mode", "push")
        mode_l = str(mode_raw or "push").strip().lower()
        if mode_l not in {"push", "async", "sse"}:
            return "Error: mode must be one of push|async|sse."

        intent_raw = kwargs.get("intent")
        intent = str(intent_raw).strip() if isinstance(intent_raw, str) else None
        route_intent = (intent or task).strip()

        origin_channel_raw = kwargs.get("origin_channel", self._origin_channel)
        origin_channel = (
            str(origin_channel_raw).strip()
            if isinstance(origin_channel_raw, str)
            else self._origin_channel
        )
        if not origin_channel:
            origin_channel = self._origin_channel

        origin_chat_id_raw = kwargs.get("origin_chat_id", self._origin_chat_id)
        origin_chat_id = (
            str(origin_chat_id_raw).strip()
            if isinstance(origin_chat_id_raw, str)
            else self._origin_chat_id
        )
        if not origin_chat_id:
            origin_chat_id = self._origin_chat_id

        # Keep contract explicit but auto-generated to minimize tool-call parameters.
        message_type = "delegation_request"
        correlation_id = uuid.uuid4().hex

        register_url = self._register_url_from_base(discovery_base_url)
        logger.debug(
            "delegate_task.register.begin register_url={} target_agent={} mode={} upstream={}:{}",
            register_url,
            target_agent or "",
            mode_l,
            origin_channel,
            origin_chat_id,
        )
        await self._register_once(discovery_register_url=register_url)
        logger.debug("delegate_task.register.ok register_url={}", register_url)

        route = await self._resolve_http_discover(
            discovery_base_url=discovery_base_url,
            task=task,
            intent=route_intent,
            target_agent=target_agent,
        )
        if route is None:
            logger.debug(
                "delegate_task.discovery.no_route target_agent={} intent={} discovery_base={}",
                target_agent or "",
                route_intent,
                discovery_base_url,
            )
            return "Error: discovery did not return a valid A2A route."

        chat_id = target_agent or route.agent_id or self._agent_from_host(route.host) or route.host
        task_id = uuid.uuid4().hex[:12]
        logger.debug(
            "delegate_task.discovery.ok task_id={} chat_id={} route_host={} route_port={} route_base={}",
            task_id,
            chat_id,
            route.host,
            route.port,
            route.base_url,
        )

        extra = {
            "message_type": message_type,
            "correlation_id": correlation_id,
            "a2a_peer_base": route.base_url,
        }

        if origin_channel:
            extra["upstream_channel"] = origin_channel
        if origin_chat_id:
            extra["upstream_chat_id"] = origin_chat_id

        local_delegation_bound = False
        delegation_queue = self._delegation_queue
        if delegation_queue and origin_channel and origin_chat_id:
            delegation_queue.create(
                reply_channel=origin_channel,
                reply_chat_id=origin_chat_id,
                correlation_id=correlation_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                delegated_channel="a2a",
                metadata={
                    "initial_request": {
                        "task": task,
                        "target_agent": target_agent,
                        "origin_channel": origin_channel,
                        "origin_chat_id": origin_chat_id,
                        "correlation_id": correlation_id,
                    },
                },
            )
            delegation_queue.bind_remote_task_id(
                correlation_id=correlation_id,
                delegated_task_id=task_id,
                delegated_agent_id=chat_id,
            )
            local_delegation_bound = True

        msg = OutboundMessage(
            channel="a2a",
            chat_id=chat_id,
            content=task,
            metadata=extra,
        )
        logger.debug(
            "delegate_task.dispatch.begin task_id={} chat_id={} mode={} has_local_task={} has_upstream={}",
            task_id,
            chat_id,
            mode_l,
            local_delegation_bound,
            bool(origin_channel and origin_chat_id),
        )
        await self._send_callback(msg)
        logger.debug("delegate_task.dispatch.sent task_id={} chat_id={}", task_id, chat_id)

        return (
            f"Delegated task {task_id} to {chat_id} via {route.base_url} "
            f"(mode={mode_l}, message_type={message_type}, correlation_id={correlation_id})."
        )

    @staticmethod
    def _normalize_discovery_base_url(url: str) -> str:
        u = (url or "").strip().rstrip("/")
        if not u:
            return ""
        if u.endswith("/register"):
            return u[: -len("/register")]
        if u.endswith("/discover"):
            return u[: -len("/discover")]
        return u

    @classmethod
    def _register_url_from_base(cls, discovery_base_url: str) -> str:
        base = cls._normalize_discovery_base_url(discovery_base_url)
        return f"{base}/register"

    @classmethod
    def _discover_url_from_base(cls, discovery_base_url: str) -> str:
        base = cls._normalize_discovery_base_url(discovery_base_url)
        return f"{base}/discover"

    async def _resolve_http_discover(
        self,
        *,
        discovery_base_url: str,
        task: str,
        intent: str,
        target_agent: str | None,
    ) -> _ResolvedRoute | None:
        discover_url = self._discover_url_from_base(discovery_base_url)
        client = await self._ensure_http_client()

        params: dict[str, Any] = {
            "task": task,
            "intent": intent,
        }
        if target_agent:
            params["target_agent"] = target_agent

        logger.debug(
            "delegate_task.discovery.request url={} target_agent={} intent_len={} task_len={}",
            discover_url,
            target_agent or "",
            len(intent),
            len(task),
        )
        try:
            resp = await client.get(discover_url, params=params)
        except Exception as exc:
            logger.debug(
                "delegate_task.discovery.error url={} error={}",
                discover_url,
                exc,
            )
            return None

        logger.debug(
            "delegate_task.discovery.response status={} url={}",
            resp.status_code,
            discover_url,
        )
        if resp.status_code >= 400:
            return None

        try:
            payload = resp.json()
        except Exception:
            logger.debug("delegate_task.discovery.invalid_json url={}", discover_url)
            return None

        if not isinstance(payload, dict):
            return None

        route_obj = payload.get("route")
        route = route_obj if isinstance(route_obj, dict) else payload

        base_url_raw = (
            route.get("base_url")
            or payload.get("base_url")
            or route.get("advertised_base_url")
            or route.get("advertisedBaseUrl")
            or payload.get("advertised_base_url")
            or payload.get("advertisedBaseUrl")
        )

        host_raw = route.get("host") or payload.get("host")
        port_raw = route.get("port") or payload.get("port")
        agent_id_raw = route.get("agent_id") or payload.get("agent_id") or target_agent

        base_url: str | None = None
        host: str | None = None
        port: int | None = None

        if isinstance(base_url_raw, str) and base_url_raw.strip():
            candidate = base_url_raw.strip().rstrip("/")
            try:
                u = httpx.URL(candidate)
                if u.scheme in {"http", "https"} and u.host:
                    base_url = candidate
                    host = u.host
                    parsed_port = u.port
                    port = int(parsed_port) if isinstance(parsed_port, int) else (443 if u.scheme == "https" else 80)
            except Exception:
                return None

        if base_url is None:
            if not isinstance(host_raw, str) or not host_raw.strip():
                return None

            parsed_port: int | None = None
            if isinstance(port_raw, int):
                parsed_port = port_raw
            elif isinstance(port_raw, float):
                parsed_port = int(port_raw)
            elif isinstance(port_raw, str):
                raw_port = port_raw.strip()
                if raw_port:
                    try:
                        parsed_port = int(raw_port)
                    except Exception:
                        parsed_port = None

            if parsed_port is None or parsed_port <= 0:
                return None

            host = host_raw.strip()
            port = parsed_port
            base_url = f"http://{host}:{port}"

        agent_id: str | None = None
        if isinstance(agent_id_raw, str) and agent_id_raw.strip():
            agent_id = agent_id_raw.strip()
        elif host:
            agent_id = self._agent_from_host(host)

        if not host or port is None or not base_url:
            return None

        logger.debug(
            "delegate_task.discovery.route_resolved agent_id={} host={} port={} base_url={}",
            agent_id or "",
            host,
            port,
            base_url,
        )
        return _ResolvedRoute(agent_id=agent_id, host=host, port=port, base_url=base_url)

    async def _register_once(self, discovery_register_url: str) -> None:
        url = discovery_register_url.strip()
        if not url:
            raise ValueError("discovery_register_url is empty")

        async with self._register_lock:
            if url in self._registered_at:
                logger.debug("delegate_task.register.cached register_url={}", url)
                return

            client = await self._ensure_http_client()
            payload = {
                "agent_id": self._agent_id,
                "transport": "a2a-http",
                "protocol": "nanobot-a2a/v1",
                "address": {
                    "host": self._listen_host,
                    "port": self._listen_port,
                    "base_url": self._advertised_base_url(),
                },
                "capabilities": {
                    "a2a_modes": ["push", "async", "sse"],
                    "default_mode": "push",
                    "semantic_routing": "discovery-http",
                },
            }

            logger.debug(
                "delegate_task.register.request url={} advertised_base_url={}",
                url,
                payload["address"]["base_url"],
            )
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.debug(
                    "delegate_task.register.failed url={} status={} body={}",
                    url,
                    resp.status_code,
                    resp.text[:300],
                )
                raise RuntimeError(f"register failed [{resp.status_code}]: {resp.text[:300]}")

            self._registered_at[url] = time.time()
            logger.debug("delegate_task.register.success url={}", url)

    def _advertised_base_url(self) -> str | None:
        # Explicit env override (recommended for Kubernetes service URL registration).
        env_value = (
            os.environ.get("NANOBOT_A2A_ADVERTISED_BASE_URL")
            or os.environ.get("NANOBOT_ADVERTISED_BASE_URL")
            or ""
        ).strip()
        if env_value:
            normalized = self._normalize_url(env_value)
            if normalized:
                return normalized

        host = (self._listen_host or "").strip()
        if not host or host in {"0.0.0.0", "::"}:
            return None
        return self._normalize_url(f"http://{host}:{self._listen_port}")

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=20.0))
        return self._http_client

    @staticmethod
    def _normalize_url(value: str) -> str | None:
        v = value.strip().rstrip("/")
        if not v:
            return None
        try:
            u = httpx.URL(v)
        except Exception:
            return None
        if u.scheme not in {"http", "https"} or not u.host:
            return None
        return v

    @staticmethod
    def _agent_from_host(host: str) -> str | None:
        first = host.split(".", 1)[0].strip()
        if not first:
            return None
        if first.endswith("-a2a"):
            return first[: -len("-a2a")] or None
        return first

