from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from artificial_life import (
    Goal,
    GoalLevel,
    GoalStatus,
    LifeRuntime,
    RawTimeline,
    RuntimeConfig,
    ScheduleBlock,
    ScheduleStatus,
    StateMode,
    TestClock,
    TimelineCorruption,
)


SHANGHAI = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, tzinfo=SHANGHAI)


def writer_plan(rate: float = 0.0):
    large = Goal(
        id="goal-large",
        level=GoalLevel.LARGE,
        title="6个月内完成一部长篇轻小说初稿。",
    )
    medium = Goal(
        id="goal-medium",
        level=GoalLevel.MEDIUM,
        parent_id=large.id,
        title="本周完成当前章节。",
    )
    small = Goal(
        id="goal-small",
        level=GoalLevel.SMALL,
        parent_id=medium.id,
        title="今天上午完成2000字。",
        target_units=2000,
    )
    block = ScheduleBlock(
        id="schedule-writing",
        goal_id=small.id,
        start_at=at(9),
        end_at=at(12),
        activity_type="writing",
        target_units=2000,
        units_per_hour=rate,
    )
    return [large, medium, small], [block]


def test_goal_levels_and_schedule_are_explicitly_separate(tmp_path):
    goals, blocks = writer_plan()
    clock = TestClock(at(8, 59))
    # Input order is not a hidden hierarchy contract.
    runtime = LifeRuntime.initialize(tmp_path / "agent", goals=list(reversed(goals)), schedule=blocks, clock=clock)

    assert [goal.level for goal in runtime.goals.all()] == [GoalLevel.LARGE, GoalLevel.MEDIUM, GoalLevel.SMALL]
    assert runtime.schedule.get("schedule-writing").status is ScheduleStatus.PLANNED
    assert runtime.state.mode is StateMode.RESERVED_FREE


def test_test_clock_is_monotonic_and_deterministic():
    clock = TestClock(at(8))
    assert clock.advance(timedelta(minutes=2)) == at(8, 2)
    clock.set(at(9))
    with pytest.raises(ValueError):
        clock.set(at(8, 59))


def test_raw_timeline_is_append_oriented_and_checksum_validated(tmp_path):
    timeline = RawTimeline(tmp_path / "timeline")
    first = timeline.append(occurred_at=at(9), event_type="one", payload={"value": 1})
    second = timeline.append(occurred_at=at(9, 1), event_type="two", payload={"value": 2})

    assert [event.seq for event in timeline.events()] == [1, 2]
    assert first.id == "evt_000000000001"
    assert second.id == "evt_000000000002"
    path = next((tmp_path / "timeline").rglob("*.jsonl"))
    path.write_text(path.read_text(encoding="utf-8").replace('"value": 1', '"value": 9'), encoding="utf-8")
    with pytest.raises(TimelineCorruption):
        timeline.events()


def test_heartbeat_starts_writing_without_user_message_and_completes_at_1032(tmp_path):
    # 2,000 words in the deterministic 09:00-10:32 interval.
    rate = 2000 * 3600 / (92 * 60)
    goals, blocks = writer_plan(rate)
    clock = TestClock(at(8, 59))
    root = tmp_path / "agent"
    runtime = LifeRuntime.initialize(
        root,
        goals=goals,
        schedule=blocks,
        clock=clock,
        config=RuntimeConfig(heartbeat_interval=timedelta(minutes=1)),
    )

    clock.set(at(9))
    started = runtime.heartbeat()
    assert any(event.event_type == "activity.started" for event in started.events)
    assert runtime.state.mode is StateMode.WORKING
    assert runtime.state.active_activity_id is not None

    # Simulate a process restart while time continues moving with no user input.
    clock.set(at(10, 32))
    restarted = LifeRuntime(root, clock=clock)
    completed = restarted.heartbeat()
    event_types = [event.event_type for event in completed.events]

    assert "time.elapsed" in event_types
    assert "goal.completed" in event_types
    assert "activity.completed" in event_types
    assert restarted.goals.get("goal-small").status is GoalStatus.COMPLETED
    assert restarted.state.mode is StateMode.RESERVED_FREE
    assert restarted.state.active_activity_id is None
    assert len(restarted.activities) == 1
    assert next(iter(restarted.activities.values())).status.value == "completed"

    # The 09:00-12:00 plan remains a plan and is not rewritten as a fact.
    assert restarted.schedule.get("schedule-writing").status is ScheduleStatus.PLANNED
    assert restarted.heartbeat().no_op is True
    assert restarted.state.mode is StateMode.RESERVED_FREE
    timeline_types = [event.event_type for event in restarted.timeline.events()]
    assert timeline_types.count("activity.started") == 1
    assert timeline_types.count("activity.completed") == 1
    assert "schedule.completed" not in timeline_types


def test_persisted_heartbeat_timer_drives_runtime(tmp_path):
    goals, blocks = writer_plan()
    clock = TestClock(at(8, 59))
    runtime = LifeRuntime.initialize(
        tmp_path / "agent",
        goals=goals,
        schedule=blocks,
        clock=clock,
        config=RuntimeConfig(heartbeat_interval=timedelta(minutes=1)),
    )
    clock.advance(timedelta(minutes=1))
    results = runtime.run_due()
    assert len(results) == 1
    assert results[0].state.mode is StateMode.WORKING
    assert results[0].heartbeat_id.startswith("hb_timer_")


def test_explicit_progress_can_complete_a_rate_less_activity(tmp_path):
    goals, blocks = writer_plan(rate=0)
    clock = TestClock(at(8, 59))
    runtime = LifeRuntime.initialize(tmp_path / "agent", goals=goals, schedule=blocks, clock=clock)
    clock.set(at(9))
    runtime.heartbeat()
    clock.set(at(10, 32))
    events = runtime.record_activity_progress(2000)
    assert [event.event_type for event in events][-3:] == ["goal.completed", "activity.completed", "state.transition"]
    assert runtime.state.mode is StateMode.RESERVED_FREE
    assert runtime.schedule.get("schedule-writing").status is ScheduleStatus.PLANNED
