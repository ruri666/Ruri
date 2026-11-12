"""Pure Phase 1 domain value objects.

These objects contain no filesystem or clock access.  JSON conversion is kept
explicit so persisted data remains versionable and inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import math
from typing import Any


SCHEMA_VERSION = 1


def as_utc(value: datetime | str) -> datetime:
    """Return an aware UTC datetime, rejecting ambiguous naive values."""

    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("datetime values must include a timezone")
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return None if value is None else as_utc(value).isoformat().replace("+00:00", "Z")


class GoalLevel(StrEnum):
    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


class GoalStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ScheduleStatus(StrEnum):
    PLANNED = "planned"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ActivityStatus(StrEnum):
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StateMode(StrEnum):
    RESERVED_FREE = "reserved_free"
    WORKING = "working"


class TimerStatus(StrEnum):
    ACTIVE = "active"
    FIRED = "fired"
    CANCELLED = "cancelled"


@dataclass
class Goal:
    id: str
    level: GoalLevel
    title: str
    parent_id: str | None = None
    target_units: float | None = None
    progress_units: float = 0.0
    status: GoalStatus = GoalStatus.ACTIVE
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.level = GoalLevel(self.level)
        self.status = GoalStatus(self.status)
        if self.deadline is not None:
            self.deadline = as_utc(self.deadline)
        if self.target_units is not None and self.target_units < 0:
            raise ValueError("target_units cannot be negative")
        if self.progress_units < 0:
            raise ValueError("progress_units cannot be negative")

    @property
    def is_complete(self) -> bool:
        return self.status is GoalStatus.COMPLETED

    def apply_progress(self, units: float) -> bool:
        """Apply deterministic progress and return whether this goal completed."""

        if units < 0:
            raise ValueError("progress cannot be negative")
        if self.is_complete:
            return False
        self.progress_units += units
        if self.target_units is not None and self.progress_units >= self.target_units:
            self.progress_units = self.target_units
            self.status = GoalStatus.COMPLETED
            return True
        return False

    def complete(self) -> None:
        if self.status in (GoalStatus.ABANDONED, GoalStatus.BLOCKED):
            raise ValueError(f"cannot complete goal in status {self.status}")
        self.status = GoalStatus.COMPLETED
        if self.target_units is not None:
            self.progress_units = self.target_units

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "level": self.level.value,
            "title": self.title,
            "parent_id": self.parent_id,
            "target_units": self.target_units,
            "progress_units": self.progress_units,
            "status": self.status.value,
            "deadline": iso(self.deadline),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        _check_schema(data)
        return cls(
            id=str(data["id"]),
            level=GoalLevel(data["level"]),
            title=str(data["title"]),
            parent_id=data.get("parent_id"),
            target_units=data.get("target_units"),
            progress_units=float(data.get("progress_units", 0.0)),
            status=GoalStatus(data.get("status", GoalStatus.ACTIVE.value)),
            deadline=data.get("deadline"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class ScheduleBlock:
    id: str
    goal_id: str
    start_at: datetime
    end_at: datetime
    activity_type: str = "writing"
    status: ScheduleStatus = ScheduleStatus.PLANNED
    target_units: float | None = None
    units_per_hour: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.start_at = as_utc(self.start_at)
        self.end_at = as_utc(self.end_at)
        self.status = ScheduleStatus(self.status)
        if self.end_at <= self.start_at:
            raise ValueError("schedule end_at must be after start_at")
        if self.target_units is not None and self.target_units < 0:
            raise ValueError("schedule target_units cannot be negative")
        if self.units_per_hour < 0:
            raise ValueError("units_per_hour cannot be negative")

    def contains(self, moment: datetime) -> bool:
        moment = as_utc(moment)
        return self.status is ScheduleStatus.PLANNED and self.start_at <= moment < self.end_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "goal_id": self.goal_id,
            "start_at": iso(self.start_at),
            "end_at": iso(self.end_at),
            "activity_type": self.activity_type,
            "status": self.status.value,
            "target_units": self.target_units,
            "units_per_hour": self.units_per_hour,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleBlock":
        _check_schema(data)
        return cls(
            id=str(data["id"]),
            goal_id=str(data["goal_id"]),
            start_at=data["start_at"],
            end_at=data["end_at"],
            activity_type=str(data.get("activity_type", "writing")),
            status=ScheduleStatus(data.get("status", ScheduleStatus.PLANNED.value)),
            target_units=data.get("target_units"),
            units_per_hour=float(data.get("units_per_hour", 0.0)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class Activity:
    id: str
    goal_id: str
    schedule_id: str
    activity_type: str
    started_at: datetime
    target_units: float | None = None
    units_per_hour: float = 0.0
    progress_units: float = 0.0
    status: ActivityStatus = ActivityStatus.ACTIVE
    last_progress_at: datetime | None = None
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        self.started_at = as_utc(self.started_at)
        if self.last_progress_at is None:
            self.last_progress_at = self.started_at
        else:
            self.last_progress_at = as_utc(self.last_progress_at)
        if self.ended_at is not None:
            self.ended_at = as_utc(self.ended_at)
        self.status = ActivityStatus(self.status)
        if self.target_units is not None and self.target_units < 0:
            raise ValueError("activity target_units cannot be negative")
        if self.units_per_hour < 0:
            raise ValueError("activity units_per_hour cannot be negative")
        if self.progress_units < 0:
            raise ValueError("activity progress cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "goal_id": self.goal_id,
            "schedule_id": self.schedule_id,
            "activity_type": self.activity_type,
            "started_at": iso(self.started_at),
            "target_units": self.target_units,
            "units_per_hour": self.units_per_hour,
            "progress_units": self.progress_units,
            "status": self.status.value,
            "last_progress_at": iso(self.last_progress_at),
            "ended_at": iso(self.ended_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Activity":
        _check_schema(data)
        return cls(
            id=str(data["id"]),
            goal_id=str(data["goal_id"]),
            schedule_id=str(data["schedule_id"]),
            activity_type=str(data["activity_type"]),
            started_at=data["started_at"],
            target_units=data.get("target_units"),
            units_per_hour=float(data.get("units_per_hour", 0.0)),
            progress_units=float(data.get("progress_units", 0.0)),
            status=ActivityStatus(data.get("status", ActivityStatus.ACTIVE.value)),
            last_progress_at=data.get("last_progress_at"),
            ended_at=data.get("ended_at"),
        )


@dataclass
class State:
    logical_time: datetime
    mode: StateMode = StateMode.RESERVED_FREE
    active_activity_id: str | None = None
    location: str = "digital_workspace"
    emotion: str = "neutral"
    energy: float = 1.0
    health: float = 1.0
    fatigue: float = 0.0
    mood_valence: float = 0.0
    work_streak: int = 0
    last_heartbeat_at: datetime | None = None
    last_heartbeat_id: str | None = None
    consumed_schedule_ids: set[str] = field(default_factory=set)
    completed_goal_ids: set[str] = field(default_factory=set)
    version: int = 1

    def __post_init__(self) -> None:
        self.logical_time = as_utc(self.logical_time)
        self.mode = StateMode(self.mode)
        if not math.isfinite(self.mood_valence) or type(self.work_streak) is not int or self.work_streak < 0:
            raise ValueError("Invalid mood_valence/work_streak")
        for field_name in ("energy", "health", "fatigue"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value):
                raise ValueError(f"{field_name} must be finite")
            setattr(self, field_name, value)
        if self.last_heartbeat_at is not None:
            self.last_heartbeat_at = as_utc(self.last_heartbeat_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "version": self.version,
            "logical_time": iso(self.logical_time),
            "current_time": iso(self.logical_time),
            "mode": self.mode.value,
            "active_activity_id": self.active_activity_id,
            "current_activity_id": self.active_activity_id,
            "location": self.location,
            "emotion": self.emotion,
            "energy": self.energy,
            "health": self.health,
            "fatigue": self.fatigue,
            "mood_valence": self.mood_valence,
            "work_streak": self.work_streak,
            "last_heartbeat_at": iso(self.last_heartbeat_at),
            "last_heartbeat_id": self.last_heartbeat_id,
            "consumed_schedule_ids": sorted(self.consumed_schedule_ids),
            "completed_goal_ids": sorted(self.completed_goal_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        _check_schema(data)
        return cls(
            logical_time=data["logical_time"],
            mode=StateMode(data.get("mode", StateMode.RESERVED_FREE.value)),
            active_activity_id=data.get("active_activity_id"),
            location=str(data.get("location", "digital_workspace")),
            emotion=str(data.get("emotion", "neutral")),
            energy=float(data.get("energy", 1.0)),
            health=float(data.get("health", 1.0)),
            fatigue=float(data.get("fatigue", 0.0)),
            mood_valence=float(data.get("mood_valence", 0.0)),
            work_streak=int(data.get("work_streak", 0)),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            last_heartbeat_id=data.get("last_heartbeat_id"),
            consumed_schedule_ids=set(data.get("consumed_schedule_ids", [])),
            completed_goal_ids=set(data.get("completed_goal_ids", [])),
            version=int(data.get("version", 1)),
        )


@dataclass
class Timer:
    id: str
    due_at: datetime
    command: str = "heartbeat"
    interval_seconds: int | None = None
    status: TimerStatus = TimerStatus.ACTIVE
    last_fired_at: datetime | None = None

    def __post_init__(self) -> None:
        self.due_at = as_utc(self.due_at)
        self.status = TimerStatus(self.status)
        if self.last_fired_at is not None:
            self.last_fired_at = as_utc(self.last_fired_at)
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("timer interval_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "due_at": iso(self.due_at),
            "command": self.command,
            "interval_seconds": self.interval_seconds,
            "status": self.status.value,
            "last_fired_at": iso(self.last_fired_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Timer":
        _check_schema(data)
        return cls(
            id=str(data["id"]),
            due_at=data["due_at"],
            command=str(data.get("command", "heartbeat")),
            interval_seconds=data.get("interval_seconds"),
            status=TimerStatus(data.get("status", TimerStatus.ACTIVE.value)),
            last_fired_at=data.get("last_fired_at"),
        )


def _check_schema(data: dict[str, Any]) -> None:
    version = int(data.get("schema_version", SCHEMA_VERSION))
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version={version}; expected {SCHEMA_VERSION}")
