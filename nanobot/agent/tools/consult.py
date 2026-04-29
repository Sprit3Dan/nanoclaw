"""Consult tool: escalate a hard sub-problem to the expert (large) model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_EXPERT_SYSTEM = (
    "You are an expert technical advisor. Give a direct, actionable answer. "
    "No preamble. Be concise but complete."
)


class ConsultTool(Tool):
    """Ask the expert model for advice on a hard sub-problem.

    The primary model calls this when it encounters complex planning,
    non-trivial code authoring, or is stuck after repeated errors.
    Provide all relevant context explicitly — the expert has no access
    to conversation history.
    """

    def __init__(self, provider: LLMProvider, model: str, memory_store: MemoryStore, max_tokens: int = 8192) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self.memory_store = memory_store

    @property
    def name(self) -> str:
        return "consult"

    @property
    def description(self) -> str:
        return (
            "ESCALATE TO EXPERT. You MUST call this tool immediately if you are asked to: "
            "1. Write more than 20 lines of novel code. "
            "2. Design a new system architecture or database schema. "
            "3. Resolve an error that has occurred more than once. "
            "Do not attempt complex coding yourself; delegate it using this tool."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # Remove 'context'. Make the barrier to entry as low as possible.
                "question": {
                    "type": "string",
                    "description": "The specific question, code request, or decision you need help with.",
                },
            },
            "required": ["question"],
        }

    async def execute(self, question: str) -> str:
        # 1. Fetch recent history from the MemoryStore
        try:
            # We use a cursor of 0 (or a recent number) to grab recent JSONL entries.
            # If your history file is massive, we just slice the last 15.
            all_history = self.memory_store.read_unprocessed_history(since_cursor=0)
            recent_entries = all_history[-15:] if all_history else []
        except Exception as e:
            logger.warning("Could not fetch history for consult tool: {}", e)
            recent_entries = []

        # 2. Format it using the store's exact JSONL structure
        transcript = []
        for entry in recent_entries:
            content = entry.get("content", "")
            if not content:
                continue

            # Truncate massive tool outputs so we don't blow the expert's context limit
            if len(content) > 3000:
                content = content[:3000] + "... [TRUNCATED]"

            timestamp = entry.get("timestamp", "?")
            transcript.append(f"[{timestamp}]: {content}")

        formatted_context = "\n\n".join(transcript) if transcript else "No recent history available."

        prompt = (
            f"Here is the recent conversation and execution history leading up to the issue:\n"
            f"---\n{formatted_context}\n---\n\n"
            f"The primary execution agent is stuck and asks you this specific question:\n"
            f"{question}"
        )

        try:
            response = await self._provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": _EXPERT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                max_tokens=self._max_tokens,
            )
            result = (response.content or "").strip()
            logger.info("consult: {} → {} chars", self._model, len(result))
            return result or "The expert model returned an empty response."
        except Exception as exc:
            logger.error("consult failed: {}", exc)
            return f"Consult failed: {exc}"
