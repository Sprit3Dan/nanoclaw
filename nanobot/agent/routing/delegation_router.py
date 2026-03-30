"""Delegation routing helper for A2A tool context and reply target resolution."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from nanobot.bus.delegation import DelegationTaskMap
from nanobot.bus.events import InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig


class DelegationRouter:
    """
    Encapsulates delegation-aware routing policy.

    Responsibilities:
    1) Resolve tool execution context for inbound messages
       (for A2A, prefer upstream user channel when configured/enabled).
    2) Resolve final outbound destination for responses
       (delegation correlation -> upstream channel -> A2A fallback).
    """

    def __init__(
        self,
        *,
        delegation_map: DelegationTaskMap,
        channels_config: ChannelsConfig | None = None,
    ) -> None:
        self._delegation = delegation_map
        self._channels_config = channels_config

    def set_channels_config(self, channels_config: ChannelsConfig | None) -> None:
        """Update channels config used for enabled-channel checks."""
        self._channels_config = channels_config

    def resolve_tool_target(self, msg: InboundMessage) -> tuple[str, str]:
        """
        Return (channel, chat_id) where tools should target by default.

        For A2A messages, if upstream routing metadata is present and that channel
        is enabled locally, route tool context to the upstream target.
        """
        tool_channel = msg.channel
        tool_chat_id = msg.chat_id

        if msg.channel != "a2a":
            return tool_channel, tool_chat_id

        meta = msg.metadata or {}
        upstream_channel = self._str_meta(meta, "upstream_channel")
        upstream_chat_id = self._str_meta(meta, "upstream_chat_id")

        if upstream_channel and upstream_chat_id and self._is_channel_enabled(upstream_channel):
            return upstream_channel, upstream_chat_id

        return tool_channel, tool_chat_id

    def route_response(
        self,
        *,
        msg: InboundMessage,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> OutboundMessage:
        """
        Resolve final outbound message destination.

        Policy:
        - Non-A2A: reply to original channel/chat.
        - A2A:
          1) Resolve upstream target by `delegation_id` only.
          2) If a correlated delegation task exists, route to its reply target and mark completed.
          3) Otherwise, fallback to replying over A2A to the sender agent.
        """
        response_metadata = dict(metadata or msg.metadata or {})

        if msg.channel != "a2a":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=response_metadata,
            )

        delegation_id = self._extract_delegation_id(response_metadata)

        if delegation_id:
            active_delegation = self._delegation.resolve({"delegation_id": delegation_id})
            if active_delegation is not None:
                reply_channel = active_delegation.reply_channel
                reply_chat_id = active_delegation.reply_chat_id
                if reply_channel and reply_chat_id:
                    self._delegation.mark_completed(active_delegation.id)
                    return OutboundMessage(
                        channel=reply_channel,
                        chat_id=reply_chat_id,
                        content=content,
                        metadata=response_metadata,
                    )
        else:
            delegation_id = uuid.uuid4().hex

        response_metadata["delegation_id"] = delegation_id
        if not self._str_meta(response_metadata, "message_type"):
            response_metadata["message_type"] = "delegation_request"

        raw_a2a = response_metadata.get("_a2a")
        a2a_ctx = raw_a2a if isinstance(raw_a2a, dict) else {}
        origin_agent = str(a2a_ctx.get("from_agent") or msg.sender_id).strip() or str(msg.sender_id)

        reply_to_base = a2a_ctx.get("reply_to_base")
        if isinstance(reply_to_base, str) and reply_to_base.strip():
            response_metadata["a2a_peer_base"] = reply_to_base.strip()

        return OutboundMessage(
            channel="a2a",
            chat_id=origin_agent,
            content=content,
            metadata=response_metadata,
        )

    @staticmethod
    def _str_meta(meta: dict[str, Any], key: str) -> str:
        value = meta.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _extract_delegation_id(self, metadata: dict[str, Any]) -> str:
        delegation_id = self._str_meta(metadata, "delegation_id")
        if delegation_id:
            return delegation_id

        raw_a2a = metadata.get("_a2a")
        a2a_ctx = raw_a2a if isinstance(raw_a2a, dict) else {}
        delegation_id = self._str_meta(a2a_ctx, "delegation_id")
        if delegation_id:
            return delegation_id

        raw_delegation = metadata.get("_delegation")
        delegation_ctx = raw_delegation if isinstance(raw_delegation, dict) else {}
        return self._str_meta(delegation_ctx, "delegation_id")

    def _is_channel_enabled(self, channel_name: str) -> bool:
        if not channel_name:
            return False
        if self._channels_config is None:
            return False

        section = getattr(self._channels_config, channel_name, None)
        if isinstance(section, dict):
            return bool(section.get("enabled", False))
        if section is not None:
            return bool(getattr(section, "enabled", False))
        return False
