from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.prompts import CATEGORIES


class Sentiment(BaseModel):
    label: Literal["positive", "neutral", "negative", "mixed"] = "neutral"
    score: float = Field(0.0, ge=-1, le=1)


class Entity(BaseModel):
    name: str
    type: str = "other"


class DocumentAnalysis(BaseModel):
    """Validated shape of the LLM analysis output (lenient where the model tends to drift)."""

    title: str = Field(max_length=512)
    summary: str
    key_points: list[str] = Field(default_factory=list, max_length=12)
    category: str = "other"
    tags: list[str] = Field(default_factory=list)
    sentiment: Sentiment = Field(default_factory=Sentiment)
    entities: list[Entity] = Field(default_factory=list)
    language: str | None = None
    suggested_questions: list[str] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: str | None) -> str:
        v = (v or "other").strip().lower()
        return v if v in CATEGORIES else "other"

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str]:
        seen: list[str] = []
        for t in v or []:
            t = "-".join(str(t).lower().split())[:64]
            if t and t not in seen:
                seen.append(t)
        return seen[:8]

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        return v.strip().lower()[:16] if isinstance(v, str) and v.strip() else None


class CompareResult(BaseModel):
    overview: str
    similarities: list[str] = Field(default_factory=list)
    differences: list[dict] = Field(default_factory=list)
    unique_points: dict[str, list[str]] = Field(default_factory=dict)
    conclusion: str | None = None
