"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels.

    Calling this tool is a terminal action for the model's current turn.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._default_metadata: dict[str, Any] = {}
        self._sent_in_turn: bool = False

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id
        self._default_metadata = dict(metadata or {})

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return (
            "Send a message to the user. Use this when you want to communicate something. "
            "Calling this tool ends your current turn, so do it only after all other needed tools."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID (for channel='a2a' this is pinned to inbound sender unless override is allowed)"
                },
                "allow_a2a_chat_override": {
                    "type": "boolean",
                    "description": "Optional: allow explicit chat_id override when channel is a2a"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(self, **kwargs: Any) -> str:
        content = kwargs.get("content")
        requested_channel = kwargs.get("channel")
        requested_chat_id = kwargs.get("chat_id")
        channel = requested_channel or self._default_channel
        chat_id = requested_chat_id or self._default_chat_id
        message_id = kwargs.get("message_id") or self._default_message_id
        media = kwargs.get("media")

        if content is None:
            return "Error: Missing required parameter 'content'"

        content = str(content)

        if channel == "a2a":
            raw_a2a = self._default_metadata.get("_a2a")
            a2a_ctx = raw_a2a if isinstance(raw_a2a, dict) else {}
            pinned_chat_id = str(a2a_ctx.get("from_agent") or "").strip()

            allow_override_raw = kwargs.get("allow_a2a_chat_override")
            if allow_override_raw is None:
                allow_override_raw = self._default_metadata.get("allow_a2a_chat_override")

            allow_override = (
                allow_override_raw is True
                or (
                    isinstance(allow_override_raw, str)
                    and allow_override_raw.strip().lower() in {"1", "true", "yes", "on"}
                )
            )

            if pinned_chat_id and not allow_override:
                chat_id = pinned_chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        is_default_target = channel == self._default_channel and chat_id == self._default_chat_id

        normalized_media = tuple(
            m.strip() for m in (media or [])
            if isinstance(m, str) and m.strip()
        )

        out_meta = dict(self._default_metadata)
        if message_id:
            out_meta["message_id"] = message_id

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=list(normalized_media),
            metadata=out_meta,
        )

        try:
            await self._send_callback(msg)
            if is_default_target:
                self._sent_in_turn = True
            media_info = f" with {len(normalized_media)} attachments" if normalized_media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
