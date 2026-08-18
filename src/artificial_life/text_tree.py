"""Human-readable, one-file Markdown memory trees."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory import RawPointer
from .models import as_utc, iso
from .storage import atomic_write_text


@dataclass
class TreeNode:
    node_id: str
    title: str
    parent_id: str | None = None
    status: str = "active"
    summary: str = ""
    live_summary: str = ""
    final_summary: str = ""
    raw_pointer: RawPointer | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_time is not None:
            self.start_time = as_utc(self.start_time)
        if self.end_time is not None:
            self.end_time = as_utc(self.end_time)


class TextTree:
    def __init__(self, tree_type: str, title: str, nodes: list[TreeNode] | None = None) -> None:
        self.tree_type = tree_type
        self.title = title
        self.nodes: dict[str, TreeNode] = {}
        for node in nodes or []:
            self.nodes[node.node_id] = node
        self.validate()

    def add(self, node: TreeNode) -> TreeNode:
        if node.node_id in self.nodes:
            raise ValueError(f"duplicate tree node: {node.node_id}")
        self.nodes[node.node_id] = node
        self.validate()
        return node

    def upsert(self, node: TreeNode) -> TreeNode:
        self.nodes[node.node_id] = node
        self.validate()
        return node

    def get(self, node_id: str) -> TreeNode:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown tree node: {node_id}") from exc

    def children(self, node_id: str | None) -> list[TreeNode]:
        return [node for node in self.nodes.values() if node.parent_id == node_id]

    def roots(self) -> list[TreeNode]:
        return self.children(None)

    def path(self, node_id: str) -> list[TreeNode]:
        result: list[TreeNode] = []
        current: str | None = node_id
        while current is not None:
            node = self.get(current)
            result.append(node)
            current = node.parent_id
        result.reverse()
        return result

    def validate(self) -> None:
        for node in self.nodes.values():
            if node.parent_id is not None and node.parent_id not in self.nodes:
                raise ValueError(f"node {node.node_id} references unknown parent {node.parent_id}")
            seen: set[str] = set()
            current: str | None = node.node_id
            while current is not None:
                if current in seen:
                    raise ValueError(f"cycle in memory tree at {current}")
                seen.add(current)
                parent = self.nodes.get(current)
                current = parent.parent_id if parent else None


class TextTreeStore:
    def __init__(self, path: Path | str, tree_type: str, title: str) -> None:
        self.path = Path(path)
        self.tree_type = tree_type
        self.title = title
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> TextTree:
        if not self.path.exists():
            return TextTree(self.tree_type, self.title)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        nodes: list[TreeNode] = []
        current: dict[str, Any] | None = None
        for line in lines:
            if re.match(r"^#+ Node: .+$", line):
                if current is not None:
                    nodes.append(self._node_from_values(current))
                current = {"node_id": line.split(": ", 1)[1].strip()}
                continue
            if current is None or not line or line.startswith("# ") or line.startswith("<!--"):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current[key.strip().lower().replace(" ", "_")] = value.strip()
        if current is not None:
            nodes.append(self._node_from_values(current))
        tree = TextTree(self.tree_type, self.title, nodes)
        validate_provenance(tree)
        return tree

    def save(self, tree: TextTree) -> None:
        tree.validate()
        validate_provenance(tree)
        lines = [f"# {tree.title}", f"<!-- tree_type:{tree.tree_type} schema_version:1 -->", ""]
        for root in tree.roots():
            self._render_node(tree, root, 2, lines)
        atomic_write_text(self.path, "\n".join(lines).rstrip() + "\n")

    def _render_node(self, tree: TextTree, node: TreeNode, depth: int, lines: list[str]) -> None:
        lines.extend(
            [
                f"{'#' * depth} Node: {node.node_id}",
                f"Title: {_single_line(node.title)}",
                f"Parent: {node.parent_id or 'null'}",
                f"Status: {_single_line(node.status)}",
                f"Start: {iso(node.start_time) or ''}",
                f"End: {iso(node.end_time) or ''}",
                f"Summary: {_single_line(node.summary)}",
                f"Live Summary: {_single_line(node.live_summary)}",
                f"Final Summary: {_single_line(node.final_summary)}",
                f"Raw: {node.raw_pointer.as_text() if node.raw_pointer else ''}",
                f"Meta: {json.dumps(node.metadata, ensure_ascii=False, sort_keys=True)}",
                "",
            ]
        )
        for child in tree.children(node.node_id):
            self._render_node(tree, child, depth + 1, lines)

    def _node_from_values(self, values: dict[str, Any]) -> TreeNode:
        def time_value(key: str) -> datetime | None:
            value = values.get(key, "")
            return as_utc(value) if value else None

        raw = RawPointer.from_text(values.get("raw", ""))
        try:
            metadata = json.loads(values.get("meta", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid tree metadata for {values['node_id']}") from exc
        return TreeNode(
            node_id=values["node_id"],
            title=values.get("title", values["node_id"]),
            parent_id=None if values.get("parent", "null") in {"", "null", "None"} else values["parent"],
            status=values.get("status", "active"),
            summary=values.get("summary", ""),
            live_summary=values.get("live_summary", ""),
            final_summary=values.get("final_summary", ""),
            raw_pointer=raw,
            start_time=time_value("start"),
            end_time=time_value("end"),
            metadata=metadata,
        )


def _single_line(value: str) -> str:
    return " ".join(str(value).splitlines()).strip()


def validate_provenance(tree: TextTree) -> None:
    """Reject a derived summary node that has no traceable source."""

    for node in tree.nodes.values():
        has_summary = bool(node.summary or node.live_summary or node.final_summary)
        has_source = bool(
            node.raw_pointer
            or node.metadata.get("summary_source_refs")
            or node.metadata.get("raw_pointers")
            or node.metadata.get("agent_raw_pointer")
            or node.metadata.get("user_raw_pointer")
        )
        if has_summary and not has_source:
            raise ValueError(f"summary node {node.node_id} has no source reference")
