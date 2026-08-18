"""Deterministic Phase 1 life runtime and heartbeat orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock, SystemClock
from .models import (
    Activity,
    ActivityStatus,
    Goal,
    GoalStatus,
    SCHEMA_VERSION,
    ScheduleStatus,
    State,
    StateMode,
    as_utc,
    iso,
)
from .plans import GoalTree, PlanStore, SchedulePlan
from .storage import atomic_write_json, read_json
from .timeline import RawTimeline, TimelineEvent
from .timers import TimerStore


@dataclass(frozen=True)
class RuntimeConfig:
    heartbeat_interval: timedelta = timedelta(minutes=1)
    default_units_per_hour: float = 0.0
    persona: str = "轻小说作家"

    def __post_init__(self) -> None:
        if self.heartbeat_interval.total_seconds() <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if self.default_units_per_hour < 0:
            raise ValueError("default_units_per_hour cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "heartbeat_interval_seconds": int(self.heartbeat_interval.total_seconds()),
            "default_units_per_hour": self.default_units_per_hour,
            "persona": self.persona,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeConfig":
        version = int(data.get("schema_version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported runtime config schema_version={version}")
        return cls(
            heartbeat_interval=timedelta(seconds=int(data.get("heartbeat_interval_seconds", 60))),
            default_units_per_hour=float(data.get("default_units_per_hour", 0.0)),
            persona=str(data.get("persona", "轻小说作家")),
        )


@dataclass(frozen=True)
class HeartbeatResult:
    heartbeat_id: str | None
    events: tuple[TimelineEvent, ...]
    state: State
    no_op: bool = False


class LifeRuntime:
    """A single-agent, filesystem-backed deterministic runtime.

    The runtime is intentionally single-writer.  Calling ``heartbeat`` is safe
    without a user message; a host scheduler can call ``run_due`` to drive the
    persisted heartbeat timer, while tests use ``TestClock`` and explicit times.
    """

    def __init__(self, root: Path | str, *, clock: Clock | None = None) -> None:
        self.root = Path(root)
        self.clock = clock or SystemClock()
        self.timeline = RawTimeline(self.root / "timeline")
        self.plan_store = PlanStore(self.root / "plans")
        self.timer_store = TimerStore(self.root / "timers" / "timers.json")
        self.state_path = self.root / "state" / "state.json"
        self.activities_path = self.root / "state" / "activities.json"
        self.config_path = self.root / "config" / "runtime.json"
        self.manifest_path = self.root / "manifest.json"
        self.goals: GoalTree
        self.schedule: SchedulePlan
        self.activities: dict[str, Activity]
        self.state: State
        self.config: RuntimeConfig
        self._load()

    @classmethod
    def initialize(
        cls,
        root: Path | str,
        *,
        goals: Iterable[Goal] = (),
        schedule: Iterable[Any] = (),
        clock: Clock | None = None,
        config: RuntimeConfig | None = None,
        agent_id: str = "writer_agent",
    ) -> "LifeRuntime":
        """Create a new agent directory and its initial plan documents."""

        root_path = Path(root)
        if (root_path / "manifest.json").exists():
            raise FileExistsError(f"agent already initialized: {root_path}")
        runtime_clock = clock or SystemClock()
        runtime_config = config or RuntimeConfig()
        now = as_utc(runtime_clock.now())
        goal_tree = GoalTree(list(goals))
        schedule_plan = SchedulePlan(list(schedule))
        for block in schedule_plan.all():
            if not goal_tree.active(block.goal_id):
                raise ValueError(f"schedule {block.id} references inactive or unknown goal {block.goal_id}")
        root_path.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            root_path / "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "agent_id": agent_id,
                "persona": runtime_config.persona,
                "created_at": iso(now),
            },
        )
        atomic_write_json(root_path / "config" / "runtime.json", runtime_config.to_dict())
        plan_store = PlanStore(root_path / "plans")
        plan_store.save_goals(goal_tree)
        plan_store.save_schedule(schedule_plan)
        timeline = RawTimeline(root_path / "timeline")
        timeline.append(occurred_at=now, event_type="runtime.initialized", payload={"agent_id": agent_id})
        for goal in goal_tree.all():
            timeline.append(
                occurred_at=now,
                event_type="goal.created",
                payload={"goal_id": goal.id, "level": goal.level.value, "title": goal.title},
            )
        for block in schedule_plan.all():
            timeline.append(
                occurred_at=now,
                event_type="schedule.created",
                payload={
                    "schedule_id": block.id,
                    "goal_id": block.goal_id,
                    "planned_start": iso(block.start_at),
                    "planned_end": iso(block.end_at),
                },
            )
        runtime = cls(root_path, clock=runtime_clock)
        runtime.timer_store.ensure_heartbeat(now + runtime_config.heartbeat_interval, runtime_config.heartbeat_interval)
        runtime._persist_projection()
        return runtime

    def _load(self) -> None:
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"not an initialized agent directory: {self.root}")
        self.config = RuntimeConfig.from_dict(read_json(self.config_path, RuntimeConfig().to_dict()))
        self.goals = self.plan_store.load_goals()
        self.schedule = self.plan_store.load_schedule()
        events = self.timeline.events()
        if not events:
            raise ValueError("initialized agent has no timeline events")
        self.state, self.activities = self._project(events)
        self._sync_goal_plan(events)
        self._persist_projection()

    def _project(self, events: list[TimelineEvent]) -> tuple[State, dict[str, Activity]]:
        initial_time = events[0].occurred_at
        state = State(logical_time=initial_time)
        activities: dict[str, Activity] = {}
        for event in events:
            payload = event.payload
            if event.event_type == "runtime.initialized":
                state.logical_time = event.occurred_at
            elif event.event_type == "heartbeat.started":
                state.logical_time = event.occurred_at
                state.last_heartbeat_at = event.occurred_at
                state.last_heartbeat_id = payload.get("heartbeat_id", event.heartbeat_id)
            elif event.event_type == "time.elapsed":
                state.logical_time = as_utc(payload["to"])
            elif event.event_type == "activity.started" and {"schedule_id", "activity_type"}.issubset(payload):
                activity = Activity(
                    id=str(payload["activity_id"]),
                    goal_id=str(payload["goal_id"]),
                    schedule_id=str(payload["schedule_id"]),
                    activity_type=str(payload["activity_type"]),
                    started_at=event.occurred_at,
                    target_units=payload.get("target_units"),
                    units_per_hour=float(payload.get("units_per_hour", 0.0)),
                )
                activities[activity.id] = activity
                state.active_activity_id = activity.id
                state.mode = StateMode.WORKING
                state.consumed_schedule_ids.add(activity.schedule_id)
            elif event.event_type == "activity.progressed":
                activity = activities[payload["activity_id"]]
                activity.progress_units = float(payload["total_units"])
                activity.last_progress_at = event.occurred_at
            elif event.event_type in ("activity.completed", "activity.paused", "activity.failed", "activity.cancelled") and payload.get("activity_id") in activities:
                activity = activities[payload["activity_id"]]
                activity.status = ActivityStatus(payload["status"])
                activity.ended_at = event.occurred_at
                state.active_activity_id = None
                state.mode = StateMode.RESERVED_FREE
            elif event.event_type == "goal.progressed":
                # Goal plan synchronization uses absolute totals, so recovery
                # remains idempotent if the mutable plan file was stale.
                pass
            elif event.event_type == "goal.completed":
                state.completed_goal_ids.add(str(payload["goal_id"]))
            elif event.event_type == "state.transition":
                state.mode = StateMode(payload["to"])
                state.active_activity_id = payload.get("active_activity_id")
            elif event.event_type == "heartbeat.completed":
                state.logical_time = event.occurred_at
                state.last_heartbeat_at = event.occurred_at
                state.last_heartbeat_id = payload.get("heartbeat_id", event.heartbeat_id)
        return state, activities

    def _sync_goal_plan(self, events: list[TimelineEvent]) -> None:
        by_id = {goal.id: goal for goal in self.goals.all()}
        changed = False
        for event in events:
            if event.event_type == "goal.progressed":
                goal = by_id.get(event.payload["goal_id"])
                if goal is not None:
                    total = float(event.payload["total_units"])
                    if total != goal.progress_units:
                        goal.progress_units = total
                        changed = True
            elif event.event_type == "goal.completed":
                goal = by_id.get(event.payload["goal_id"])
                if goal is not None and not goal.is_complete:
                    goal.complete()
                    changed = True
        if changed:
            self.plan_store.save_goals(self.goals)

    def _persist_projection(self) -> None:
        atomic_write_json(self.state_path, self.state.to_dict())
        atomic_write_json(
            self.activities_path,
            {"schema_version": SCHEMA_VERSION, "activities": [item.to_dict() for item in self.activities.values()]},
        )

    def _spec(
        self,
        event_type: str,
        at: datetime,
        payload: dict[str, Any],
        heartbeat_id: str,
    ) -> dict[str, Any]:
        return {"event_type": event_type, "occurred_at": at, "payload": payload, "heartbeat_id": heartbeat_id}

    def heartbeat(self, at: datetime | None = None, *, heartbeat_id: str | None = None) -> HeartbeatResult:
        now = as_utc(at or self.clock.now())
        # A repeated heartbeat at the same logical instant is a no-op after the
        # first completed heartbeat, which makes retries safe.
        if self.state.last_heartbeat_id is not None and now <= self.state.logical_time:
            return HeartbeatResult(self.state.last_heartbeat_id, (), self.state, no_op=True)
        hb_id = heartbeat_id or f"hb_{now:%Y%m%dT%H%M%S}_{self.timeline.last_seq() + 1:012d}"
        previous = self.state.logical_time
        specifications: list[dict[str, Any]] = [
            self._spec("heartbeat.started", now, {"heartbeat_id": hb_id, "from": iso(previous)}, hb_id)
        ]
        if now > previous:
            specifications.append(
                self._spec(
                    "time.elapsed",
                    now,
                    {"from": iso(previous), "to": iso(now), "seconds": (now - previous).total_seconds()},
                    hb_id,
                )
            )

        started_this_heartbeat = False
        active = self.activities.get(self.state.active_activity_id) if self.state.active_activity_id else None
        # A PersistentLifeAgent may own an Activity tree outside the compact
        # Phase 1 schedule projection.  Its state transition records the
        # external activity id here, but that id is intentionally absent from
        # ``self.activities``.  Treat that state as occupied so the schedule
        # driver does not start a second activity underneath it.
        if active is None and self.state.active_activity_id is None:
            for block in self.schedule.due(now):
                if block.id in self.state.consumed_schedule_ids or not self.goals.active(block.goal_id):
                    continue
                goal = self.goals.get(block.goal_id)
                activity_id = f"act_{block.id}_{now:%Y%m%dT%H%M%S}"
                target = block.target_units if block.target_units is not None else goal.target_units
                rate = block.units_per_hour or self.config.default_units_per_hour
                active = Activity(
                    id=activity_id,
                    goal_id=block.goal_id,
                    schedule_id=block.id,
                    activity_type=block.activity_type,
                    started_at=now,
                    target_units=target,
                    units_per_hour=rate,
                )
                self.activities[activity_id] = active
                self.state.active_activity_id = activity_id
                self.state.mode = StateMode.WORKING
                self.state.consumed_schedule_ids.add(block.id)
                specifications.extend(
                    [
                        self._spec(
                            "activity.started",
                            now,
                            {
                                "activity_id": activity_id,
                                "goal_id": active.goal_id,
                                "schedule_id": active.schedule_id,
                                "activity_type": active.activity_type,
                                "target_units": active.target_units,
                                "units_per_hour": active.units_per_hour,
                            },
                            hb_id,
                        ),
                        self._spec(
                            "state.transition",
                            now,
                            {"from": StateMode.RESERVED_FREE.value, "to": StateMode.WORKING.value, "active_activity_id": activity_id},
                            hb_id,
                        ),
                    ]
                )
                started_this_heartbeat = True
                break

        if active is not None and active.status is ActivityStatus.ACTIVE and not started_this_heartbeat:
            block = self.schedule.get(active.schedule_id)
            work_until = min(now, block.end_at)
            last = active.last_progress_at or active.started_at
            if work_until > last and active.units_per_hour > 0:
                elapsed_seconds = (work_until - last).total_seconds()
                delta = active.units_per_hour * elapsed_seconds / 3600.0
                remaining = None if active.target_units is None else max(active.target_units - active.progress_units, 0.0)
                completed_at = work_until
                if remaining is not None and delta >= remaining:
                    completion_seconds = remaining / active.units_per_hour * 3600.0
                    completed_at = last + timedelta(seconds=completion_seconds)
                    delta = remaining
                if delta > 0:
                    active.progress_units += delta
                    active.last_progress_at = completed_at
                    specifications.append(
                        self._spec(
                            "activity.progressed",
                            completed_at,
                            {
                                "activity_id": active.id,
                                "delta_units": delta,
                                "total_units": active.progress_units,
                            },
                            hb_id,
                        )
                    )
                    goal = self.goals.get(active.goal_id)
                    goal_completed = self.goals.apply_progress(active.goal_id, delta)
                    specifications.append(
                        self._spec(
                            "goal.progressed",
                            completed_at,
                            {
                                "goal_id": goal.id,
                                "delta_units": delta,
                                "total_units": goal.progress_units,
                            },
                            hb_id,
                        )
                    )
                    if goal_completed:
                        self.state.completed_goal_ids.add(goal.id)
                        specifications.extend(
                            [
                                self._spec("goal.completed", completed_at, {"goal_id": goal.id}, hb_id),
                                self._spec(
                                    "activity.completed",
                                    completed_at,
                                    {"activity_id": active.id, "status": ActivityStatus.COMPLETED.value, "goal_id": active.goal_id, "schedule_id": active.schedule_id},
                                    hb_id,
                                ),
                                self._spec(
                                    "state.transition",
                                    completed_at,
                                    {"from": StateMode.WORKING.value, "to": StateMode.RESERVED_FREE.value, "active_activity_id": None, "reason": "goal_completed"},
                                    hb_id,
                                ),
                            ]
                        )
                        active.status = ActivityStatus.COMPLETED
                        active.ended_at = completed_at
                        self.state.active_activity_id = None
                        self.state.mode = StateMode.RESERVED_FREE
                    elif work_until >= block.end_at and now >= block.end_at:
                        specifications.extend(
                            [
                                self._spec(
                                    "activity.paused",
                                    block.end_at,
                                    {"activity_id": active.id, "status": ActivityStatus.PAUSED.value, "reason": "schedule_ended"},
                                    hb_id,
                                ),
                                self._spec(
                                    "state.transition",
                                    block.end_at,
                                    {"from": StateMode.WORKING.value, "to": StateMode.RESERVED_FREE.value, "active_activity_id": None, "reason": "schedule_ended"},
                                    hb_id,
                                ),
                            ]
                        )
                        active.status = ActivityStatus.PAUSED
                        active.ended_at = block.end_at
                        self.state.active_activity_id = None
                        self.state.mode = StateMode.RESERVED_FREE
            elif work_until >= block.end_at and now >= block.end_at:
                specifications.extend(
                    [
                        self._spec(
                            "activity.paused",
                            block.end_at,
                            {"activity_id": active.id, "status": ActivityStatus.PAUSED.value, "reason": "schedule_ended"},
                            hb_id,
                        ),
                        self._spec(
                            "state.transition",
                            block.end_at,
                            {"from": StateMode.WORKING.value, "to": StateMode.RESERVED_FREE.value, "active_activity_id": None, "reason": "schedule_ended"},
                            hb_id,
                        ),
                    ]
                )
                active.status = ActivityStatus.PAUSED
                active.ended_at = block.end_at
                self.state.active_activity_id = None
                self.state.mode = StateMode.RESERVED_FREE

        specifications.append(self._spec("heartbeat.completed", now, {"heartbeat_id": hb_id}, hb_id))
        events = tuple(self.timeline.append_batch(specifications))
        self.state.logical_time = now
        self.state.last_heartbeat_at = now
        self.state.last_heartbeat_id = hb_id
        self._persist_projection()
        self.plan_store.save_goals(self.goals)
        return HeartbeatResult(hb_id, events, self.state, no_op=False)

    def run_due(self, at: datetime | None = None) -> list[HeartbeatResult]:
        """Fire due persisted timers; intended for a host scheduler/daemon."""

        now = as_utc(at or self.clock.now())
        results: list[HeartbeatResult] = []
        for timer in self.timer_store.due(now):
            if timer.command == "heartbeat":
                result = self.heartbeat(now, heartbeat_id=f"hb_timer_{timer.due_at:%Y%m%dT%H%M%S}")
                results.append(result)
            self.timer_store.fire(timer.id, now)
        return results

    def record_activity_progress(
        self,
        units: float,
        *,
        activity_id: str | None = None,
        at: datetime | None = None,
    ) -> tuple[TimelineEvent, ...]:
        """Record explicit deterministic work progress.

        The deterministic kernel has no LLM or autonomous event generator. A
        host can therefore report an observed writing delta explicitly when a
        schedule has no configured rate. The same append-oriented completion
        rules are used as for rate-based heartbeat progress.
        """

        if units < 0:
            raise ValueError("progress cannot be negative")
        active_id = activity_id or self.state.active_activity_id
        if active_id is None or active_id not in self.activities:
            raise ValueError("there is no active activity")
        active = self.activities[active_id]
        if active.status is not ActivityStatus.ACTIVE:
            raise ValueError(f"activity is not active: {active.status.value}")
        moment = as_utc(at or self.clock.now())
        if moment < self.state.logical_time:
            raise ValueError("progress cannot occur before the runtime logical time")
        remaining = None if active.target_units is None else max(active.target_units - active.progress_units, 0.0)
        delta = units if remaining is None else min(units, remaining)
        if delta == 0:
            return ()
        active.progress_units += delta
        active.last_progress_at = moment
        goal = self.goals.get(active.goal_id)
        goal_completed = self.goals.apply_progress(active.goal_id, delta)
        if active.target_units is not None and active.progress_units >= active.target_units and not goal.is_complete:
            # A schedule may define the concrete Small Goal unit target even
            # when the plan goal is qualitative; reaching that target is still
            # deterministic evidence for completing the linked Small Goal.
            goal.complete()
            goal_completed = True
        hb_id = f"progress_{moment:%Y%m%dT%H%M%S}_{self.timeline.last_seq() + 1:012d}"
        specifications = [
            self._spec(
                "activity.progressed",
                moment,
                {"activity_id": active.id, "delta_units": delta, "total_units": active.progress_units},
                hb_id,
            ),
            self._spec(
                "goal.progressed",
                moment,
                {"goal_id": goal.id, "delta_units": delta, "total_units": goal.progress_units},
                hb_id,
            ),
        ]
        if goal_completed:
            self.state.completed_goal_ids.add(goal.id)
            active.status = ActivityStatus.COMPLETED
            active.ended_at = moment
            self.state.active_activity_id = None
            self.state.mode = StateMode.RESERVED_FREE
            specifications.extend(
                [
                    self._spec("goal.completed", moment, {"goal_id": goal.id}, hb_id),
                    self._spec(
                        "activity.completed",
                        moment,
                        {"activity_id": active.id, "status": ActivityStatus.COMPLETED.value, "goal_id": active.goal_id, "schedule_id": active.schedule_id},
                        hb_id,
                    ),
                    self._spec(
                        "state.transition",
                        moment,
                        {"from": StateMode.WORKING.value, "to": StateMode.RESERVED_FREE.value, "active_activity_id": None, "reason": "goal_completed"},
                        hb_id,
                    ),
                ]
            )
        events = tuple(self.timeline.append_batch(specifications))
        self.state.logical_time = moment
        self._persist_projection()
        self.plan_store.save_goals(self.goals)
        return events

    def mark_goal_complete(self, goal_id: str, at: datetime | None = None) -> TimelineEvent:
        """Explicit deterministic administrative completion, for setup/tests."""

        moment = as_utc(at or self.clock.now())
        goal = self.goals.get(goal_id)
        goal.complete()
        self.state.completed_goal_ids.add(goal_id)
        event = self.timeline.append(occurred_at=moment, event_type="goal.completed", payload={"goal_id": goal_id})
        self.plan_store.save_goals(self.goals)
        self._persist_projection()
        return event
