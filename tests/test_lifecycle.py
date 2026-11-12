import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from artificial_life import PersistentLifeAgent, MockLLMProvider, TestClock
from artificial_life.host import Interrupted
from artificial_life.lifecycle import Lifecycle
from artificial_life.plugin import PluginService


class MockHost:
    """Explicit deterministic host fixture. Never launches a model."""
    name = "mock-host"
    def __init__(self):
        self.calls = []
    def run(self, request, cancel):
        self.calls.append(request)
        if request["kind"] == "plan":
            return dict(name="独立创作", goal="实际完成两个步骤", steps=["写片段", "检查片段"])
        if request["kind"] == "reply":
            return {"reply": "收到，我先回答，再继续。"}
        return {"body": "这是由显式 MockHost 测试夹具生成的创作成果，不是线上模型实测。"}


def fixture(root, host=None):
    clock = TestClock(datetime(2026, 10, 23, 13, tzinfo=timezone.utc))
    agent = PersistentLifeAgent.initialize(root, clock=clock, provider=MockLLMProvider())
    service = PluginService(agent)
    lifecycle = Lifecycle(service, host or MockHost(), interval=30, mode="rhythm")
    return service, lifecycle, clock


def test_autonomous_plan_work_complete_and_next_cycle_without_user(tmp_path):
    service, life, clock = fixture(tmp_path)
    life.tick()  # No user input or seeded schedule: host plans real future work.
    assert len(service.agent.activities.all()) == 3
    snapshot = service.call("structure_get")
    assert any(n["parent_id"] and n["node_id"].startswith("schedule:") for n in snapshot["schedule_tree"])
    life.tick()
    assert len(list((tmp_path / "works/autonomous").glob("*.md"))) == 1
    life.tick()
    assert len(life.host.calls) == 2  # idle does not spend another model call
    clock.set(clock.now()+timedelta(seconds=31))
    life.tick()
    assert all(a.status == "completed" for a in service.agent.activities.all())
    assert len(list((tmp_path / "works/autonomous").glob("*.md"))) == 2
    clock.set(clock.now()+timedelta(seconds=31))
    life.tick()
    assert len(service.agent.activities.all()) == 6
    assert not service.agent.raw.user.records()


