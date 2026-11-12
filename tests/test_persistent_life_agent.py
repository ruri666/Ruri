from __future__ import annotations

from datetime import datetime, timedelta, timezone

from artificial_life import (
    ActivityManager,
    ConversationMemory,
    ContextBuilder,
    FileTreeRetriever,
    MemoryRouter,
    MockLLMProvider,
    PersistentLifeAgent,
    RandomEventEngine,
    RawMemory,
    TemporalMemory,
    TestClock,
    Goal,
    GoalLevel,
    ScheduleBlock,
    RuntimeConfig,
    TextTree,
    TextTreeStore,
    TreeNode,
)


UTC = timezone.utc


def moment(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, tzinfo=UTC)


def test_raw_dialogue_and_markdown_tree_round_trip(tmp_path):
    raw = RawMemory(tmp_path)
    first = raw.agent.append(recorded_at=moment(9), kind="thought", role="agent", text="开始构思旅行小说")
    second = raw.agent.append(recorded_at=moment(9, 1), kind="execution", role="agent", text="写下宾馆场景")
    assert [record.record_id for record in raw.agent.query(start_record=first.record_id, end_record=second.record_id)] == [first.record_id, second.record_id]

    manager = ActivityManager(tmp_path, clock=TestClock(moment(9)), raw_memory=raw)
    manager.create(activity_id="novel", name="写小说", goal="完成长篇初稿")
    manager.create(activity_id="chapter-2", name="第二章", parent_activity="novel")
    manager.create(activity_id="outline", name="大纲设计", parent_activity="chapter-2")
    manager.start("outline", moment(9))
    manager.interrupt("outline", "身体不适", moment(10))
    manager.resume("outline", moment(14))
    manager.complete("outline", moment(16))

    loaded = ActivityManager(tmp_path, raw_memory=raw)
    outline = loaded.get("outline")
    assert outline.status == "completed"
    assert len(outline.execution_segments) == 2
    assert loaded.get("novel").children == ["chapter-2"]
    assert [item.activity_id for item in loaded.active_path("outline")] == ["novel", "chapter-2", "outline"]


def test_summary_updates_direct_parent_only(tmp_path):
    raw = RawMemory(tmp_path)
    manager = ActivityManager(tmp_path, raw_memory=raw)
    manager.create(activity_id="root", name="写小说")
    manager.create(activity_id="chapter", name="第二章", parent_activity="root")
    manager.create(activity_id="scene", name="宾馆场景", parent_activity="chapter")
    manager.start("scene", moment(9))
    manager.complete("scene", moment(10))
    provider = MockLLMProvider()
    from artificial_life import SummaryService

    service = SummaryService(tmp_path, activities=manager, raw_memory=raw, provider=provider)
    service.finalize_activity("scene")
    assert manager.get("scene").final_summary
    assert manager.get("chapter").live_summary
    assert not manager.get("root").live_summary
    manager.complete("chapter", moment(11))
    service.finalize_activity("chapter")
    assert manager.get("chapter").final_summary


def test_temporal_tree_and_conversation_episode(tmp_path):
    raw = RawMemory(tmp_path)
    temporal = TemporalMemory(tmp_path, raw_memory=raw)
    record = raw.agent.append(recorded_at=moment(9), kind="execution", role="agent", text="今天写了宾馆场景")
    temporal.record(record, summary_text="完成宾馆场景")
    path = temporal.active_path(moment(9))
    assert [node.node_id for node in path] == ["year:2026", "month:2026-08", "week:2026-W34", "day:2026-08-18"]
    assert temporal.recent_days(1)[0].live_summary
    temporal.finalize_day("day:2026-08-18", at=moment(23, 59))
    assert temporal.tree.get("day:2026-08-18").status == "final"

    conversations = ConversationMemory(tmp_path, idle_timeout=timedelta(hours=2), raw_memory=raw)
    conversations.append_user("你今天写小说了吗？", conversation_id="chat-1", at=moment(18))
    conversations.append_agent("写了宾馆场景。", conversation_id="chat-1", at=moment(18, 10))
    episodes = conversations.close_idle(moment(21))
    assert len(episodes) == 1
    assert conversations.episodes()[0].raw_pointer is not None
    assert conversations.close_idle(moment(22)) == []
    conversations.append_user("第二次继续聊。", conversation_id="chat-1", at=moment(23))
    conversations.append_agent("我还记得。", conversation_id="chat-1", at=moment(23, 1))
    assert len(conversations.close_idle(moment(23, 1) + timedelta(hours=2))) == 1
    assert len(conversations.episodes()) == 2


