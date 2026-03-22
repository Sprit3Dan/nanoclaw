from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
import nanobot.agent.memory as memory_module
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.config.schema import RoutingConfig


def _make_loop(tmp_path, *, estimated_tokens: int, context_window_tokens: int) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (estimated_tokens, "test-counter")
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_prompt_below_threshold_does_not_consolidate(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.memory_consolidator.consolidate_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_above_threshold_triggers_consolidation(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]
    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _message: 500)

    await loop.process_direct("hello", session_key="cli:test")

    assert loop.memory_consolidator.consolidate_messages.await_count >= 1


@pytest.mark.asyncio
async def test_prompt_above_threshold_archives_until_next_user_boundary(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
    ]
    loop.sessions.save(session)

    token_map = {"u1": 120, "a1": 120, "u2": 120, "a2": 120, "u3": 120}
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda message: token_map[message["content"]])

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    archived_chunk = loop.memory_consolidator.consolidate_messages.await_args.args[0]
    assert [message["content"] for message in archived_chunk] == ["u1", "a1", "u2", "a2"]
    assert session.last_consolidated == 4


@pytest.mark.asyncio
async def test_consolidation_loops_until_target_met(tmp_path, monkeypatch) -> None:
    """Verify maybe_consolidate_by_tokens keeps looping until under threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]
    def mock_estimate(_session):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (300, "test")
        return (80, "test")

    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    assert loop.memory_consolidator.consolidate_messages.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_continues_below_trigger_until_half_target(tmp_path, monkeypatch) -> None:
    """Once triggered, consolidation should continue until it drops below half threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.memory_consolidator.consolidate_messages = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
        {"role": "assistant", "content": "a2", "timestamp": "2026-01-01T00:00:03"},
        {"role": "user", "content": "u3", "timestamp": "2026-01-01T00:00:04"},
        {"role": "assistant", "content": "a3", "timestamp": "2026-01-01T00:00:05"},
        {"role": "user", "content": "u4", "timestamp": "2026-01-01T00:00:06"},
    ]
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (150, "test")
        return (80, "test")

    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.memory_consolidator.maybe_consolidate_by_tokens(session)

    assert loop.memory_consolidator.consolidate_messages.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_preflight_consolidation_before_llm_call(tmp_path, monkeypatch) -> None:
    """Verify preflight consolidation runs before the LLM call in process_direct."""
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)

    async def track_consolidate(messages):
        order.append("consolidate")
        return True
    loop.memory_consolidator.consolidate_messages = track_consolidate  # type: ignore[method-assign]

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])
    loop.provider.chat_with_retry = track_llm

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "u1", "timestamp": "2026-01-01T00:00:00"},
        {"role": "assistant", "content": "a1", "timestamp": "2026-01-01T00:00:01"},
        {"role": "user", "content": "u2", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 500)

    call_count = [0]
    def mock_estimate(_session):
        call_count[0] += 1
        return (1000 if call_count[0] <= 1 else 80, "test")
    loop.memory_consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate" in order
    assert "llm" in order
    assert order.index("consolidate") < order.index("llm")


@pytest.mark.asyncio
async def test_route_selects_vl_for_image_messages(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    vl_provider = MagicMock()
    reasoning_provider = MagicMock()
    default_provider = loop.provider

    loop.vl_provider = vl_provider
    loop.vl_model = "vl-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"

    # Ensure router is not needed for explicit image input.
    loop.router_provider = MagicMock()
    loop.router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))
    loop.router_model = "router-model"

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "text", "text": "What is in this image?"},
        ]},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "custom_vl"
    assert provider is vl_provider
    assert model == "vl-model"
    assert provider is not default_provider


@pytest.mark.asyncio
async def test_route_selects_reasoning_when_router_returns_reasoning(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))
    reasoning_provider = MagicMock()

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Prove correctness and analyze complexity for this algorithm."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "reasoning"
    assert provider is reasoning_provider
    assert model == "reasoning-model"
    router_provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_falls_back_to_custom_when_reasoning_unavailable(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))

    default_provider = loop.provider
    default_model = loop.model

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = MagicMock()
    loop.reasoning_provider.get_default_model.return_value = ""
    loop.reasoning_model = ""

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Deeply reason about this hard multi-step architecture decision."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "custom"
    assert provider is default_provider
    assert model == default_model


@pytest.mark.asyncio
async def test_route_without_router_fields_skips_router(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))
    reasoning_provider = MagicMock()

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"
    loop.routing_config = RoutingConfig()

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Solve this with deep multi-step reasoning."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "custom"
    assert provider is loop.provider
    assert model == loop.model
    router_provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_secondary_not_selected_without_secondary_fields_even_if_router_says_reasoning(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))
    reasoning_provider = MagicMock()

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"
    loop.routing_config = RoutingConfig()

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Do hardcore planning and proofs."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "custom"
    assert provider is loop.provider
    assert model == loop.model
    router_provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_force_secondary_pattern_overrides_router_call(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="custom", tool_calls=[]))
    reasoning_provider = MagicMock()

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"
    loop.routing_config = RoutingConfig(
        force_secondary_patterns=["always-hard"],
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Please always-hard route this request."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "reasoning"
    assert provider is reasoning_provider
    assert model == "reasoning-model"
    router_provider.chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_force_primary_pattern_takes_priority(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)

    router_provider = MagicMock()
    router_provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="reasoning", tool_calls=[]))
    reasoning_provider = MagicMock()

    loop.router_provider = router_provider
    loop.router_model = "router-model"
    loop.reasoning_provider = reasoning_provider
    loop.reasoning_model = "reasoning-model"
    loop.routing_config = RoutingConfig(
        force_primary_patterns=["cheap-path"],
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Use the cheap-path for this request."},
    ]

    provider, model, route = await loop._select_provider_for_request(messages)

    assert route == "custom"
    assert provider is loop.provider
    assert model == loop.model
    router_provider.chat_with_retry.assert_not_awaited()
