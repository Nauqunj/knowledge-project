#!/usr/bin/env python3
"""Unified LLM client for DeepSeek, Qwen and OpenAI compatible endpoints.

The client talks to OpenAI-compatible ``/chat/completions`` APIs through
``httpx`` directly, so no vendor SDK is required.

This is the V3 home of the client; ``pipeline.model_client`` re-exports the
legacy names for backward compatibility.

Environment variables:
    LLM_PROVIDER: Provider name, one of ``deepseek``, ``qwen``, ``openai``.
        Defaults to ``deepseek``.
    LLM_MODEL: Optional model override; falls back to the provider default.
    DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY: The API key for the
        selected provider.

High-level helpers:
    chat(message, ...)                -> (text, Usage)
    chat_json(message, ...)           -> (dict, Usage)
    get_client(...)                   -> OpenAICompatibleProvider
    accumulate_usage(tracker, usage)  -> dict (updated in place)

Example:
    >>> from workflows.model_client import chat, chat_json
    >>> text, usage = chat("Explain RAG in one sentence.")
    >>> result = chat_json("Reply with {\\"ok\\": true}")
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:  # 加载本地 .env（已 gitignore）；文件不存在时静默跳过
    from workflows.env import load_env as _load_env
except ImportError:  # 以脚本方式在 workflows/ 内运行时的回退
    from env import load_env as _load_env

_load_env()

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


# --- cost tracking -----------------------------------------------------------

# Approximate public list prices for domestic models, in CNY per 1M tokens.
# Each value is an ``(input, output)`` tuple; adjust as vendor pricing changes.
CNY_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "deepseek": (1.0, 2.0),
    "qwen": (4.0, 12.0),
    "openai": (150.0, 600.0),
}

UNKNOWN_PRICE: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class CostRecord:
    """One recorded LLM call.

    Attributes:
        provider: Provider that served the call.
        prompt_tokens: Input tokens billed.
        completion_tokens: Output tokens billed.
        total_tokens: Total tokens billed.
        cost: Estimated cost in CNY.
    """

    provider: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float


class CostTracker:
    """Track token usage and estimated cost across LLM calls.

    Prices are expressed in CNY per million tokens.
    """

    def __init__(self, prices: dict[str, tuple[float, float]] | None = None) -> None:
        """Initialise the tracker.

        Args:
            prices: Optional price table overriding :data:`CNY_PRICE_TABLE`.
                Each value is an ``(input, output)`` tuple in CNY per 1M tokens.
        """
        self._prices = dict(prices) if prices else dict(CNY_PRICE_TABLE)
        self._records: list[CostRecord] = []

    @property
    def records(self) -> list[CostRecord]:
        """Return a copy of every recorded call."""
        return list(self._records)

    def price_for(self, provider: str) -> tuple[float, float]:
        """Return the ``(input, output)`` CNY price per 1M tokens.

        Args:
            provider: Provider name to look up.

        Returns:
            The price tuple, or :data:`UNKNOWN_PRICE` when unmapped.
        """
        price = self._prices.get(provider)
        if price is None:
            logger.warning("no CNY price for provider %r; cost treated as 0", provider)
            return UNKNOWN_PRICE
        return price

    def record(self, usage: Usage, provider: str) -> float:
        """Record one API call and return its estimated cost in CNY.

        Args:
            usage: Token usage reported by the provider.
            provider: Provider name used for pricing.

        Returns:
            The estimated cost of this call in CNY.
        """
        input_price, output_price = self.price_for(provider)
        cost = (
            usage.prompt_tokens * input_price + usage.completion_tokens * output_price
        ) / PRICE_PER_MILLION
        self._records.append(
            CostRecord(
                provider=provider,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cost=cost,
            )
        )
        return cost

    def estimated_cost(self, provider: str | None = None) -> float:
        """Return the total estimated cost in CNY.

        Args:
            provider: When given, restrict the total to that provider.

        Returns:
            Accumulated estimated cost in CNY.
        """
        return sum(
            record.cost
            for record in self._records
            if provider is None or record.provider == provider
        )

    def report(self, provider: str | None = None) -> None:
        """Print a per-provider cost report.

        Args:
            provider: When given, report only that provider.
        """
        records = [
            record
            for record in self._records
            if provider is None or record.provider == provider
        ]
        title = f"LLM 成本报告（provider={provider}）" if provider else "LLM 成本报告"
        print(f"\n=== {title} ===")

        if not records:
            print("无调用记录。")
            return

        by_provider: dict[str, list[CostRecord]] = {}
        for record in records:
            by_provider.setdefault(record.provider, []).append(record)

        header = f"{'provider':<10}{'calls':>7}{'input':>12}{'output':>12}{'cost(元)':>12}"
        print(header)
        print("-" * 58)
        for name, group in by_provider.items():
            prompt = sum(item.prompt_tokens for item in group)
            completion = sum(item.completion_tokens for item in group)
            cost = sum(item.cost for item in group)
            print(f"{name:<10}{len(group):>7}{prompt:>12}{completion:>12}{cost:>12.4f}")

        total_prompt = sum(item.prompt_tokens for item in records)
        total_completion = sum(item.completion_tokens for item in records)
        total_cost = sum(item.cost for item in records)
        print("-" * 58)
        print(
            f"{'TOTAL':<10}{len(records):>7}{total_prompt:>12}"
            f"{total_completion:>12}{total_cost:>12.4f}"
        )


# Global tracker shared by all providers. Call ``cost_tracker.report()`` at the
# end of a pipeline run, or import it directly:
#     from pipeline.model_client import cost_tracker
cost_tracker = CostTracker()


# --- budget guard ------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_cost_guard: CostGuard | None = None
_cost_guard_lock = threading.Lock()


def get_cost_guard() -> CostGuard:
    """返回进程级唯一的 :class:`CostGuard`，首次调用时创建。

    预算从环境变量 ``BUDGET_YUAN`` 读取，缺省或非法时回退为 ``1.0``。
    采用双重检查锁，保证并发下只创建一次。

    Returns:
        复用的 :class:`~tests.cost_guard.CostGuard` 实例。
    """
    global _cost_guard
    if _cost_guard is None:
        with _cost_guard_lock:
            if _cost_guard is None:
                from tests.cost_guard import CostGuard

                raw = os.getenv("BUDGET_YUAN", "").strip()
                try:
                    budget = float(raw) if raw else 1.0
                except ValueError:
                    logger.warning("invalid BUDGET_YUAN=%r; falling back to 1.0", raw)
                    budget = 1.0
                _cost_guard = CostGuard(budget_yuan=budget)
    return _cost_guard


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

        cost_tracker.record(usage, self._config.name)

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


# --- high-level helpers ------------------------------------------------------


DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的技术助手，回答简洁、准确、使用中文。"
JSON_INSTRUCTION = "只输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码块。"

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def get_client(
    provider_name: str | None = None,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> OpenAICompatibleProvider:
    """Return a configured LLM client for the given provider.

    Args:
        provider_name: Provider key; defaults to ``LLM_PROVIDER`` or
            ``deepseek``.
        model: Optional model override; defaults to ``LLM_MODEL`` or the
            provider default.
        timeout: Per-request timeout in seconds.

    Returns:
        A ready-to-use :class:`OpenAICompatibleProvider`.

    Raises:
        ValueError: If the provider name is unknown.
        RuntimeError: If the required API key environment variable is unset.
    """
    return get_provider(provider_name, model=model, timeout=timeout)


def _split_provider(
    provider: "LLMProvider | str | None",
) -> tuple[LLMProvider | None, str | None]:
    """Split a provider argument into an instance and/or a name.

    Args:
        provider: A provider instance, a provider name, or ``None``.

    Returns:
        A ``(provider_instance, provider_name)`` tuple.
    """
    if isinstance(provider, str):
        return None, provider
    return provider, None


def chat(
    message: str,
    *,
    system: str | None = None,
    provider: LLMProvider | str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    node_name: str = "unknown",
    **kwargs: Any,
) -> tuple[str, Usage]:
    """Send a single-turn chat request.

    Args:
        message: The user message.
        system: Optional system prompt; defaults to a generic assistant prompt.
        provider: A provider instance, a provider name, or ``None`` to resolve
            from the environment.
        temperature: Optional sampling temperature.
        max_tokens: Optional cap on generated tokens.
        node_name: 流水线节点名，用于成本归集；缺省 ``"unknown"``。
        **kwargs: Extra fields forwarded to the provider.

    Returns:
        A ``(text, usage)`` tuple where ``text`` is the assistant reply and
        ``usage`` is the reported token usage.

    Raises:
        RuntimeError: If the request fails after retries.
        ValueError: If the provider configuration is invalid.
        BudgetExceededError: If the accumulated cost exceeds ``BUDGET_YUAN``.
    """
    messages: list[dict[str, str]] = []
    if system or DEFAULT_SYSTEM_PROMPT:
        messages.append({"role": "system", "content": system or DEFAULT_SYSTEM_PROMPT})
    messages.append({"role": "user", "content": message})

    chat_kwargs: dict[str, Any] = dict(kwargs)
    if temperature is not None:
        chat_kwargs["temperature"] = temperature
    if max_tokens is not None:
        chat_kwargs["max_tokens"] = max_tokens

    provider_obj, provider_name = _split_provider(provider)
    response = chat_with_retry(
        messages,
        provider=provider_obj,
        provider_name=provider_name,
        **chat_kwargs,
    )

    guard = get_cost_guard()
    guard.record(node_name, response.usage, model=response.model)
    guard.check()

    logger.info(
        "chat via %s/%s: %d token(s)",
        response.provider,
        response.model,
        response.usage.total_tokens,
    )
    return response.content, response.usage


def _parse_json(text: str) -> dict | None:
    """Extract the first JSON object from a model response.

    Args:
        text: Raw model output, possibly wrapped in Markdown fences.

    Returns:
        The parsed object, or ``None`` when parsing fails.
    """
    cleaned = _FENCE_RE.sub("", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def chat_json(
    message: str,
    *,
    system: str | None = None,
    provider: LLMProvider | str | None = None,
    node_name: str = "unknown",
    **kwargs: Any,
) -> tuple[dict, Usage]:
    """Send a chat request and parse the reply as a JSON object.

    Args:
        message: The user message describing the JSON to produce.
        system: Optional system prompt; a JSON-only instruction is appended.
        provider: A provider instance, a provider name, or ``None``.
        node_name: 流水线节点名，透传给 :func:`chat` 用于成本归集。
        **kwargs: Extra fields forwarded to :func:`chat`.

    Returns:
        A ``(data, usage)`` tuple where ``data`` is the parsed JSON object and
        ``usage`` is the reported token usage.

    Raises:
        RuntimeError: If the request fails after retries.
        ValueError: If the reply does not contain a JSON object.
        BudgetExceededError: If the accumulated cost exceeds ``BUDGET_YUAN``.
    """
    json_system = f"{system}\n{JSON_INSTRUCTION}" if system else JSON_INSTRUCTION
    text, usage = chat(
        message,
        system=json_system,
        provider=provider,
        node_name=node_name,
        **kwargs,
    )
    data = _parse_json(text)
    if data is None:
        raise ValueError(f"model reply is not valid JSON: {text[:200]!r}")
    return data, usage


def accumulate_usage(
    tracker: dict,
    usage: Usage,
    provider: str | None = None,
) -> dict:
    """Accumulate one call's token usage and cost into ``tracker``.

    Args:
        tracker: A cumulative dict shaped like ``KBState.cost_tracker``; missing
            keys are filled with zeros.
        usage: Token usage from a single call.
        provider: Provider key used for pricing; defaults to ``LLM_PROVIDER`` or
            ``deepseek``.

    Returns:
        The same ``tracker`` dict, updated in place.
    """
    name = (provider or os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
    input_price, output_price = CNY_PRICE_TABLE.get(name, UNKNOWN_PRICE)
    cost = (
        usage.prompt_tokens * input_price + usage.completion_tokens * output_price
    ) / PRICE_PER_MILLION

    tracker["calls"] = tracker.get("calls", 0) + 1
    tracker["prompt_tokens"] = tracker.get("prompt_tokens", 0) + usage.prompt_tokens
    tracker["completion_tokens"] = (
        tracker.get("completion_tokens", 0) + usage.completion_tokens
    )
    tracker["total_tokens"] = tracker.get("total_tokens", 0) + usage.total_tokens
    tracker["cost_cny"] = round(tracker.get("cost_cny", 0.0) + cost, 6)

    by_provider = tracker.setdefault("by_provider", {})
    bucket = by_provider.setdefault(
        name,
        {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_cny": 0.0,
        },
    )
    bucket["calls"] += 1
    bucket["prompt_tokens"] += usage.prompt_tokens
    bucket["completion_tokens"] += usage.completion_tokens
    bucket["total_tokens"] += usage.total_tokens
    bucket["cost_cny"] = round(bucket["cost_cny"] + cost, 6)

    return tracker


__all__ = [
    "CostRecord",
    "CostTracker",
    "DEFAULT_PROVIDER",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "ProviderConfig",
    "PROVIDERS",
    "Usage",
    "accumulate_usage",
    "chat",
    "chat_json",
    "chat_with_retry",
    "cost_tracker",
    "create_provider",
    "estimate_cost",
    "estimate_tokens",
    "get_client",
    "get_cost_guard",
    "get_provider",
    "quick_chat",
]


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
