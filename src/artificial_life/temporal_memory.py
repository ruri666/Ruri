"""Year -> Month -> Week -> Day textual temporal memory."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from pathlib import Path

from .clock import Clock, SystemClock
from .llm import LLMProvider, MockLLMProvider
from .memory import RawMemory, RawPointer, RawRecord
from .models import as_utc
from .text_tree import TextTreeStore, TreeNode


class TemporalMemory:
    def __init__(
        self,
        memory_root: Path | str,
        *,
        clock: Clock | None = None,
        raw_memory: RawMemory | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.root = Path(memory_root)
        self.clock = clock or SystemClock()
        self.raw = raw_memory or RawMemory(self.root)
        self.provider = provider or MockLLMProvider()
        self.store = TextTreeStore(self.root / "temporal_memory.md", "temporal", "Temporal Memory")
        self.tree = self.store.load()

    def _save(self) -> None:
        self.store.save(self.tree)

    def ensure_path(self, moment: datetime) -> list[TreeNode]:
        moment = as_utc(moment)
        day = moment.date()
        iso_calendar = day.isocalendar()
        specs = [
            (f"year:{day.year:04d}", str(day.year), None, "year"),
            (f"month:{day.year:04d}-{day.month:02d}", f"{day.year:04d}-{day.month:02d}", f"year:{day.year:04d}", "month"),
            (f"week:{iso_calendar.year:04d}-W{iso_calendar.week:02d}", f"Week {iso_calendar.week}", f"month:{day.year:04d}-{day.month:02d}", "week"),
            (f"day:{day.isoformat()}", day.isoformat(), f"week:{iso_calendar.year:04d}-W{iso_calendar.week:02d}", "day"),
        ]
        for node_id, title, parent_id, scale in specs:
            if node_id not in self.tree.nodes:
                self.tree.add(TreeNode(node_id=node_id, title=title, parent_id=parent_id, status="active", metadata={"scale": scale}))
        self._save()
        return [self.tree.get(node_id) for node_id, _, _, _ in specs]

    def record(
        self,
        record: RawRecord,
        *,
        summary_text: str | None = None,
        update_parent_summaries: bool = True,
    ) -> list[TreeNode]:
        nodes = self.ensure_path(record.recorded_at)
        day = nodes[-1]
        self._merge_pointer(day, RawPointer(record.stream, record.record_id, record.record_id))
        if summary_text:
            day.live_summary = self._append_summary(day.live_summary, summary_text)
            day.summary = day.live_summary
        if update_parent_summaries:
            self._update_parent_live(nodes[-2::-1])
        self._save()
        return nodes

    def update_live(
        self,
        moment: datetime,
        text: str,
        *,
        source: RawPointer | None = None,
        update_parent_summaries: bool = True,
    ) -> TreeNode:
        nodes = self.ensure_path(moment)
        day = nodes[-1]
        if source:
            self._merge_pointer(day, source)
            refs = list(day.metadata.get("summary_source_refs", []))
            refs.append(f"raw:{source.stream}:{source.start_record}-{source.end_record}")
            day.metadata["summary_source_refs"] = refs[-50:]
        day.live_summary = self._append_summary(day.live_summary, text)
        day.summary = day.live_summary
        if update_parent_summaries:
            self._update_parent_live(nodes[-2::-1])
        self._save()
        return day

    def finalize_day(self, day_id: str, *, at: datetime | None = None) -> TreeNode:
        day = self.tree.get(day_id)
        if not day_id.startswith("day:"):
            raise ValueError("finalize_day expects a day node")
        day.status = "final"
        day.final_summary = day.live_summary or day.summary
        day.end_time = as_utc(at or self.clock.now())
        parent = self.tree.get(day.parent_id) if day.parent_id else None
        if parent is not None:
            self._update_parent_live([parent])
        self._save()
        return day

    def finalize_closed_periods(self, now: datetime | None = None) -> list[str]:
        """Finalize closed day/week/month/year nodes without touching open ones."""

        moment = as_utc(now or self.clock.now())
        current_day = moment.date()
        current_iso = current_day.isocalendar()
        current_month = (current_day.year, current_day.month)
        current_year = current_day.year
        finalized: list[str] = []
        for node in self.tree.nodes.values():
            if node.status == "final":
                continue
            if node.node_id.startswith("day:"):
                node_day = date.fromisoformat(node.node_id.split(":", 1)[1])
                if node_day < current_day:
                    node.status = "final"
                    node.final_summary = node.live_summary or node.summary
                    node.end_time = datetime.combine(node_day, datetime.max.time(), tzinfo=timezone.utc)
                    finalized.append(node.node_id)
            elif node.node_id.startswith("month:"):
                year, month = (int(part) for part in node.node_id.split(":", 1)[1].split("-"))
                if (year, month) < current_month:
                    node.status = "final"
                    node.final_summary = node.live_summary or node.summary
                    finalized.append(node.node_id)
            elif node.node_id.startswith("year:"):
                year = int(node.node_id.split(":", 1)[1])
                if year < current_year:
                    node.status = "final"
                    node.final_summary = node.live_summary or node.summary
                    finalized.append(node.node_id)
            elif node.node_id.startswith("week:"):
                match = re.match(r"week:(\d{4})-W(\d{2})", node.node_id)
                if match and (int(match.group(1)), int(match.group(2))) < (current_iso.year, current_iso.week):
                    node.status = "final"
                    node.final_summary = node.live_summary or node.summary
                    finalized.append(node.node_id)
        if finalized:
            self._save()
        return finalized

    def active_path(self, moment: datetime | None = None) -> list[TreeNode]:
        nodes = self.ensure_path(moment or self.clock.now())
        return nodes

    def recent_days(self, count: int = 3) -> list[TreeNode]:
        days = [node for node in self.tree.nodes.values() if node.node_id.startswith("day:")]
        return sorted(days, key=lambda node: node.node_id, reverse=True)[:count]

    def _merge_pointer(self, node: TreeNode, pointer: RawPointer) -> None:
        pointers = list(node.metadata.get("raw_pointers", []))
        if node.raw_pointer is None:
            node.raw_pointer = pointer
        elif node.raw_pointer.stream == pointer.stream:
            node.raw_pointer = RawPointer(node.raw_pointer.stream, node.raw_pointer.start_record, pointer.end_record)
        else:
            pointers.append(node.raw_pointer.to_dict())
        pointers.append(pointer.to_dict())
        node.metadata["raw_pointers"] = pointers[-20:]

    def _update_parent_live(self, parents: list[TreeNode]) -> None:
        for parent in parents:
            children = self.tree.children(parent.node_id)
            snippets = [child.final_summary or child.live_summary or child.summary for child in children]
            snippets = [snippet for snippet in snippets if snippet]
            if snippets:
                scale = parent.metadata.get("scale", "period")
                source_text = "；".join(_strip_scale_prefix(snippet) for snippet in snippets)
                result = self.provider.summarize(source_text[:1200], purpose=f"temporal_{scale}_live", source_refs=[child.node_id for child in children], scale=scale)
                parent.live_summary = result.text[:1400]
                parent.summary = parent.live_summary
                parent.metadata["summary_source_refs"] = [child.node_id for child in children]

    @staticmethod
    def _append_summary(existing: str, addition: str) -> str:
        if not existing:
            return addition.strip()
        if addition.strip() in existing:
            return existing
        return f"{existing}；{addition.strip()}"[-1600:]


def _strip_scale_prefix(value: str) -> str:
    return re.sub(r"(?:年度|本月|本周|当日)摘要：", "", value).strip()
