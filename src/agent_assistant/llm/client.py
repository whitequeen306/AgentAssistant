"""OpenAI-compatible LLM client for DeepSeek API.

Provides both sync and async interfaces.
The async interface is the primary one used by the agent loop.
Sync is kept for backward compat (sub-agent, research).
"""

from typing import Any

from agent_assistant.config import env_or_dotenv, settings


class MissingModelConfigError(RuntimeError):
    """未配置模型 API Key 时抛出。

    提示文案面向最终用户：软件本身可以正常打开浏览，只有聊天、调研、
    出题判分这类需要模型的功能才会在调用点报这个错。
    ``user_message`` 供 ``tools.sanitize.public_llm_error`` 识别并原样透传，
    避免被"模型服务调用失败"这类泛化文案盖掉。
    """

    MESSAGE = "尚未配置模型 API Key。请在「设置 → 模型配置」中填写后重试。"
    user_message = MESSAGE

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.MESSAGE)


class LLMClient:
    """Thin wrapper around OpenAI SDK pointing to DeepSeek.

    Lazy-initializes clients on first use to avoid
    import-time failures when API key is not yet configured.
    """

    def __init__(self) -> None:
        self._sync_client = None
        self._async_client = None
        self.model = settings.deepseek_model

    @staticmethod
    def _require_api_key() -> str:
        """返回可用的 API Key，拿不到就抛面向用户的提示。

        先看内存配置（设置页保存后立即生效），再即时读一次 .env
        （与 web_search key 一致：用户补写 .env 后无需重启）。
        """
        key = (settings.deepseek_api_key or "").strip()
        if not key:
            key = env_or_dotenv("DEEPSEEK_API_KEY").strip()
            if key:
                settings.deepseek_api_key = key  # 回填，避免每次都读文件
        if not key:
            raise MissingModelConfigError(MissingModelConfigError.MESSAGE)
        return key

    def _ensure_sync_client(self):
        if self._sync_client is None:
            from openai import OpenAI

            self._sync_client = OpenAI(
                api_key=self._require_api_key(),
                base_url=settings.deepseek_base_url,
            )

    def _ensure_async_client(self):
        if self._async_client is None:
            from openai import AsyncOpenAI

            self._async_client = AsyncOpenAI(
                api_key=self._require_api_key(),
                base_url=settings.deepseek_base_url,
            )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """Synchronous chat completion (for sub-agent / research)."""
        self._ensure_sync_client()
        kwargs = self._build_kwargs(
            messages, tools, temperature, stream, response_format
        )
        return self._sync_client.chat.completions.create(**kwargs)

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        """Async chat completion (primary — used by agent loop)."""
        self._ensure_async_client()
        kwargs = self._build_kwargs(
            messages, tools, temperature, stream, response_format
        )
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
        response_format: dict[str, Any] | None = None,
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
        if response_format:
            # JSON mode ({"type": "json_object"}) — prompt must contain "json".
            kwargs["response_format"] = response_format
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
