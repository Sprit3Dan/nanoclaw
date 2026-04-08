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


class RabbitMQTransport(A2ATransport):
    """Direct-exchange AMQP transport via RabbitMQ.

    Topology (auto-declared on subscribe):
        exchange  ``{exchange}``          direct, durable
        queue     ``{queue_prefix}{id}``  durable, bound to exchange w/ routing_key=agent_id

    Requires: ``aio-pika`` (included in core ``nanobot-ai`` dependencies).
    """

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost:5672/",
        exchange: str = "a2a",
        queue_prefix: str = "a2a.",
        prefetch: int = 10,
        status_exchange: str = "a2a.status",
        status_queue_prefix: str = "a2a.status.",
    ) -> None:
        self._url = url
        self._exchange_name = exchange
        self._queue_prefix = queue_prefix
        self._prefetch = prefetch
        self._status_exchange_name = status_exchange
        self._status_queue_prefix = status_queue_prefix

        self._connection = None
        self._channel = None
        self._exchange = None
        self._status_exchange = None
        self._stopped = False

    async def subscribe(
        self,
        agent_id: str,
        on_envelope: OnEnvelope,
        on_status_event: OnStatusEvent | None = None,
    ) -> None:
        import aio_pika

        queue_name = f"{self._queue_prefix}{agent_id}"
        status_queue_name = f"{self._status_queue_prefix}{agent_id}"

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch)

        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )
        self._status_exchange = await self._channel.declare_exchange(
            self._status_exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )

        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._exchange, routing_key=agent_id)

        logger.info(
            "A2A RabbitMQ: agent={} queue={} exchange={}",
            agent_id, queue_name, self._exchange_name,
        )

        async def _on_message(msg: aio_pika.IncomingMessage) -> None:
            async with msg.process(requeue=True):
                try:
                    envelope = json.loads(msg.body.decode("utf-8"))
                except Exception as exc:
                    logger.warning("A2A RabbitMQ: bad envelope: {}", exc)
                    return
                try:
                    await on_envelope(envelope)
                except Exception as exc:
                    logger.error("A2A RabbitMQ: on_envelope error: {}", exc)

        await queue.consume(_on_message)

        if on_status_event is not None:
            status_queue = await self._channel.declare_queue(status_queue_name, durable=True)
            await status_queue.bind(self._status_exchange, routing_key=agent_id)

            logger.info(
                "A2A RabbitMQ: agent={} status_queue={} status_exchange={}",
                agent_id, status_queue_name, self._status_exchange_name,
            )

            async def _on_status_message(msg: aio_pika.IncomingMessage) -> None:
                async with msg.process(requeue=True):
                    try:
                        event = json.loads(msg.body.decode("utf-8"))
                    except Exception as exc:
                        logger.warning("A2A RabbitMQ: bad status event: {}", exc)
                        return
                    try:
                        await on_status_event(event)
                    except Exception as exc:
                        logger.error("A2A RabbitMQ: on_status_event error: {}", exc)

            await status_queue.consume(_on_status_message)

        while not self._stopped:
            await asyncio.sleep(0.5)

    async def publish(self, to_agent: str, envelope: Envelope) -> None:
        import aio_pika

        if self._exchange is None:
            raise RuntimeError("RabbitMQ transport not started")

        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        await self._exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=envelope.get("message_id", ""),
                correlation_id=envelope.get("delegation_id", ""),
            ),
            routing_key=to_agent,
        )
        logger.debug(
            "A2A RabbitMQ: published to_agent={} delegation_id={}",
            to_agent, envelope.get("delegation_id", ""),
        )

    async def publish_status(self, to_agent: str, event: StatusEvent) -> None:
        import aio_pika

        if self._status_exchange is None:
            raise RuntimeError("RabbitMQ status transport not started")

        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        await self._status_exchange.publish(
            aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(event.get("event_id", "")),
                correlation_id=str(event.get("delegation_id", "")),
            ),
            routing_key=to_agent,
        )
        logger.debug(
            "A2A RabbitMQ: status published to_agent={} delegation_id={} status={}",
            to_agent,
            event.get("delegation_id", ""),
            event.get("status", ""),
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
