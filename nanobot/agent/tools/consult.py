"""Consult tool: escalate a hard sub-problem to the expert (large) model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

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

    def __init__(self, provider: LLMProvider, model: str, max_tokens: int = 2048) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "consult"

    @property
    def description(self) -> str:
        return (
            "Ask the expert model for help with a hard problem. "
            "Call this when: (1) you need to design or write non-trivial code; "
            "(2) you are planning a multi-step approach and are unsure how to proceed; "
            "(3) you have hit the same error twice and need a different strategy. "
            "Provide all relevant context in 'context' — the expert has no access to "
            "conversation history. Ask one focused question."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": (
                        "All background the expert needs: what you are trying to build, "
                        "relevant code or error messages, and what you have already tried."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "The specific question or decision you need help with.",
                },
            },
            "required": ["context", "question"],
        }

    async def execute(self, context: str, question: str) -> str:
        prompt = f"Context:\n{context}\n\nQuestion:\n{question}"
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
