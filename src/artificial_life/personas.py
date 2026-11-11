"""Persona management and workspace isolation for Artificial Life.

Each persona owns an independent directory with canonical memory, activity tree,
schedule tree, timeline, and deliverables.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, atomic_write_text, read_json

LOCAL_TZ = timezone(timedelta(hours=8))


def resolve_workspace(root, persona_id=None, persona=None):
    root = Path(root).resolve()
    mgr = PersonaManager(root)
    mgr.ensure_layout()
    if persona_id is None:
        if persona is not None:
            raise ValueError("persona requires persona_id")
        active = mgr.get_active_persona_id()
        return mgr.get_workspace(active)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", persona_id):
        raise ValueError("Invalid persona_id")
    if persona_id.upper().split('.')[0] in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
        raise ValueError("Reserved persona_id")
    workspace = root / "personas" / persona_id
    path = workspace / "persona.json"
    profile = read_json(path, None)
    if profile is None:
        if not isinstance(persona, str) or not persona.strip() or len(persona) > 8000:
            raise ValueError("A new persona requires 1..8000 characters")
        if workspace.exists() and any(workspace.iterdir()):
            raise ValueError("Persona workspace is not empty")
        atomic_write_json(path, {"version": 1, "id": persona_id, "name": persona_id, "definition": persona.strip()})
    elif profile.get("version") != 1 or profile.get("id") != persona_id or not isinstance(profile.get("definition"), str) or not profile["definition"].strip():
        raise ValueError("Invalid persona profile")
    elif persona is not None and persona.strip() != profile["definition"]:
        raise ValueError("Use a new persona_id for a different persona; existing memory stays isolated")
    return workspace


def persona_arguments(parser):
    parser.add_argument("--persona-id", help="Isolated persona workspace ID under ROOT/personas")
    parser.add_argument("--persona", help="Exact user-defined persona; required on first use of an ID")


class PersonaManager:
    """Manages multi-persona directories under a shared base root."""

    def __init__(self, base_root: Path | str) -> None:
        self.base_root = Path(base_root).resolve()
        self.personas_dir = self.base_root / "personas"
        self.config_dir = self.base_root / "config"
        self.active_file = self.config_dir / "active_persona.json"

    def ensure_layout(self) -> None:
        """Ensure the personas directory exists and migrate legacy root if needed."""
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        existing_personas = [p for p in self.personas_dir.iterdir() if p.is_dir() and (p / "persona.json").exists()]
        if existing_personas:
            if not self.active_file.exists():
                atomic_write_json(self.active_file, {"active": existing_personas[0].name})
            return

        # Migration from legacy root where manifest.json was at base_root
        legacy_manifest = self.base_root / "manifest.json"
        target_id = "lindeng"
        target_dir = self.personas_dir / target_id

        if legacy_manifest.exists() and not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)
            # Copy contents into target_dir
            for item in self.base_root.iterdir():
                if item.name in {"personas", "runtime", ".writer.lock", "personas.json"}:
                    continue
                dest = target_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, dest)

            definition = (
                "我是林灯，一位日常幻想轻小说作者。姓名与职业是本会话创作角色设定，不是真实人物经历。"
                "温和、好奇、务实；少说空话，先产出再报告。偏爱小人物、轻微奇幻与有余味的结尾。"
                "没有设定的经历不编造；计划不当成果。"
            )
            atomic_write_json(target_dir / "persona.json", {
                "version": 1,
                "id": target_id,
                "name": "林灯 · 日常幻想轻小说作者",
                "definition": definition,
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            atomic_write_json(self.active_file, {"active": target_id})
        elif not any(self.personas_dir.iterdir()):
            # Fresh initialization of a default persona
            self.create_persona(
                target_id,
                name="林灯 · 日常幻想轻小说作者",
                definition=(
                    "我是林灯，一位日常幻想轻小说作者。姓名与职业是本会话创作角色设定，不是真实人物经历。"
                    "温和、好奇、务实；少说空话，先产出再报告。偏爱小人物、轻微奇幻与有余味的结尾。"
                    "没有设定的经历不编造；计划不当成果。"
                )
            )
            atomic_write_json(self.active_file, {"active": target_id})

    def get_active_persona_id(self) -> str:
        data = read_json(self.active_file, {})
        active = data.get("active")
        if active and (self.personas_dir / active).exists():
            return active
        personas = self.list_personas()
        if personas:
            self.set_active_persona_id(personas[0]["id"])
            return personas[0]["id"]
        return "lindeng"

    def set_active_persona_id(self, persona_id: str) -> None:
        target = self.personas_dir / persona_id
        if not target.exists() or not (target / "persona.json").exists():
            raise ValueError(f"人格 '{persona_id}' 不存在")
        atomic_write_json(self.active_file, {"active": persona_id})

    def get_workspace(self, persona_id: str | None = None) -> Path:
        pid = persona_id or self.get_active_persona_id()
        workspace = self.personas_dir / pid
        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def list_personas(self) -> list[dict[str, Any]]:
        self.ensure_layout()
        active_id = self.get_active_persona_id()
        results = []
        for p in sorted(self.personas_dir.iterdir()):
            if not p.is_dir():
                continue
            profile = read_json(p / "persona.json", None)
            if not profile:
                continue
            works_dir = p / "works"
            works_count = len([f for f in works_dir.rglob("*.md") if f.is_file()]) if works_dir.exists() else 0
            
            activity_count = 0
            try:
                tree_data = read_json(p / "trees" / "overview.json", {})
                activity_count = len(tree_data.get("activity_tree", []))
            except Exception:
                pass

            results.append({
                "id": profile.get("id", p.name),
                "name": profile.get("name", p.name),
                "definition": profile.get("definition", ""),
                "is_active": profile.get("id", p.name) == active_id,
                "created_at": profile.get("created_at", ""),
                "works_count": works_count,
                "activity_count": activity_count,
            })
        return results

    def create_persona(self, persona_id: str, name: str, definition: str) -> dict[str, Any]:
        persona_id = persona_id.strip()
        name = name.strip() or persona_id
        definition = definition.strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", persona_id):
            raise ValueError("人格ID只能包含英文字母、数字、下划线或连字符，且长度在1-64位")
        if not definition:
            raise ValueError("核心人设描述不能为空")

        workspace = self.personas_dir / persona_id
        if workspace.exists() and any(workspace.iterdir()):
            raise ValueError(f"人格 '{persona_id}' 已存在")

        workspace.mkdir(parents=True, exist_ok=True)
        profile = {
            "version": 1,
            "id": persona_id,
            "name": name,
            "definition": definition,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        atomic_write_json(workspace / "persona.json", profile)

        # Initialize the persistent agent in this workspace
        from .persistent_agent import PersistentLifeAgent
        from .runtime import RuntimeConfig
        from .plugin import HarnessProvider

        PersistentLifeAgent.initialize(
            workspace,
            provider=HarnessProvider(),
            config=RuntimeConfig(persona=definition)
        )
        return profile

    def update_persona(self, persona_id: str, definition: str, name: str | None = None) -> dict[str, Any]:
        workspace = self.get_workspace(persona_id)
        path = workspace / "persona.json"
        profile = read_json(path, {})
        if not profile:
            raise ValueError(f"人格 '{persona_id}' 不存在")

        definition = definition.strip()
        if not definition:
            raise ValueError("核心人设描述不能为空")

        profile["definition"] = definition
        if name and name.strip():
            profile["name"] = name.strip()
        atomic_write_json(path, profile)

        # Update runtime config
        cfg_path = workspace / "config" / "runtime.json"
        if cfg_path.exists():
            cfg = read_json(cfg_path, {})
            cfg["persona"] = definition
            atomic_write_json(cfg_path, cfg)

        # Update manifest.json
        manifest_path = workspace / "manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path, {})
            manifest["persona"] = definition
            atomic_write_json(manifest_path, manifest)

        # Update core memory markdown
        core_path = workspace / "memory" / "core_memory.md"
        if core_path.exists():
            from .core_memory import CoreMemoryStore, DEFAULT_CORE
            store = CoreMemoryStore(core_path, title="Core Memory", defaults=DEFAULT_CORE)
            sections = store.read()
            sections["Identity"] = definition
            sections["Persona"] = definition
            store.write(sections)

        return profile

    def reset_persona(self, persona_id: str, new_definition: str | None = None, new_name: str | None = None) -> dict[str, Any]:
        """Reset all past memories, activity trees, schedule trees, timeline, and works to zero.
        Preserves or updates core persona definition.
        """
        workspace = self.get_workspace(persona_id)
        profile = read_json(workspace / "persona.json", {})
        if not profile:
            raise ValueError(f"人格 '{persona_id}' 不存在")

        definition = new_definition.strip() if new_definition and new_definition.strip() else profile.get("definition", "")
        name = new_name.strip() if new_name and new_name.strip() else profile.get("name", persona_id)

        # Clean all directories and files except persona.json
        for item in workspace.iterdir():
            if item.name == "persona.json":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file():
                item.unlink(missing_ok=True)

        profile.update({"name": name, "definition": definition, "reset_at": datetime.now(timezone.utc).isoformat()})
        atomic_write_json(workspace / "persona.json", profile)

        # Re-initialize clean agent
        from .persistent_agent import PersistentLifeAgent
        from .runtime import RuntimeConfig
        from .plugin import HarnessProvider

        PersistentLifeAgent.initialize(
            workspace,
            provider=HarnessProvider(),
            config=RuntimeConfig(persona=definition)
        )
        return profile

    def delete_persona(self, persona_id: str) -> None:
        """Directly delete the persona and all its associated memory and deliverables data."""
        personas = self.list_personas()
        if len(personas) <= 1:
            raise ValueError("至少需要保留一个人格，无法删除唯一个人格")

        workspace = self.get_workspace(persona_id)
        if not workspace.exists():
            raise ValueError(f"人格 '{persona_id}' 不存在")

        shutil.rmtree(workspace, ignore_errors=True)

        # If deleted active persona, switch to another remaining one
        if self.get_active_persona_id() == persona_id:
            remaining = [p for p in self.list_personas() if p["id"] != persona_id]
            if remaining:
                self.set_active_persona_id(remaining[0]["id"])

    def get_works_by_date(self, persona_id: str | None = None) -> dict[str, Any]:
        """Collect all works/deliverables in workspace, extract readable titles, and group by day."""
        workspace = self.get_workspace(persona_id)
        works_dir = workspace / "works"
        if not works_dir.exists():
            return {"total_count": 0, "days": []}

        items = []
        for file_path in works_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".md", ".txt", ".json", ".html"}:
                continue

            rel_path = str(file_path.relative_to(workspace)).replace("\\", "/")
            stat = file_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=LOCAL_TZ)
            date_str = mtime.strftime("%Y-%m-%d")
            time_str = mtime.strftime("%H:%M:%S")

            # Extract human-readable title and clean excerpt
            title = ""
            excerpt = ""
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("<!--")]
                if lines:
                    candidate = lines[0]
                    # Strip leading markdown header markers or list bullets
                    candidate = re.sub(r"^[#*>\-\s]+", "", candidate).strip()
                    title = candidate
                    # Find body excerpt that is not the title line
                    body_lines = []
                    for l in lines[1:]:
                        clean_l = re.sub(r"^[#*>\-\s]+", "", l).strip()
                        if clean_l and not clean_l.startswith("==") and not clean_l.startswith("--"):
                            body_lines.append(clean_l)
                        if sum(len(x) for x in body_lines) >= 160:
                            break
                    if body_lines:
                        excerpt = " ".join(body_lines)[:180].strip()
                if not excerpt and content:
                    clean_content = re.sub(r"[#*`>]+", "", content).strip()
                    excerpt = clean_content[:180].strip()
            except Exception:
                pass

            if not title:
                title = file_path.stem

            size_kb = max(0.1, round(stat.st_size / 1024, 1))
            items.append({
                "name": file_path.name,
                "title": title,
                "path": rel_path,
                "date": date_str,
                "time": time_str,
                "datetime": mtime.isoformat(),
                "size_text": f"{size_kb} KB",
                "excerpt": excerpt
            })

        # Group by date descending
        by_day: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_day.setdefault(item["date"], []).append(item)

        day_groups = []
        for d in sorted(by_day.keys(), reverse=True):
            day_items = sorted(by_day[d], key=lambda x: x["datetime"], reverse=True)
            day_groups.append({
                "date": d,
                "count": len(day_items),
                "items": day_items
            })

        return {
            "total_count": len(items),
            "days": day_groups
        }
