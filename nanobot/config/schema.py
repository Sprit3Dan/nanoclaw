"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.nanobot/workspace"
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    vision_model: str | None = None  # Dedicated model for image inputs (defaults to `model`)
    vision_provider: str | None = None  # Dedicated provider for `vision_model`
    reasoning_router_model: str | None = None  # Tiny classifier model that routes: custom / custom_vl / reasoning
    reasoning_router_provider: str | None = None  # Provider for `reasoning_router_model` (often custom/custom_router)
    reasoning_fallback_model: str | None = None  # SOTA reasoning model for hard tasks (e.g. Anthropic/OpenAI/Gemini)
    reasoning_fallback_provider: str | None = None  # Provider for `reasoning_fallback_model`
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    max_tool_iterations: int = 40
    reasoning_effort: str | None = None  # low / medium / high - enables LLM thinking mode


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class RoutingExample(Base):
    """One optional routing example for few-shot steering."""

    input: str = ""
    route: Literal["custom", "custom_vl", "reasoning"] = "custom"


class RouteDescriptions(Base):
    """Human-readable routing criteria used to construct the router prompt."""

    primary: str = Field(
        default="default choice, questions, tool calls, low level effort reasoning",
        validation_alias=AliasChoices("primary", "custom"),
    )
    vision: str = Field(
        default="describe images, OCR, screenshots, diagrams, and other visual understanding tasks",
        validation_alias=AliasChoices("vision", "vl", "custom_vl"),
    )
    secondary: str = Field(
        default="hardcore reasoning, deep problem solving, and complex multi-step technical analysis",
        validation_alias=AliasChoices("secondary", "reasoning"),
    )


class RoutingConfig(Base):
    """Request-routing policy configuration."""

    # Naming normalized to primary / secondary / vision.
    # Backward compatible aliases:
    # - custom_* -> primary_*
    # - reasoning_* -> secondary_*
    # - vl_* -> vision_*
    primary_share: float = Field(
        default=0.85,
        validation_alias=AliasChoices("primary_share", "primaryShare", "custom_share", "customShare"),
    )

    # Optional per-route provider/model overrides.
    # If unset, nanobot uses existing defaults from `agents.defaults` and provider auto-matching.
    primary_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("primary_provider", "primaryProvider", "custom_provider", "customProvider"),
    )
    primary_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("primary_model", "primaryModel", "custom_model", "customModel"),
    )
    vision_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("vision_provider", "visionProvider", "vl_provider", "vlProvider"),
    )
    vision_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("vision_model", "visionModel", "vl_model", "vlModel"),
    )
    secondary_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices("secondary_provider", "secondaryProvider", "reasoning_provider", "reasoningProvider"),
    )
    secondary_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("secondary_model", "secondaryModel", "reasoning_model", "reasoningModel"),
    )
    router_provider: str | None = None
    router_model: str | None = None
    route_descriptions: RouteDescriptions = Field(
        default_factory=RouteDescriptions,
        validation_alias=AliasChoices("route_descriptions", "routeDescriptions"),
    )

    prompt_override: str | None = None  # Full router system prompt override
    force_primary_patterns: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("force_primary_patterns", "forcePrimaryPatterns", "force_custom_patterns", "forceCustomPatterns"),
    )
    force_vision_patterns: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("force_vision_patterns", "forceVisionPatterns", "force_vl_patterns", "forceVlPatterns"),
    )
    force_secondary_patterns: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("force_secondary_patterns", "forceSecondaryPatterns", "force_reasoning_patterns", "forceReasoningPatterns"),
    )
    examples: list[RoutingExample] = Field(default_factory=list)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    custom_router: ProviderConfig = Field(default_factory=ProviderConfig)  # Dedicated OpenAI-compatible endpoint for reasoning routing
    custom_vision: ProviderConfig = Field(default_factory=ProviderConfig)  # Dedicated OpenAI-compatible vision endpoint
    custom_vl: ProviderConfig = Field(default_factory=ProviderConfig)  # Dedicated OpenAI-compatible vision-language endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    path_append: str = ""

class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools

class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self,
        model: str | None = None,
        forced_provider: str | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from nanobot.providers.registry import PROVIDERS

        forced = forced_provider or self.agents.defaults.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(
        self,
        model: str | None = None,
        forced_provider: str | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, forced_provider=forced_provider)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        forced_provider: str | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, forced_provider=forced_provider)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        forced_provider: str | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, forced_provider=forced_provider)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        forced_provider: str | None = None,
    ) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from nanobot.providers.registry import find_by_name

        p, name = self._match_provider(model, forced_provider=forced_provider)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env
        # to avoid polluting the global litellm.api_base.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    def get_vision_model(self) -> str:
        """Get the configured vision model, falling back to the default text model."""
        return self.agents.defaults.vision_model or self.agents.defaults.model

    def get_vision_provider(self) -> ProviderConfig | None:
        """Get the configured provider for the vision model."""
        return self.get_provider(
            self.get_vision_model(),
            forced_provider=self.agents.defaults.vision_provider,
        )

    def get_vision_provider_name(self) -> str | None:
        """Get the registry name of the configured vision provider."""
        return self.get_provider_name(
            self.get_vision_model(),
            forced_provider=self.agents.defaults.vision_provider,
        )

    def get_vision_api_key(self) -> str | None:
        """Get API key for the configured vision model/provider."""
        return self.get_api_key(
            self.get_vision_model(),
            forced_provider=self.agents.defaults.vision_provider,
        )

    def get_vision_api_base(self) -> str | None:
        """Get API base URL for the configured vision model/provider."""
        return self.get_api_base(
            self.get_vision_model(),
            forced_provider=self.agents.defaults.vision_provider,
        )

    def get_reasoning_router_model(self) -> str | None:
        """Get the configured routing model for complexity classification."""
        return self.agents.defaults.reasoning_router_model

    def get_reasoning_router_provider_name(self) -> str | None:
        """Get the registry name of the configured reasoning router provider."""
        model = self.get_reasoning_router_model()
        forced = self.agents.defaults.reasoning_router_provider
        if not model and not forced:
            return None
        return self.get_provider_name(model, forced_provider=forced)

    def get_reasoning_fallback_model(self) -> str | None:
        """Get the configured fallback model for hard coding/reasoning tasks."""
        return self.agents.defaults.reasoning_fallback_model

    def get_reasoning_fallback_provider_name(self) -> str | None:
        """Get the registry name of the configured reasoning fallback provider."""
        model = self.get_reasoning_fallback_model()
        forced = self.agents.defaults.reasoning_fallback_provider
        if not model and not forced:
            return None
        return self.get_provider_name(model, forced_provider=forced)

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
