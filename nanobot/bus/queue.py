"""Async message queue for decoupled channel-agent communication."""

import asyncio
import time

from nanobot.bus.delegation import DelegationTaskMap
from nanobot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self.delegation_map = DelegationTaskMap()
        self.delegation = self.delegation_map

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    def pending_delegation_report(
        self,
        *,
        older_than_seconds: int | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        """Return a compact report of active delegation tasks."""
        rows = self.delegation_map.list_active()
        now = time.time()

        if older_than_seconds is not None:
            cutoff = now - max(1, int(older_than_seconds))
            rows = [t for t in rows if t.created_at <= cutoff]

        rows.sort(key=lambda t: t.created_at)
        rows = rows[: max(1, int(limit))]

        return {
            "count": len(rows),
            "pending": [
                {
                    "id": t.id,
                    "delegation_id": t.delegation_id,
                    "status": t.status,
                    "age_seconds": max(0, int(now - t.created_at)),
                    "reply_channel": t.reply_channel,
                    "reply_chat_id": t.reply_chat_id,
                    "delegated_task_id": t.delegated_task_id,
                    "delegated_agent_id": t.delegated_agent_id,
                }
                for t in rows
            ],
        }
