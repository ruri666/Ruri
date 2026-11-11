from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.client import HTTPResponse
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from artificial_life import MockLLMProvider, PersistentLifeAgent, TestClock
from artificial_life.webui import WebUIConfig, create_server


UTC = timezone.utc


def instant(hour: int) -> datetime:
    return datetime(2026, 8, 18, hour, tzinfo=UTC)


def read_json(response: HTTPResponse) -> dict:
    return json.loads(response.read().decode("utf-8"))


def call(base_url: str, path: str, *, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(base_url + path, method=method, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, read_json(response)
    except HTTPError as error:
        return error.code, read_json(error)


def test_webui_serves_state_dialogue_and_heartbeat(tmp_path: Path):
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=TestClock(instant(8)), provider=MockLLMProvider())
    server = create_server(agent, WebUIConfig(port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, health = call(base_url, "/api/health")
        assert status == 200
        assert health["ok"] is True
        assert health["persona"] == ""

        status, page = call(base_url, "/api/state")
        assert status == 200
        assert page["state"]["mode"] == "reserved_free"

        status, message = call(base_url, "/api/message", method="POST", body={"text": "今天写什么？"})
        assert status == 201
        assert message["user"]["role"] == "user"
        assert message["agent"]["role"] == "agent"
        assert "今天写什么？" in message["agent"]["text"]

        status, dialogue = call(base_url, "/api/dialogue?conversation_id=default")
        assert status == 200
        assert [record["role"] for record in dialogue["records"]] == ["user", "agent"]

        status, heartbeat = call(
            base_url,
            "/api/heartbeat",
            method="POST",
            body={"at": "2026-08-18T09:00:00Z"},
        )
        assert status == 200
        assert heartbeat["heartbeat_id"]
        assert any(event["event_type"] == "heartbeat.completed" for event in heartbeat["events"])

        status, timeline = call(base_url, "/api/timeline?limit=10")
        assert status == 200
        assert any(event["event_type"] == "heartbeat.completed" for event in timeline["events"])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_webui_rejects_invalid_message(tmp_path: Path):
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=TestClock(instant(8)), provider=MockLLMProvider())
    server = create_server(agent, WebUIConfig(port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = call(base_url, "/api/message", method="POST", body={"text": "   "})
        assert status == 400
        assert payload["error"] == "bad_request"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_webui_streams_dialogue_and_enter_sends(tmp_path: Path):
    agent = PersistentLifeAgent.initialize(tmp_path / "agent", clock=TestClock(instant(8)), provider=MockLLMProvider())
    server = create_server(agent, WebUIConfig(port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = Request(
            base_url + "/api/message/stream",
            method="POST",
            data=json.dumps({"text": "流式测试", "conversation_id": "stream"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert "event: start" in body
        assert "event: delta" in body
        assert "event: done" in body
        assert "收到，我已记下：流式测试" in body

        with urlopen(base_url + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert response.status == 200
        assert "/api/message/stream" in page
        assert "event.key==='Enter'&&!event.shiftKey" in page
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
