"""OpenAI-compatible LLM client for DeepSeek API.

Provides both sync and async interfaces.
The async interface is the primary one used by the agent loop.
Sync is kept for backward compat (sub-agent, research).
"""

from typing import Any

from agent_assistant.config import settings


class LLMClient:
    """Thin wrapper around OpenAI SDK pointing to DeepSeek.

    Lazy-initializes clients on first use to avoid
    import-time failures when API key is not yet configured.
    """

    def __init__(self) -> None:
        self._sync_client = None
        self._async_client = None
        self.model = settings.deepseek_model

    def _ensure_sync_client(self):
        if self._sync_client is None:
            from openai import OpenAI
            self._sync_client = OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
            )

    def _ensure_async_client(self):
        if self._async_client is None:
            from openai import AsyncOpenAI
            self._async_client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
            )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> Any:
        """Synchronous chat completion (for sub-agent / research)."""
        self._ensure_sync_client()
        kwargs = self._build_kwargs(messages, tools, temperature, stream)
        return self._sync_client.chat.completions.create(**kwargs)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> Any:
        """Async chat completion (primary — used by agent loop)."""
        self._ensure_async_client()
        kwargs = self._build_kwargs(messages, tools, temperature, stream)
        return await self._async_client.chat.completions.create(**kwargs)

    async def achat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> Any:
        """Async streaming chat completion (J12: token-by-token UI)."""
        self._ensure_async_client()
        kwargs = self._build_kwargs(messages, tools, temperature, stream=True)
        return await self._async_client.chat.completions.create(**kwargs)

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # DeepSeek thinking mode: CoT arrives as delta.reasoning_content.
        # When tools are used, that reasoning_content must be echoed back on
        # subsequent assistant messages (API 400 otherwise).
        # V4 Flash defaults to thinking ON if extra_body is omitted — hosted
        # mode must send type=disabled explicitly, not just skip enabled.
        effort = (settings.reasoning_effort or "low").strip().lower()
        if settings.thinking_enabled and effort != "off":
            kwargs["reasoning_effort"] = effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return kwargs


# Singleton (lazy — won't connect until first call)
llm_client = LLMClient()
