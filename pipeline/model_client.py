#!/usr/bin/env python3
"""Unified LLM client for DeepSeek, Qwen and OpenAI compatible endpoints.

The client talks to OpenAI-compatible ``/chat/completions`` APIs through
``httpx`` directly, so no vendor SDK is required.

Environment variables:
    LLM_PROVIDER: Provider name, one of ``deepseek``, ``qwen``, ``openai``.
        Defaults to ``deepseek``.
    LLM_MODEL: Optional model override; falls back to the provider default.
    DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY: The API key for the
        selected provider.

Example:
    >>> from pipeline.model_client import quick_chat
    >>> answer = quick_chat("Explain RAG in one sentence.")
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "deepseek"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_TEMPERATURE = 0.7
PRICE_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Usage:
    """Token usage reported by the provider.

    Attributes:
        prompt_tokens: Number of tokens in the request.
        completion_tokens: Number of tokens in the response.
        total_tokens: Sum of prompt and completion tokens.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_api(cls, payload: dict) -> "Usage":
        """Build a ``Usage`` from an OpenAI-style ``usage`` payload.

        Args:
            payload: The ``usage`` object returned by the chat API.

        Returns:
            A normalised :class:`Usage` instance; ``total_tokens`` falls back
            to the sum of prompt and completion tokens when absent.
        """
        prompt = int(payload.get("prompt_tokens", 0) or 0)
        completion = int(payload.get("completion_tokens", 0) or 0)
        total = int(payload.get("total_tokens", prompt + completion) or 0)
        return cls(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


@dataclass
class LLMResponse:
    """A unified chat completion result.

    Attributes:
        content: The assistant message text.
        usage: Token usage statistics.
        model: The model identifier used for the request.
        provider: The provider name that served the request.
        finish_reason: Optional reason the model stopped generating.
    """

    content: str
    usage: Usage
    model: str
    provider: str
    finish_reason: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """Static configuration for an OpenAI-compatible provider.

    Attributes:
        name: Provider key, e.g. ``deepseek``.
        base_url: API root, without the trailing ``/chat/completions``.
        model: Default model identifier.
        api_key_env: Environment variable holding the API key.
        input_price_per_million: USD per 1M prompt tokens.
        output_price_per_million: USD per 1M completion tokens.
    """

    name: str
    base_url: str
    model: str
    api_key_env: str
    input_price_per_million: float
    output_price_per_million: float


# Prices are approximate public list prices in USD per 1M tokens and are meant
# for rough cost estimation only; adjust them as vendor pricing changes.
PROVIDERS: dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        input_price_per_million=0.27,
        output_price_per_million=1.10,
    ),
    "qwen": ProviderConfig(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        api_key_env="DASHSCOPE_API_KEY",
        input_price_per_million=0.40,
        output_price_per_million=1.20,
    ),
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        input_price_per_million=0.15,
        output_price_per_million=0.60,
    ),
}


