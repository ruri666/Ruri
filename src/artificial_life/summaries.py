"""Incremental and final summary services with raw provenance."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .activity_life import ActivityManager
from .conversation import ConversationMemory
from .llm import LLMProvider, MockLLMProvider
from .memory import RawMemory, RawPointer, RawRecord
from .temporal_memory import TemporalMemory


class SummaryService:
    def __init__(
        self,
        memory_root: Path | str,
        *,
        activities: ActivityManager | None = None,
        temporal: TemporalMemory | None = None,
        conversations: ConversationMemory | None = None,
        raw_memory: RawMemory | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.root = Path(memory_root)
        self.raw = raw_memory or RawMemory(self.root)
        self.provider = provider or MockLLMProvider()
        self.activities = activities or ActivityManager(self.root, raw_memory=self.raw)
        self.temporal = temporal or TemporalMemory(self.root, raw_memory=self.raw, provider=self.provider)
        self.conversations = conversations or ConversationMemory(self.root, raw_memory=self.raw, provider=self.provider)

    def summarize_activity(self, activity_id: str, *, final: bool | None = None) -> str:
        activity = self.activities.get(activity_id)
        records = [record for record in self.raw.agent.resolve(activity.raw_pointer)
                   if record.activity_id == activity_id] if activity.raw_pointer else []
        children = [child for child in self.activities.all() if child.parent_activity == activity_id]
        child_snippets = [child.final_summary or child.live_summary or child.summary for child in children]
        source_text = "；".join(record.text for record in records) or "；".join(child_snippets)
        refs = [{"record_id": record.record_id, "stream": record.stream} for record in records]
        refs.extend({"activity_id": child.activity_id} for child in children if child.final_summary or child.live_summary or child.summary)
        source_ids = [record.record_id for record in records] + [child.activity_id for child in children if child.final_summary or child.live_summary or child.summary]
        summary = self._safe_summarize(source_text, purpose="activity_summary", source_refs=refs, scale="event")
        should_finalize = final if final is not None else activity.status in {"completed", "cancelled"}
        if should_finalize:
            self.activities.update_final_summary(activity_id, summary, source_refs=source_ids)
            parent_id = activity.parent_activity
            if parent_id:
                self._update_direct_parent_live(parent_id)
        else:
            self.activities.update_live_summary(activity_id, summary, source_refs=source_ids)
        return summary

    def finalize_activity(self, activity_id: str) -> str:
        return self.summarize_activity(activity_id, final=True)

    def update_temporal_from_raw(self, record: RawRecord, *, use_llm: bool = True) -> None:
        pointer = RawPointer(record.stream, record.record_id, record.record_id)
        if not use_llm:
            # Heartbeats can produce several execution facts.  Preserve them
            # in the day node without making a provider call on every tick;
            # an explicit summary/finalization pass may condense them later.
            self.temporal.record(record, summary_text=record.text, update_parent_summaries=False)
            return
        summary = self._safe_summarize(
            record.text,
            purpose="temporal_day_live",
            source_refs=[{"record_id": record.record_id}],
            scale="day",
        )
        self.temporal.update_live(record.recorded_at, summary, source=pointer)

    def finalize_day(self, day_id: str, *, at: datetime | None = None) -> str:
        node = self.temporal.finalize_day(day_id, at=at)
        return node.final_summary

    def close_idle_conversations(self, now: datetime | None = None) -> list[Any]:
        return self.conversations.close_idle(now)

    def _update_direct_parent_live(self, parent_id: str) -> None:
        parent = self.activities.get(parent_id)
        children = [child for child in self.activities.all() if child.parent_activity == parent_id]
        snippets = [child.final_summary or child.live_summary or child.summary for child in children]
        snippets = [snippet for snippet in snippets if snippet]
        if not snippets:
            return
        source_text = "；".join(snippets)
        summary = self._safe_summarize(
            source_text,
            purpose="activity_parent_live",
            source_refs=[{"activity_id": child.activity_id} for child in children if child.final_summary or child.live_summary or child.summary],
            scale="event",
        )
        self.activities.update_live_summary(
            parent.activity_id,
            summary,
            source_refs=[child.activity_id for child in children],
        )

    def _safe_summarize(
        self,
        source_text: str,
        *,
        purpose: str,
        source_refs: list[dict[str, Any]],
        scale: str,
    ) -> str:
        try:
            result = self.provider.summarize(
                source_text,
                purpose=purpose,
                source_refs=source_refs,
                scale=scale,
            )
            if result.text.strip():
                return result.text.strip()
        except Exception:
            # A provider outage must not roll back a committed Activity fact.
            # The deterministic text is explicitly a degraded summary, not a
            # new historical claim.
            pass
        compact = " ".join(source_text.split())
        if len(compact) > 500:
            compact = compact[:497] + "..."
        prefix = {"event": "事件摘要", "day": "当日摘要", "week": "本周摘要", "month": "本月摘要", "year": "年度摘要"}.get(scale, "摘要")
        return f"{prefix}：{compact}" if compact else f"{prefix}：暂无可总结内容"
