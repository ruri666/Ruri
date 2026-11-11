"""Persistent Life Agent V1 composition facade."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .activity_life import ActivityManager, ActivityNode, ExecutionSegment
from .clock import Clock, SystemClock
from .context import ContextBuilder, ContextBundle
from .conversation import ConversationMemory
from .core_memory import CoreMemoryStore, DEFAULT_CORE, DEFAULT_USER_CORE
from .dialogue_service import DialogueResponse, DialogueService
from .llm import LLMProvider, default_provider
from .memory import RawMemory, RawRecord
from .models import Activity as RuntimeActivity, ActivityStatus, Goal, ScheduleBlock, StateMode, as_utc, iso
from .planning import PlanningDecision, PlanningService
from .random_events import RandomEventEngine, RandomEventSpec
from .retrieval import FileTreeRetriever, RetrievalResult
from .runtime import HeartbeatResult, LifeRuntime, RuntimeConfig
from .storage import atomic_write_json, read_json
from .summaries import SummaryService
from .temporal_memory import TemporalMemory


@dataclass(frozen=True)
class PersistentHeartbeatResult:
    heartbeat: HeartbeatResult
    random_event: RandomEventSpec | None = None
    interrupted_activity_id: str | None = None
    random_activity_id: str | None = None
    expected_end_activity_ids: tuple[str, ...] = ()
    planning: PlanningDecision | None = None


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
        # All heartbeat, runner, and dialogue writes in one process share this
        # lock.  Timeline append itself remains the durable commit boundary.
        self.lock = threading.RLock()
        self.clock = clock or SystemClock()
        self.runtime = LifeRuntime(self.root, clock=self.clock)
        self.memory_root = self.root / "memory"
        self.raw = RawMemory(self.memory_root)
        self.provider = provider or default_provider()
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
        core_sections = self.core.read()
        profile = read_json(self.root / "persona.json", {})
        if profile:
            core_sections["Identity"] = profile["definition"]
            core_sections["Persona"] = profile["definition"]
        self.core.write(core_sections)
        self.user_core.write(self.user_core.read())
        self.random_events = random_events or RandomEventEngine()
        self.cursor_path = self.memory_root / "cursors.json"
        self.dialogue_checkpoint_root = self.root / "contexts" / "dialogue"
        self.dialogue = DialogueService(self)
        self.planning = PlanningService(self)
        self._migrate_legacy_activity_projection()
        # Reconcile the durable state projection with the richer Activity
        # tree on every restart.  The timeline remains authoritative; this
        # only repairs projections created by older V1 writes.
        self._sync_dynamic_state(at=self.runtime.state.logical_time)
        self._sync_legacy_activity_projection(at=self.runtime.state.logical_time)
        self._recover_dialogue_checkpoints()

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
        with self.lock:
            return self._heartbeat(at)

    def _heartbeat(self, at: datetime | None = None) -> PersistentHeartbeatResult:
        had_active_activity = next((activity for activity in self.activities.all() if activity.status == "active"), None) is not None
        # The persistent facade owns the canonical Activity tree.  The compact
        # runtime only advances time and records heartbeat facts here; Schedule
        # entries are candidates for Planning, never direct execution commands.
        result = self.runtime.heartbeat(at, drive_schedule=False)
        self._append_timeline_events_to_raw(result.events)
        self._sync_raw_memory()
        moment = as_utc(at or self.clock.now())
        self._update_legacy_activity_progress(moment)
        self.temporal.finalize_closed_periods(moment)
        expected_end_ids = self._handle_expected_end(moment, result.heartbeat_id)
        self.conversations.close_idle(moment)
        # A replayed same-instant heartbeat is idempotent; it must not create a
        # second random activity for the same heartbeat ID.
        event = None if result.no_op else self.random_events.check(result.heartbeat_id)
        interrupted_id: str | None = None
        random_activity_id: str | None = None
        if event is not None:
            if getattr(self, "host_decides_activities", False):
                # An environmental stimulus is an observation for the next
                # model decision, not an automatically started activity.
                self._append_dialogue_fact("environment.observed", moment, {"kind": event.kind, "name": event.name,
                    "description": event.description, "heartbeat_id": result.heartbeat_id})
                return PersistentHeartbeatResult(result, event, None, None, tuple(expected_end_ids), None)
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
        planning = self._run_planning_if_needed(
            moment,
            result,
            expected_end_ids,
            event,
            had_active_activity=had_active_activity,
        )
        return PersistentHeartbeatResult(result, event, interrupted_id, random_activity_id, tuple(expected_end_ids), planning)

    def run_due(self, at: datetime | None = None) -> list[PersistentHeartbeatResult]:
        """Fire due timers through the full facade, including memory hooks."""

        with self.lock:
            now = as_utc(at or self.clock.now())
            results: list[PersistentHeartbeatResult] = []
            for timer in self.runtime.timer_store.due(now):
                if timer.command == "heartbeat":
                    result = self._heartbeat(now,)
                    results.append(result)
                self.runtime.timer_store.fire(timer.id, now)
            return results

    def create_activity(self, **kwargs: Any) -> ActivityNode:
        activity = self.activities.create(**kwargs)
        self._append_activity_fact("activity.planned", activity)
        self._sync_legacy_activity_projection(at=as_utc(kwargs.get("planned_start_time") or self.clock.now()))
        return activity

    def start_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        # Validate the target before changing the current activity.
        if self.activities.get(activity_id).status not in {"planned", "interrupted"}:
            raise ValueError("activity cannot be started in its current status")
        for other in self.activities.all():
            if other.status == "active" and other.activity_id != activity_id:
                self.interrupt_activity(other.activity_id, f"switch to {activity_id}", at)
        activity = self.activities.start(activity_id, at)
        self._append_activity_fact("activity.started", activity, at=at)
        self._sync_legacy_activity_projection(at=as_utc(at or self.clock.now()))
        self._sync_raw_memory()
        return activity

    def interrupt_activity(self, activity_id: str, reason: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.interrupt(activity_id, reason, at)
        self._append_activity_fact("activity.interrupted", activity, at=at, extra={"reason": reason})
        self._sync_legacy_activity_projection(at=as_utc(at or self.clock.now()))
        self._sync_raw_memory()
        return activity

    def resume_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        if self.activities.get(activity_id).status != "interrupted":
            raise ValueError("only interrupted activities can be resumed")
        for other in self.activities.all():
            if other.status == "active" and other.activity_id != activity_id:
                self.interrupt_activity(other.activity_id, f"resume {activity_id}", at)
        activity = self.activities.resume(activity_id, at)
        self._append_activity_fact("activity.resumed", activity, at=at)
        self._sync_legacy_activity_projection(at=as_utc(at or self.clock.now()))
        self._sync_raw_memory()
        return activity

    def complete_activity(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.complete(activity_id, at)
        self._append_activity_fact("activity.completed", activity, at=at)
        self.summaries.finalize_activity(activity_id)
        self._sync_legacy_activity_projection(at=as_utc(at or self.clock.now()))
        self._sync_raw_memory()
        if activity_id.startswith("event:"):
            # Completing an interruption event records the next planning
            # decision, but does not synchronously resume another activity.
            # Heartbeat owns autonomous application so the event boundary is
            # not an accidental re-entry point for the previous activity.
            self.replan(at=at, apply=False)
        return self.activities.get(activity_id)

    def cancel_activity(self, activity_id: str, reason: str, at: datetime | None = None) -> ActivityNode:
        activity = self.activities.cancel(activity_id, at)
        self._append_activity_fact("activity.cancelled", activity, at=at, extra={"reason": reason})
        self.summaries.finalize_activity(activity_id)
        self._sync_legacy_activity_projection(at=as_utc(at or self.clock.now()))
        self._sync_raw_memory()
        return self.activities.get(activity_id)

    def receive_user_message(self, text: str, *, conversation_id: str = "default", at: datetime | None = None) -> RawRecord:
        record = self.conversations.append_user(text, conversation_id=conversation_id, at=at)
        self._sync_raw_memory()
        return record

    def send_agent_message(self, text: str, *, conversation_id: str = "default", at: datetime | None = None) -> RawRecord:
        record = self.conversations.append_agent(text, conversation_id=conversation_id, at=at)
        self._sync_raw_memory()
        return record

    def respond_to_user(
        self,
        text: str,
        *,
        conversation_id: str = "default",
        at: datetime | None = None,
    ) -> DialogueResponse:
        """Delegate every synchronous dialogue turn to the unified service."""

        return self.dialogue.respond(text, conversation_id=conversation_id, at=at)

    def stream_to_user(
        self,
        text: str,
        *,
        conversation_id: str = "default",
        at: datetime | None = None,
    ):
        """Yield versioned dialogue stream events for HTTP or other transports."""

        return self.dialogue.stream(text, conversation_id=conversation_id, at=at)

    def configure_heartbeat_interval(self, interval: timedelta, *, at: datetime | None = None) -> None:
        with self.lock:
            self.runtime.configure_heartbeat_interval(interval, at=at)

    def _checkpoint_for_dialogue(self, conversation_id: str, moment: datetime) -> dict[str, Any] | None:
        """Pause one active activity and persist a recoverable checkpoint."""

        dynamic = next((item for item in self.activities.all() if item.status == "active"), None)
        scheduled_id = self.runtime.state.active_activity_id
        scheduled = scheduled_id if scheduled_id in self.runtime.activities else None
        activity_id = dynamic.activity_id if dynamic is not None else scheduled
        if activity_id is None:
            return None
        checkpoint_id = f"dlgchk_{conversation_id}_{moment:%Y%m%dT%H%M%S}_{self.runtime.timeline.last_seq() + 1:06d}"
        checkpoint = {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "conversation_id": conversation_id,
            "created_at": iso(moment),
            "status": "interrupted",
            "activity_kind": "dynamic" if dynamic is not None else "scheduled",
            "activity_id": activity_id,
            "previous_mode": self.runtime.state.mode.value,
        }
        path = self.dialogue_checkpoint_root / conversation_id / "checkpoint.json"
        atomic_write_json(path, checkpoint)
        if dynamic is not None:
            self.activities.interrupt(dynamic.activity_id, "user dialogue", moment)
            self._append_activity_fact(
                "activity.interrupted", self.activities.get(dynamic.activity_id), at=moment, extra={"reason": "user_dialogue", "checkpoint_id": checkpoint_id}
            )
        else:
            events = self.runtime.interrupt_activity(scheduled, moment, reason="user_dialogue")
            self._append_timeline_events_to_raw(events)
            self._sync_raw_memory()
        self._append_dialogue_fact(
            "dialogue.activity_interrupted",
            moment,
            {"conversation_id": conversation_id, "checkpoint_id": checkpoint_id, "activity_id": activity_id},
        )
        return checkpoint

    def _resume_dialogue_checkpoint(self, checkpoint: dict[str, Any] | None, moment: datetime) -> str | None:
        if checkpoint is None:
            return None
        activity_id = str(checkpoint["activity_id"])
        resumed = False
        if checkpoint.get("activity_kind") == "dynamic":
            activity = self.activities.get(activity_id)
            if activity.status == "interrupted":
                self.activities.resume(activity_id, moment)
                self._append_activity_fact(
                    "activity.resumed", self.activities.get(activity_id), at=moment, extra={"reason": "dialogue_finished", "checkpoint_id": checkpoint["checkpoint_id"]}
                )
                resumed = True
        elif activity_id in self.runtime.activities and self.runtime.activities[activity_id].status is not None:
            if self.runtime.activities[activity_id].status.value == "paused":
                events = self.runtime.resume_activity(activity_id, moment)
                self._append_timeline_events_to_raw(events)
                self._sync_raw_memory()
                resumed = True
        path = self.dialogue_checkpoint_root / str(checkpoint["conversation_id"]) / "checkpoint.json"
        updated = dict(checkpoint)
        updated.update({"status": "resumed", "resumed_at": iso(moment)})
        atomic_write_json(path, updated)
        self._append_dialogue_fact(
            "dialogue.activity_resumed",
            moment,
            {"conversation_id": checkpoint["conversation_id"], "checkpoint_id": checkpoint["checkpoint_id"], "activity_id": activity_id, "resumed": resumed},
        )
        return activity_id if resumed else None

    def _recover_dialogue_checkpoints(self) -> None:
        """Resume checkpoints left interrupted by a process restart."""

        if not self.dialogue_checkpoint_root.exists():
            return
        for path in sorted(self.dialogue_checkpoint_root.glob("*/checkpoint.json")):
            try:
                checkpoint = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if checkpoint.get("status") != "interrupted":
                continue
            try:
                self._resume_dialogue_checkpoint(checkpoint, self.runtime.state.logical_time)
            except (KeyError, ValueError):
                # A completed/removed activity is left auditable rather than
                # blocking the entire agent restart.
                updated = dict(checkpoint)
                updated.update({"status": "recovery_failed", "recovered_at": iso(self.runtime.state.logical_time)})
                atomic_write_json(path, updated)

    def _append_dialogue_fact(self, event_type: str, at: datetime, payload: dict[str, Any]) -> None:
        event = self.runtime.timeline.append(occurred_at=at, event_type=event_type, payload=payload)
        self._append_timeline_events_to_raw((event,))
        self._sync_raw_memory()

    def close_idle_conversations(self, at: datetime | None = None) -> list[Any]:
        result = self.conversations.close_idle(at or self.clock.now())
        self._sync_raw_memory()
        return result

    def replan(
        self,
        *,
        query: str | None = None,
        at: datetime | None = None,
        apply: bool = True,
    ) -> dict[str, Any]:
        moment = as_utc(at or self.clock.now())
        candidates = self.planning.candidates(moment)
        if not candidates:
            candidates = [{"action": "reserved_free", "candidate_kind": "fallback"}]
        context = self.build_self_context(query=query, now=moment).to_dict()
        decision = self.planning.decide(context, candidates, reason="explicit_replan", at=moment)
        selected = decision.proposal.to_dict() if decision.proposal else {"action": "reserved_free"}
        self._record_planning_decision(decision, at=moment, apply_requested=apply)
        if decision.accepted and apply:
            try:
                self.planning.apply(decision)
            except (KeyError, ValueError) as exc:
                self._record_planning_rejection(decision, str(exc), at=moment)
        return {
            "candidates": candidates,
            "selected": selected,
            "provider": decision.provider,
            "accepted": decision.accepted,
            "errors": list(decision.errors),
        }

    def _run_planning_if_needed(
        self,
        moment: datetime,
        heartbeat: HeartbeatResult,
        expected_end_ids: list[str],
        random_event: RandomEventSpec | None,
        *,
        had_active_activity: bool,
    ) -> PlanningDecision | None:
        if getattr(self, "host_decides_activities", False) or getattr(self.provider, "provider_name", None) == "harness":
            return None  # the host owns generation and task execution
        active = next((activity for activity in self.activities.all() if activity.status == "active"), None)
        if active is not None:
            return None
        candidates = self.planning.candidates(moment)
        if not candidates:
            return None
        candidates_available = any(item.get("available") is True for item in candidates)
        has_interrupted_activity = any(item.get("status") == "interrupted" for item in candidates)
        if not had_active_activity and not expected_end_ids and random_event is None and not candidates_available and not has_interrupted_activity:
            return None
        fingerprint = self.planning.fingerprint(candidates)
        if any(
            event.event_type == "planning.accepted"
            and event.payload.get("candidate_fingerprint") == fingerprint
            and (event.payload.get("proposal") or {}).get("action") != "reserved_free"
            for event in self.runtime.timeline.events()
        ):
            return None
        context = self.build_self_context(now=moment).to_dict()
        decision = self.planning.decide(context, candidates, reason="heartbeat_gate", at=moment)
        self._record_planning_decision(decision, at=moment)
        if decision.accepted:
            try:
                self.planning.apply(decision)
            except (KeyError, ValueError) as exc:
                self._record_planning_rejection(decision, str(exc), at=moment)
        return decision

    def _record_planning_decision(
        self,
        decision: PlanningDecision,
        *,
        at: datetime,
        apply_requested: bool | None = None,
    ) -> None:
        # ``deferred`` is an audit fact for callers that only want to inspect
        # a proposal.  It must not look like an applied decision, otherwise a
        # later Heartbeat would suppress the same still-unfinished candidate.
        event_type = "planning.accepted" if decision.accepted else "planning.rejected"
        if decision.accepted and apply_requested is False:
            event_type = "planning.deferred"
        payload = decision.to_dict()
        if apply_requested is not None:
            payload["apply_requested"] = apply_requested
        self._append_dialogue_fact(event_type, at, payload)
        self.record_agent(
            f"Planning candidates: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
            kind="thought",
            at=at,
        )

    def _record_planning_rejection(self, decision: PlanningDecision, error: str, *, at: datetime) -> None:
        payload = {**decision.to_dict(), "errors": [*decision.errors, f"application failed: {error}"]}
        self._append_dialogue_fact("planning.rejected", at, payload)

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
            if last_id and records and records[-1].record_id == last_id:
                start = len(records)
            elif last_id:
                start = 0
                for index in range(len(records) - 1, -1, -1):
                    if records[index].record_id == last_id:
                        start = index + 1
                        break
            else:
                start = 0
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

        Persistent Activity-tree transitions are projected into the compact
        runtime state.  The legacy runtime schedule driver is disabled on this
        facade, so the projection never becomes an execution authority and is
        preserved across restart through append-only ``state.transition`` facts.
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

    def _sync_legacy_activity_projection(self, *, at: datetime) -> None:
        """Mirror schedule-linked tree activities for old read-only callers.

        The ActivityManager tree remains canonical.  This projection exists so
        older integrations reading ``runtime.activities`` continue to observe
        progress without allowing the legacy runtime to drive execution.
        """

        moment = as_utc(at)
        changed = False
        for activity in self.activities.all():
            schedule_id = activity.metadata.get("schedule_id")
            if not schedule_id:
                continue
            try:
                block = self.runtime.schedule.get(str(schedule_id))
            except KeyError:
                continue
            shadow = self.runtime.activities.get(activity.activity_id)
            if shadow is None:
                shadow = RuntimeActivity(
                    id=activity.activity_id,
                    goal_id=str(activity.metadata.get("goal_id", block.goal_id)),
                    schedule_id=block.id,
                    activity_type=activity.name,
                    started_at=activity.actual_start_time or moment,
                    target_units=block.target_units,
                    units_per_hour=block.units_per_hour,
                )
                self.runtime.activities[activity.activity_id] = shadow
            shadow.status = {
                "planned": ActivityStatus.READY,
                "active": ActivityStatus.ACTIVE,
                "interrupted": ActivityStatus.PAUSED,
                "completed": ActivityStatus.COMPLETED,
                "cancelled": ActivityStatus.CANCELLED,
            }.get(activity.status, ActivityStatus.READY)
            shadow.last_progress_at = shadow.last_progress_at or shadow.started_at
            shadow.ended_at = activity.actual_end_time
            changed = True
        if changed:
            self.runtime._persist_projection()

    def _migrate_legacy_activity_projection(self) -> None:
        """Import old compact runtime Activities into the canonical tree.

        Older V1 roots may contain only ``state/activities.json`` and Timeline
        facts. The migration creates missing semantic nodes and execution
        segments from that derived snapshot without appending duplicate facts.
        """

        status_map = {
            ActivityStatus.READY: "planned",
            ActivityStatus.ACTIVE: "active",
            ActivityStatus.PAUSED: "interrupted",
            ActivityStatus.COMPLETED: "completed",
            ActivityStatus.CANCELLED: "cancelled",
        }
        segment_status_map = {
            ActivityStatus.ACTIVE: "active",
            ActivityStatus.PAUSED: "interrupted",
            ActivityStatus.COMPLETED: "completed",
            ActivityStatus.CANCELLED: "interrupted",
        }
        for shadow in list(self.runtime.activities.values()):
            if shadow.id in self.activities.tree.nodes:
                continue
            try:
                block = self.runtime.schedule.get(shadow.schedule_id)
            except KeyError:
                block = None
            try:
                goal = self.runtime.goals.get(shadow.goal_id).title
            except KeyError:
                goal = ""
            status = status_map[shadow.status]
            segments: list[ExecutionSegment] = []
            if shadow.status in segment_status_map:
                segments.append(
                    ExecutionSegment(
                        segment_id=f"{shadow.id}:segment:1",
                        activity_id=shadow.id,
                        started_at=shadow.started_at,
                        ended_at=shadow.ended_at,
                        status=segment_status_map[shadow.status],
                        interruption_reason="migrated from compact runtime projection" if shadow.status is ActivityStatus.CANCELLED else None,
                    )
                )
            self.activities.restore_projection(
                activity_id=shadow.id,
                name=shadow.activity_type,
                goal=goal,
                planned_start_time=shadow.started_at,
                expected_end_time=block.end_at if block is not None else None,
                actual_start_time=shadow.started_at if shadow.status is not ActivityStatus.READY else None,
                actual_end_time=shadow.ended_at,
                status=status,
                execution_segments=segments,
                metadata={
                    "schedule_id": shadow.schedule_id,
                    "goal_id": shadow.goal_id,
                    "created_from": "legacy_runtime_projection",
                    "progress_units": shadow.progress_units,
                    "target_units": shadow.target_units,
                    "units_per_hour": shadow.units_per_hour,
                },
            )

    def _update_legacy_activity_progress(self, moment: datetime) -> None:
        """Update only the compatibility projection from elapsed wall time."""

        changed = False
        for activity in self.activities.all():
            if activity.status != "active":
                continue
            shadow = self.runtime.activities.get(activity.activity_id)
            schedule_id = activity.metadata.get("schedule_id")
            if shadow is None or not schedule_id:
                continue
            try:
                block = self.runtime.schedule.get(str(schedule_id))
            except KeyError:
                continue
            last = shadow.last_progress_at or shadow.started_at
            until = min(moment, block.end_at)
            if until <= last or shadow.units_per_hour <= 0:
                continue
            delta = shadow.units_per_hour * (until - last).total_seconds() / 3600.0
            if shadow.target_units is not None:
                delta = min(delta, max(shadow.target_units - shadow.progress_units, 0.0))
            if delta <= 0:
                continue
            shadow.progress_units += delta
            shadow.last_progress_at = until
            changed = True
        if changed:
            self.runtime._persist_projection()
