"""Self and User Context builders with explicit budgets and source refs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .activity_life import ActivityManager, ActivityNode
from .conversation import ConversationMemory
from .core_memory import CoreMemoryStore, DEFAULT_CORE, DEFAULT_USER_CORE
from .memory import RawMemory, RawRecord
from .models import State, iso
from .retrieval import FileTreeRetriever, RetrievalResult
from .temporal_memory import TemporalMemory


@dataclass(frozen=True)
class ContextConfig:
    recent_activity_count: int = 5
    recent_day_count: int = 3
    recent_episode_count: int = 5
    recent_agent_raw_chars: int = 24000
    recent_user_raw_chars: int = 32000


@dataclass(frozen=True)
class ContextBundle:
    kind: str
    base: dict[str, Any]
    working: dict[str, Any]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "base": self.base, "working": self.working, "source_refs": list(self.source_refs)}


class ContextBuilder:
    def __init__(
        self,
        memory_root: Path | str,
        *,
        activities: ActivityManager | None = None,
        temporal: TemporalMemory | None = None,
        conversations: ConversationMemory | None = None,
        raw_memory: RawMemory | None = None,
        config: ContextConfig | None = None,
    ) -> None:
        self.root = Path(memory_root)
        self.raw = raw_memory or RawMemory(self.root)
        self.activities = activities or ActivityManager(self.root, raw_memory=self.raw)
        self.temporal = temporal or TemporalMemory(self.root, raw_memory=self.raw)
        self.conversations = conversations or ConversationMemory(self.root, raw_memory=self.raw)
        self.retriever = FileTreeRetriever(self.root, raw_memory=self.raw)
        self.core = CoreMemoryStore(self.root / "core_memory.md", title="Core Memory", defaults=DEFAULT_CORE)
        self.user_core = CoreMemoryStore(self.root / "user_core_memory.md", title="User Core Memory", defaults=DEFAULT_USER_CORE)
        self.config = config or ContextConfig()

    def build_self(
        self,
        *,
        current_state: State | dict[str, Any],
        current_activity_id: str | None = None,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ContextBundle:
        state = _state_dict(current_state)
        activity_path = self.activities.active_path(current_activity_id)
        leaf_id = activity_path[-1].activity_id if activity_path else current_activity_id
        recent_activities = self._recent_siblings(activity_path[-1] if activity_path else None)
        temporal_path = [node_to_dict(node) for node in self.temporal.active_path(now)]
        recent_temporal = [node_to_dict(node) for node in self.temporal.recent_days(self.config.recent_day_count)]
        recent_raw = self._activity_raw(leaf_id)
        retrieval = self.retriever.retrieve(query, raw_fallback=True) if query else RetrievalResult("", ())
        base = {
            "core_memory": self.core.read(),
            "current_state": state,
            "current_time": iso(now) if now else state.get("logical_time"),
            "temporal_active_path": temporal_path,
            "recent_temporal_summaries": recent_temporal,
        }
        working = {
            "active_activity_path": [activity_to_dict(activity) for activity in activity_path],
            "activity_plan_snapshot": self.activities.plan_snapshot(leaf_id),
            "recent_activity_summaries": [activity_to_dict(activity) for activity in recent_activities],
            "current_activity_recent_raw": _raw_dicts_with_budget(recent_raw, self.config.recent_agent_raw_chars),
            "retrieved_memory": retrieval_to_dict(retrieval),
        }
        source_refs = _unique_refs(
            list(retrieval.source_refs)
            + [f"activity:{activity.activity_id}" for activity in activity_path]
            + [f"temporal:{node.node_id}" for node in self.temporal.active_path(now)]
            + [f"raw:agent:{record.record_id}" for record in recent_raw]
        )
        return ContextBundle("self", base, working, source_refs)

    def build_user(
        self,
        *,
        current_state: State | dict[str, Any],
        conversation_id: str | None = None,
        current_activity_id: str | None = None,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ContextBundle:
        self_context = self.build_self(current_state=current_state, current_activity_id=current_activity_id, query=query, now=now)
        recent_user = [
            record
            for record in (self.raw.user.records() + self.raw.agent.records())
            if conversation_id is None or record.conversation_id == conversation_id
        ]
        recent_user.sort(key=lambda record: record.recorded_at)
        recent_user = recent_user[-50:]
        retrieval = self.retriever.retrieve(query, raw_fallback=True) if query else RetrievalResult("", ())
        base = dict(self_context.base)
        base.update(
            {
                "user_core_memory": self.user_core.read(),
                "conversation_timeline_current_path": [node_to_dict(node) for node in self.conversations.active_path(now)],
                "recent_conversation_episodes": [node_to_dict(node) for node in self.conversations.recent_episodes(self.config.recent_episode_count)],
            }
        )
        working = dict(self_context.working)
        working.update(
            {
                "recent_raw_user_agent_conversation": _raw_dicts_with_budget(recent_user, self.config.recent_user_raw_chars),
                "retrieved_memory": retrieval_to_dict(retrieval),
            }
        )
        source_refs = _unique_refs(
            list(retrieval.source_refs)
            + list(self_context.source_refs)
            + [f"conversation:{node.node_id}" for node in self.conversations.active_path(now)]
            + [f"raw:{record.stream}:{record.record_id}" for record in recent_user]
        )
        return ContextBundle("user", base, working, source_refs)

    def _recent_siblings(self, current: ActivityNode | None) -> list[ActivityNode]:
        if current is None:
            return []
        return [
            activity
            for activity in self.activities.all()
            if activity.parent_activity == current.parent_activity
            and activity.activity_id != current.activity_id
            and activity.status == "completed"
        ][-self.config.recent_activity_count :]

    def _activity_raw(self, activity_id: str | None) -> list[RawRecord]:
        records = self.raw.agent.records()
        if activity_id is None:
            return records[-50:]
        matched = [record for record in records if record.activity_id == activity_id]
        return matched[-100:]


def _state_dict(value: State | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, State):
        return value.to_dict()
    return dict(value)


def activity_to_dict(activity: ActivityNode) -> dict[str, Any]:
    return {
        "activity_id": activity.activity_id,
        "name": activity.name,
        "goal": activity.goal,
        "parent_activity": activity.parent_activity,
        "status": activity.status,
        "summary": activity.final_summary or activity.live_summary or activity.summary,
        "execution_segments": [segment.to_dict() for segment in activity.execution_segments],
    }


def node_to_dict(node: Any) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "title": node.title,
        "parent_id": node.parent_id,
        "status": node.status,
        "summary": node.final_summary or node.live_summary or node.summary,
        "raw_pointer": node.raw_pointer.to_dict() if node.raw_pointer else None,
    }


def retrieval_to_dict(result: RetrievalResult) -> dict[str, Any]:
    return {
        "query": result.query,
        "memory_types": list(result.memory_types),
        "summary_nodes": [node_to_dict(node) for node in result.summary_nodes],
        "raw_records": [record.to_dict() for record in result.raw_records],
        "source_refs": list(result.source_refs),
        "raw_fallback": result.raw_fallback,
    }


def _raw_dicts_with_budget(records: list[RawRecord], budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for record in reversed(records):
        size = len(record.text)
        if selected and used + size > budget:
            break
        selected.append(record.to_dict())
        used += size
    selected.reverse()
    return selected


def _unique_refs(refs: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(refs))
