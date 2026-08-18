"""Raw user/agent conversation episodes and a textual conversation timeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .llm import LLMProvider, MockLLMProvider
from .memory import RawMemory, RawPointer, RawRecord
from .models import as_utc
from .text_tree import TextTreeStore, TreeNode


class ConversationMemory:
    def __init__(
        self,
        memory_root: Path | str,
        *,
        idle_timeout: timedelta = timedelta(hours=2),
        clock: Clock | None = None,
        raw_memory: RawMemory | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.root = Path(memory_root)
        self.idle_timeout = idle_timeout
        self.clock = clock or SystemClock()
        self.raw = raw_memory or RawMemory(self.root)
        self.provider = provider or MockLLMProvider()
        self.store = TextTreeStore(self.root / "conversation_memory.md", "conversation", "Conversation Memory")
        self.tree = self.store.load()

    def append_user(
        self,
        text: str,
        *,
        conversation_id: str,
        at: datetime | None = None,
        activity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RawRecord:
        return self.raw.user.append(
            recorded_at=at or self.clock.now(),
            kind="dialogue",
            role="user",
            text=text,
            conversation_id=conversation_id,
            activity_id=activity_id,
            metadata=metadata,
        )

    def append_agent(
        self,
        text: str,
        *,
        conversation_id: str,
        at: datetime | None = None,
        activity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RawRecord:
        return self.raw.agent.append(
            recorded_at=at or self.clock.now(),
            kind="dialogue",
            role="agent",
            text=text,
            conversation_id=conversation_id,
            activity_id=activity_id,
            metadata=metadata,
        )

    def close_idle(self, now: datetime | None = None) -> list[TreeNode]:
        moment = as_utc(now or self.clock.now())
        records = self.raw.user.records() + self.raw.agent.records()
        by_conversation: dict[str, list[RawRecord]] = {}
        for record in records:
            if record.conversation_id:
                by_conversation.setdefault(record.conversation_id, []).append(record)
        created: list[TreeNode] = []
        for conversation_id, episode_records in by_conversation.items():
            episode_records.sort(key=lambda record: record.recorded_at)
            existing = [node for node in self.episodes() if node.metadata.get("conversation_id") == conversation_id]
            last_closed_at = max((node.end_time for node in existing if node.end_time is not None), default=None)
            if last_closed_at is not None:
                episode_records = [record for record in episode_records if record.recorded_at > last_closed_at]
            if not episode_records:
                continue
            last = episode_records[-1]
            if moment - last.recorded_at < self.idle_timeout:
                continue
            created.append(self._create_episode(conversation_id, episode_records))
        if created:
            self.store.save(self.tree)
        return created

    def episodes(self) -> list[TreeNode]:
        return [node for node in self.tree.nodes.values() if node.metadata.get("kind") == "episode"]

    def active_path(self, moment: datetime | None = None) -> list[TreeNode]:
        moment = as_utc(moment or self.clock.now())
        day = moment.date()
        iso_week = day.isocalendar()
        specs = [
            (f"year:{day.year:04d}", str(day.year), None, "year"),
            (f"month:{day.year:04d}-{day.month:02d}", f"{day.year:04d}-{day.month:02d}", f"year:{day.year:04d}", "month"),
            (f"week:{iso_week.year:04d}-W{iso_week.week:02d}", f"Week {iso_week.week}", f"month:{day.year:04d}-{day.month:02d}", "week"),
        ]
        for node_id, title, parent_id, scale in specs:
            if node_id not in self.tree.nodes:
                self.tree.add(TreeNode(node_id=node_id, title=title, parent_id=parent_id, metadata={"scale": scale}))
        self.store.save(self.tree)
        return [self.tree.get(node_id) for node_id, _, _, _ in specs]

    def recent_episodes(self, count: int = 3) -> list[TreeNode]:
        return sorted(self.episodes(), key=lambda node: node.start_time or datetime.min.astimezone(), reverse=True)[:count]

    def _create_episode(self, conversation_id: str, records: list[RawRecord]) -> TreeNode:
        first, last = records[0], records[-1]
        path = self.active_path(first.recorded_at)
        episode_id = f"episode:{conversation_id}:{last.record_id}"
        user_records = [record for record in records if record.stream == "user"]
        agent_records = [record for record in records if record.stream == "agent"]
        source_text = "；".join(f"{record.role}: {record.text}" for record in records)
        result = self.provider.summarize(source_text, purpose="conversation_episode", source_refs=[], scale="event")
        user_pointer = self._pointer(user_records, "user")
        agent_pointer = self._pointer(agent_records, "agent")
        node = TreeNode(
            node_id=episode_id,
            title=f"Conversation Episode: {conversation_id}",
            parent_id=path[-1].node_id,
            status="final",
            summary=result.text,
            live_summary=result.text,
            final_summary=result.text,
            raw_pointer=user_pointer or agent_pointer,
            start_time=first.recorded_at,
            end_time=last.recorded_at,
            metadata={
                "kind": "episode",
                "conversation_id": conversation_id,
                "last_record_id": last.record_id,
                "agent_raw_pointer": agent_pointer.to_dict() if agent_pointer else None,
                "user_raw_pointer": user_pointer.to_dict() if user_pointer else None,
                "summary_source_refs": [record.record_id for record in records],
            },
        )
        self.tree.add(node)
        self._update_timeline_live(path)
        return node

    def _update_timeline_live(self, path: list[TreeNode]) -> None:
        for parent in reversed(path):
            children = [child for child in self.tree.children(parent.node_id) if child.metadata.get("kind") == "episode"]
            snippets = [child.final_summary or child.live_summary or child.summary for child in children]
            snippets = [snippet for snippet in snippets if snippet]
            if snippets:
                result = self.provider.summarize(
                    "；".join(snippets),
                    purpose="conversation_timeline_live",
                    source_refs=[child.node_id for child in children],
                    scale=parent.metadata.get("scale", "week"),
                )
                parent.live_summary = result.text
                parent.summary = result.text
                parent.metadata["summary_source_refs"] = [child.node_id for child in children]

    @staticmethod
    def _pointer(records: list[RawRecord], stream: str) -> RawPointer | None:
        if not records:
            return None
        return RawPointer(stream, records[0].record_id, records[-1].record_id)
