"""Stable self and user core memory Markdown files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .models import iso
from .storage import atomic_write_text


DEFAULT_CORE = {
    "Identity": "轻小说作家人造生命 Agent",
    "Persona": "轻小说作家",
    "Stable Personality": "",
    "Long-term Preferences": "",
    "Long-term Goals": "",
    "Important Experiences": "",
    "Important Relationships": "",
}

DEFAULT_USER_CORE = {
    "Important Facts": "",
    "Stable Preferences": "",
    "Communication Habits": "",
    "Relationship": "",
    "Shared Experiences": "",
    "Important Agreements": "",
    "Long-term Goals": "",
    "Stable Understanding": "",
}


class CoreMemoryStore:
    def __init__(self, path: Path | str, *, title: str, defaults: dict[str, str]) -> None:
        self.path = Path(path)
        self.title = title
        self.defaults = dict(defaults)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, str]:
        if not self.path.exists():
            return dict(self.defaults)
        sections: dict[str, str] = {}
        current: str | None = None
        body: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if current is not None:
                    sections[current] = "\n".join(body).strip()
                current = line[3:].strip()
                body = []
            elif current is not None and not line.startswith("# "):
                body.append(line)
        if current is not None:
            sections[current] = "\n".join(body).strip()
        return {**self.defaults, **sections}

    def write(self, sections: dict[str, str]) -> None:
        merged = {**self.defaults, **sections}
        lines = [f"# {self.title}", "<!-- core_memory schema_version:1 -->", ""]
        for name, value in merged.items():
            lines.extend([f"## {name}", value.strip(), ""])
        atomic_write_text(self.path, "\n".join(lines).rstrip() + "\n")

    def merge(self, section: str, value: str, *, source_refs: list[str] | None = None, at: datetime | None = None) -> dict[str, str]:
        sections = self.read()
        stamp = f"\n\n[{iso(at) if at else 'undated'}]"
        refs = f" Sources: {', '.join(source_refs)}." if source_refs else ""
        addition = f"{stamp}{refs}\n{value.strip()}"
        sections[section] = (sections.get(section, "") + addition).strip()
        self.write(sections)
        return sections