def test_context_contains_active_paths_plan_recent_raw_and_user_memory(tmp_path):
    raw = RawMemory(tmp_path)
    clock = TestClock(moment(9))
    manager = ActivityManager(tmp_path, clock=clock, raw_memory=raw)
    manager.create(activity_id="novel", name="写小说")
    manager.create(activity_id="chapter", name="第二章", parent_activity="novel")
    manager.start("chapter", moment(9))
    raw.agent.append(recorded_at=moment(9, 5), kind="thought", role="agent", text="正在设计冲突", activity_id="chapter")
    temporal = TemporalMemory(tmp_path, raw_memory=raw)
    conversations = ConversationMemory(tmp_path, raw_memory=raw)
    builder = ContextBuilder(tmp_path, activities=manager, temporal=temporal, conversations=conversations, raw_memory=raw)
    self_context = builder.build_self(current_state={"logical_time": moment(9).isoformat(), "mode": "working"}, current_activity_id="chapter", now=moment(9, 5))
    assert [item["activity_id"] for item in self_context.working["active_activity_path"]] == ["novel", "chapter"]
    assert self_context.working["activity_plan_snapshot"]
    assert self_context.working["current_activity_recent_raw"]
    assert len(self_context.base["temporal_active_path"]) == 4
    assert "activity:novel" in self_context.source_refs
    assert any(ref.startswith("raw:agent:") for ref in self_context.source_refs)

    conversations.append_user("记得这个场景吗？", conversation_id="chat-1", at=moment(9, 10))
    user_context = builder.build_user(current_state={"logical_time": moment(9, 10).isoformat()}, conversation_id="chat-1", now=moment(9, 10))
    assert "user_core_memory" in user_context.base
    assert user_context.working["recent_raw_user_agent_conversation"]
    assert "active_activity_path" not in user_context.working
    assert "current_activity_recent_raw" not in user_context.working
    assert user_context.base["conversation_timeline_current_path"]


def test_file_retrieval_routes_and_falls_back_to_raw(tmp_path):
    raw = RawMemory(tmp_path)
    manager = ActivityManager(tmp_path, raw_memory=raw)
    manager.create(activity_id="travel", name="京都旅行", goal="体验宾馆")
    manager.start("travel", moment(9))
    raw.agent.append(recorded_at=moment(9, 1), kind="execution", role="agent", text="京都的宾馆隔音很好", activity_id="travel")
    manager.complete("travel", moment(10))
    result = FileTreeRetriever(tmp_path, raw_memory=raw).retrieve("上次旅行宾馆具体细节", memory_type="activity")
    assert result.summary_nodes
    assert result.raw_fallback is True
    assert any("隔音" in record.text for record in result.raw_records)
    assert MemoryRouter().route("你还记得上次我们聊那个宾馆时我说过什么吗？") == ("conversation",)
    assert MemoryRouter().route("上次旅行那个宾馆的实际住宿体验") == ("mixed",)

    temporal = TemporalMemory(tmp_path, raw_memory=raw)
    temporal.record(raw.agent.append(recorded_at=moment(9, 30), kind="execution", role="agent", text="当天整理了旅行细节"), summary_text="整理旅行细节")
    temporal_result = FileTreeRetriever(tmp_path, raw_memory=raw).retrieve("过去某天旅行细节", memory_type="temporal")
    assert any(node.node_id.startswith("day:") for node in temporal_result.summary_nodes)

    conversations = ConversationMemory(tmp_path, idle_timeout=timedelta(hours=1), raw_memory=raw)
    conversations.append_user("我们聊过宾馆隔音。", conversation_id="chat", at=moment(17))
    conversations.append_agent("记得。", conversation_id="chat", at=moment(17, 1))
    conversations.close_idle(moment(19))
    conversation_result = FileTreeRetriever(tmp_path, raw_memory=raw).retrieve("我们聊过宾馆", memory_type="conversation")
    assert conversation_result.summary_nodes
    assert any(node.metadata.get("kind") == "episode" for node in conversation_result.summary_nodes)


