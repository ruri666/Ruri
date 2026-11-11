"""Local Web UI for the Persistent Life Agent with Persona Management.

The adapter exposes read-only state/timeline views, multi-persona lifecycle management,
and grouped deliverables.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .plugin import PluginService, TOOLS, load_agent
from .lease import RootLease
from .persistent_agent import PersistentLifeAgent
from .personas import PersonaManager
from .runner import HeartbeatRunner, RunnerConfig


MAX_REQUEST_BYTES = 512 * 1024


@dataclass(frozen=True)
class WebUIConfig:
    """Network and conversation defaults for the local UI."""

    host: str = "127.0.0.1"
    port: int = 8765
    conversation_id: str = "default"
    title: str = "Persistent Life Agent"
    auto_heartbeat: bool = True
    runner_poll_interval_seconds: float = 1.0
    heartbeat_interval_minutes: int = 30

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("Web UI host cannot be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError("Web UI port must be between 0 and 65535")
        if not self.conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if self.runner_poll_interval_seconds <= 0:
            raise ValueError("runner_poll_interval_seconds must be positive")
        if self.heartbeat_interval_minutes <= 0:
            raise ValueError("heartbeat_interval_minutes must be positive")


class AgentWebUIServer(ThreadingHTTPServer):
    """Threaded local server with one serialized canonical Agent writer."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], agent: PersistentLifeAgent | PluginService, config: WebUIConfig,
                 persona_manager: PersonaManager | None = None, lifecycle: Any = None, host: Any = None) -> None:
        super().__init__(address, AgentWebUIHandler)
        self.tools = agent if isinstance(agent, PluginService) else PluginService(agent)
        self.agent = self.tools.agent
        self.config = config
        self.agent_lock = self.agent.lock
        self.runner: HeartbeatRunner | None = None
        self.persona_manager = persona_manager
        self.lifecycle = lifecycle
        self.host = host

    def start_runner(self) -> None:
        if not self.config.auto_heartbeat or self.runner is not None:
            return
        self.runner = self.tools.runner
        self.runner.config = RunnerConfig(self.config.runner_poll_interval_seconds)
        self.runner.start()

    def shutdown(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self.runner = None
        super().shutdown()

    def switch_persona(self, persona_id: str) -> str:
        if not self.persona_manager:
            raise ValueError("PersonaManager not configured")
        with self.agent_lock:
            if self.runner:
                self.runner.stop()
                self.runner = None
            if self.lifecycle:
                self.lifecycle.stop()
            workspace = self.persona_manager.get_workspace(persona_id)
            new_agent = load_agent(workspace, harness=True)
            self.agent = new_agent
            self.tools.agent = new_agent
            self.agent_lock = new_agent.lock
            self.tools.runner = HeartbeatRunner(new_agent)
            if self.host:
                self.host.root = workspace.resolve()
                self.host.folder = self.host.root / "runtime/host-jobs"
                self.host.folder.mkdir(parents=True, exist_ok=True)
                self.host.pending.clear()
            if self.lifecycle:
                from .lifecycle import Lifecycle
                mode = self.lifecycle.state.get("mode", "continuous")
                self.lifecycle = Lifecycle(self.tools, self.host, interval=self.lifecycle.interval, mode=mode)
                self.tools.lifecycle = self.lifecycle
                self.lifecycle.start()
            self.start_runner()
            self.persona_manager.set_active_persona_id(persona_id)
            return persona_id

    def update_core_persona(self, persona_id: str, definition: str, name: str | None = None) -> dict[str, Any]:
        if not self.persona_manager:
            raise ValueError("PersonaManager not configured")
        profile = self.persona_manager.update_persona(persona_id, definition, name)
        if persona_id == self.persona_manager.get_active_persona_id():
            with self.agent_lock:
                core_sections = self.agent.core.read()
                core_sections["Identity"] = definition
                core_sections["Persona"] = definition
                self.agent.core.write(core_sections)
                from dataclasses import replace
                self.agent.runtime.config = replace(self.agent.runtime.config, persona=definition)
        return profile

    def reset_persona(self, persona_id: str, new_definition: str | None = None, new_name: str | None = None) -> dict[str, Any]:
        if not self.persona_manager:
            raise ValueError("PersonaManager not configured")
        is_active = persona_id == self.persona_manager.get_active_persona_id()
        if is_active:
            if self.runner:
                self.runner.stop()
                self.runner = None
            if self.lifecycle:
                self.lifecycle.stop()
        profile = self.persona_manager.reset_persona(persona_id, new_definition, new_name)
        if is_active:
            workspace = self.persona_manager.get_workspace(persona_id)
            new_agent = load_agent(workspace, harness=True)
            self.agent = new_agent
            self.tools.agent = new_agent
            self.agent_lock = new_agent.lock
            self.tools.runner = HeartbeatRunner(new_agent)
            if self.host:
                self.host.root = workspace.resolve()
                self.host.folder = self.host.root / "runtime/host-jobs"
                self.host.folder.mkdir(parents=True, exist_ok=True)
                self.host.pending.clear()
            if self.lifecycle:
                from .lifecycle import Lifecycle
                mode = self.lifecycle.state.get("mode", "continuous")
                self.lifecycle = Lifecycle(self.tools, self.host, interval=self.lifecycle.interval, mode=mode)
                self.tools.lifecycle = self.lifecycle
                self.lifecycle.start()
            self.start_runner()
        return profile

    def delete_persona(self, persona_id: str) -> str:
        if not self.persona_manager:
            raise ValueError("PersonaManager not configured")
        is_active = persona_id == self.persona_manager.get_active_persona_id()
        if is_active:
            remaining = [p for p in self.persona_manager.list_personas() if p["id"] != persona_id]
            if not remaining:
                raise ValueError("至少需要保留一个人格，无法删除唯一个人格")
            self.switch_persona(remaining[0]["id"])
        self.persona_manager.delete_persona(persona_id)
        return self.persona_manager.get_active_persona_id()


class AgentWebUIHandler(BaseHTTPRequestHandler):
    """HTTP routes for the browser client."""

    server_version = "ArtificialLifeWebUI/2.0"

    @property
    def app_server(self) -> AgentWebUIServer:
        return cast(AgentWebUIServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(WEB_UI_HTML.replace("__TITLE__", self.app_server.config.title))
            return
        if parsed.path == "/api/personas":
            if not self.app_server.persona_manager:
                self._send_json({"active": "default", "personas": []})
                return
            self._send_json({
                "active": self.app_server.persona_manager.get_active_persona_id(),
                "personas": self.app_server.persona_manager.list_personas()
            })
            return
        if parsed.path == "/api/works":
            if not self.app_server.persona_manager:
                self._send_json({"total_count": 0, "days": []})
                return
            active_id = self.app_server.persona_manager.get_active_persona_id()
            self._send_json(self.app_server.persona_manager.get_works_by_date(active_id))
            return
        if parsed.path == "/api/work-preview":
            query = parse_qs(parsed.query)
            rel_path = query.get("path", [""])[0].strip()
            if not rel_path:
                self._send_json({"error": "path required"}, status=HTTPStatus.BAD_REQUEST)
                return
            workspace = self.app_server.agent.root
            target = (workspace / rel_path).resolve()
            if not target.is_relative_to(workspace / "works") or not target.is_file():
                self._send_json({"error": "file not found"}, status=HTTPStatus.NOT_FOUND)
                return
            content = target.read_text(encoding="utf-8", errors="replace")
            title = ""
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("<!--"):
                    title = re.sub(r"^[#*>\-\s]+", "", line).strip()
                    break
            self._send_json({
                "name": target.name,
                "title": title or target.stem,
                "path": rel_path,
                "content": content,
                "size": target.stat().st_size
            })
            return
        if parsed.path == "/api/tools":
            self._send_json({"tools": [{"name": name, **definition} for name, definition in TOOLS.items()]})
            return
        if parsed.path == "/api/work-file":
            query = parse_qs(parsed.query)
            rel_path = query.get("path", [""])[0].strip()
            workspace = self.app_server.agent.root
            target = (workspace / rel_path).resolve() if rel_path else None
            if target and target.is_relative_to(workspace / "works") and target.is_file():
                content = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            try:
                file = self.app_server.tools.call("work_file", {
                    "path": rel_path,
                    "conversation_id": query.get("conversation_id", [self.app_server.config.conversation_id])[0]})
                content = file["content"].encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="' + file["name"] + '"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            except Exception:
                self._send_json({"error": "work_file_unavailable"}, status=HTTPStatus.NOT_FOUND)
                return
        if parsed.path == "/api/health":
            self._send_json(self._health())
            return
        if parsed.path == "/api/state":
            self._send_json(self._state())
            return
        if parsed.path == "/api/structures":
            self._send_json(self.app_server.tools.call("structure_get"))
            return
        if parsed.path == "/api/work":
            query = parse_qs(parsed.query)
            self._send_json(self.app_server.tools.call("work_list", {
                "conversation_id": query.get("conversation_id", [self.app_server.config.conversation_id])[0]}))
            return
        if parsed.path == "/api/dialogue":
            query = parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [self.app_server.config.conversation_id])[0]
            limit = _bounded_int(query.get("limit", ["100"])[0], 1, 200)
            self._send_json(self._dialogue(conversation_id, limit))
            return
        if parsed.path == "/api/timeline":
            query = parse_qs(parsed.query)
            limit = _bounded_int(query.get("limit", ["50"])[0], 1, 200)
            self._send_json(self._timeline(limit))
            return
        self._send_json({"error": "not_found", "path": parsed.path}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/personas/select":
                active = self.app_server.switch_persona(body.get("persona_id"))
                self._send_json({"ok": True, "active": active})
                return
            if parsed.path == "/api/personas/create":
                created = self.app_server.persona_manager.create_persona(
                    body.get("persona_id", ""),
                    body.get("name", ""),
                    body.get("definition", "")
                )
                self.app_server.switch_persona(created["id"])
                self._send_json({"ok": True, "persona": created, "active": created["id"]})
                return
            if parsed.path == "/api/personas/update":
                updated = self.app_server.update_core_persona(
                    body.get("persona_id", ""),
                    body.get("definition", ""),
                    body.get("name")
                )
                self._send_json({"ok": True, "persona": updated})
                return
            if parsed.path == "/api/personas/reset":
                reset = self.app_server.reset_persona(
                    body.get("persona_id", ""),
                    body.get("definition"),
                    body.get("name")
                )
                self._send_json({"ok": True, "persona": reset})
                return
            if parsed.path == "/api/personas/delete":
                active = self.app_server.delete_persona(body.get("persona_id", ""))
                self._send_json({"ok": True, "active": active})
                return
            if parsed.path == "/api/tools/call":
                self._send_json(self.app_server.tools.call(body.get("name"), body.get("arguments")))
                return
            if parsed.path == "/api/message":
                self._send_json(self._message(body), status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/message/stream":
                self._stream_message(body)
                return
            if parsed.path == "/api/heartbeat":
                self._send_json(self._heartbeat(body))
                return
            self._send_json({"error": "not_found", "path": parsed.path}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._send_json({"error": "internal_error", "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _health(self):
        return self.app_server.tools.call("health")

    def _state(self):
        return self.app_server.tools.call("state")

    def _dialogue(self, conversation_id, limit):
        return self.app_server.tools.call("dialogue_history", {"conversation_id": conversation_id, "limit": limit})

    def _timeline(self, limit):
        return self.app_server.tools.call("timeline", {"limit": limit})

    def _message(self, body):
        return self.app_server.tools.call("dialogue_reply", {
            "text": _required_text(body, "text", max_length=10000),
            "conversation_id": body.get("conversation_id") or self.app_server.config.conversation_id})

    def _stream_message(self, body):
        from contextlib import closing
        text = _required_text(body, "text", max_length=10000)
        conversation_id = body.get("conversation_id") or self.app_server.config.conversation_id
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            with closing(self.app_server.tools.stream(text, conversation_id)) as stream:
                for event in stream:
                    self.wfile.write(event.to_sse())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _heartbeat(self, body):
        return self.app_server.tools.call("heartbeat", body)

    def _read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f"request body exceeds {MAX_REQUEST_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, value: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def create_server(agent: PersistentLifeAgent | PluginService, config: WebUIConfig | None = None,
                  persona_manager: PersonaManager | None = None, lifecycle: Any = None, host: Any = None) -> AgentWebUIServer:
    config = config or WebUIConfig()
    return AgentWebUIServer((config.host, config.port), agent, config, persona_manager=persona_manager, lifecycle=lifecycle, host=host)


def serve(agent: PersistentLifeAgent, config: WebUIConfig | None = None, persona_manager: PersonaManager | None = None) -> None:
    server = create_server(agent, config, persona_manager=persona_manager)
    print(f"Persistent Life Agent Web UI: http://{server.server_address[0]}:{server.server_address[1]}")
    server.start_runner()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.runner is not None:
            server.runner.stop()
        server.server_close()


def load_or_initialize(root: Path, *, heartbeat_interval: timedelta | None = None) -> PersistentLifeAgent:
    return load_agent(root, heartbeat_interval=heartbeat_interval)


def main(argv: list[str] | None = None) -> None:
    from .personas import persona_arguments
    parser = argparse.ArgumentParser(description="Run the local Persistent Life Agent Web UI")
    persona_arguments(parser)
    parser.add_argument("--root", type=Path, default=Path("var/web_agent"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--conversation-id", default="default")
    parser.add_argument("--heartbeat-minutes", type=int, default=30)
    parser.add_argument("--harness", action="store_true", help="Record input for the harness; never call a nested LLM")
    args = parser.parse_args(argv)

    mgr = PersonaManager(args.root)
    mgr.ensure_layout()
    active_id = args.persona_id or mgr.get_active_persona_id()
    workspace = mgr.get_workspace(active_id)

    with RootLease(args.root):
        agent = load_agent(workspace, harness=args.harness, heartbeat_interval=timedelta(minutes=args.heartbeat_minutes))
        serve(agent, WebUIConfig(args.host, args.port, args.conversation_id, heartbeat_interval_minutes=args.heartbeat_minutes), persona_manager=mgr)


def _required_text(body: dict[str, Any], key: str, *, max_length: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{key} exceeds {max_length} characters")
    return value.strip()


def _bounded_int(value: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("invalid integer query parameter") from exc
    return max(minimum, min(maximum, parsed))


WEB_UI_HTML = (Path(__file__).parent / 'assets' / 'webui.html').read_text(encoding='utf-8')


if __name__ == "__main__":
    main()
