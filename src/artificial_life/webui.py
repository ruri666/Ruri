"""Small local Web UI for the Persistent Life Agent.

The adapter intentionally uses only the Python standard library.  It exposes
read-only state/timeline views and explicit commands for dialogue and
Heartbeat; it does not turn a user message into a hidden timeline fact or
mutate canonical memory outside the existing ``PersistentLifeAgent`` APIs.
"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .persistent_agent import PersistentHeartbeatResult, PersistentLifeAgent
from .models import iso


MAX_REQUEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class WebUIConfig:
    """Network and conversation defaults for the local UI."""

    host: str = "127.0.0.1"
    port: int = 8765
    conversation_id: str = "default"
    title: str = "轻小说作家 · Persistent Life Agent"

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("Web UI host cannot be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError("Web UI port must be between 0 and 65535")
        if not self.conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")


class AgentWebUIServer(ThreadingHTTPServer):
    """Threaded local server with one serialized canonical Agent writer."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], agent: PersistentLifeAgent, config: WebUIConfig) -> None:
        super().__init__(address, AgentWebUIHandler)
        self.agent = agent
        self.config = config
        self.agent_lock = threading.RLock()


class AgentWebUIHandler(BaseHTTPRequestHandler):
    """HTTP routes for the browser client."""

    server_version = "ArtificialLifeWebUI/1.0"

    @property
    def app_server(self) -> AgentWebUIServer:
        return cast(AgentWebUIServer, self.server)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(WEB_UI_HTML.replace("__TITLE__", self.app_server.config.title))
            return
        if parsed.path == "/api/health":
            self._send_json(self._health())
            return
        if parsed.path == "/api/state":
            self._send_json(self._state())
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
            if parsed.path == "/api/message":
                self._send_json(self._message(body), status=HTTPStatus.CREATED)
                return
            if parsed.path == "/api/heartbeat":
                self._send_json(self._heartbeat(body))
                return
            self._send_json({"error": "not_found", "path": parsed.path}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": "bad_request", "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - final safety net for a live server
            self._send_json({"error": "internal_error", "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the demo terminal readable; callers can add their own logging.
        return

    def _health(self) -> dict[str, Any]:
        app = self.app_server.agent
        with self.app_server.agent_lock:
            return {
                "ok": True,
                "persona": app.runtime.config.persona,
                "provider": type(app.provider).__name__,
                "root": str(app.root),
                "logical_time": iso(app.runtime.state.logical_time),
            }

    def _state(self) -> dict[str, Any]:
        app = self.app_server.agent
        with self.app_server.agent_lock:
            activities = [_activity_dict(activity) for activity in app.activities.all()]
            goals = [goal.to_dict() for goal in app.runtime.goals.all()]
            schedule = [block.to_dict() for block in app.runtime.schedule.all()]
            state = app.runtime.state.to_dict()
            return {
                "state": state,
                "activities": activities,
                "goals": goals,
                "schedule": schedule,
                "timeline": [event.to_dict() for event in app.runtime.timeline.events()[-50:]],
            }

    def _dialogue(self, conversation_id: str, limit: int) -> dict[str, Any]:
        app = self.app_server.agent
        with self.app_server.agent_lock:
            records = [
                record
                for record in app.raw.agent.records() + app.raw.user.records()
                if record.conversation_id == conversation_id
            ]
            # User input is appended before the explicit UI response.  When
            # both use the same clock instant, role order preserves that
            # conversational sequence instead of sorting by separate stream
            # prefixes (``ar_``/``ur_``).
            records.sort(key=lambda record: (record.recorded_at, 0 if record.role == "user" else 1, record.record_id))
            return {"conversation_id": conversation_id, "records": [record.to_dict() for record in records[-limit:]]}

    def _timeline(self, limit: int) -> dict[str, Any]:
        app = self.app_server.agent
        with self.app_server.agent_lock:
            events = app.runtime.timeline.events()
            return {"events": [event.to_dict() for event in events[-limit:]]}

    def _message(self, body: dict[str, Any]) -> dict[str, Any]:
        text = _required_text(body, "text", max_length=10000)
        conversation_id = str(body.get("conversation_id") or self.app_server.config.conversation_id).strip()
        if not conversation_id:
            raise ValueError("conversation_id cannot be empty")
        app = self.app_server.agent
        with self.app_server.agent_lock:
            user_record = app.receive_user_message(text, conversation_id=conversation_id)
            context = app.build_user_context(conversation_id=conversation_id, query=text)
            # The UI makes an explicit response request.  A raw user-message
            # ingestion elsewhere still remains input-only per the invariant.
            result = app.provider.generate(
                text,
                purpose="dialogue_response",
                context=context.to_dict(),
            )
            reply = result.text.strip()
            if not reply or result.provider == "mock":
                reply = f"收到，我已记下：{text}"
            agent_record = app.send_agent_message(reply, conversation_id=conversation_id)
            return {
                "conversation_id": conversation_id,
                "user": user_record.to_dict(),
                "agent": agent_record.to_dict(),
                "provider": result.provider,
                "context_source_refs": list(context.source_refs),
            }

    def _heartbeat(self, body: dict[str, Any]) -> dict[str, Any]:
        # ``at`` is intentionally optional.  Parsing an explicit instant is
        # useful for deterministic/manual UI tests; normal browsers use the
        # persisted SystemClock.
        at = body.get("at")
        from datetime import datetime

        moment = datetime.fromisoformat(str(at).replace("Z", "+00:00")) if at else None
        app = self.app_server.agent
        with self.app_server.agent_lock:
            result = app.heartbeat(moment)
            return _heartbeat_dict(result)

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


def create_server(agent: PersistentLifeAgent, config: WebUIConfig | None = None) -> AgentWebUIServer:
    """Create a server without starting a background thread.

    Passing port ``0`` lets tests or embedding hosts ask the OS for a free
    local port.  The caller owns ``serve_forever``/``shutdown``.
    """

    config = config or WebUIConfig()
    return AgentWebUIServer((config.host, config.port), agent, config)


def serve(agent: PersistentLifeAgent, config: WebUIConfig | None = None) -> None:
    """Run the blocking local server until Ctrl+C."""

    server = create_server(agent, config)
    print(f"Persistent Life Agent Web UI: http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def load_or_initialize(root: Path) -> PersistentLifeAgent:
    """Load an existing agent or create an empty light-novel-writer root."""

    if (root / "manifest.json").exists():
        return PersistentLifeAgent(root)
    return PersistentLifeAgent.initialize(root)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local Persistent Life Agent Web UI")
    parser.add_argument("--root", type=Path, default=Path("var/web_agent"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--conversation-id", default="default")
    args = parser.parse_args(argv)
    agent = load_or_initialize(args.root)
    serve(agent, WebUIConfig(args.host, args.port, args.conversation_id))


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


def _activity_dict(activity: Any) -> dict[str, Any]:
    return {
        "activity_id": activity.activity_id,
        "name": activity.name,
        "goal": activity.goal,
        "parent_activity": activity.parent_activity,
        "planned_start_time": iso(activity.planned_start_time),
        "expected_end_time": iso(activity.expected_end_time),
        "actual_start_time": iso(activity.actual_start_time),
        "actual_end_time": iso(activity.actual_end_time),
        "status": activity.status,
        "summary": activity.final_summary or activity.live_summary or activity.summary,
        "children": list(activity.children),
        "execution_segments": [segment.to_dict() for segment in activity.execution_segments],
    }


def _heartbeat_dict(result: PersistentHeartbeatResult) -> dict[str, Any]:
    return {
        "heartbeat_id": result.heartbeat.heartbeat_id,
        "no_op": result.heartbeat.no_op,
        "events": [event.to_dict() for event in result.heartbeat.events],
        "state": result.heartbeat.state.to_dict(),
        "random_event": asdict(result.random_event) if result.random_event else None,
        "interrupted_activity_id": result.interrupted_activity_id,
        "random_activity_id": result.random_activity_id,
        "expected_end_activity_ids": list(result.expected_end_activity_ids),
    }


WEB_UI_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <style>
    :root { color-scheme: dark; --bg:#111827; --panel:#1f2937; --panel2:#243247; --line:#374151; --text:#eef2ff; --muted:#9ca3af; --accent:#f59e0b; --user:#155e75; --agent:#334155; }
    * { box-sizing:border-box; } body { margin:0; min-height:100vh; background:linear-gradient(135deg,#111827,#172033); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
    header { border-bottom:1px solid var(--line); padding:16px 24px; display:flex; align-items:center; justify-content:space-between; gap:16px; }
    h1,h2,h3,p { margin:0; } h1 { font-size:19px; } h2 { font-size:15px; color:#fde68a; } .muted { color:var(--muted); font-size:12px; }
    .layout { max-width:1400px; margin:0 auto; padding:20px; display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:18px; }
    .panel { background:rgba(31,41,55,.88); border:1px solid var(--line); border-radius:14px; box-shadow:0 16px 40px #0003; overflow:hidden; }
    .panel-head { padding:14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; }
    #messages { height:calc(100vh - 230px); min-height:400px; overflow-y:auto; padding:18px; display:flex; flex-direction:column; gap:12px; }
    .message { max-width:82%; padding:11px 14px; border-radius:13px; white-space:pre-wrap; word-break:break-word; }
    .message.user { align-self:flex-end; background:var(--user); border-bottom-right-radius:3px; } .message.agent { align-self:flex-start; background:var(--agent); border-bottom-left-radius:3px; }
    .message .meta { color:#cbd5e1; font-size:11px; margin-bottom:4px; } .empty { color:var(--muted); text-align:center; margin:auto; }
    form { border-top:1px solid var(--line); padding:14px; display:flex; gap:10px; align-items:flex-end; } textarea { flex:1; resize:vertical; min-height:50px; max-height:160px; padding:11px; border:1px solid var(--line); border-radius:10px; background:#111827; color:var(--text); font:inherit; }
    button { border:0; border-radius:9px; padding:10px 14px; background:var(--accent); color:#241300; font-weight:700; cursor:pointer; } button.secondary { background:#475569; color:var(--text); } button:disabled { opacity:.5; cursor:wait; }
    .stack { padding:14px 16px; display:flex; flex-direction:column; gap:14px; } .row { display:flex; justify-content:space-between; gap:12px; } .value { text-align:right; color:#d1d5db; word-break:break-word; }
    .badge { display:inline-block; padding:3px 8px; border-radius:999px; background:#065f46; color:#bbf7d0; font-size:12px; } .badge.free { background:#374151; color:#d1d5db; }
    .list { display:flex; flex-direction:column; gap:6px; max-height:180px; overflow:auto; } .list-item { padding:8px 9px; border:1px solid #334155; border-radius:8px; font-size:12px; }
    .timeline { max-height:230px; overflow:auto; font:11px/1.4 ui-monospace,SFMono-Regular,monospace; color:#cbd5e1; } .timeline div { padding:5px 0; border-bottom:1px solid #334155; }
    .toolbar { display:flex; gap:8px; align-items:center; } .error { color:#fca5a5; font-size:12px; min-height:18px; }
    @media (max-width:900px) { .layout { grid-template-columns:1fr; } #messages { height:55vh; } }
  </style>
</head>
<body>
  <header><div><h1>轻小说作家</h1><div class="muted">__TITLE__ · 文件持久化 Agent</div></div><div class="toolbar"><span id="status" class="badge">连接中</span><button id="heartbeat" class="secondary">推进 Heartbeat</button></div></header>
  <main class="layout">
    <section class="panel"><div class="panel-head"><h2>对话</h2><span id="conversation-id" class="muted"></span></div><div id="messages"><div class="empty">还没有对话。输入一句话开始。</div></div><form id="message-form"><textarea id="message" placeholder="和轻小说作家聊聊……（Ctrl/⌘ + Enter 发送）"></textarea><button id="send" type="submit">发送</button></form><div id="error" class="error" style="padding:0 14px 12px"></div></section>
    <aside class="panel"><div class="panel-head"><h2>生命状态</h2><span id="logical-time" class="muted">—</span></div><div class="stack"><div class="row"><span>模式</span><span id="mode" class="value">—</span></div><div class="row"><span>当前活动</span><span id="active" class="value">—</span></div><div class="row"><span>地点 / 情绪</span><span id="place" class="value">—</span></div><div class="row"><span>能量 / 疲劳</span><span id="energy" class="value">—</span></div><div><h3>活动</h3><div id="activities" class="list" style="margin-top:7px"></div></div><div><h3>目标</h3><div id="goals" class="list" style="margin-top:7px"></div></div><div><h3>最近 Timeline</h3><div id="timeline" class="timeline" style="margin-top:7px"></div></div></div></aside>
  </main>
  <script>
    const conversationId = new URLSearchParams(location.search).get('conversation_id') || 'default';
    const $ = (id) => document.getElementById(id);
    $('conversation-id').textContent = 'conversation: ' + conversationId;
    function esc(value) { return String(value ?? ''); }
    function time(value) { if (!value) return '—'; try { return new Date(value).toLocaleString(); } catch (_) { return value; } }
    function showError(message='') { $('error').textContent = message; }
    async function api(path, options) { const res = await fetch(path, options); const data = await res.json(); if (!res.ok) throw new Error(data.message || data.error || '请求失败'); return data; }
    function renderDialogue(data) { const box=$('messages'); box.textContent=''; if (!data.records.length) { box.innerHTML='<div class="empty">还没有对话。输入一句话开始。</div>'; return; } data.records.forEach(record => { const el=document.createElement('div'); el.className='message '+(record.role==='user'?'user':'agent'); const meta=document.createElement('div'); meta.className='meta'; meta.textContent=(record.role==='user'?'你':'轻小说作家')+' · '+time(record.recorded_at); const body=document.createElement('div'); body.textContent=record.text; el.append(meta,body); box.appendChild(el); }); box.scrollTop=box.scrollHeight; }
    function renderState(data) { const s=data.state; $('status').textContent='在线'; $('status').className='badge '+(s.mode==='reserved_free'?'free':''); $('logical-time').textContent=time(s.logical_time); $('mode').textContent=s.mode; $('active').textContent=s.active_activity_id || 'reserved_free'; $('place').textContent=esc(s.location)+' / '+esc(s.emotion); $('energy').textContent=Math.round(s.energy*100)+'% / '+Math.round(s.fatigue*100)+'%'; const list=$('activities'); list.textContent=''; data.activities.forEach(a=>{const el=document.createElement('div');el.className='list-item';el.textContent=a.name+' · '+a.status;list.appendChild(el)}); if(!data.activities.length) list.innerHTML='<span class="muted">暂无活动</span>'; const goals=$('goals'); goals.textContent=''; data.goals.forEach(g=>{const el=document.createElement('div');el.className='list-item';el.textContent=g.title+' · '+g.level+' · '+g.status;goals.appendChild(el)}); if(!data.goals.length) goals.innerHTML='<span class="muted">暂无目标</span>'; const timeline=$('timeline');timeline.textContent=''; data.timeline.slice(-20).reverse().forEach(e=>{const el=document.createElement('div');el.textContent='#'+e.seq+' '+time(e.occurred_at)+' '+e.event_type;timeline.appendChild(el)}); }
    async function refresh() { try { const [state, dialogue] = await Promise.all([api('/api/state'),api('/api/dialogue?conversation_id='+encodeURIComponent(conversationId))]); renderState(state); renderDialogue(dialogue); showError(''); } catch (e) { $('status').textContent='离线'; $('status').className='badge free'; showError(e.message); } }
    $('message-form').addEventListener('submit', async (event)=>{event.preventDefault();const input=$('message');const text=input.value.trim();if(!text)return; $('send').disabled=true;showError('');try{await api('/api/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,conversation_id:conversationId})});input.value='';await refresh();}catch(e){showError(e.message)}finally{$('send').disabled=false;input.focus()}});
    $('message').addEventListener('keydown',(event)=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter')$('message-form').requestSubmit()});
    $('heartbeat').addEventListener('click',async()=>{ $('heartbeat').disabled=true;showError('');try{await api('/api/heartbeat',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh()}catch(e){showError(e.message)}finally{$('heartbeat').disabled=false}});
    refresh(); setInterval(refresh,15000);
  </script>
</body>
</html>'''


if __name__ == "__main__":
    main()