def test_persistent_agent_heartbeat_random_hook_and_user_context(tmp_path):
    clock = TestClock(moment(8))
    agent = PersistentLifeAgent.initialize(
        tmp_path / "agent",
        clock=clock,
        provider=MockLLMProvider(),
        random_events=RandomEventEngine(seed=7, probability=1.0),
    )
    agent.create_activity(activity_id="novel", name="写小说", goal="完成第二章")
    agent.start_activity("novel", moment(8))
    result = agent.heartbeat(moment(8, 30))
    assert any("Timeline heartbeat.started" in record.text for record in agent.raw.agent.records())
    assert result.random_event is not None
    assert result.interrupted_activity_id == "novel"
    assert result.random_activity_id is not None
    assert agent.activities.get("novel").status == "interrupted"
    assert agent.activities.get(result.random_activity_id).status == "active"
    assert agent.runtime.state.active_activity_id == result.random_activity_id
    assert agent.runtime.state.mode.value == "working"
    agent.complete_activity(result.random_activity_id, moment(11))
    assert any("Planning candidates" in record.text for record in agent.raw.agent.records())

    agent.receive_user_message("今天写小说了吗？", conversation_id="chat-1", at=moment(18))
    agent.send_agent_message("身体不适，先休息了一会儿。", conversation_id="chat-1", at=moment(18, 1))
    context = agent.build_user_context(conversation_id="chat-1", query="今天发生了什么", now=moment(18, 1))
    assert context.kind == "user"
    assert context.working["recent_raw_user_agent_conversation"]
    assert any(event.event_type == "random_event.triggered" for event in agent.runtime.timeline.events())
    restarted = PersistentLifeAgent(tmp_path / "agent", clock=clock, provider=MockLLMProvider())
    assert restarted.activities.get("novel").status == "interrupted"
    assert restarted.runtime.state.active_activity_id is None
    assert restarted.runtime.state.mode.value == "reserved_free"
    repeated = restarted.heartbeat(moment(8, 30))
    assert repeated.random_event is None


def test_heartbeat_interrupts_activity_at_expected_end_and_keeps_it_unfinished(tmp_path):
    clock = TestClock(moment(9))
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=clock, provider=MockLLMProvider())
    agent.create_activity(activity_id="chapter", name="第二章", expected_end_time=moment(10))
    agent.start_activity("chapter", moment(9))
    result = agent.heartbeat(moment(10))
    assert result.expected_end_activity_ids == ("chapter",)
    assert agent.activities.get("chapter").status == "interrupted"
    assert agent.replan()["candidates"][0]["activity_id"] == "chapter"
    assert any(event.event_type == "activity.expected_end_reached" for event in agent.runtime.timeline.events())


def test_dynamic_activity_state_projection_survives_restart(tmp_path):
    clock = TestClock(moment(9))
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=clock, provider=MockLLMProvider())
    agent.create_activity(activity_id="scene", name="场景", goal="完成场景")
    agent.start_activity("scene", moment(9))
    assert agent.runtime.state.active_activity_id == "scene"
    assert agent.runtime.state.mode.value == "working"

    restarted = PersistentLifeAgent(tmp_path / "agent", clock=clock, provider=MockLLMProvider())
    assert restarted.runtime.state.active_activity_id == "scene"
    assert restarted.runtime.state.mode.value == "working"
    restarted.interrupt_activity("scene", "用户对话打断", moment(9, 30))
    assert restarted.runtime.state.active_activity_id is None
    assert restarted.runtime.state.mode.value == "reserved_free"


def test_heartbeat_does_not_call_provider_for_each_raw_fact(tmp_path):
    clock = TestClock(moment(8))
    provider = MockLLMProvider()
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=clock, provider=provider)
    agent.heartbeat(moment(8, 1))
    assert provider.calls == []


def test_replan_includes_planned_schedule_candidates(tmp_path):
    clock = TestClock(moment(8))
    goal = Goal("small", GoalLevel.SMALL, "完成场景", parent_id="medium")
    medium = Goal("medium", GoalLevel.MEDIUM, "完成章节", parent_id="large")
    large = Goal("large", GoalLevel.LARGE, "完成小说")
    schedule = ScheduleBlock("writing", "small", moment(9), moment(12), activity_type="writing")
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", goals=[large, medium, goal], schedule=[schedule], clock=clock, config=RuntimeConfig(), provider=MockLLMProvider())
    candidates = agent.replan(at=moment(8))["candidates"]
    assert any(candidate.get("schedule_id") == "writing" for candidate in candidates)


def test_summary_without_source_reference_is_rejected(tmp_path):
    store = TextTreeStore(tmp_path / "activity_memory.md", "activity", "Activity Memory")
    tree = TextTree("activity", "Activity Memory", [TreeNode("a", "无来源", summary="不可审计")])
    try:
        store.save(tree)
    except ValueError as exc:
        assert "source reference" in str(exc)
    else:
        raise AssertionError("summary without provenance must be rejected")
