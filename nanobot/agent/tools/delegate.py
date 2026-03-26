"""Delegate task tool: register-once + VectorDNS SRV resolution + A2A dispatch."""

from __future__ import annotations

import asyncio
import os
import random
import socket
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

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
    """Delegate a task to another agent over A2A using VectorDNS SRV lookup."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        delegation_queue: DelegationTaskMap | None = None,
        *,
        default_vectordns_domain: str | None = None,
        default_vectordns_resolver: str | None = None,
        default_vectordns_port: int = 53,
        default_vectordns_timeout_ms: int = 1500,
        agent_id: str | None = None,
        listen_host: str | None = None,
        listen_port: int = 19100,
    ):
        self._send_callback = send_callback
        self._delegation_queue = delegation_queue

        self._default_domain = (
            default_vectordns_domain
            or os.environ.get("NANOBOT_VECTORDNS_DOMAIN", "botfactory.svc.cluster.local")
        )
        self._default_resolver = (
            default_vectordns_resolver or os.environ.get("NANOBOT_VECTORDNS_RESOLVER", "127.0.0.1")
        )
        self._default_dns_port = int(
            os.environ.get("NANOBOT_VECTORDNS_PORT", str(default_vectordns_port))
        )
        self._default_timeout_ms = int(
            os.environ.get("NANOBOT_VECTORDNS_TIMEOUT_MS", str(default_vectordns_timeout_ms))
        )

        self._agent_id = agent_id or os.environ.get("NANOBOT_A2A_AGENT_ID", "agent")
        self._listen_host = listen_host or os.environ.get("NANOBOT_A2A_LISTEN_HOST", "0.0.0.0")
        self._listen_port = int(os.environ.get("NANOBOT_A2A_LISTEN_PORT", str(listen_port)))

        self._register_lock = asyncio.Lock()
        self._registered_at: dict[str, float] = {}
        self._http_client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return (
            "Delegate a task to another agent over A2A. "
            "Always uses VectorDNS SRV resolution. Default mode is push."
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
                    "description": "Optional explicit agent id. If omitted, semantic/default SRV name is used.",
                },
                "mode": {
                    "type": "string",
                    "description": "A2A send mode. Defaults to push.",
                    "enum": ["push", "async", "sse"],
                },
                "intent": {
                    "type": "string",
                    "description": "Optional routing intent. Defaults to task text.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional extra metadata merged into outbound message metadata.",
                },
                "vectordns_domain": {
                    "type": "string",
                    "description": "VectorDNS zone/domain.",
                },
                "vectordns_resolver": {
                    "type": "string",
                    "description": "DNS resolver address for VectorDNS queries.",
                },
                "vectordns_port": {
                    "type": "integer",
                    "description": "DNS resolver port.",
                    "minimum": 1,
                    "maximum": 65535,
                },
                "vectordns_timeout_ms": {
                    "type": "integer",
                    "description": "DNS timeout in milliseconds.",
                    "minimum": 50,
                    "maximum": 30000,
                },
                "vectordns_name": {
                    "type": "string",
                    "description": (
                        "Optional fully-qualified SRV owner name override "
                        "(example: _a2a._tcp.researcher.botfactory.svc.cluster.local)."
                    ),
                },
                "await_result": {
                    "type": "boolean",
                    "description": "Compatibility flag. Current implementation is fire-and-ack only.",
                },
                "origin_channel": {
                    "type": "string",
                    "description": "Optional source channel for upstream response routing (e.g., telegram, discord).",
                },
                "origin_chat_id": {
                    "type": "string",
                    "description": "Optional source chat id for upstream response routing.",
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

        discovery_base_url = str(
            os.environ.get("NANOBOT_DISCOVERY_BASE_URL", "")
        ).strip()
        discovery_register_url = (
            self._register_url_from_base(discovery_base_url)
            if discovery_base_url
            else ""
        )

        if not discovery_register_url:
            return "Error: NANOBOT_DISCOVERY_BASE_URL is required."

        target_agent_raw = kwargs.get("target_agent")
        target_agent = str(target_agent_raw).strip() if isinstance(target_agent_raw, str) else None

        mode_raw = kwargs.get("mode", "push")
        mode_l = str(mode_raw or "push").strip().lower()
        if mode_l not in {"push", "async", "sse"}:
            return "Error: mode must be one of push|async|sse."

        intent_raw = kwargs.get("intent")
        intent = str(intent_raw).strip() if isinstance(intent_raw, str) else None

        metadata_raw = kwargs.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}

        origin_channel_raw = kwargs.get("origin_channel")
        origin_channel = (
            str(origin_channel_raw).strip()
            if isinstance(origin_channel_raw, str)
            else ""
        )

        origin_chat_id_raw = kwargs.get("origin_chat_id")
        origin_chat_id = (
            str(origin_chat_id_raw).strip()
            if isinstance(origin_chat_id_raw, str)
            else ""
        )

        vectordns_domain_raw = kwargs.get("vectordns_domain")
        vectordns_domain = (
            str(vectordns_domain_raw).strip()
            if isinstance(vectordns_domain_raw, str)
            else None
        )

        vectordns_resolver_raw = kwargs.get("vectordns_resolver")
        vectordns_resolver = (
            str(vectordns_resolver_raw).strip()
            if isinstance(vectordns_resolver_raw, str)
            else None
        )

        vectordns_name_raw = kwargs.get("vectordns_name")
        vectordns_name = (
            str(vectordns_name_raw).strip()
            if isinstance(vectordns_name_raw, str)
            else None
        )

        await_result_raw = kwargs.get("await_result", False)
        await_result = (
            await_result_raw
            if isinstance(await_result_raw, bool)
            else str(await_result_raw).strip().lower() in {"1", "true", "yes"}
        )

        def _to_int(value: Any, default: int) -> int:
            try:
                if value is None:
                    return default
                return int(value)
            except Exception:
                return default

        domain = (vectordns_domain or self._default_domain).strip().strip(".")
        resolver = (vectordns_resolver or self._default_resolver).strip()
        dns_port = _to_int(kwargs.get("vectordns_port"), self._default_dns_port)
        timeout_ms = _to_int(kwargs.get("vectordns_timeout_ms"), self._default_timeout_ms)
        route_intent = (intent or task).strip()

        await self._register_once(discovery_register_url=discovery_register_url)

        owner_name = self._build_srv_owner_name(
            target_agent=target_agent,
            domain=domain,
            owner_override=vectordns_name,
        )

        route = await self._resolve_vectordns_srv(
            owner_name=owner_name,
            resolver=resolver,
            port=dns_port,
            timeout_ms=timeout_ms,
        )
        if route is None:
            route = await self._resolve_http_discover(
                discovery_register_url=discovery_register_url,
                task=task,
                intent=route_intent,
                target_agent=target_agent,
            )
        if route is None:
            return (
                f"Error: VectorDNS SRV resolution failed for '{owner_name}' "
                "and HTTP discovery fallback did not return a valid route."
            )

        # Keep chat_id semantic as agent id if possible; A2A will use a2a_peer_base route hint directly.
        chat_id = target_agent or route.agent_id or route.host.split(".", 1)[0]
        task_id = uuid.uuid4().hex[:12]

        extra = dict(metadata)
        extra["task_id"] = task_id
        extra["intent"] = route_intent
        extra["a2a_mode"] = mode_l
        extra["a2a_peer_base"] = route.base_url
        extra["_delegation"] = {
            "tool": "delegate_task",
            "resolved_host": route.host,
            "resolved_port": route.port,
            "resolver": resolver,
            "owner_name": owner_name,
        }

        if origin_channel:
            extra.setdefault("upstream_channel", origin_channel)
        if origin_chat_id:
            extra.setdefault("upstream_chat_id", origin_chat_id)

        if self._delegation_queue and origin_channel and origin_chat_id:
            local_task = self._delegation_queue.create(
                reply_channel=origin_channel,
                reply_chat_id=origin_chat_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                delegated_channel="a2a",
            )
            self._delegation_queue.bind_delegated_task_id(
                delegation_task_id=local_task.id,
                delegated_task_id=task_id,
                delegated_agent_id=chat_id,
            )
            extra["delegation_task_id"] = local_task.id
            extra["_delegation"]["delegation_task_id"] = local_task.id

        msg = OutboundMessage(
            channel="a2a",
            chat_id=chat_id,
            content=task,
            metadata=extra,
        )
        await self._send_callback(msg)

        if await_result:
            return (
                f"Delegated task {task_id} to {chat_id} via {route.base_url} "
                f"(mode={mode_l}). await_result=true requested; delivery is queued."
            )
        return f"Delegated task {task_id} to {chat_id} via {route.base_url} (mode={mode_l})."

    @staticmethod
    def _normalize_discovery_base_url(url: str) -> str:
        u = url.strip().rstrip("/")
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
    def _discover_url_from_register(cls, discovery_register_url: str) -> str:
        base = cls._normalize_discovery_base_url(discovery_register_url)
        return f"{base}/discover"

    async def _resolve_http_discover(
        self,
        *,
        discovery_register_url: str,
        task: str,
        intent: str,
        target_agent: str | None,
    ) -> _ResolvedRoute | None:
        discover_url = self._discover_url_from_register(discovery_register_url)
        client = await self._ensure_http_client()

        body: dict[str, Any] = {
            "task": task,
            "intent": intent,
        }
        if target_agent:
            body["target_agent"] = target_agent

        try:
            resp = await client.get(discover_url, params=body)
        except Exception:
            return None

        if resp.status_code >= 400:
            return None

        try:
            payload = resp.json()
        except Exception:
            return None

        if not isinstance(payload, dict):
            return None

        route_obj = payload.get("route")
        route = route_obj if isinstance(route_obj, dict) else payload

        base_url_raw = route.get("base_url")
        host_raw = route.get("host")
        port_raw = route.get("port")
        agent_id_raw = route.get("agent_id") or payload.get("agent_id")

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
                    if isinstance(parsed_port, int):
                        port = parsed_port
                    else:
                        port = 443 if u.scheme == "https" else 80
            except Exception:
                return None

        if base_url is None:
            if not isinstance(host_raw, str) or not host_raw.strip():
                return None

            parsed_port_value: int | None = None
            if isinstance(port_raw, int):
                parsed_port_value = port_raw
            elif isinstance(port_raw, float):
                parsed_port_value = int(port_raw)
            elif isinstance(port_raw, str):
                raw = port_raw.strip()
                if raw:
                    try:
                        parsed_port_value = int(raw)
                    except Exception:
                        parsed_port_value = None

            if parsed_port_value is None or parsed_port_value <= 0:
                return None

            port = parsed_port_value
            host = host_raw.strip()
            base_url = f"http://{host}:{port}"

        agent_id: str | None = None
        if isinstance(agent_id_raw, str) and agent_id_raw.strip():
            agent_id = agent_id_raw.strip()
        elif host:
            agent_id = self._agent_from_target(host)

        if not host or port is None or not base_url:
            return None

        return _ResolvedRoute(agent_id=agent_id, host=host, port=port, base_url=base_url)

    async def _register_once(self, discovery_register_url: str) -> None:
        url = discovery_register_url.strip()
        if not url:
            raise ValueError("discovery_register_url is empty")
        async with self._register_lock:
            if url in self._registered_at:
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
                    "semantic_routing": "vectordns-srv",
                },
            }
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"register failed [{resp.status_code}]: {resp.text[:300]}")
            self._registered_at[url] = time.time()

    def _advertised_base_url(self) -> str | None:
        host = (self._listen_host or "").strip()
        if not host or host in {"0.0.0.0", "::"}:
            return None
        return f"http://{host}:{self._listen_port}"

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=20.0))
        return self._http_client

    @staticmethod
    def _build_srv_owner_name(
        *,
        target_agent: str | None,
        domain: str,
        owner_override: str | None,
    ) -> str:
        if owner_override and owner_override.strip():
            return owner_override.strip().strip(".")
        if target_agent:
            # Convention: _a2a._tcp.<agent>.<zone>
            return f"_a2a._tcp.{target_agent}.{domain}"
        # Semantic/default lane managed by VectorDNS policy.
        return f"_a2a._tcp.{domain}"

    async def _resolve_vectordns_srv(
        self,
        *,
        owner_name: str,
        resolver: str,
        port: int,
        timeout_ms: int,
    ) -> _ResolvedRoute | None:
        response = await asyncio.to_thread(
            self._dns_query_udp,
            resolver,
            port,
            owner_name,
            33,  # SRV
            timeout_ms,
        )
        if response is None:
            return None

        srv_records = [r for r in response if r["type"] == 33]
        a_records = [r for r in response if r["type"] == 1]

        if not srv_records:
            return None

        # RFC-ish selection: min priority, then weighted random by weight.
        min_pri = min(int(r["priority"]) for r in srv_records)
        bucket = [r for r in srv_records if int(r["priority"]) == min_pri]
        selected = self._weighted_pick(bucket)
        if selected is None:
            return None

        target = str(selected["target"]).rstrip(".")
        target_port = int(selected["port"])

        # Try additional-section A first, then explicit A lookup.
        ip = None
        for rec in a_records:
            if str(rec.get("name", "")).rstrip(".").lower() == target.lower():
                ip = str(rec.get("address"))
                break

        if ip is None:
            a_resp = await asyncio.to_thread(
                self._dns_query_udp,
                resolver,
                port,
                target,
                1,  # A
                timeout_ms,
            )
            if a_resp:
                for rec in a_resp:
                    if rec.get("type") == 1 and rec.get("address"):
                        ip = str(rec["address"])
                        break

        host = ip or target
        base_url = f"http://{host}:{target_port}"
        agent_id = self._agent_from_target(target)
        return _ResolvedRoute(agent_id=agent_id, host=host, port=target_port, base_url=base_url)

    @staticmethod
    def _agent_from_target(target: str) -> str | None:
        first = target.split(".", 1)[0]
        if first.endswith("-a2a"):
            return first[: -len("-a2a")]
        return first or None

    @staticmethod
    def _weighted_pick(records: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not records:
            return None
        weights = [max(0, int(r.get("weight", 0))) for r in records]
        total = sum(weights)
        if total <= 0:
            return records[0]
        needle = random.randint(1, total)
        acc = 0
        for rec, w in zip(records, weights, strict=False):
            acc += w
            if needle <= acc:
                return rec
        return records[0]

    @classmethod
    def _dns_query_udp(
        cls,
        resolver: str,
        port: int,
        qname: str,
        qtype: int,
        timeout_ms: int,
    ) -> list[dict[str, Any]] | None:
        qid = random.randint(0, 0xFFFF)
        packet = cls._build_dns_query(qid=qid, qname=qname, qtype=qtype)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(max(0.05, timeout_ms / 1000.0))
            sock.sendto(packet, (resolver, int(port)))
            data, _ = sock.recvfrom(4096)
        except Exception:
            return None
        finally:
            sock.close()

        try:
            return cls._parse_dns_response(data, expected_id=qid)
        except Exception:
            return None

    @staticmethod
    def _encode_qname(name: str) -> bytes:
        out = bytearray()
        for label in name.strip(".").split("."):
            raw = label.encode("utf-8")
            if len(raw) > 63:
                raise ValueError("label too long")
            out.append(len(raw))
            out.extend(raw)
        out.append(0)
        return bytes(out)

    @classmethod
    def _build_dns_query(cls, *, qid: int, qname: str, qtype: int) -> bytes:
        # Header: ID, flags(RD), QDCOUNT=1, AN=NS=AR=0
        header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
        question = cls._encode_qname(qname) + struct.pack("!HH", qtype, 1)
        return header + question

    @classmethod
    def _parse_dns_response(cls, data: bytes, *, expected_id: int) -> list[dict[str, Any]]:
        if len(data) < 12:
            return []

        qid, flags, qdcount, ancount, nscount, arcount = struct.unpack("!HHHHHH", data[:12])
        if qid != expected_id:
            return []
        rcode = flags & 0x000F
        if rcode != 0:
            return []

        offset = 12
        for _ in range(qdcount):
            _, offset = cls._read_name(data, offset)
            offset += 4  # qtype + qclass

        total_rr = ancount + nscount + arcount
        out: list[dict[str, Any]] = []

        for _ in range(total_rr):
            rr_name, offset = cls._read_name(data, offset)
            if offset + 10 > len(data):
                break
            rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
            offset += 10
            rdata_end = offset + rdlen
            if rdata_end > len(data):
                break
            rdata = data[offset:rdata_end]
            offset = rdata_end

            if rclass != 1:
                continue

            if rtype == 33 and len(rdata) >= 6:  # SRV
                priority, weight, port = struct.unpack("!HHH", rdata[:6])
                target, _ = cls._read_name(data, rdata_end - rdlen + 6)
                out.append(
                    {
                        "type": 33,
                        "name": rr_name,
                        "priority": priority,
                        "weight": weight,
                        "port": port,
                        "target": target.rstrip("."),
                    }
                )
            elif rtype == 1 and len(rdata) == 4:  # A
                out.append(
                    {
                        "type": 1,
                        "name": rr_name,
                        "address": socket.inet_ntoa(rdata),
                    }
                )

        return out

    @classmethod
    def _read_name(cls, data: bytes, offset: int) -> tuple[str, int]:
        labels: list[str] = []
        jumped = False
        start = offset
        seen = set()

        while True:
            if offset >= len(data):
                break
            if offset in seen:
                break
            seen.add(offset)

            length = data[offset]
            if length == 0:
                offset += 1
                break

            # Compression pointer
            if (length & 0xC0) == 0xC0:
                if offset + 1 >= len(data):
                    break
                ptr = ((length & 0x3F) << 8) | data[offset + 1]
                if not jumped:
                    start = offset + 2
                offset = ptr
                jumped = True
                continue

            offset += 1
            end = offset + length
            if end > len(data):
                break
            labels.append(data[offset:end].decode("utf-8", errors="ignore"))
            offset = end

        name = ".".join(labels)
        return (name, start if jumped else offset)

