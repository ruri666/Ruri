"""Coarse-to-fine textual memory routing and Raw fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from .memory import RawMemory, RawRecord
from .text_tree import TextTree, TreeNode


MEMORY_TYPES = {"activity", "temporal", "conversation"}
MEMORY_TYPE_ORDER = ("activity", "temporal", "conversation")


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    memory_types: tuple[str, ...]
    summary_nodes: tuple[TreeNode, ...] = ()
    raw_records: tuple[RawRecord, ...] = ()
    source_refs: tuple[str, ...] = ()
    raw_fallback: bool = False


class MemoryRouter:
    def route(self, query: str) -> tuple[str, ...]:
        text = query.lower()
        conversation_signal = any(word in text for word in ("聊", "说", "对话", "记得我", "我们讨论", "告诉我当时"))
        creative_activity = any(word in text for word in ("撰写", "创作", "写作", "写小说", "正文", "场景", "设定稿", "短篇", "章节", "对白", "台词"))
        activity_signal = creative_activity or any(word in text for word in ("做过", "旅行", "旅游", "写小说", "活动", "住过", "住宿", "项目"))
        temporal_signal = bool(re.search(r"20\d{2}|今天|昨天|最近|上次|上周|本周|那天|某天|哪天|过去|几月|日期|星期", text))
        strong_conversation = any(word in text for word in ("我们聊", "聊天", "对话", "说过什么", "我说", "和你抱怨"))
        explicit_chat_history = any(word in text for word in ("我们聊", "我们讨论", "聊天记录", "对话记录", "说过什么", "我说过", "和你抱怨"))
        if strong_conversation and not any(word in text for word in ("旅行", "旅游", "实际住宿", "住宿体验")) and (not creative_activity or explicit_chat_history):
            return ("conversation",)
        selected: list[str] = []
        if conversation_signal:
            selected.append("conversation")
        if activity_signal:
            selected.append("activity")
        if temporal_signal:
            selected.append("temporal")
        if not selected:
            selected = ["activity", "temporal", "conversation"]
        if len(selected) > 1:
            return ("mixed",)
        return tuple(selected)


class FileTreeRetriever:
    def __init__(self, memory_root: Path | str, *, raw_memory: RawMemory | None = None) -> None:
        self.root = Path(memory_root)
        self.raw = raw_memory or RawMemory(self.root)

    def retrieve(
        self,
        query: str,
        *,
        memory_type: str | None = None,
        limit: int = 5,
        raw_fallback: bool = True,
        conversation_id: str | None = None,
    ) -> RetrievalResult:
        raw_ref = re.fullmatch(r"raw:(agent|user):([A-Za-z0-9_-]+)", query.strip())
        if raw_ref:
            stream, record_id = raw_ref.groups()
            records = [r for r in self.raw.store(stream).query(record_id=record_id)
                       if conversation_id is None or (r.conversation_id == conversation_id and r.kind == "dialogue")]
            return RetrievalResult(query, ("conversation",) if conversation_id is not None else ("mixed",),
                                   raw_records=tuple(records),
                                   source_refs=tuple(f"raw:{r.stream}:{r.record_id}" for r in records),
                                   raw_fallback=bool(records))
        selected = (memory_type,) if memory_type in MEMORY_TYPES or memory_type == "mixed" else MemoryRouter().route(query)
        types = list(MEMORY_TYPE_ORDER) if selected == ("mixed",) else list(selected)
        if conversation_id is not None:
            types = ["conversation"]
        nodes: list[tuple[float, TreeNode, str]] = []
        for tree_type in types:
            tree = self._load_tree(tree_type)
            if conversation_id is not None:
                # Shared period nodes contain other conversations. Only
                # isolated episode summaries are eligible for User retrieval.
                tree = TextTree("conversation", tree.title, [replace(node, parent_id=None)
                    for node in tree.nodes.values() if node.metadata.get("conversation_id") == conversation_id])
            # Retrieval is deliberately hierarchical.  We first score coarse
            # roots, propagate evidence from matching descendants to those
            # roots, then expand only the winning branches toward fine event
            # nodes.  This keeps the textual tree as the retrieval index and
            # avoids turning the system into an embedding top-k lookup.
            for score, node in self._coarse_to_fine(tree, query, limit):
                nodes.append((score, node, tree_type))
        nodes.sort(key=lambda item: (-item[0], item[1].node_id))
        chosen = nodes[:limit]
        raw_records: list[RawRecord] = []
        fallback_used = False
        for score, node, tree_type in chosen:
            records = self._resolve_node_raw(node)
            if conversation_id is not None:
                records = [r for r in records if r.conversation_id == conversation_id and r.kind == "dialogue"]
            elif tree_type == "activity":
                records = [r for r in records if r.activity_id == node.node_id]
            need_raw = raw_fallback and (
                not node.summary and not node.live_summary and not node.final_summary
                or score <= 1
                or any(word in query for word in ("具体", "细节", "说过什么", "原话", "当时"))
            )
            if need_raw:
                raw_records.extend(records)
                fallback_used = fallback_used or bool(records)
        if raw_fallback and not chosen:
            raw_records.extend(self._raw_search(query, conversation_id=conversation_id))
            fallback_used = bool(raw_records)
        unique_raw = {record.record_id: record for record in raw_records}
        refs = [f"node:{node.node_id}" for _, node, _ in chosen] + [f"raw:{record.stream}:{record.record_id}" for record in unique_raw.values()]
        return RetrievalResult(
            query=query,
            memory_types=tuple(types),
            summary_nodes=tuple(node for _, node, _ in chosen),
            raw_records=tuple(unique_raw.values()),
            source_refs=tuple(refs),
            raw_fallback=fallback_used,
        )

    def _coarse_to_fine(self, tree: TextTree, query: str, limit: int) -> list[tuple[float, TreeNode]]:
        """Return query matches after selecting branches from coarse to fine.

        A leaf match contributes a discounted score to each ancestor.  Roots
        with no evidence are never expanded, and sibling subtrees that do not
        contain evidence are pruned.  The returned score is the local node
        score; the branch selection itself is driven by the propagated score.
        """

        if limit <= 0 or not tree.nodes:
            return []
        own = {node_id: self._score(query, node) for node_id, node in tree.nodes.items()}
        children = {node_id: tree.children(node_id) for node_id in tree.nodes}
        aggregate: dict[str, float] = {}

        def fold(node_id: str, active: set[str] | None = None) -> float:
            if node_id in aggregate:
                return aggregate[node_id]
            active = active or set()
            if node_id in active:
                raise ValueError(f"cycle in retrieval tree at {node_id}")
            active = active | {node_id}
            child_values = [fold(child.node_id, active) for child in children.get(node_id, [])]
            # The strongest child is enough to retain a branch while local
            # summary evidence remains dominant at its own level.
            aggregate[node_id] = own[node_id] + (0.35 * max(child_values, default=0.0))
            return aggregate[node_id]

        roots = tree.roots()
        for root in roots:
            fold(root.node_id)
        winning_roots = [root for root in roots if aggregate[root.node_id] > 0]
        if not winning_roots:
            return []
        winning_roots.sort(key=lambda node: (-aggregate[node.node_id], node.node_id))

        candidates: list[tuple[float, float, int, TreeNode]] = []

        def expand(node: TreeNode, depth: int) -> None:
            branch_score = aggregate[node.node_id]
            if branch_score <= 0:
                return
            candidates.append((own[node.node_id], branch_score, depth, node))
            branch_children = [child for child in children.get(node.node_id, []) if aggregate[child.node_id] > 0]
            branch_children.sort(key=lambda child: (-aggregate[child.node_id], child.node_id))
            for child in branch_children:
                expand(child, depth + 1)

        for root in winning_roots:
            expand(root, 0)
        # Prefer fine nodes with direct lexical evidence, while retaining
        # coarse ancestors as source-bearing context when the budget allows.
        candidates.sort(key=lambda item: (-item[0], -item[2], -item[1], item[3].node_id))
        return [(own_score, node) for own_score, _, _, node in candidates[:limit]]

    def _load_tree(self, tree_type: str) -> TextTree:
        from .text_tree import TextTreeStore

        title = {"activity": "Activity Memory", "temporal": "Temporal Memory", "conversation": "Conversation Memory"}[tree_type]
        return TextTreeStore(self.root / f"{tree_type}_memory.md", tree_type, title).load()

    def _resolve_node_raw(self, node: TreeNode) -> list[RawRecord]:
        records: list[RawRecord] = []
        if node.raw_pointer:
            records.extend(self.raw.store(node.raw_pointer.stream).resolve(node.raw_pointer))
        for value in node.metadata.get("raw_pointers", []):
            from .memory import RawPointer

            pointer = RawPointer.from_dict(value)
            records.extend(self.raw.store(pointer.stream).resolve(pointer))
        for key in ("agent_raw_pointer", "user_raw_pointer"):
            value = node.metadata.get(key)
            if value:
                from .memory import RawPointer

                records.extend(self.raw.store(value["stream"]).resolve(RawPointer.from_dict(value)))
        return records

    def _raw_search(self, query: str, *, conversation_id: str | None = None) -> list[RawRecord]:
        scored: list[tuple[float, RawRecord]] = []
        for record in self.raw.agent.records() + self.raw.user.records():
            if conversation_id is not None and (record.conversation_id != conversation_id or record.kind != "dialogue"):
                continue
            reference = f"raw:{record.stream}:{record.record_id}"
            score = 1000.0 if query.strip() == reference else self._text_score(query, record.text)
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].recorded_at))
        return [record for _, record in scored[:10]]

    @staticmethod
    def _score(query: str, node: TreeNode) -> float:
        if query.strip() in {node.node_id, f"node:{node.node_id}", f"activity:{node.node_id}",
                             f"temporal:{node.node_id}", f"conversation:{node.node_id}"}:
            return 1000.0
        # Rank the same summary that callers actually receive. A stale live
        # field must not select a node whose preferred final summary is unrelated
        # and thereby suppress the genuine raw fallback.
        summary = node.final_summary or node.live_summary or node.summary
        text = " ".join([node.title, summary, json_metadata(node.metadata)])
        relevance = FileTreeRetriever._text_score(query, text)
        # Finalization may rank relevant evidence higher, but cannot make an
        # unrelated memory eligible or suppress the raw fallback.
        return relevance + (0.5 if relevance > 0 and node.final_summary else 0.0)

    @staticmethod
    def _text_score(query: str, text: str) -> float:
        query_terms = set(_terms(query))
        text_terms = set(_terms(text))
        if not query_terms:
            return 0.0
        overlap = len(query_terms & text_terms)
        phrase = 1.0 if query.strip() and query.strip().lower() in text.lower() else 0.0
        return float(overlap) + phrase


def _terms(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", value.lower())


def json_metadata(value: dict) -> str:
    return " ".join(str(item) for item in value.values())
