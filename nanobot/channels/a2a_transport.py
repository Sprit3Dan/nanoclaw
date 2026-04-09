"""Pluggable transport backends for A2A agent-to-agent messaging.

Each transport handles two concerns:
  - subscribe(agent_id, on_envelope): start consuming messages addressed to this agent
  - publish(to_agent, envelope): deliver an envelope to another agent

The envelope format is transport-agnostic (nanobot-a2a/v1 JSON).  Broker
transports replace the direct HTTP push model; the channel's envelope
building/parsing and delegation tracking remain unchanged.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from loguru import logger

Envelope = dict[str, Any]
OnEnvelope = Callable[[Envelope], Awaitable[None]]
StatusEvent = dict[str, Any]
OnStatusEvent = Callable[[StatusEvent], Awaitable[None]]


class A2ATransport(ABC):
    @abstractmethod
    async def subscribe(
        self,
        agent_id: str,
        on_envelope: OnEnvelope,
        on_status_event: OnStatusEvent | None = None,
    ) -> None:
        """Declare channels and start consuming. Returns when stop() is called."""

    @abstractmethod
    async def publish(self, to_agent: str, envelope: Envelope) -> None:
        """Deliver envelope to to_agent's task channel."""

    @abstractmethod
    async def publish_status(self, to_agent: str, event: StatusEvent) -> None:
        """Deliver status event to to_agent's status channel."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down."""


class NatsJetStreamTransport(A2ATransport):
    """NATS JetStream transport for A2A agent-to-agent messaging.

    Subject topology (mirrors webagent convention):
        delegations: ``{prefix}.delegations.{agent_id}``  — inbound tasks
        status:      ``{prefix}.status.{agent_id}``        — lifecycle events

    Requires: ``nats-py>=2.6.0`` (add to pyproject.toml optional extras or core deps).
    """

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        stream_name: str = "a2a",
        subject_prefix: str = "a2a",
    ) -> None:
        self._nats_url = nats_url
        self._stream_name = stream_name
        self._subject_prefix = subject_prefix.rstrip(".")
        self._nc: Any = None
        self._js: Any = None
        self._stopped = False
        self._consumer_tasks: list[asyncio.Task] = []

    def _delegation_subject(self, agent_id: str) -> str:
        return f"{self._subject_prefix}.delegations.{agent_id}"

    def _status_subject(self, agent_id: str) -> str:
        return f"{self._subject_prefix}.status.{agent_id}"

    async def subscribe(
        self,
        agent_id: str,
        on_envelope: OnEnvelope,
        on_status_event: OnStatusEvent | None = None,
    ) -> None:
        try:
            import nats
            from nats.js.api import StreamConfig
        except ImportError as exc:
            raise RuntimeError(
                "nats-py is not installed. Add 'nats-py>=2.6.0' to dependencies."
            ) from exc

        self._nc = await nats.connect(
            self._nats_url,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()
        logger.info("A2A NATS: connected agent_id={} url={}", agent_id, self._nats_url)

        try:
            await self._js.add_stream(
                StreamConfig(
                    name=self._stream_name,
                    subjects=[
                        f"{self._subject_prefix}.delegations.*",
                        f"{self._subject_prefix}.status.*",
                    ],
                )
            )
            logger.info("A2A NATS: stream {!r} ready", self._stream_name)
        except Exception as exc:
            logger.warning("A2A NATS: stream setup (may already exist): {}", exc)

        delegation_task = asyncio.create_task(
            self._consume_loop(
                subject=self._delegation_subject(agent_id),
                durable_name=f"nanobot-{agent_id}-delegation",
                handler=on_envelope,
            ),
            name=f"a2a-nats-delegation-{agent_id}",
        )
        self._consumer_tasks.append(delegation_task)

        if on_status_event is not None:
            status_task = asyncio.create_task(
                self._consume_loop(
                    subject=self._status_subject(agent_id),
                    durable_name=f"nanobot-{agent_id}-status",
                    handler=on_status_event,
                ),
                name=f"a2a-nats-status-{agent_id}",
            )
            self._consumer_tasks.append(status_task)

        while not self._stopped:
            await asyncio.sleep(0.5)

    async def _consume_loop(
        self,
        *,
        subject: str,
        durable_name: str,
        handler: OnEnvelope | OnStatusEvent,
    ) -> None:
        while not self._stopped:
            try:
                sub = await self._js.pull_subscribe(subject, durable_name)
                logger.info("A2A NATS: consumer ready subject={}", subject)
                while not self._stopped:
                    try:
                        msgs = await sub.fetch(batch=10, timeout=5.0)
                        for msg in msgs:
                            try:
                                data = json.loads(msg.data)
                                await handler(data)
                                await msg.ack()
                            except json.JSONDecodeError as exc:
                                logger.error("A2A NATS: bad JSON on {}: {}", subject, exc)
                                await msg.nak()
                            except Exception as exc:
                                logger.error(
                                    "A2A NATS: handler error on {}: {}", subject, exc, exc_info=True
                                )
                                await msg.nak()
                    except asyncio.CancelledError:
                        raise
                    except Exception as fetch_exc:
                        exc_name = type(fetch_exc).__name__
                        if "Timeout" not in exc_name and "timeout" not in str(fetch_exc).lower():
                            logger.warning("A2A NATS: fetch error on {}: {}", subject, fetch_exc)
            except asyncio.CancelledError:
                logger.info("A2A NATS: consumer cancelled for {}", subject)
                return
            except Exception as exc:
                logger.error(
                    "A2A NATS: consumer loop error on {}: {} — retrying in 5s", subject, exc
                )
                await asyncio.sleep(5)

    async def publish(self, to_agent: str, envelope: Envelope) -> None:
        subject = self._delegation_subject(to_agent)
        data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        await self._js.publish(subject, data)
        logger.debug(
            "A2A NATS: published delegation to_agent={} delegation_id={}",
            to_agent, envelope.get("delegation_id"),
        )

    async def publish_status(self, to_agent: str, event: StatusEvent) -> None:
        subject = self._status_subject(to_agent)
        data = json.dumps(event, ensure_ascii=False).encode("utf-8")
        await self._js.publish(subject, data)
        logger.debug(
            "A2A NATS: status published to_agent={} delegation_id={} status={}",
            to_agent, event.get("delegation_id"), event.get("status"),
        )

    async def stop(self) -> None:
        self._stopped = True
        for task in self._consumer_tasks:
            task.cancel()
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        if self._nc:
            try:
                await self._nc.drain()
            except Exception as exc:
                logger.warning("A2A NATS: drain error: {}", exc)
