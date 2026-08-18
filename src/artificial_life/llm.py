"""Provider-neutral LLM port with a deterministic test implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMResult:
    text: str
    structured: dict[str, Any] = field(default_factory=dict)
    provider: str = "mock"
    purpose: str = ""


class LLMProvider(Protocol):
    def generate(self, prompt: str, *, purpose: str = "generation", context: dict[str, Any] | None = None) -> LLMResult: ...

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult: ...

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult: ...


class MockLLMProvider:
    """Deterministic provider used by V1 and tests; no network calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, *, purpose: str = "generation", context: dict[str, Any] | None = None) -> LLMResult:
        self.calls.append({"method": "generate", "purpose": purpose, "prompt": prompt, "context": context or {}})
        return LLMResult(text=prompt.strip(), provider="mock", purpose=purpose)

    def summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str = "event",
    ) -> LLMResult:
        self.calls.append(
            {
                "method": "summarize",
                "purpose": purpose,
                "source_text": source_text,
                "source_refs": source_refs,
                "scale": scale,
            }
        )
        compact = " ".join(source_text.split())
        if len(compact) > 500:
            compact = compact[:497] + "..."
        prefix = {"event": "事件摘要", "day": "当日摘要", "week": "本周摘要", "month": "本月摘要", "year": "年度摘要"}.get(scale, "摘要")
        return LLMResult(text=f"{prefix}：{compact}" if compact else f"{prefix}：暂无可总结内容", provider="mock", purpose=purpose)

    def plan(self, context: dict[str, Any], candidates: list[dict[str, Any]]) -> LLMResult:
        self.calls.append({"method": "plan", "context": context, "candidates": candidates})
        selected = candidates[0] if candidates else {"action": "reserved_free"}
        return LLMResult(text=str(selected), structured={"selected": selected}, provider="mock", purpose="planning")

