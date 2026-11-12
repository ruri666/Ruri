"""Canonical goal and schedule plan documents."""

from __future__ import annotations

from pathlib import Path

from .models import Goal, GoalLevel, GoalStatus, ScheduleBlock, ScheduleStatus
from .storage import atomic_write_json, read_json


class GoalTree:
    def __init__(self, goals: list[Goal] | None = None) -> None:
        self._goals: dict[str, Goal] = {}
        for goal in goals or []:
            if goal.id in self._goals:
                raise ValueError(f"duplicate goal id: {goal.id}")
            self._goals[goal.id] = goal
        self.validate()

    def add(self, goal: Goal) -> None:
        if goal.id in self._goals:
            raise ValueError(f"duplicate goal id: {goal.id}")
        self._goals[goal.id] = goal
        self.validate()

    def get(self, goal_id: str) -> Goal:
        try:
            return self._goals[goal_id]
        except KeyError as exc:
            raise KeyError(f"unknown goal: {goal_id}") from exc

    def all(self) -> list[Goal]:
        return [self._goals[key] for key in sorted(self._goals)]

    def active(self, goal_id: str) -> bool:
        return self.get(goal_id).status in (GoalStatus.PROPOSED, GoalStatus.ACTIVE)

    def apply_progress(self, goal_id: str, units: float) -> bool:
        goal = self.get(goal_id)
        completed = goal.apply_progress(units)
        self.validate()
        return completed

    def complete(self, goal_id: str) -> None:
        self.get(goal_id).complete()
        self.validate()

    def validate(self) -> None:
        for goal in self._goals.values():
            if goal.level is GoalLevel.LARGE:
                if goal.parent_id is not None:
                    raise ValueError("large goals cannot have a parent")
                continue
            if goal.parent_id is None:
                raise ValueError(f"{goal.level.value} goal {goal.id} requires a parent")
            parent = self._goals.get(goal.parent_id)
            if parent is None:
                raise ValueError(f"goal {goal.id} references unknown parent {goal.parent_id}")
            expected_parent = GoalLevel.LARGE if goal.level is GoalLevel.MEDIUM else GoalLevel.MEDIUM
            if parent.level is not expected_parent:
                raise ValueError(f"{goal.level.value} goal {goal.id} must be under a {expected_parent.value} goal")
        for goal_id in self._goals:
            seen: set[str] = set()
            current: str | None = goal_id
            while current is not None:
                if current in seen:
                    raise ValueError(f"cycle in goal tree at {current}")
                seen.add(current)
                parent = self._goals.get(current)
                current = parent.parent_id if parent else None


class SchedulePlan:
    def __init__(self, blocks: list[ScheduleBlock] | None = None) -> None:
        self._blocks: dict[str, ScheduleBlock] = {}
        for block in blocks or []:
            self.add(block)
        self.validate()

    def validate(self) -> None:
        for block in self._blocks.values():
            seen = {block.id}
            current = block
            while current.metadata.get("parent_schedule_id"):
                parent_id = current.metadata["parent_schedule_id"]
                if parent_id in seen:
                    raise ValueError("cycle in schedule tree")
                seen.add(parent_id)
                if parent_id not in self._blocks:
                    raise ValueError(f"unknown schedule parent: {parent_id}")
                parent = self._blocks[parent_id]
                if not parent.start_at <= current.start_at < current.end_at <= parent.end_at:
                    raise ValueError("child schedule must fit its parent window")
                current = parent

    def add(self, block: ScheduleBlock) -> None:
        if block.id in self._blocks:
            raise ValueError(f"duplicate schedule id: {block.id}")
        self._blocks[block.id] = block

    def remove(self, schedule_id: str) -> None:
        if schedule_id in self._blocks:
            del self._blocks[schedule_id]
            self.validate()

    def all(self) -> list[ScheduleBlock]:
        return sorted(self._blocks.values(), key=lambda item: (item.start_at, item.id))

    def get(self, schedule_id: str) -> ScheduleBlock:
        try:
            return self._blocks[schedule_id]
        except KeyError as exc:
            raise KeyError(f"unknown schedule: {schedule_id}") from exc

    def due(self, now) -> list[ScheduleBlock]:
        return [block for block in self.all() if block.contains(now)]


class PlanStore:
    """Read/write goals and schedule without mutating schedule facts."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.goals_path = root / "goals.json"
        self.schedule_path = root / "schedule.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def save_goals(self, goals: GoalTree) -> None:
        goals.validate()
        atomic_write_json(self.goals_path, {"schema_version": 1, "goals": [goal.to_dict() for goal in goals.all()]})

    def load_goals(self) -> GoalTree:
        data = read_json(self.goals_path, {"schema_version": 1, "goals": []})
        if int(data.get("schema_version", 1)) != 1:
            raise ValueError(f"unsupported goals schema_version={data.get('schema_version')}")
        return GoalTree([Goal.from_dict(item) for item in data.get("goals", [])])

    def save_schedule(self, schedule: SchedulePlan) -> None:
        schedule.validate()
        atomic_write_json(
            self.schedule_path,
            {"schema_version": 1, "schedule": [block.to_dict() for block in schedule.all()]},
        )

    def load_schedule(self) -> SchedulePlan:
        data = read_json(self.schedule_path, {"schema_version": 1, "schedule": []})
        if int(data.get("schema_version", 1)) != 1:
            raise ValueError(f"unsupported schedule schema_version={data.get('schema_version')}")
        return SchedulePlan([ScheduleBlock.from_dict(item) for item in data.get("schedule", [])])
