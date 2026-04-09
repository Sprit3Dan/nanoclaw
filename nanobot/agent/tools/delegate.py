"""Delegate task tool: discovery-only routing + A2A dispatch."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.bus.delegation import DelegationTaskMap
from nanobot.bus.events import OutboundMessage


@dataclass
class _ResolvedRoute:
    agent_id: str | None
    base_url: str = ""   # empty for broker transports
    host: str = ""
    port: int = 0


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
        workspace: Path | None = None,
    ):
        self._send_callback = send_callback
        self._delegation_queue = delegation_queue

        self._agent_id = agent_id or os.environ.get("NANOBOT_A2A_AGENT_ID", "agent")
        self._listen_host = listen_host or os.environ.get("NANOBOT_A2A_LISTEN_HOST", "0.0.0.0")
        self._listen_port = int(os.environ.get("NANOBOT_A2A_LISTEN_PORT", str(listen_port)))
        self._workspace = workspace

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
        delegation_id = uuid.uuid4().hex

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
        logger.debug(
            "delegate_task.discovery.ok delegation_id={} chat_id={} route_host={} route_port={} route_base={}",
            delegation_id,
            chat_id,
            route.host,
            route.port,
            route.base_url,
        )

        extra: dict[str, Any] = {
            "message_type": message_type,
            "delegation_id": delegation_id,
        }
        if route.base_url:
            extra["a2a_peer_base"] = route.base_url

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
                delegation_id=delegation_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                delegated_channel="a2a",
                metadata={
                    "initial_request": {
                        "task": task,
                        "target_agent": target_agent,
                        "origin_channel": origin_channel,
                        "origin_chat_id": origin_chat_id,
                        "delegation_id": delegation_id,
                    },
                },
            )
            delegation_queue.bind_remote_task_id(
                delegation_id=delegation_id,
                delegated_task_id=delegation_id,
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
            "delegate_task.dispatch.begin delegation_id={} chat_id={} mode={} has_local_task={} has_upstream={}",
            delegation_id,
            chat_id,
            mode_l,
            local_delegation_bound,
            bool(origin_channel and origin_chat_id),
        )
        await self._send_callback(msg)
        logger.debug("delegate_task.dispatch.sent delegation_id={} chat_id={}", delegation_id, chat_id)

        return (
            f"Delegated task {delegation_id} to {chat_id} via {route.base_url} "
            f"(mode={mode_l}, message_type={message_type}, delegation_id={delegation_id})."
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

        agent_id_raw = route.get("agent_id") or payload.get("agent_id") or target_agent
        agent_id: str | None = None
        if isinstance(agent_id_raw, str) and agent_id_raw.strip():
            agent_id = agent_id_raw.strip()

        if not agent_id:
            return None

        # base_url is optional — absent for broker transports (NATS)
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

        base_url = ""
        host = ""
        port = 0

        if isinstance(base_url_raw, str) and base_url_raw.strip():
            candidate = base_url_raw.strip().rstrip("/")
            try:
                u = httpx.URL(candidate)
                if u.scheme in {"http", "https"} and u.host:
                    base_url = candidate
                    host = u.host
                    port = int(u.port) if isinstance(u.port, int) else (443 if u.scheme == "https" else 80)
            except Exception:
                pass

        if not base_url and isinstance(host_raw, str) and host_raw.strip():
            _port: int | None = None
            if isinstance(port_raw, int):
                _port = port_raw
            elif isinstance(port_raw, (float, str)):
                try:
                    _port = int(port_raw)
                except Exception:
                    pass
            if _port and _port > 0:
                host = host_raw.strip()
                port = _port
                base_url = f"http://{host}:{port}"

        if not host and base_url:
            agent_id = agent_id or self._agent_from_host(base_url)

        logger.debug(
            "delegate_task.discovery.route_resolved agent_id={} base_url={}",
            agent_id,
            base_url or "(broker)",
        )
        return _ResolvedRoute(agent_id=agent_id, base_url=base_url, host=host, port=port)

    async def on_agent_start(self) -> None:
        """Eagerly register with the discovery service at agent startup."""
        discovery_base_url = self._normalize_discovery_base_url(
            str(os.environ.get("NANOBOT_DISCOVERY_BASE_URL", "")).strip()
        )
        if not discovery_base_url:
            return
        register_url = self._register_url_from_base(discovery_base_url)
        try:
            await self._register_once(discovery_register_url=register_url)
        except Exception as exc:
            logger.warning("delegate_task.startup.register_failed error={}", exc)

    def _build_capabilities(self) -> dict[str, Any]:
        """Derive A2A capabilities from workspace SKILL.md files.

        Each skill can opt in to A2A routing metadata via its ``nanobot`` frontmatter
        block::

            metadata: {"nanobot": {"a2a": {"tags": ["aircraft", "aviation"],
                                           "examples": ["Track flight BA123"]}}}

        Workspace skills are always included (they are domain-specific by definition).
        Built-in skills are only included when they carry an ``a2a`` block.
        """
        from nanobot.agent.skills import SkillsLoader

        caps: dict[str, Any] = {
            "a2a_modes": ["push", "async", "sse"],
            "default_mode": "push",
            "semantic_routing": "discovery-knn",
        }

        workspace = self._workspace
        if workspace is None:
            env_ws = os.environ.get("NANOBOT_WORKSPACE", "").strip()
            if env_ws:
                workspace = Path(env_ws).expanduser()

        if workspace is None:
            return caps

        loader = SkillsLoader(workspace)
        skills: list[dict[str, Any]] = []

        for s in loader.list_skills(filter_unavailable=False):
            meta = loader.get_skill_metadata(s["name"]) or {}
            nanobot_meta = loader._parse_nanobot_metadata(meta.get("metadata", ""))
            a2a = nanobot_meta.get("a2a", {})

            is_workspace_skill = s["source"] == "workspace"
            if not is_workspace_skill and not a2a:
                continue  # built-in generic skill — skip unless explicitly opted in

            skills.append({
                "id": s["name"],
                "name": s["name"],
                "description": meta.get("description", s["name"]),
                "tags": a2a.get("tags", []),
                "examples": a2a.get("examples", []),
            })

        if skills:
            caps["skills"] = skills
            logger.debug(
                "delegate_task.capabilities.built skills={} workspace={}",
                len(skills),
                workspace,
            )

        return caps

    async def _register_once(self, discovery_register_url: str) -> None:
        """Register with the discovery service."""
        url = discovery_register_url.strip()
        if not url:
            raise ValueError("discovery_register_url is empty")

        async with self._register_lock:
            if url in self._registered_at:
                logger.debug("delegate_task.register.cached register_url={}", url)
                return

            client = await self._ensure_http_client()
            capabilities = self._build_capabilities()

            payload = {
                "agent_id": self._agent_id,
                "transport": "a2a-http",
                "protocol": "nanobot-a2a/v1",
                "address": {
                    "host": self._listen_host,
                    "port": self._listen_port,
                    "base_url": self._advertised_base_url(),
                },
                "capabilities": capabilities,
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

