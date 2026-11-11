"""Non-destructive mechanism audit: isolated roots, no production mutations.

Run with PYTHONPATH=src. FAIL means a reproduced product limitation, not a
test-harness exception. --live adds a small real DeepSeek streaming/restart probe.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from artificial_life import (
    ActivityManager, ConversationMemory, DeepSeekProvider, HeartbeatRunner, MockLLMProvider,
    PersistentLifeAgent, RawMemory, SummaryService, TemporalMemory, TestClock,
)
from artificial_life.llm import LLMResult
from artificial_life.planning import PlanningProposal


def audit(live=False):
    checks = []
    base = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)

    def record(name, correct, observed):
        checks.append({"check": name, "status": "PASS" if correct else "FAIL", "observed": observed})

    with tempfile.TemporaryDirectory(prefix="life-agent-audit-") as directory:
        root = Path(directory)
        raw = RawMemory(root / "raw")
        first = raw.user.append(recorded_at=base, kind="dialogue", role="user", text="AUDIT_FIRST")
        prefix = raw.user.path.read_bytes()
        raw.user.append(recorded_at=base, kind="dialogue", role="user", text="AUDIT_SECOND")
        loaded = RawMemory(root / "raw").user.records()
        record("raw_append_restart", raw.user.path.read_bytes().startswith(prefix) and loaded[0].record_id == first.record_id and len(loaded) == 2, {"records": len(loaded)})

        manager = ActivityManager(root / "activity")
        manager.create(activity_id="a", name="AUDIT_ACTIVITY_A")
        manager.create(activity_id="b", name="AUDIT_ACTIVITY_B")
        manager.start("a", base)
        manager.start("b", base + timedelta(minutes=1))
        count = sum(a.status == "active" for a in manager.all())
        record("single_active_activity", count == 1, {"active_count": count})
        manager.complete("b", base + timedelta(minutes=2))
        manager.complete("a", base + timedelta(minutes=3))
        restored = ActivityManager(root / "activity").get("a")
        record("actual_times_persist", restored.actual_start_time is not None and restored.actual_end_time is not None, {"actual_start": str(restored.actual_start_time), "actual_end": str(restored.actual_end_time), "segments": len(restored.execution_segments)})
        provider = MockLLMProvider()
        summary = SummaryService(root / "activity", activities=manager, provider=provider).finalize_activity("a")
        record("activity_summary_source_isolation", "AUDIT_ACTIVITY_B" not in summary, {"contains_other_activity": "AUDIT_ACTIVITY_B" in summary})

        temporal_raw = RawMemory(root / "temporal")
        temporal = TemporalMemory(root / "temporal", raw_memory=temporal_raw)
        event = temporal_raw.agent.append(recorded_at=base, kind="execution", role="agent", text="AUDIT_DAILY_WORK")
        temporal.record(event, summary_text=event.text, update_parent_summaries=False)
        temporal.finalize_closed_periods(base.replace(year=2027, month=1, day=5))
        parents = [n for n in temporal.tree.nodes.values() if n.metadata.get("scale") in {"week", "month", "year"}]
        record("automatic_period_summary_rollup", all(n.final_summary for n in parents), {"empty_final_periods": [n.node_id for n in parents if not n.final_summary]})
        temporal.ensure_path(datetime(2026, 8, 31, tzinfo=timezone.utc))
        path = temporal.ensure_path(datetime(2026, 9, 1, tzinfo=timezone.utc))
        record("cross_month_temporal_path", path[2].parent_id == path[1].node_id, {"month": path[1].node_id, "week_parent": path[2].parent_id})

        conversations = ConversationMemory(root / "conversation", provider=MockLLMProvider())
        conversations.append_user("AUDIT_CHAT_A_FIRST", conversation_id="a", at=base)
        conversations.append_user("AUDIT_CHAT_B_SECRET", conversation_id="b", at=base + timedelta(minutes=1))
        conversations.append_user("AUDIT_CHAT_A_LAST", conversation_id="a", at=base + timedelta(minutes=2))
        episodes = conversations.close_idle(base + timedelta(hours=3))
        episode_a = next(e for e in episodes if e.metadata["conversation_id"] == "a")
        resolved = conversations.raw.user.resolve(episode_a.raw_pointer)
        record("conversation_raw_pointer_isolation", all(r.conversation_id == "a" for r in resolved), {"resolved_conversation_ids": [r.conversation_id for r in resolved]})
        upper = [n for n in conversations.tree.nodes.values() if n.metadata.get("scale") in {"month", "year"}]
        record("conversation_period_summary_rollup", all(n.live_summary for n in upper), {"empty_periods": [n.node_id for n in upper if not n.live_summary]})
        gap = ConversationMemory(root / "conversation-gap", provider=MockLLMProvider())
        gap.append_user("first session", conversation_id="same", at=base)
        gap.append_user("second session", conversation_id="same", at=base + timedelta(hours=4))
        gap_episodes = gap.close_idle(base + timedelta(hours=7))
        record("offline_idle_gap_segmentation", len(gap_episodes) == 2, {"episodes": len(gap_episodes), "gap_hours": 4})

        clock = TestClock(base)
        mock = MockLLMProvider()
        agent = PersistentLifeAgent.initialize(root / "bot", clock=clock, provider=mock)
        runner = HeartbeatRunner(agent)
        clock.set(base + timedelta(minutes=30))
        fired = runner.run_once()
        record("persisted_30min_timer", len(fired) == 1 and agent.runtime.timer_store.load()["heartbeat"].interval_seconds == 1800, {"fired": len(fired), "interval_seconds": 1800})
        record("empty_root_autonomous_activity", bool(agent.activities.all()), {"activities": len(agent.activities.all()), "planning_calls": sum(c["method"] == "plan" for c in mock.calls)})
        agent.create_activity(activity_id="work", name="AUDIT_WRITING")
        agent.start_activity("work", clock.now())
        reply = agent.respond_to_user("audit hello", at=clock.now())
        record("normal_dialogue_independence", reply.resumed_activity_id is None and reply.interrupted_activity_id is None and agent.activities.get("work").status == "active", {"resumed_id": reply.resumed_activity_id})
        stream = agent.stream_to_user("audit disconnected", at=clock.now())
        next(stream)
        next(stream)
        stream.close()
        status = agent.activities.get("work").status
        record("stream_disconnect_resume", status == "active", {"status_after_close": status})
        restarted = PersistentLifeAgent(root / "bot", clock=clock, provider=MockLLMProvider())
        record("checkpoint_restart_recovery", restarted.activities.get("work").status == "active", {"status_after_restart": restarted.activities.get("work").status})
        facts_before = [(p, p.read_bytes()) for p in (root / "bot" / "timeline").rglob("*.jsonl")]
        clock.set(base + timedelta(hours=1))
        observed_tick = threading.Event()
        background = HeartbeatRunner(restarted, on_results=lambda results: observed_tick.set())
        background.start()
        try:
            ticked = observed_tick.wait(3)
        finally:
            background.stop()
        record("background_runner_without_user", ticked, {"callback_observed": ticked})
        record("timeline_append_after_restart", all(p.read_bytes().startswith(content) for p, content in facts_before), {"original_files": len(facts_before)})

        rejected = False
        try:
            PlanningProposal.from_result(LLMResult(text="", structured={"proposal": {"schema_version": 2, "action": "reserved_free"}}))
        except ValueError:
            rejected = True
        record("planning_schema_version_validation", rejected, {"version_2_rejected": rejected})

        if live:
            provider = DeepSeekProvider(os.environ.get("DEEPSEEK_API_KEY", ""), timeout=25, max_tokens=160)
            live_agent = PersistentLifeAgent.initialize(root / "live", clock=TestClock(base), provider=provider)
            events = list(live_agent.stream_to_user("请记住测试口令是琥珀猫731，只简短确认。", conversation_id="audit", at=base))
            response = events[-1].data["response"]
            record("live_deepseek_stream", response["provider_error"] is None and any(e.event == "delta" for e in events), {"model": provider.model, "delta_events": sum(e.event == "delta" for e in events), "provider_error": response["provider_error"]})
            live_restart = PersistentLifeAgent(root / "live", clock=TestClock(base), provider=provider)
            recalled = live_restart.respond_to_user("刚才的测试口令是什么？只给出口令。", conversation_id="audit", at=base)
            record("live_restart_recent_raw_recall", recalled.provider_error is None and "731" in recalled.agent.text and "琥珀" in recalled.agent.text, {"reply": recalled.agent.text, "provider_error": recalled.provider_error, "scope": "recent raw only; not long-term summary retrieval"})

    return {"audit_date": "2026-10-12", "isolation": "temporary roots; explicit MockLLMProvider except --live", "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(audit(args.live), ensure_ascii=False, indent=2))
