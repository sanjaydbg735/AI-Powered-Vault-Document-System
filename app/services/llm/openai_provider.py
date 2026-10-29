import time
from collections.abc import AsyncIterator

import openai
from openai import AsyncOpenAI, OpenAI

from app.services.llm.base import AIProviderError, Completion, Embeddings, LLMProvider, Message, StreamResult

_TRANSIENT = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


def _wrap(exc: Exception) -> AIProviderError:
    return AIProviderError(f"{type(exc).__name__}: {exc}", transient=isinstance(exc, _TRANSIENT))


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self, api_key: str, *, base_url: str | None, timeout: float, max_retries: int, embedding_dim: int | None
    ):
        # The SDK already retries 429/5xx/timeouts with exponential backoff + jitter.
        kwargs = dict(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._sync = OpenAI(**kwargs)
        self._async = AsyncOpenAI(**kwargs)
        self._embedding_dim = embedding_dim

    def _embed_params(self, texts: list[str], model: str) -> dict:
        params: dict = dict(model=model, input=texts)
        # text-embedding-3-* can be shortened natively; must match the pgvector column size.
        if self._embedding_dim and model.startswith("text-embedding-3"):
            params["dimensions"] = self._embedding_dim
        return params

    @staticmethod
    def _params(messages, model, temperature, max_tokens, json_mode) -> dict:
        params = dict(
            model=model,
            messages=[m.as_dict() for m in messages],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_mode:
            params["response_format"] = {"type": "json_object"}
        return params

    @staticmethod
    def _to_completion(resp, start: float) -> Completion:
        usage = resp.usage
        return Completion(
            text=resp.choices[0].message.content or "",
            model=resp.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=_ms(start),
        )

    def complete(self, messages, *, model, purpose, temperature=0.2, max_tokens=1024, json_mode=False):
        start = time.perf_counter()
        try:
            resp = self._sync.chat.completions.create(**self._params(messages, model, temperature, max_tokens, json_mode))
        except openai.OpenAIError as exc:
            raise _wrap(exc) from exc
        return self._to_completion(resp, start)

    async def acomplete(self, messages, *, model, purpose, temperature=0.2, max_tokens=1024, json_mode=False):
        start = time.perf_counter()
        try:
            resp = await self._async.chat.completions.create(
                **self._params(messages, model, temperature, max_tokens, json_mode)
            )
        except openai.OpenAIError as exc:
            raise _wrap(exc) from exc
        return self._to_completion(resp, start)

    async def astream(
        self, messages: list[Message], *, model, purpose, result: StreamResult, temperature=0.2, max_tokens=1024
    ) -> AsyncIterator[str]:
        start = time.perf_counter()
        params = self._params(messages, model, temperature, max_tokens, False)
        try:
            stream = await self._async.chat.completions.create(
                **params, stream=True, stream_options={"include_usage": True}
            )
            parts: list[str] = []
            async for event in stream:
                if event.usage:
                    result.prompt_tokens = event.usage.prompt_tokens
                    result.completion_tokens = event.usage.completion_tokens
                if event.model:
                    result.model = event.model
                if event.choices and (delta := event.choices[0].delta.content):
                    if result.first_token_ms is None:
                        result.first_token_ms = _ms(start)
                    parts.append(delta)
                    yield delta
        except openai.OpenAIError as exc:
            raise _wrap(exc) from exc
        result.text = "".join(parts)
        result.latency_ms = _ms(start)

    def embed(self, texts: list[str], *, model: str) -> Embeddings:
        start = time.perf_counter()
        try:
            resp = self._sync.embeddings.create(**self._embed_params(texts, model))
        except openai.OpenAIError as exc:
            raise _wrap(exc) from exc
        return Embeddings([d.embedding for d in resp.data], resp.model, resp.usage.prompt_tokens, _ms(start))

    async def aembed(self, texts: list[str], *, model: str) -> Embeddings:
        start = time.perf_counter()
        try:
            resp = await self._async.embeddings.create(**self._embed_params(texts, model))
        except openai.OpenAIError as exc:
            raise _wrap(exc) from exc
        return Embeddings([d.embedding for d in resp.data], resp.model, resp.usage.prompt_tokens, _ms(start))