class LLMProvider(ABC):
    """Abstract interface every provider implementation must satisfy."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model identifier in use."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        """Send a chat completion request.

        Args:
            messages: OpenAI-style message list.
            temperature: Sampling temperature.
            max_tokens: Optional cap on generated tokens.
            **kwargs: Extra fields merged into the request payload.

        Returns:
            The parsed :class:`LLMResponse`.

        Raises:
            httpx.HTTPError: If the HTTP request fails.
            ValueError: If the response payload is malformed.
        """


class OpenAICompatibleProvider(LLMProvider):
    """Provider that talks to any OpenAI-compatible chat endpoint via httpx."""

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        *,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialise the provider.

        Args:
            config: Static provider configuration.
            api_key: Bearer token for authentication.
            model: Optional model override.
            timeout: Per-request timeout in seconds.
        """
        if not api_key:
            raise ValueError(f"empty API key for provider {config.name!r}")
        self._config = config
        self._api_key = api_key
        self._model = model or config.model
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        """Send a chat completion request to the provider.

        Args:
            messages: OpenAI-style message list.
            temperature: Sampling temperature.
            max_tokens: Optional cap on generated tokens.
            **kwargs: Extra fields merged into the request payload.

        Returns:
            The parsed :class:`LLMResponse`.

        Raises:
            httpx.HTTPError: If the HTTP request fails or returns an error.
            ValueError: If the response payload has no usable choice.
        """
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, object] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(kwargs)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"no choices in response from {self._config.name}")
        choice = choices[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = Usage.from_api(data.get("usage") or {})

        return LLMResponse(
            content=content,
            usage=usage,
            model=data.get("model", self._model),
            provider=self._config.name,
            finish_reason=choice.get("finish_reason"),
        )


def get_provider(
    provider_name: str | None = None,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OpenAICompatibleProvider:
    """Build a provider from environment configuration.

    Args:
        provider_name: Provider key; defaults to ``LLM_PROVIDER`` or
            ``deepseek``.
        model: Optional model override; defaults to ``LLM_MODEL`` or the
            provider default.
        timeout: Per-request timeout in seconds.

    Returns:
        A configured :class:`OpenAICompatibleProvider`.

    Raises:
        ValueError: If the provider name is unknown.
        RuntimeError: If the required API key environment variable is unset.
    """
    name = (provider_name or os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
    config = PROVIDERS.get(name)
    if config is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unknown provider {name!r}; expected one of: {known}")

    api_key = os.getenv(config.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"missing API key: set {config.api_key_env} for provider {name!r}"
        )

    resolved_model = model or os.getenv("LLM_MODEL") or config.model
    logger.info(
        "LLM provider=%s model=%s key_from=%s", name, resolved_model, config.api_key_env
    )
    return OpenAICompatibleProvider(
        config, api_key, model=resolved_model, timeout=timeout
    )


def create_provider(
    provider_name: str | None = None,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OpenAICompatibleProvider:
    """Build a provider from environment configuration.

    Thin wrapper around :func:`get_provider` kept as the pipeline entry point.

    Args:
        provider_name: Provider key; defaults to ``LLM_PROVIDER``.
        model: Optional model override.
        timeout: Per-request timeout in seconds.

    Returns:
        A configured :class:`OpenAICompatibleProvider`.
    """
    return get_provider(provider_name, model=model, timeout=timeout)


def chat_with_retry(
    messages: list[dict[str, str]],
    *,
    provider: LLMProvider | None = None,
    provider_name: str | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    **chat_kwargs: object,
) -> LLMResponse:
    """Call ``chat`` with retries and exponential backoff.

    Args:
        messages: OpenAI-style message list.
        provider: An existing provider; built from the environment if omitted.
        provider_name: Provider key used when ``provider`` is omitted.
        max_retries: Total attempts (default 3).
        timeout: Per-request timeout in seconds (default 60).
        backoff_base: Base delay in seconds; doubles each retry.
        **chat_kwargs: Forwarded to :meth:`LLMProvider.chat`.

    Returns:
        The parsed :class:`LLMResponse`.

    Raises:
        RuntimeError: If every attempt fails, chained from the last error.
    """
    active = provider or get_provider(provider_name, timeout=timeout)
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return active.chat(messages, **chat_kwargs)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in (401, 403)
            ):
                config = PROVIDERS.get(active.name)
                env_hint = config.api_key_env if config else "the matching API key"
                raise RuntimeError(
                    f"authentication failed ({exc.response.status_code}) via "
                    f"{active.name!r}; verify {env_hint} is a valid, unexpired key"
                ) from exc
            if attempt >= max_retries:
                break
            delay = backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "chat attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"chat failed after {max_retries} attempts via {active.name!r}: {last_error}"
    ) from last_error


def estimate_tokens(text: str) -> int:
    """Roughly estimate token count for mixed Chinese/English text.

    CJK characters are counted as one token each; other characters are
    approximated at four characters per token. Useful only for budgeting.

    Args:
        text: The text to estimate.

    Returns:
        Estimated token count; ``0`` for empty input.
    """
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    non_cjk = len(text) - cjk
    return cjk + max(1, round(non_cjk / 4))


def estimate_cost(
    usage: Usage,
    *,
    provider_name: str | None = None,
    config: ProviderConfig | None = None,
) -> float:
    """Estimate request cost in USD from token usage.

    Args:
        usage: Token usage to price.
        provider_name: Provider key used to look up pricing when ``config`` is
            omitted; defaults to ``LLM_PROVIDER`` or ``deepseek``.
        config: Explicit pricing config, overriding ``provider_name``.

    Returns:
        Estimated cost in USD.

    Raises:
        ValueError: If the provider name is unknown.
    """
    if config is None:
        name = (provider_name or os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
        config = PROVIDERS.get(name)
        if config is None:
            raise ValueError(f"unknown provider {name!r} for cost estimation")

    return (
        usage.prompt_tokens / PRICE_PER_MILLION * config.input_price_per_million
        + usage.completion_tokens / PRICE_PER_MILLION * config.output_price_per_million
    )


def quick_chat(
    prompt: str,
    *,
    system: str | None = None,
    provider_name: str | None = None,
    **chat_kwargs: object,
) -> str:
    """Send a single prompt and return the assistant text.

    Args:
        prompt: The user message.
        system: Optional system message.
        provider_name: Provider key; defaults to the environment.
        **chat_kwargs: Forwarded to :meth:`LLMProvider.chat`.

    Returns:
        The assistant message content.

    Raises:
        RuntimeError: If the request fails after retries.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = chat_with_retry(messages, provider_name=provider_name, **chat_kwargs)
    cost = estimate_cost(response.usage, provider_name=response.provider)
    logger.info(
        "quick_chat via %s/%s: %d tokens, est. $%.6f",
        response.provider,
        response.model,
        response.usage.total_tokens,
        cost,
    )
    return response.content


def _main() -> None:
    """Smoke-test the client against the configured provider."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("provider from env: %s", os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER))
    try:
        answer = quick_chat("用一句话解释什么是 RAG。")
    except (RuntimeError, ValueError) as exc:
        logger.error("quick_chat failed: %s", exc)
        logger.error(
            "check that LLM_PROVIDER and the matching *_API_KEY are set "
            "(e.g. DEEPSEEK_API_KEY)"
        )
        return
    logger.info("answer: %s", answer)


if __name__ == "__main__":
    _main()
