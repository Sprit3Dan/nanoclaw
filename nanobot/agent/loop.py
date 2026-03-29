"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from loguru import logger

from nanobot import __version__
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryConsolidator
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.delegate import DelegateTaskTool
from nanobot.agent.tools.delegation_tasks import DelegationTasksTool
from nanobot.agent.tools.delegation_remote_status import DelegationRemoteStatusTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.utils.helpers import build_status_content
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, RoutingConfig, WebSearchConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000
    _ROUTER_MAX_USER_CHARS = 2_000


    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        vision_provider: LLMProvider | None = None,
        vision_model: str | None = None,
        router_provider: LLMProvider | None = None,
        router_model: str | None = None,
        reasoning_provider: LLMProvider | None = None,
        reasoning_model: str | None = None,
        vl_provider: LLMProvider | None = None,
        vl_model: str | None = None,
        routing_config: RoutingConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self.vision_provider = vision_provider or provider
        self.vision_model = vision_model or self.model
        self.router_provider = router_provider or provider
        self.router_model = router_model or self.model
        self.reasoning_provider = reasoning_provider or provider
        self.reasoning_model = reasoning_model or self.model
        self.vl_provider = vl_provider or self.vision_provider
        self.vl_model = vl_model or self.vision_model
        self.routing_config = routing_config

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._processing_lock = asyncio.Lock()
        self.memory_consolidator = MemoryConsolidator(
            workspace=workspace,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
        )
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read))
        for cls in (WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        if self.exec_config.enable:
            self.tools.register(ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            ))
        self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(DelegateTaskTool(
            send_callback=self.bus.publish_outbound,
            delegation_queue=self.bus.delegation,
        ))
        self.tools.register(DelegationTasksTool(delegation_map=self.bus.delegation_map))
        self.tools.register(DelegationRemoteStatusTool(delegation_map=self.bus.delegation_map))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        self._current_origin_channel = channel
        self._current_origin_chat_id = chat_id

        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

        # Ensure delegate_task always receives inbound origin context unless explicitly overridden.
        if delegate_tool := self.tools.get("delegate_task"):
            if not getattr(delegate_tool, "_origin_ctx_wrapped", False):
                original_execute = delegate_tool.execute

                async def _execute_with_origin(**kwargs: Any) -> Any:
                    kwargs.setdefault(
                        "origin_channel",
                        getattr(self, "_current_origin_channel", ""),
                    )
                    kwargs.setdefault(
                        "origin_chat_id",
                        getattr(self, "_current_origin_chat_id", ""),
                    )
                    return await original_execute(**kwargs)

                delegate_tool.execute = _execute_with_origin  # type: ignore[method-assign]
                delegate_tool._origin_ctx_wrapped = True  # type: ignore[attr-defined]

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    def _status_response(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        """Build an outbound status message for a session."""
        ctx_est = 0
        try:
            ctx_est, _ = self.memory_consolidator.estimate_session_prompt_tokens(session)
        except Exception:
            pass
        if ctx_est <= 0:
            ctx_est = self._last_usage.get("prompt_tokens", 0)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=build_status_content(
                version=__version__, model=self.model,
                start_time=self._start_time, last_usage=self._last_usage,
                context_window_tokens=self.context_window_tokens,
                session_msg_count=len(session.get_history(max_messages=0)),
                context_tokens_estimate=ctx_est,
            ),
            metadata={"render_as": "text"},
        )

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        turn_usage_max = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        consecutive_message_only_tool_rounds = 0
        max_consecutive_message_only_tool_rounds = 3

        active_provider, active_model, route_label = await self._select_provider_for_request(messages)
        logger.info("Request route selected: {}", route_label)

        while iteration < self.max_iterations:
            iteration += 1

            tool_defs = self.tools.get_definitions()

            response = await active_provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=active_model,
            )
            usage = response.usage or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0)
            turn_usage_max["prompt_tokens"] = max(turn_usage_max["prompt_tokens"], prompt_tokens)
            turn_usage_max["completion_tokens"] = max(turn_usage_max["completion_tokens"], completion_tokens)
            turn_usage_max["total_tokens"] = max(turn_usage_max["total_tokens"], total_tokens)

            if response.has_tool_calls:
                tool_names = [tc.name for tc in response.tool_calls]
                if tool_names and all(name == "message" for name in tool_names):
                    consecutive_message_only_tool_rounds += 1
                else:
                    consecutive_message_only_tool_rounds = 0

                if consecutive_message_only_tool_rounds > max_consecutive_message_only_tool_rounds:
                    logger.warning(
                        "Runaway message-only tool loop detected ({} consecutive rounds), stopping",
                        consecutive_message_only_tool_rounds,
                    )
                    final_content = (
                        "I stopped to avoid a runaway messaging loop. "
                        "Please resend your request and I will continue."
                    )
                    break

                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    tool_hint = self._tool_hint(response.tool_calls)
                    tool_hint = self._strip_think(tool_hint)
                    await on_progress(tool_hint, tool_hint=True)

                tool_call_dicts = [
                    tc.to_openai_tool_call()
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                ordered_tool_calls = [
                    tc for tc in response.tool_calls
                    if tc.name != "message"
                ] + [
                    tc for tc in response.tool_calls
                    if tc.name == "message"
                ]
                end_turn_after_message = any(tc.name == "message" for tc in ordered_tool_calls)

                for tool_call in ordered_tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                if end_turn_after_message:
                    break
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
        self._last_usage = turn_usage_max
        return final_content, tools_used, messages

    @staticmethod
    def _flatten_content_to_text(content: Any) -> str:
        """Flatten message content (string/list blocks) into plain text."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "\n".join(chunks).strip()



    def _extract_latest_user_text(self, messages: list[dict[str, Any]]) -> str:
        """Get the latest user message as plain text."""
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            return self._flatten_content_to_text(message.get("content"))
        return ""



    def _routing_primary_share(self) -> float:
        cfg = self.routing_config
        if cfg is None:
            val = 0.85
        elif hasattr(cfg, "primary_share"):
            val = getattr(cfg, "primary_share")
        else:
            val = getattr(cfg, "custom_share", 0.85)
        try:
            f = float(val)
        except Exception:
            return 0.85
        return max(0.0, min(1.0, f))

    def _routing_patterns(self, attr: str) -> list[str]:
        cfg = self.routing_config
        if cfg is None:
            return []

        legacy_aliases: dict[str, tuple[str, ...]] = {
            "force_primary_patterns": ("force_primary_patterns", "force_custom_patterns"),
            "force_vision_patterns": ("force_vision_patterns", "force_vl_patterns"),
            "force_secondary_patterns": ("force_secondary_patterns", "force_reasoning_patterns"),
        }
        lookup_attrs = legacy_aliases.get(attr, (attr,))
        raw: list[str] | Any = []
        for key in lookup_attrs:
            value = getattr(cfg, key, None)
            if isinstance(value, list):
                raw = value
                break

        if not isinstance(raw, list):
            return []
        return [p.lower().strip() for p in raw if isinstance(p, str) and p.strip()]

    def _routing_flag_notes(self) -> str:
        """Build optional router guidance for explicit route flags."""
        primary_flags = self._routing_patterns("force_primary_patterns")
        vision_flags = self._routing_patterns("force_vision_patterns")
        secondary_flags = self._routing_patterns("force_secondary_patterns")

        lines: list[str] = []
        if primary_flags:
            joined = ", ".join(primary_flags)
            lines.append(f"- If user text includes one of [{joined}], output primary.")
        if vision_flags:
            joined = ", ".join(vision_flags)
            lines.append(f"- If user text includes one of [{joined}], output vision.")
        if secondary_flags:
            joined = ", ".join(secondary_flags)
            lines.append(f"- If user text includes one of [{joined}], output secondary.")

        if not lines:
            return ""
        return "Flag overrides:\n" + "\n".join(lines) + "\n"

    def _routing_prompt(self) -> str:
        cfg = self.routing_config
        override = getattr(cfg, "prompt_override", None) if cfg is not None else None
        if isinstance(override, str) and override.strip():
            return override.strip()

        primary_desc = "default choice, questions, tool calls, low level effort reasoning"
        vision_desc = "describe images, OCR, screenshots, diagrams, and other visual understanding tasks"
        secondary_desc = "hardcore reasoning, deep problem solving, and complex multi-step technical analysis"

        route_descriptions = getattr(cfg, "route_descriptions", None) if cfg is not None else None
        if isinstance(route_descriptions, dict):
            primary_desc = route_descriptions.get("primary") or route_descriptions.get("custom") or primary_desc
            vision_desc = (
                route_descriptions.get("vision")
                or route_descriptions.get("vl")
                or route_descriptions.get("custom_vl")
                or vision_desc
            )
            secondary_desc = (
                route_descriptions.get("secondary")
                or route_descriptions.get("reasoning")
                or secondary_desc
            )
        elif route_descriptions is not None:
            primary_desc = (
                getattr(route_descriptions, "primary", None)
                or getattr(route_descriptions, "custom", None)
                or primary_desc
            )
            vision_desc = (
                getattr(route_descriptions, "vision", None)
                or getattr(route_descriptions, "vl", None)
                or getattr(route_descriptions, "custom_vl", None)
                or vision_desc
            )
            secondary_desc = (
                getattr(route_descriptions, "secondary", None)
                or getattr(route_descriptions, "reasoning", None)
                or secondary_desc
            )

        primary_pct = int(round(self._routing_primary_share() * 100))
        vision_note = f"- Route to vision for: {vision_desc}.\n"
        secondary_note = f"- Route to secondary for: {secondary_desc}.\n"
        flag_note = self._routing_flag_notes()
        return (
            "You are a routing classifier for an AI assistant.\n"
            "Output exactly one label: primary, vision, or secondary.\n"
            f"- Route to primary for: {primary_desc}. This should be the dominant route (~{primary_pct}%).\n"
            f"{vision_note}"
            f"{secondary_note}"
            f"{flag_note}"
            "Do not explain. Return one label only."
        )

    def _routing_examples(self) -> list[tuple[str, str]]:
        cfg = self.routing_config
        raw = getattr(cfg, "examples", []) if cfg is not None else []
        if not isinstance(raw, list):
            return []
        result: list[tuple[str, str]] = []
        for item in raw:
            if isinstance(item, dict):
                user_in = item.get("input")
                route = item.get("route")
            else:
                user_in = getattr(item, "input", None)
                route = getattr(item, "route", None)
            if not isinstance(user_in, str) or not user_in.strip():
                continue
            normalized = self._parse_router_route(route)
            if normalized not in ("primary", "vision", "secondary"):
                continue
            result.append((user_in.strip(), normalized))
        return result



    @staticmethod
    def _is_route_available(provider: LLMProvider | None, model: str | None) -> bool:
        """Return True when a provider/model pair is usable for routing."""
        if provider is None:
            return False
        if model:
            return True
        try:
            return bool(provider.get_default_model())
        except Exception:
            return False

    def _routing_provider_key(self, route: Literal["primary", "vision", "secondary"]) -> str:
        """Return configured provider key for the selected route."""
        cfg = self.routing_config
        if cfg is None:
            return route

        if route == "primary":
            key = getattr(cfg, "primary_provider", None)
        elif route == "vision":
            key = getattr(cfg, "vision_provider", None) or getattr(cfg, "vl_provider", None)
        else:
            key = getattr(cfg, "secondary_provider", None) or getattr(cfg, "reasoning_provider", None)

        if isinstance(key, str) and key.strip():
            return key.strip()
        return route

    def _route_target(
        self,
        route: Literal["primary", "vision", "secondary"],
    ) -> tuple[LLMProvider, str, str]:
        """Resolve provider/model/label from configured routing keys."""
        cfg = self.routing_config
        if route == "vision":
            provider = self.vl_provider or self.vision_provider or self.provider
            model = self.vl_model or self.vision_model or self.model
            cfg_model = (
                getattr(cfg, "vision_model", None) if cfg is not None else None
            ) or (getattr(cfg, "vl_model", None) if cfg is not None else None)
        elif route == "secondary":
            provider = self.reasoning_provider or self.provider
            model = self.reasoning_model or self.model
            cfg_model = (
                getattr(cfg, "secondary_model", None) if cfg is not None else None
            ) or (getattr(cfg, "reasoning_model", None) if cfg is not None else None)
        else:
            provider = self.provider
            model = self.model
            cfg_model = getattr(cfg, "primary_model", None) if cfg is not None else None

        if isinstance(cfg_model, str) and cfg_model.strip():
            model = cfg_model.strip()

        return provider, model, self._routing_provider_key(route)

    @staticmethod
    def _parse_router_route(content: str | None) -> Literal["primary", "vision", "secondary"]:
        """Parse route label from router LLM output."""
        text = (content or "").strip().lower()
        if "vision" in text or "custom_vl" in text or "customvl" in text:
            return "vision"
        if "secondary" in text or "reasoning" in text or "fallback" in text or "sota" in text:
            return "secondary"
        return "primary"

    async def _route_with_router_llm(
        self,
        messages: list[dict[str, Any]],
    ) -> Literal["primary", "vision", "secondary"]:
        """Use a tiny router model to classify the request path."""
        user_text = self._extract_latest_user_text(messages)
        if len(user_text) > self._ROUTER_MAX_USER_CHARS:
            user_text = user_text[:self._ROUTER_MAX_USER_CHARS]

        router_messages: list[dict[str, str]] = [
            {"role": "system", "content": self._routing_prompt()},
        ]
        for ex_input, ex_route in self._routing_examples():
            router_messages.append({"role": "user", "content": ex_input})
            router_messages.append({"role": "assistant", "content": ex_route})
        router_messages.append({"role": "user", "content": user_text or "(empty user request)"})

        response = await self.router_provider.chat_with_retry(
            messages=router_messages,
            model=self.router_model,
            max_tokens=8,
            temperature=0.0,
        )
        route = self._parse_router_route(response.content)
        return route

    async def _select_provider_for_request(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[LLMProvider, str, str]:
        """Route a request solely via router LLM, then map to configured route target."""
        default_provider, default_model, default_label = self._route_target("primary")
        if not self._is_route_available(self.router_provider, self.router_model):
            return default_provider, default_model, default_label

        route: Literal["primary", "vision", "secondary"] = "primary"
        try:
            route = await self._route_with_router_llm(messages)
        except Exception as e:
            logger.warning("Router model failed, defaulting to primary route: {}", e)
            return default_provider, default_model, default_label

        if route == "vision" and self._is_route_available(self.vl_provider, self.vl_model):
            return self._route_target("vision")
        if route == "secondary" and self._is_route_available(self.reasoning_provider, self.reasoning_model):
            return self._route_target("secondary")
        return default_provider, default_model, default_label

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            elif cmd == "/status":
                session = self.sessions.get_or_create(msg.session_key)
                await self.bus.publish_outbound(self._status_response(msg, session))
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            # Use -m nanobot instead of sys.argv[0] for Windows compatibility
            # (sys.argv[0] may be just "nanobot" without full path on Windows)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            await self.memory_consolidator.maybe_consolidate_by_tokens(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=0)
            # Subagent results should be assistant role, other system messages use user role
            current_role = "assistant" if msg.sender_id == "subagent" else "user"
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                current_role=current_role,
                metadata=msg.metadata or {},
            )
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, 1 + len(history))
            if self._last_usage:
                session.metadata["last_prompt_tokens"] = int(self._last_usage.get("prompt_tokens", 0) or 0)
                session.metadata["last_completion_tokens"] = int(self._last_usage.get("completion_tokens", 0) or 0)
                session.metadata["last_total_tokens"] = int(self._last_usage.get("total_tokens", 0) or 0)
            self.sessions.save(session)
            self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            snapshot = session.messages[session.last_consolidated:]
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)

            if snapshot:
                self._schedule_background(self.memory_consolidator.archive_messages(snapshot))

            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/status":
            return self._status_response(msg, session)
        if cmd == "/help":
            lines = [
                "🐈 nanobot commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/status — Show bot status",
                "/help — Show available commands",
            ]
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
                metadata={"render_as": "text"},
            )
        await self.memory_consolidator.maybe_consolidate_by_tokens(session)

        tool_channel = msg.channel
        tool_chat_id = msg.chat_id
        msg_meta = msg.metadata or {}

        if msg.channel == "a2a":
            upstream_channel_raw = msg_meta.get("upstream_channel")
            upstream_chat_id_raw = msg_meta.get("upstream_chat_id")
            upstream_channel = (
                str(upstream_channel_raw).strip()
                if isinstance(upstream_channel_raw, str)
                else ""
            )
            upstream_chat_id = (
                str(upstream_chat_id_raw).strip()
                if isinstance(upstream_chat_id_raw, str)
                else ""
            )
            if upstream_channel and upstream_chat_id:
                upstream_channel_enabled = False
                if self.channels_config is not None:
                    section = getattr(self.channels_config, upstream_channel, None)
                    if isinstance(section, dict):
                        upstream_channel_enabled = bool(section.get("enabled", False))
                    elif section is not None:
                        upstream_channel_enabled = bool(getattr(section, "enabled", False))
                if upstream_channel_enabled:
                    tool_channel = upstream_channel
                    tool_chat_id = upstream_chat_id

        self._set_tool_context(tool_channel, tool_chat_id, msg_meta.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=0)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=tool_channel, chat_id=tool_chat_id,
            metadata=msg_meta,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history))
        if self._last_usage:
            session.metadata["last_prompt_tokens"] = int(self._last_usage.get("prompt_tokens", 0) or 0)
            session.metadata["last_completion_tokens"] = int(self._last_usage.get("completion_tokens", 0) or 0)
            session.metadata["last_total_tokens"] = int(self._last_usage.get("total_tokens", 0) or 0)
        self.sessions.save(session)
        self._schedule_background(self.memory_consolidator.maybe_consolidate_by_tokens(session))

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        response_metadata = dict(msg.metadata or {})
        if msg.channel == "a2a":
            # Correlate by delegated task id via direct map lookup (order-independent).
            active_delegation = self.bus.delegation.resolve(response_metadata)
            if active_delegation is not None:
                reply_channel = active_delegation.reply_channel
                reply_chat_id = active_delegation.reply_chat_id
                if reply_channel and reply_chat_id:
                    self.bus.delegation.mark_completed(active_delegation.id)
                    return OutboundMessage(
                        channel=reply_channel,
                        chat_id=reply_chat_id,
                        content=final_content,
                        metadata=response_metadata,
                    )

            upstream_channel_raw = response_metadata.get("upstream_channel")
            upstream_chat_id_raw = response_metadata.get("upstream_chat_id")
            upstream_channel = (
                str(upstream_channel_raw).strip()
                if isinstance(upstream_channel_raw, str)
                else ""
            )
            upstream_chat_id = (
                str(upstream_chat_id_raw).strip()
                if isinstance(upstream_chat_id_raw, str)
                else ""
            )

            upstream_channel_enabled = False
            if upstream_channel and self.channels_config is not None:
                section = getattr(self.channels_config, upstream_channel, None)
                if isinstance(section, dict):
                    upstream_channel_enabled = bool(section.get("enabled", False))
                elif section is not None:
                    upstream_channel_enabled = bool(getattr(section, "enabled", False))

            # If delegation carried original user routing info and that channel is enabled here, respond there directly.
            if upstream_channel and upstream_chat_id and upstream_channel_enabled:
                return OutboundMessage(
                    channel=upstream_channel,
                    chat_id=upstream_chat_id,
                    content=final_content,
                    metadata=response_metadata,
                )

            # Fallback: reply over A2A to sender of the inbound envelope.
            raw_a2a = response_metadata.get("_a2a")
            a2a_ctx = raw_a2a if isinstance(raw_a2a, dict) else {}
            origin_agent = str(a2a_ctx.get("from_agent") or msg.sender_id).strip() or str(msg.sender_id)
            reply_to_base = a2a_ctx.get("reply_to_base")
            if isinstance(reply_to_base, str) and reply_to_base.strip():
                response_metadata["a2a_peer_base"] = reply_to_base.strip()

            return OutboundMessage(
                channel="a2a",
                chat_id=origin_agent,
                content=final_content,
                metadata=response_metadata,
            )

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=response_metadata,
        )

    @staticmethod
    def _image_placeholder(block: dict[str, Any]) -> dict[str, str]:
        """Convert an inline image block into a compact text placeholder."""
        path = (block.get("_meta") or {}).get("path", "")
        return {"type": "text", "text": f"[image: {path}]" if path else "[image]"}

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if (
                block.get("type") == "image_url"
                and block.get("image_url", {}).get("url", "").startswith("data:image/")
            ):
                filtered.append(self._image_placeholder(block))
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if truncate_text and len(text) > self._TOOL_RESULT_MAX_CHARS:
                    text = text[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant":
                if not content and not entry.get("tool_calls"):
                    continue  # skip empty assistant messages — they poison session context
                if self._last_usage:
                    entry["usage"] = {
                        "prompt_tokens": int(self._last_usage.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(self._last_usage.get("completion_tokens", 0) or 0),
                        "total_tokens": int(self._last_usage.get("total_tokens", 0) or 0),
                    }
            if role == "tool":
                if isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(msg, session_key=session_key, on_progress=on_progress)
