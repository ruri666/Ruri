"""Self and User Context builders with explicit budgets and source refs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .activity_life import ActivityManager, ActivityNode
from .conversation import ConversationMemory
from .core_memory import CoreMemoryStore, DEFAULT_CORE, DEFAULT_USER_CORE
from .memory import RawMemory, RawRecord
from .models import State, iso
from .retrieval import FileTreeRetriever, RetrievalResult
from .temporal_memory import TemporalMemory


@dataclass(frozen=True)
class ContextConfig:
    recent_activity_count: int = 3
    recent_day_count: int = 0
    recent_episode_count: int = 1
    recent_agent_raw_chars: int = 600
    recent_user_raw_chars: int = 1800
    ancestor_summary_chars: int = 120
    leaf_summary_chars: int = 320


@dataclass(frozen=True)
class ContextBundle:
    kind: str
    base: dict[str, Any]
    working: dict[str, Any]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "base": self.base, "working": self.working, "source_refs": list(self.source_refs)}


class ContextBuilder:
    def __init__(
        self,
        memory_root: Path | str,
        *,
        activities: ActivityManager | None = None,
        temporal: TemporalMemory | None = None,
        conversations: ConversationMemory | None = None,
        raw_memory: RawMemory | None = None,
        config: ContextConfig | None = None,
    ) -> None:
        self.root = Path(memory_root)
        self.raw = raw_memory or RawMemory(self.root)
        self.activities = activities or ActivityManager(self.root, raw_memory=self.raw)
        self.temporal = temporal or TemporalMemory(self.root, raw_memory=self.raw)
        self.conversations = conversations or ConversationMemory(self.root, raw_memory=self.raw)
        self.retriever = FileTreeRetriever(self.root, raw_memory=self.raw)
        self.core = CoreMemoryStore(self.root / "core_memory.md", title="Core Memory", defaults=DEFAULT_CORE)
        self.user_core = CoreMemoryStore(self.root / "user_core_memory.md", title="User Core Memory", defaults=DEFAULT_USER_CORE)
        self.config = config or ContextConfig()

    def build_self(
        self,
        *,
        current_state: State | dict[str, Any],
        current_activity_id: str | None = None,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ContextBundle:
        state = _state_dict(current_state)
        path = self.activities.active_path(current_activity_id)
        leaf_id = path[-1].activity_id if path else current_activity_id
        base = self._base(state, now)
        temporal = self.temporal.active_path(now)
        base["temporal_active_path"] = self._tree_path(temporal, "temporal")
        base["autonomous_activity_status"] = [self._activity_view(a, summary=False) for a in path]
        base["recent_dialogue_updates"] = self._committed_dialogue_updates()
        from .exploration import settings, exchanges
        exploration = settings(self.root.parent)
        base["exploration"] = exploration
        if exploration["mode"] == "user_service":
            evidence = [brief_exchange(v) for v in exchanges(self.raw, exploration["conversation_id"])[-5:]]
            base["user_service"] = {
                "conversation_id": exploration["conversation_id"],
                "user_core_memory": brief_core(self.user_core.read()),
                "preference_memory_access": {"tool": "user_preferences_get", "instruction": "核心偏好是概览；详细兴趣、依据、置信度与待确认点可按query检索，按next_offset分页。"},
                "committed_exchanges": evidence,
                "source_refs": [r for v in evidence for r in v["source_refs"]] +
                    (["memory/user_core_memory.md#Stable Preferences"] if self.user_core.read()["Stable Preferences"].strip() else []),
                "recent_results": self._shared_work(exploration["conversation_id"]),
                "interpretation": "历史是线索，不是当前任务或授权；推测需要与事实区分。",
            }
            # One owner-scoped exchange bridge, no duplicate history or mixed
            # temporal rollups from another conversation.
            base["recent_dialogue_updates"] = []
            base["temporal_active_path"] = self._tree_path(temporal, "temporal", summaries=False)
            base["autonomous_activity_status"] = [v for v in base["autonomous_activity_status"]
                if self._visible_activity(v["activity_id"], exploration["conversation_id"])]
        recent_raw = self._activity_raw(leaf_id)[-1:] if leaf_id else []
        siblings = self.activities.plan_snapshot(leaf_id) if leaf_id else []
        roots = [a for a in self.activities.all() if a.parent_activity is None
                 and (not path or a.activity_id != path[0].activity_id)
                 and (exploration["mode"] != "user_service"
                      or self._visible_activity(a.activity_id, exploration["conversation_id"]))]
        open_roots = [a for a in roots if a.status in {"planned", "active", "interrupted"}]
        recent_roots = [a for a in roots if a.status == "completed" and a.actual_end_time
                        and (now is None or a.actual_end_time >= now-timedelta(days=7))]
        index = (open_roots + recent_roots[-self.config.recent_activity_count:])[:self.config.recent_activity_count]
        working = {
            "activity_memory_index": [self._activity_view(a, summary=True) for a in index],
            "activity_memory_index_omitted": len(open_roots) + len(recent_roots) - len(index),
            "active_activity_path": [self._activity_view(a, summary=True, leaf=i == len(path)-1)
                                     for i, a in enumerate(path)],
            "activity_plan_snapshot": [dict(v, title=excerpt(v["title"], 120))
                for v in siblings if v["status"] not in {"completed", "cancelled"}][:self.config.recent_activity_count],
            "current_activity_recent_raw": _raw_dicts_with_budget(recent_raw, self.config.recent_agent_raw_chars),
            "retrieved_memory": retrieval_to_dict(self.retriever.retrieve(query, raw_fallback=True)) if query else retrieval_to_dict(RetrievalResult("", ())),
        }
        return self._bundle("self", base, working)

    def build_user(
        self, *, current_state: State | dict[str, Any], conversation_id: str | None = None,
        current_activity_id: str | None = None, query: str | None = None,
        now: datetime | None = None,
    ) -> ContextBundle:
        state = _state_dict(current_state)
        base = self._base(state, now)
        base["conversation_id"] = conversation_id
        # Mixed time rollups are never dialogue content. The conversation
        # branch below carries only its own source-backed leaf summary.
        base["temporal_active_path"] = self._tree_path(self.temporal.active_path(now), "temporal", summaries=False)
        base["conversation_timeline_current_path"] = self._tree_path(self.conversations.active_path(now), "conversation", summaries=False)
        path = self.activities.active_path(current_activity_id)
        base["autonomous_activity_status"] = [self._activity_view(a, summary=False) for a in path
            if self._visible_activity(a.activity_id, conversation_id)]
        base["user_core_memory"] = brief_core(self.user_core.read())
        base["preference_memory_access"] = {"tool": "user_preferences_get", "instruction": "详细用户偏好可按query检索，按next_offset分页，推测状态需要保留。"}
        if self.user_core.read()["Stable Preferences"].strip():
            base["user_core_source_refs"] = ["memory/user_core_memory.md#Stable Preferences"]
        base["shared_user_work"] = self._shared_work(conversation_id)
        current_branch = base["conversation_timeline_current_path"][-1]["node_id"]
        episodes = [n for n in self.conversations.recent_episodes(len(self.conversations.episodes()))
                    if n.metadata.get("conversation_id") == conversation_id and n.parent_id == current_branch]
        # Follow the selected branch root -> leaf, never a flat list of old episodes.
        branch = self.conversations.tree.path(episodes[0].node_id) if episodes else []
        conversation_path = self._tree_path(branch, "conversation", summaries=False)
        if branch:
            conversation_path[-1] = self._tree_node(branch[-1], "conversation", self.config.leaf_summary_chars)
        recent_user = [r for r in self.raw.user.records() + self.raw.agent.records()
                       if r.kind == "dialogue" and r.conversation_id == conversation_id]
        recent_user.sort(key=lambda r: (r.recorded_at, 0 if r.role == "user" else 1, r.record_id))
        working = {
            "conversation_memory_path": conversation_path,
            "recent_raw_user_agent_conversation": _raw_dicts_with_budget(recent_user[-6:], self.config.recent_user_raw_chars),
            "retrieved_memory": retrieval_to_dict(self.retriever.retrieve(query, raw_fallback=True, conversation_id=conversation_id)) if query else retrieval_to_dict(RetrievalResult("", ())),
        }
        return self._bundle("user", base, working)

    def _base(self, state, now):
        from .storage import read_json
        core = self.core.read()
        profile = read_json(self.root.parent / "persona.json", {})
        if profile:
            # Keep the exact user definition once; accumulated core sections
            # cannot replace it or repeat it under multiple identity fields.
            core["Identity"] = profile["definition"]
            core.pop("Persona", None)
        compact_core = brief_core(core, preserve_identity=True)
        unique_core = {}
        for key, value in compact_core.items():
            if value not in unique_core.values():
                unique_core[key] = value
        compact_core = unique_core
        fields = ("mode", "active_activity_id", "location", "emotion", "energy", "health", "fatigue", "mood_valence")
        base = {"core_memory": compact_core, "current_state": {k:state[k] for k in fields if k in state},
                "current_time": iso(now) if now else state.get("logical_time"),
                "memory_access": {"tool": "memory.search", "instruction": "先按当前树路径理解；需要细节时用节点标题或来源编号检索，再下钻原始记录。"}}
        if profile:
            base["persona"] = {"id": profile["id"], "source_ref": "persona.json"}
        from .affect import routing_context
        from .models import State
        disposition = routing_context(State.from_dict(state))
        base["activity_disposition"] = {k:disposition[k] for k in ("recovery_attraction", "social_attraction")}
        base["environment"] = {"kind": "digital_workspace", "workspace": str(self.root.parent.resolve()),
                               "ambient_physical_scene": None,
                               "observation_rule": "只有来源材料和真实工具回执构成观察。"}
        return base

    def _activity_view(self, activity, *, summary, leaf=False):
        value = {"activity_id": activity.activity_id, "parent_activity": activity.parent_activity,
                 "name": excerpt(activity.name, 160), "goal": excerpt(activity.goal, 240),
                 "status": activity.status, "source_ref": "activity:" + activity.activity_id}
        if summary:
            text = activity.final_summary or activity.live_summary or activity.summary
            value.update(summary=excerpt(text, self.config.leaf_summary_chars if leaf else self.config.ancestor_summary_chars),
                         truncated=len(text) > (self.config.leaf_summary_chars if leaf else self.config.ancestor_summary_chars))
        return value

    def _tree_node(self, node, tree_type, budget):
        text = node.final_summary or node.live_summary or node.summary
        return {"node_id": node.node_id, "parent_id": node.parent_id, "title": node.title,
                "summary": excerpt(text, budget), "truncated": len(text) > budget,
                "source_ref": f"{tree_type}:{node.node_id}"}

    def _tree_path(self, nodes, tree_type, summaries=True):
        return [self._tree_node(n, tree_type, (self.config.leaf_summary_chars if i == len(nodes)-1
                else self.config.ancestor_summary_chars) if summaries else 0) for i, n in enumerate(nodes)]

    def _visible_activity(self, activity_id, conversation_id):
        owner = self.activities.get(activity_id).metadata.get("user_service", {})
        return not owner or owner["conversation_id"] == conversation_id

    def _shared_work(self, conversation_id):
        from .exploration import recent_results
        from .timeline import RawTimeline
        values = recent_results(self.root.parent, RawTimeline(self.root.parent / "timeline").events(), conversation_id, limit=2)
        return [{"activity_id": v["activity_id"], "path": v["path"], "source_ref": v["source_ref"],
                 "excerpt": excerpt(v["excerpt"], 240), "truncated": len(v["excerpt"]) > 240,
                 "outcome": {k:v["outcome"].get(k) for k in ("status", "kind")},
                 "deliverables": v["deliverables"]} for v in values]

    @staticmethod
    def _bundle(kind, base, working):
        refs = []
        def visit(value):
            if isinstance(value, dict):
                if value.get("source_ref"):
                    refs.append(value["source_ref"])
                refs.extend(value.get("source_refs", []))
                if "record_id" in value and "stream" in value:
                    refs.append(f"raw:{value['stream']}:{value['record_id']}")
                for k, v in value.items():
                    if k not in {"source_ref", "source_refs"}:
                        visit(v)
            elif isinstance(value, list):
                for v in value:
                    visit(v)
        visit(base); visit(working)
        return ContextBundle(kind, base, working, _unique_refs(refs))

    def _committed_dialogue_updates(self):
        users = {r.record_id: r for r in self.raw.user.records()}
        updates = []
        for reply in self.raw.agent.records():
            user = users.get(reply.metadata.get("reply_to_user_record_id"))
            if user is not None and user.conversation_id == reply.conversation_id:
                updates.append({"conversation_id": user.conversation_id, "user_text": user.text[:1500],
                    "reply": reply.text[:500], "truncated": len(user.text)>1500 or len(reply.text)>500,
                    "source_refs": [f"raw:user:{user.record_id}", f"raw:agent:{reply.record_id}"]})
        return [brief_exchange(v) for v in updates[-2:]]

    def _recent_siblings(self, current: ActivityNode | None) -> list[ActivityNode]:
        if current is None:
            return []
        return [
            activity
            for activity in self.activities.all()
            if activity.parent_activity == current.parent_activity
            and activity.activity_id != current.activity_id
            and activity.status == "completed"
        ][-self.config.recent_activity_count :]

    def _activity_raw(self, activity_id: str | None) -> list[RawRecord]:
        records = self.raw.agent.records()
        if activity_id is None:
            return records[-50:]
        matched = [record for record in records if record.activity_id == activity_id]
        return matched[-100:]


def _state_dict(value: State | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, State):
        return value.to_dict()
    return dict(value)


def activity_to_dict(activity: ActivityNode) -> dict[str, Any]:
    return {
        "activity_id": activity.activity_id,
        "name": activity.name,
        "goal": activity.goal,
        "parent_activity": activity.parent_activity,
        "status": activity.status,
        "summary": activity.final_summary or activity.live_summary or activity.summary,
        "execution_segments": [segment.to_dict() for segment in activity.execution_segments],
    }


def node_to_dict(node: Any) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "title": node.title,
        "parent_id": node.parent_id,
        "status": node.status,
        "summary": node.final_summary or node.live_summary or node.summary,
        "raw_pointer": node.raw_pointer.to_dict() if node.raw_pointer else None,
    }


def retrieval_to_dict(result: RetrievalResult) -> dict[str, Any]:
    return {
        "query": result.query,
        "memory_types": list(result.memory_types),
        "summary_nodes": [node_to_dict(node) for node in result.summary_nodes],
        "raw_records": [record.to_dict() for record in result.raw_records],
        "source_refs": list(result.source_refs),
        "raw_fallback": result.raw_fallback,
    }


def _raw_dicts_with_budget(records: list[RawRecord], budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for record in reversed(records):
        value = {"record_id": record.record_id, "stream": record.stream, "role": record.role,
                 "text": record.text, "source_ref": f"raw:{record.stream}:{record.record_id}"}
        size = len(record.text)
        if size > budget and not selected:
            selected.append({**value, "text": excerpt(record.text, budget), "truncated": True})
            break
        if selected and used + size > budget:
            break
        selected.append(value)
        used += size
    selected.reverse()
    return selected


def _unique_refs(refs: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(refs))


def excerpt(text: str, budget: int) -> str:
    """Source excerpt, not an invented summary. Full text stays in memory."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    return text[:max(0, budget-1)] + "…"


def brief_core(values, preserve_identity=False):
    return {k: (v if preserve_identity and k == "Identity" else excerpt(v, 1200 if k == "Stable Preferences" else 240))
            for k, v in values.items() if isinstance(v, str) and v.strip()}


def brief_exchange(value):
    return {**value, "user_text": excerpt(value["user_text"], 320), "reply": excerpt(value["reply"], 160),
            "truncated": value.get("truncated", False) or len(value["user_text"]) > 320 or len(value["reply"]) > 160}
