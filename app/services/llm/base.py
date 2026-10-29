from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class Message:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(slots=True)
class Embeddings:
    vectors: list[list[float]]
    model: str
    prompt_tokens: int
    latency_ms: int


@dataclass(slots=True)
class StreamResult:
    """Filled in while a stream is consumed; read after the iterator is exhausted."""

    model: str
    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    first_token_ms: int | None = None


class AIProviderError(Exception):
    def __init__(self, message: str, *, transient: bool):
        super().__init__(message)
        self.transient = transient


class LLMProvider(ABC):
    """Single choke point for all model calls (sync for workers, async for the API).

    `purpose` labels the call for metrics/cost tracking and lets the offline
    provider pick a behaviour.
    """

    name: str

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        purpose: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> Completion: ...

    @abstractmethod
    async def acomplete(
        self,
        messages: list[Message],
        *,
        model: str,
        purpose: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> Completion: ...

    @abstractmethod
    def astream(
        self,
        messages: list[Message],
        *,
        model: str,
        purpose: str,
        result: StreamResult,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]: ...

    @abstractmethod
    def embed(self, texts: list[str], *, model: str) -> Embeddings: ...

    @abstractmethod
    async def aembed(self, texts: list[str], *, model: str) -> Embeddings: ...
