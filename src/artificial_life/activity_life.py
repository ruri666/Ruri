"""Dynamic Activity tree and Execution Segment lifecycle."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .memory import RawMemory, RawPointer
from .models import as_utc, iso
from .text_tree import TextTree, TextTreeStore, TreeNode


ACTIVITY_STATUSES = {"planned", "active", "interrupted", "completed", "cancelled"}


@dataclass
class ExecutionSegment:
    segment_id: str
    activity_id: str
    started_at: datetime
    ended_at: datetime | None = None
    status: str = "active"
    interruption_reason: str | None = None

    def __post_init__(self) -> None:
        self.started_at = as_utc(self.started_at)
        if self.ended_at is not None:
            self.ended_at = as_utc(self.ended_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "activity_id": self.activity_id,
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "status": self.status,
            "interruption_reason": self.interruption_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionSegment":
        return cls(
            segment_id=str(value["segment_id"]),
            activity_id=str(value["activity_id"]),
            started_at=value["started_at"],
            ended_at=value.get("ended_at"),
            status=str(value.get("status", "active")),
            interruption_reason=value.get("interruption_reason"),
        )


@dataclass
class ActivityNode:
    activity_id: str
    name: str
    goal: str = ""
    parent_activity: str | None = None
    planned_start_time: datetime | None = None
    expected_end_time: datetime | None = None
    actual_start_time: datetime | None = None
    actual_end_time: datetime | None = None
    status: str = "planned"
    summary: str = ""
    live_summary: str = ""
    final_summary: str = ""
    raw_pointer: RawPointer | None = None
    children: list[str] = field(default_factory=list)
    execution_segments: list[ExecutionSegment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ACTIVITY_STATUSES:
            raise ValueError(f"invalid activity status: {self.status}")
        for field_name in ("planned_start_time", "expected_end_time", "actual_start_time", "actual_end_time"):
            value = getattr(self, field_name)
            if value is not None:
                setattr(self, field_name, as_utc(value))

    def to_tree_node(self) -> TreeNode:
        metadata = dict(self.metadata)
        metadata.update(
            {
                "goal": self.goal,
                "execution_segments": [segment.to_dict() for segment in self.execution_segments],
            }
        )
        return TreeNode(
            node_id=self.activity_id,
            title=self.name,
            parent_id=self.parent_activity,
            status=self.status,
            summary=self.summary,
            live_summary=self.live_summary,
            final_summary=self.final_summary,
            raw_pointer=self.raw_pointer,
            start_time=self.planned_start_time,
            end_time=self.expected_end_time,
            metadata=metadata,
        )

    @classmethod
    def from_tree_node(cls, node: TreeNode) -> "ActivityNode":
        metadata = dict(node.metadata)
        segments = [ExecutionSegment.from_dict(value) for value in metadata.pop("execution_segments", [])]
        return cls(
            activity_id=node.node_id,
            name=node.title,
            goal=str(metadata.pop("goal", "")),
            parent_activity=node.parent_id,
            planned_start_time=node.start_time,
            expected_end_time=node.end_time,
            status=node.status,
            summary=node.summary,
            live_summary=node.live_summary,
            final_summary=node.final_summary,
            raw_pointer=node.raw_pointer,
            execution_segments=segments,
            metadata=metadata,
        )


class ActivityManager:
    """Persist and operate on an arbitrary-depth Activity tree."""

    def __init__(
        self,
        memory_root: Path | str,
        *,
        clock: Clock | None = None,
        raw_memory: RawMemory | None = None,
    ) -> None:
        self.memory_root = Path(memory_root)
        self.clock = clock or SystemClock()
        self.raw = raw_memory or RawMemory(self.memory_root)
        self.store = TextTreeStore(self.memory_root / "activity_memory.md", "activity", "Activity Memory")
        self.tree = self.store.load()

    def _save(self) -> None:
        self.store.save(self.tree)

    def all(self) -> list[ActivityNode]:
        return [self._with_children(ActivityNode.from_tree_node(node)) for node in self.tree.nodes.values()]

    def get(self, activity_id: str) -> ActivityNode:
        return self._with_children(ActivityNode.from_tree_node(self.tree.get(activity_id)))

    def _put(self, activity: ActivityNode) -> ActivityNode:
        self.tree.upsert(activity.to_tree_node())
        self._save()
        return activity

    def create(
        self,
        *,
        activity_id: str,
        name: str,
        goal: str = "",
        parent_activity: str | None = None,
        planned_start_time: datetime | None = None,
        expected_end_time: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActivityNode:
        if activity_id in self.tree.nodes:
            raise ValueError(f"activity already exists: {activity_id}")
        if parent_activity is not None:
            parent = self.get(parent_activity)
            if parent.status == "cancelled":
                raise ValueError("cannot create a child under a cancelled activity")
        activity = ActivityNode(
            activity_id=activity_id,
            name=name,
            goal=goal,
            parent_activity=parent_activity,
            planned_start_time=planned_start_time,
            expected_end_time=expected_end_time,
            metadata=dict(metadata or {}),
        )
        return self._put(activity)

    def start(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        if activity.status not in {"planned", "interrupted"}:
            raise ValueError(f"cannot start activity in status {activity.status}")
        moment = as_utc(at or self.clock.now())
        activity.status = "active"
        activity.actual_start_time = activity.actual_start_time or moment
        segment_id = f"{activity.activity_id}:segment:{len(activity.execution_segments) + 1}"
        activity.execution_segments.append(ExecutionSegment(segment_id, activity.activity_id, moment))
        self._record(activity, f"Activity started: {activity.name}", moment)
        return self._put(activity)

    def interrupt(self, activity_id: str, reason: str, at: datetime | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        if activity.status != "active":
            raise ValueError(f"cannot interrupt activity in status {activity.status}")
        moment = as_utc(at or self.clock.now())
        segment = self._active_segment(activity)
        segment.ended_at = moment
        segment.status = "interrupted"
        segment.interruption_reason = reason
        activity.status = "interrupted"
        self._record(activity, f"Activity interrupted: {reason}", moment)
        return self._put(activity)

    def resume(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        return self.start(activity_id, at)

    def complete(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        if activity.status not in {"active", "interrupted", "planned"}:
            raise ValueError(f"cannot complete activity in status {activity.status}")
        moment = as_utc(at or self.clock.now())
        if activity.status == "active":
            segment = self._active_segment(activity)
            segment.ended_at = moment
            segment.status = "completed"
        activity.status = "completed"
        activity.actual_end_time = moment
        self._record(activity, f"Activity completed: {activity.name}", moment)
        return self._put(activity)

    def cancel(self, activity_id: str, at: datetime | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        if activity.status == "completed":
            raise ValueError("completed activity cannot be cancelled")
        moment = as_utc(at or self.clock.now())
        if activity.status == "active":
            segment = self._active_segment(activity)
            segment.ended_at = moment
            segment.status = "interrupted"
        activity.status = "cancelled"
        activity.actual_end_time = moment
        self._record(activity, f"Activity cancelled: {activity.name}", moment)
        return self._put(activity)

    def unfinished(self) -> list[ActivityNode]:
        return [activity for activity in self.all() if activity.status in {"planned", "active", "interrupted"}]

    def active_path(self, activity_id: str | None = None) -> list[ActivityNode]:
        target = activity_id or next((node.activity_id for node in self.all() if node.status == "active"), None)
        if target is None:
            return []
        return [self._with_children(ActivityNode.from_tree_node(node)) for node in self.tree.path(target)]

    def plan_snapshot(self, activity_id: str | None = None) -> list[dict[str, Any]]:
        target = activity_id or next((node.activity_id for node in self.all() if node.status == "active"), None)
        if target is None:
            return []
        current = self.tree.get(target)
        siblings = self.tree.children(current.parent_id)
        return [
            {"activity_id": node.node_id, "title": node.title, "status": node.status}
            for node in siblings
        ]

    def update_live_summary(self, activity_id: str, summary: str, *, source_refs: list[str] | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        activity.live_summary = summary
        if source_refs is not None:
            activity.metadata["summary_source_refs"] = list(source_refs)
        return self._put(activity)

    def update_final_summary(self, activity_id: str, summary: str, *, source_refs: list[str] | None = None) -> ActivityNode:
        activity = self.get(activity_id)
        activity.final_summary = summary
        activity.summary = summary
        if source_refs is not None:
            activity.metadata["summary_source_refs"] = list(source_refs)
        return self._put(activity)

    def _active_segment(self, activity: ActivityNode) -> ExecutionSegment:
        for segment in reversed(activity.execution_segments):
            if segment.status == "active":
                return segment
        raise ValueError(f"activity {activity.activity_id} has no active execution segment")

    def _with_children(self, activity: ActivityNode) -> ActivityNode:
        activity.children = [child.node_id for child in self.tree.children(activity.activity_id)]
        return activity

    def _record(self, activity: ActivityNode, text: str, moment: datetime) -> None:
        record = self.raw.agent.append(
            recorded_at=moment,
            kind="execution",
            role="agent",
            text=text,
            activity_id=activity.activity_id,
        )
        if activity.raw_pointer is None:
            activity.raw_pointer = RawPointer("agent", record.record_id, record.record_id)
        else:
            activity.raw_pointer = RawPointer("agent", activity.raw_pointer.start_record, record.record_id)
