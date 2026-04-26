"""Vision analysis tool: delegate image understanding to a dedicated VL model."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import detect_image_mime

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider


class VisionAnalyzeTool(Tool):
    """Analyze an image via a dedicated vision-language model.

    The primary (text-only) model calls this tool when it receives image
    attachments.  Only the image and the question are sent to the VL model —
    no conversation history — so there is no context-window bleed between the
    two models.

    When update_context=True the tool registers the description into a shared
    dict so the loop can inline it back into the user message, keeping session
    history clean for subsequent turns.
    """

    def __init__(
        self,
        vl_provider: LLMProvider,
        vl_model: str,
        context_updates: dict[str, str] | None = None,
    ) -> None:
        self._vl_provider = vl_provider
        self._vl_model = vl_model
        # Shared dict written by the tool, read by the loop after the run.
        # Maps absolute image path → description.
        self._context_updates = context_updates if context_updates is not None else {}

    @property
    def name(self) -> str:
        return "vision_analyze"

    @property
    def description(self) -> str:
        return (
            "Analyze an image file and return a detailed description or answer a specific "
            "question about it.  Call this tool whenever the user sends an image or asks "
            "you to look at one.  Only provide the image path and your question — do not "
            "summarize or guess before calling this tool.  Set update_context=true to "
            "store the description back into the conversation so it is available in future turns."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Absolute path to the image file to analyze.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Specific question to answer about the image.  "
                        "Leave empty for a general detailed description."
                    ),
                },
                "update_context": {
                    "type": "boolean",
                    "description": (
                        "If true, replace the image placeholder in the conversation history "
                        "with the description so future turns see it without re-analyzing."
                    ),
                },
            },
            "required": ["image_path"],
        }

    async def execute(
        self,
        image_path: str,
        question: str = "",
        update_context: bool = False,
    ) -> str:
        p = Path(image_path)
        if not p.is_file():
            return f"Error: image file not found: {image_path}"

        raw = p.read_bytes()
        mime = detect_image_mime(raw) or mimetypes.guess_type(image_path)[0] or "image/jpeg"
        if not mime.startswith("image/"):
            return f"Error: {image_path} does not appear to be an image file"

        b64 = base64.b64encode(raw).decode()
        prompt = question.strip() if question.strip() else "Describe what you see in this image in detail."

        try:
            response = await self._vl_provider.chat_with_retry(
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                model=self._vl_model,
                max_tokens=1024,
            )
            result = (response.content or "").strip()
            logger.info("vision_analyze: {} → {} chars", p.name, len(result))
            if not result:
                return "The vision model returned an empty response."
            if update_context:
                self._context_updates[str(p)] = result
                logger.debug("vision_analyze: queued context update for {}", p.name)
            return result
        except Exception as exc:
            logger.error("vision_analyze failed for {}: {}", image_path, exc)
            return f"Vision analysis failed: {exc}"
