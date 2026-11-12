"""Run the V1 Persistent Life Agent lifecycle demo.

Usage:
    python examples/demo.py
    python examples/demo.py --root var/demo_agent
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from artificial_life import (
    MockLLMProvider,
    PersistentLifeAgent,
    RandomEventEngine,
    RandomEventSpec,
    RuntimeConfig,
    TestClock,
)


UTC = timezone.utc


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, tzinfo=UTC)


def run(root: Path) -> None:
    if root.exists():
        raise SystemExit(f"demo root already exists; choose another --root: {root}")
    clock = TestClock(dt(8))
    agent = PersistentLifeAgent.initialize(
        root,
        clock=clock,
        provider=MockLLMProvider(),
        config=RuntimeConfig(),
        random_events=RandomEventEngine(
            seed=7,
            probability=0.0,
            events=(RandomEventSpec("illness", "身体不适", "身体不适，需要休息。"),),
        ),
    )

    agent.record_agent("起床，今天先推进第二章。", kind="thought", at=dt(8))
    agent.create_activity(activity_id="novel", name="写小说", goal="完成长篇轻小说初稿")
    agent.create_activity(activity_id="chapter-2", name="第二章创作", parent_activity="novel", goal="完成当前章节")
    agent.create_activity(
        activity_id="outline",
        name="大纲设计",
        parent_activity="chapter-2",
        goal="设计第二章冲突和节奏",
        expected_end_time=dt(12),
    )
    agent.start_activity("outline", dt(8))

    clock.set(dt(9))
    agent.heartbeat()
    agent.random_events = RandomEventEngine(
        seed=7,
        probability=1.0,
        events=(RandomEventSpec("illness", "身体不适", "身体不适，需要休息。"),),
    )
    clock.set(dt(10))
    interrupted = agent.heartbeat()
    print(f"10:00 heartbeat -> interrupted={interrupted.interrupted_activity_id}, random={interrupted.random_activity_id}")

    if interrupted.random_activity_id:
        clock.set(dt(11))
        agent.complete_activity(interrupted.random_activity_id, dt(11))
    plan = agent.replan(at=dt(11))
    print(f"replan candidates -> {plan['candidates']}")
    agent.resume_activity("outline", dt(11, 15))
    agent.record_agent("休息后恢复大纲设计。", kind="thought", activity_id="outline", at=dt(11, 15))

    agent.receive_user_message("今天写小说了吗？", conversation_id="chat-1", at=dt(18))
    agent.send_agent_message("上午完成了第二章大纲，中途身体不适休息过。", conversation_id="chat-1", at=dt(18, 1))
    agent.close_idle_conversations(dt(21))
    context = agent.build_user_context(conversation_id="chat-1", query="今天发生了什么", now=dt(21))
    print(f"user context sections -> base={sorted(context.base)}, working={sorted(context.working)}")
    print(f"persisted memory -> {root / 'memory'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("var/demo_agent"))
    run(parser.parse_args().root)
