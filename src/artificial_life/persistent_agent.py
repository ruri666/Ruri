"""Persistent Life Agent V1 composition facade."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .activity_life import ActivityManager, ActivityNode
from .clock import Clock, SystemClock
from .context import ContextBuilder, ContextBundle
from .conversation import ConversationMemory
from .core_memory import CoreMemoryStore, DEFAULT_CORE, DEFAULT_USER_CORE
from .llm import LLMProvider, MockLLMProvider
from .memory import RawMemory, RawRecord
from .models import Goal, ScheduleBlock, StateMode, as_utc, iso
from .random_events import RandomEventEngine, RandomEventSpec
from .retrieval import FileTreeRetriever, RetrievalResult
from .runtime import HeartbeatResult, LifeRuntime, RuntimeConfig
from .storage import atomic_write_json
from .summaries import SummaryService
from .temporal_memory import TemporalMemory


@dataclass(frozen=True)
class PersistentHeartbeatResult:
    heartbeat: HeartbeatResult
    random_event: RandomEventSpec | None = None
    interrupted_activity_id: str | None = None
    random_activity_id: str | None = None
    expected_end_activity_ids: tuple[str, ...] = ()


class PersistentLifeAgent:
    """V1 facade connecting the deterministic kernel and file-backed memory."""

    def __init__(
        self,
        root: Path | str,
        *,
        clock: Clock | None = None,
        provider: LLMProvider | None = None,
        random_events: RandomEventEngine | None = None,
    ) -> None:
        self.root = Path(root)
        self.clock = clock or SystemClock()
        self.runtime = LifeRuntime(self.root, clock=self.clock)
        self.memory_root = self.root / "memory"
        self.raw = RawMemory(self.memory_root)
        self.provider = provider or MockLLMProvider()
        self.activities = ActivityManager(self.memory_root, clock=self.clock, raw_memory=self.raw)
        self.temporal = TemporalMemory(self.memory_root, clock=self.clock, raw_memory=self.raw, provider=self.provider)
        self.conversations = ConversationMemory(self.memory_root, clock=self.clock, raw_memory=self.raw, provider=self.provider)
        self.summaries = SummaryService(
            self.memory_root,
            activities=self.activities,
            temporal=self.temporal,
            conversations=self.conversations,
            raw_memory=self.raw,
            provider=self.provider,
        )
        self.contexts = ContextBuilder(
            self.memory_root,
            activities=self.activities,
            temporal=self.temporal,
            conversations=self.conversations,
            raw_memory=self.raw,
        )
        self.retriever = FileTreeRetriever(self.memory_root, raw_memory=self.raw)
        self.core = CoreMemoryStore(self.memory_root / "core_memory.md", title="Core Memory", defaults=DEFAULT_CORE)
        self.user_core = CoreMemoryStore(self.memory_root / "user_core_memory.md", title="User Core Memory", defaults=DEFAULT_USER_CORE)
        self.core.write(self.core.read())
        self.user_core.write(self.user_core.read())
        self.random_events = random_events or RandomEventEngine()
        self.cursor_path = self.memory_root / "cursors.json"
        # Reconcile the durable state projection with the richer Activity
        # tree on every restart.  The timeline remains authoritative; this
        # only repairs projections created by older V1 writes.
        self._sync_dynamic_state(at=self.runtime.state.logical_time)

    @classmethod
    def initialize(
        cls,
        root: Path | str,
        *,
        goals: Iterable[Goal] = (),
        schedule: Iterable[ScheduleBlock] = (),
        clock: Clock | None = None,
        config: RuntimeConfig | None = None,
        provider: LLMProvider | None = None,
        random_events: RandomEventEngine | None = None,
    ) -> "PersistentLifeAgent":
        LifeRuntime.initialize(root, goals=goals, schedule=schedule, clock=clock, config=config)
        return cls(root, clock=clock, provider=provider, random_events=random_events)

    def heartbeat(self, at: datetime | None = None) -> PersistentHeartbeatResult:
        result = self.runtime.heartbeat(at)
        self._append_timeline_events_to_raw(result.events)
        self._sync_raw_memory()
        moment = as_utc(at or self.clock.now())
        self.temporal.finalize_closed_periods(moment)
        expected_end_ids = self._handle_expected_end(moment, result.heartbeat_id)
        self.conversations.close_idle(moment)
        # A replayed same-instant heartbeat is idempotent; it must not create a
        # second random activity for the same heartbeat ID.
        event = None if result.no_op else self.random_events.check(result.heartbeat_id)
        interrupted_id: str | None = None
        random_activity_id: str | None = None
        if event is not None:
            active = next((activity for activity in self.activities.all() if activity.status == "active"), None)
            if active is not None:
                self.activities.interrupt(active.activity_id, event.description, moment)
                interrupted_id = active.activity_id
                self._append_activity_fact("activity.interrupted", self.activities.get(active.activity_id), at=moment, extra={"reason": event.description})
            random_activity_id = f"event:{event.kind}:{result.heartbeat_id}"
            self.activities.create(activity_id=random_activity_id, name=event.name, goal=event.description)
            self._append_activity_fact("activity.planned", self.activities.get(random_activity_id), at=moment)
            self.activities.start(random_activity_id, moment)
            self._append_activity_fact("activity.started", self.activities.get(random_activity_id), at=moment)
            random_events = self.runtime.timeline.append_batch(
                [
                    {
                        "occurred_at": moment,
                        "event_type": "random_event.triggered",
                        "payload": {"kind": event.kind, "name": event.name, "activity_id": random_activity_id, "interrupted_activity_id": interrupted_id},
                        "heartbeat_id": result.heartbeat_id,
                    }
                ]
            )
            self._append_timeline_events_to_raw(random_events)
            self._sync_raw_memory()
        return PersistentHeartbeatResult(result, event, interrupted_id, random_activity_id, tuple(expected_end_ids))

    def create_activity(self, **kwargs: Any) -> ActivityNode:
        activity = self.activities.create(**kwargs)
        self._append_activity_fact("activity.planned", activity)
        return activity

    def start_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.start(activity_id, at)
        self._append_activity_fact("activity.started", activity, at=at)
        self._sync_raw_memory()
        return activity

    def interrupt_activity(self, activity_id: str, reason: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.interrupt(activity_id, reason, at)
        self._append_activity_fact("activity.interrupted", activity, at=at, extra={"reason": reason})
        self._sync_raw_memory()
        return activity

    def resume_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.resume(activity_id, at)
        self._append_activity_fact("activity.resumed", activity, at=at)
        self._sync_raw_memory()
        return activity

    def complete_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.complete(activity_id, at)
        self._append_activity_fact("activity.completed", activity, at=at)
        self.summaries.finalize_activity(activity_id)
        self._sync_raw_memory()
        if activity_id.startswith("event:"):
            self.replan(at=at)
        return self.activities.get(activity_id)

    def receive_user_message(self, text: str, *, conversation_id: str = "default", at: datetime | None = None) -> RawRecord:
        record = self.conversations.append_user(text, conversation_id=conversation_id, at=at)
        self._sync_raw_memory()
        return record

    def send_agent_message(self, text: str, *, conversation_id: str = "default", at: datetime | None = None) -> RawRecord:
        record = self.conversations.append_agent(text, conversation_id=conversation_id, at=at)
        self._sync_raw_memory()
        return record

    def close_idle_conversations(self, at: datetime | None = None) -> list[Any]:
        result = self.conversations.close_idle(at or self.clock.now())
        self._sync_raw_memory()
        return result

    def replan(self, *, query: str | None = None, at: datetime | None = None) -> dict[str, Any]:
        unfinished = [
            {"activity_id": activity.activity_id, "name": activity.name, "status": activity.status}
            for activity in self.activities.unfinished()
        ]
        unfinished.sort(key=lambda item: (0 if item["status"] == "interrupted" else 1 if item["status"] == "active" else 2, item["activity_id"]))
        existing_schedule_ids = {activity.metadata.get("schedule_id") for activity in self.activities.all()}
        scheduled = [
            {
                "schedule_id": block.id,
                "activity_id": f"schedule:{block.id}",
                "name": block.activity_type,
                "status": "scheduled",
                "goal_id": block.goal_id,
            }
            for block in self.runtime.schedule.all()
            if block.status.value == "planned" and block.id not in existing_schedule_ids
        ]
        unfinished.extend(scheduled)
        candidates = unfinished or [{"action": "reserved_free"}]
        context = self.build_self_context(query=query, now=at or self.clock.now()).to_dict()
        result = self.provider.plan(context, candidates)
        selected = result.structured.get("selected", result.text)
        if isinstance(selected, dict):
            selected_id = selected.get("activity_id") or selected.get("action")
        else:
            selected_id = selected
        valid_ids = {candidate.get("activity_id") or candidate.get("action") for candidate in candidates}
        if selected_id not in valid_ids:
            selected = candidates[0]
        else:
            selected = result.structured.get("selected", result.text)
        self.record_agent(
            f"Planning candidates: {json.dumps(candidates, ensure_ascii=False)}; selected: {selected}",
            kind="thought",
            at=at,
        )
        return {"candidates": candidates, "selected": selected, "provider": result.provider}

    def build_self_context(self, *, query: str | None = None, now: datetime | None = None) -> ContextBundle:
        active_id = next((activity.activity_id for activity in self.activities.all() if activity.status == "active"), None)
        return self.contexts.build_self(current_state=self._context_state(active_id), current_activity_id=active_id, query=query, now=now)

    def build_user_context(self, *, conversation_id: str | None = None, query: str | None = None, now: datetime | None = None) -> ContextBundle:
        active_id = next((activity.activity_id for activity in self.activities.all() if activity.status == "active"), None)
        return self.contexts.build_user(current_state=self._context_state(active_id), current_activity_id=active_id, conversation_id=conversation_id, query=query, now=now)

    def retrieve(self, query: str, *, memory_type: str | None = None, limit: int = 5) -> RetrievalResult:
        return self.retriever.retrieve(query, memory_type=memory_type, limit=limit)

    def record_agent(
        self,
        text: str,
        *,
        kind: str = "thought",
        activity_id: str | None = None,
        conversation_id: str | None = None,
        at: datetime | None = None,
        timeline_seq: int | None = None,
    ) -> RawRecord:
        record = self.raw.agent.append(
            recorded_at=at or self.clock.now(),
            kind=kind,
            role="agent",
            text=text,
            activity_id=activity_id,
            conversation_id=conversation_id,
            timeline_seq=timeline_seq,
        )
        self._sync_raw_memory()
        return record

    def merge_user_core(self, section: str, value: str, *, source_refs: list[str] | None = None, at: datetime | None = None) -> dict[str, str]:
        return self.user_core.merge(section, value, source_refs=source_refs, at=at or self.clock.now())

    def merge_core(self, section: str, value: str, *, source_refs: list[str] | None = None, at: datetime | None = None) -> dict[str, str]:
        return self.core.merge(section, value, source_refs=source_refs, at=at or self.clock.now())

    def _sync_raw_memory(self) -> None:
        cursor_data = {}
        if self.cursor_path.exists():
            cursor_data = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        for stream_name, store in (("agent", self.raw.agent), ("user", self.raw.user)):
            records = store.records()
            last_id = cursor_data.get(stream_name)
            start = next((index + 1 for index, record in enumerate(records) if record.record_id == last_id), 0)
            for record in records[start:]:
                # Activity summaries are generated at explicit lifecycle
                # boundaries (for example completion), not for every raw
                # heartbeat fact.
                self.summaries.update_temporal_from_raw(record, use_llm=False)
                cursor_data[stream_name] = record.record_id
        atomic_write_json(self.cursor_path, {"schema_version": 1, **cursor_data})

    def _append_timeline_events_to_raw(self, events: Iterable[Any]) -> None:
        for event in events:
            payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
            self.raw.agent.append(
                recorded_at=event.occurred_at,
                kind="execution",
                role="agent",
                text=f"Timeline {event.event_type}: {payload}",
                activity_id=event.payload.get("activity_id"),
                timeline_seq=event.seq,
                metadata={"timeline_event_id": event.id, "event_type": event.event_type},
            )

    def _context_state(self, active_id: str | None) -> dict[str, Any]:
        state = self.runtime.state.to_dict()
        state["active_activity_id"] = active_id
        state["current_activity_id"] = active_id
        state["mode"] = "working" if active_id else state.get("mode", "reserved_free")
        return state

    def _handle_expected_end(self, moment: datetime, heartbeat_id: str | None) -> list[str]:
        interrupted: list[str] = []
        for activity in list(self.activities.all()):
            if activity.status != "active" or activity.expected_end_time is None or moment < activity.expected_end_time:
                continue
            self.activities.interrupt(activity.activity_id, "expected_end_time reached", moment)
            interrupted.append(activity.activity_id)
            self._append_activity_fact("activity.interrupted", self.activities.get(activity.activity_id), at=moment, extra={"reason": "expected_end_time reached"})
            self.runtime.timeline.append(
                occurred_at=moment,
                event_type="activity.expected_end_reached",
                payload={"activity_id": activity.activity_id, "expected_end_time": iso(activity.expected_end_time)},
                heartbeat_id=heartbeat_id,
            )
        if interrupted:
            self._sync_raw_memory()
        return interrupted

    def _append_activity_fact(
        self,
        event_type: str,
        activity: ActivityNode,
        *,
        at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "activity_id": activity.activity_id,
            "name": activity.name,
            "goal": activity.goal,
            "parent_activity": activity.parent_activity,
            "status": activity.status,
        }
        payload.update(extra or {})
        event = self.runtime.timeline.append(
            occurred_at=as_utc(at or self.clock.now()),
            event_type=event_type,
            payload=payload,
        )
        self._append_timeline_events_to_raw((event,))
        if event_type in {"activity.started", "activity.resumed", "activity.interrupted", "activity.completed", "activity.cancelled"}:
            self._sync_dynamic_state(at=event.occurred_at)

    def _sync_dynamic_state(self, *, at: datetime) -> None:
        """Project the active Activity-tree leaf into the durable State.

        Scheduled Phase 1 activities remain owned by ``LifeRuntime``.  An
        unknown active id therefore denotes a richer Activity-tree activity;
        it is preserved across restart through a normal append-only
        ``state.transition`` fact.
        """

        dynamic_active = next((item for item in self.activities.all() if item.status == "active"), None)
        current_id = self.runtime.state.active_activity_id
        current_is_scheduled = current_id is not None and current_id in self.runtime.activities
        if dynamic_active is None and current_is_scheduled:
            return
        desired_id = dynamic_active.activity_id if dynamic_active is not None else None
        desired_mode = StateMode.WORKING if dynamic_active is not None else StateMode.RESERVED_FREE
        if self.runtime.state.active_activity_id == desired_id and self.runtime.state.mode is desired_mode:
            return
        previous_id = self.runtime.state.active_activity_id
        previous_mode = self.runtime.state.mode
        event = self.runtime.timeline.append(
            occurred_at=as_utc(at),
            event_type="state.transition",
            payload={
                "from": previous_mode.value,
                "to": desired_mode.value,
                "active_activity_id": desired_id,
                "previous_activity_id": previous_id,
                "reason": "activity_tree_projection",
            },
        )
        self.runtime.state.active_activity_id = desired_id
        self.runtime.state.mode = desired_mode
        if as_utc(at) > self.runtime.state.logical_time:
            self.runtime.state.logical_time = as_utc(at)
        self.runtime._persist_projection()
        self._append_timeline_events_to_raw((event,))