def test_pending_user_never_interrupts_autonomous_model(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class ConcurrentHost(MockHost):
        def run(self, request, cancel):
            if request["kind"] == "work":
                entered.set()
                assert release.wait(5)
                assert not cancel.is_set()
            return super().run(request, cancel)
    service, life, _ = fixture(tmp_path, ConcurrentHost())
    life.tick()
    failures = []
    def work():
        try:
            life.tick()
        except Interrupted:
            failures.append("interrupted")
    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(5)
    active = next(a for a in service.agent.activities.all() if a.status == "active")
    turn = service.call("dialogue_begin", {"text": "现在怎么样？", "conversation_id": "chat"})
    life.dialogue.tick()
    assert not failures
    assert service.agent.activities.get(active.activity_id).status == "active"
    assert service.call("dialogue_history", {"conversation_id": "chat"})["records"][-1]["text"] == "收到，我先回答，再继续。"
    release.set(); thread.join(5)
    assert not thread.is_alive()
    assert service.agent.activities.get(active.activity_id).status == "completed"
    assert len(list((tmp_path / "works/autonomous").glob("*.md"))) == 1


def test_restart_preserves_due_time_and_completed_work(tmp_path):
    service, life, clock = fixture(tmp_path)
    life.tick(); life.tick()
    due = life.state["next_wake_at"]
    prefixes = {p: p.read_bytes() for p in (tmp_path / "timeline").rglob("*.jsonl")}
    reload = PluginService(PersistentLifeAgent(tmp_path, clock=clock, provider=MockLLMProvider()))
    resumed = Lifecycle(reload, MockHost(), interval=30)
    assert resumed.state["next_wake_at"] == due
    resumed.tick()
    assert resumed.host.calls == []
    assert all(p.read_bytes().startswith(before) for p, before in prefixes.items())
    assert len(list((tmp_path / "works/autonomous").glob("*.md"))) == 1


def test_invalid_host_plan_does_not_create_facts(tmp_path):
    service, life, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        life._apply_plan(dict(name="bad", goal="bad", steps=[]))
    assert not service.agent.activities.all()
    assert not service.agent.runtime.schedule.all()


def test_user_task_is_committed_context_not_an_automatic_priority_plan(tmp_path):
    class TaskHost(MockHost):
        def run(self, request, cancel):
            if request["kind"] == "reply":
                return dict(reply="收到", name="便签", goal="写便签", steps=["起草", "最终版"])
            return super().run(request, cancel)

    service, life, clock = fixture(tmp_path, TaskHost())
    original = "写80到160字，标题《晴雨灯》，含出门前记得带伞。"
    service.call("dialogue_begin", {"text": original})
    life.dialogue.tick()
    assert not service.agent.runtime.schedule.all()
    assert not service.agent.activities.all()
    service = PluginService(PersistentLifeAgent(tmp_path, clock=clock, provider=MockLLMProvider()))
    host = MockHost()
    life = Lifecycle(service, host, interval=30)
    life.tick()
    assert host.calls[-1]["kind"] == "plan"
    assert host.calls[-1]["context"]["base"]["recent_dialogue_updates"][-1]["user_text"] == original
    assert service.agent.activities.all()[0].name == "独立创作"


def test_four_visible_structures_are_exported_without_editing_raw(tmp_path):
    service, life, _ = fixture(tmp_path)
    life.tick(); life.tick()
    raw = service.agent.raw.agent.path.read_bytes()
    data = service.call("structure_get")
    assert data["memory_tree"]["activity"]
    assert data["memory_tree"]["temporal"]
    assert data["activity_tree"] and data["schedule_tree"]
    assert data["heartbeat"]["timers"]["heartbeat"]["interval_seconds"] == 1800
    for name in ["activity_tree.md", "schedule_tree.md", "memory_activity_tree.md", "memory_temporal_tree.md", "heartbeat.md", "overview.json"]:
        assert (tmp_path / "trees" / name).is_file()
    assert service.agent.raw.agent.path.read_bytes() == raw


def test_model_failure_never_completes_unexecuted_work(tmp_path):
    service, life, _ = fixture(tmp_path)
    life.tick()
    class BrokenHost(MockHost):
        def run(self, request, cancel):
            raise RuntimeError("provider offline")
    life.host = BrokenHost()
    with pytest.raises(RuntimeError, match="offline"):
        life.tick()
    assert not any(a.status == "completed" for a in service.agent.activities.all())
    assert not list((tmp_path / "works").rglob("*.md"))


def test_pause_keeps_user_reply_alive_and_future_schedule_is_respected(tmp_path):
    service, life, clock = fixture(tmp_path)
    life.tick(); life.tick()
    service.call("lifecycle_control", {"action": "pause"})
    count = len(life.host.calls)
    life.tick()
    assert len(life.host.calls) == count
    service.call("dialogue_begin", {"text": "暂停后还能回答吗？"})
    life.dialogue.tick()
    assert service.call("dialogue_history")["records"][-1]["role"] == "agent"
    assert not life.state["enabled"]
    service.call("lifecycle_control", {"action": "resume"})
    life.tick()
    # Reply/resume may wake the loop, but the second planned task is in a
    # future window; it must not be executed early merely because a user spoke.
    assert len(list((tmp_path / "works/autonomous").glob("*.md"))) == 1


def test_schedule_tree_rejects_cycles_and_invalid_parent_windows():
    from artificial_life import ScheduleBlock
    from artificial_life.plans import SchedulePlan
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    parent = ScheduleBlock("p", "g", now, now+timedelta(hours=1))
    child = ScheduleBlock("c", "g", now, now+timedelta(hours=2), metadata={"parent_schedule_id":"p"})
    with pytest.raises(ValueError, match="fit"):
        SchedulePlan([parent, child])
    with pytest.raises(ValueError, match="unknown"):
        SchedulePlan([child])
    parent.metadata["parent_schedule_id"] = "p"
    with pytest.raises(ValueError, match="cycle"):
        SchedulePlan([parent])
