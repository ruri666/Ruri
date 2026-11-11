import asyncio
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from artificial_life import PersistentLifeAgent, TestClock, MockLLMProvider
from artificial_life.plugin import PluginService, HarnessProvider, TOOLS
from artificial_life.lease import RootLease
from artificial_life.webui import create_server, WebUIConfig


def service(tmp_path):
    clock = TestClock(datetime(2026, 10, 12, 8, tzinfo=timezone.utc))
    return PluginService(PersistentLifeAgent.initialize(tmp_path, clock=clock, provider=HarnessProvider()))


def test_harness_turn_activity_and_idempotent_commit(tmp_path):
    app = service(tmp_path)
    app.call("activity_create", {"activity_id": "a", "name": "Work"})
    app.call("activity_start", {"activity_id": "a"})
    turn = app.call("dialogue_begin", {"text": "remember marker", "conversation_id": "chat"})
    assert app.agent.activities.get("a").status == "active"
    assert turn["context"]["working"]["recent_raw_user_agent_conversation"][-1]["text"] == "remember marker"
    with pytest.raises(ValueError):
        app.call("activity_start", {"activity_id": "a"})
    reply = app.call("dialogue_commit", {"turn_id": turn["turn_id"], "reply": "host response"})
    assert reply["provider"] == "harness"
    assert reply["resumed_activity_id"] is None
    count = len(app.agent.raw.agent.records())
    assert app.call("dialogue_commit", {"turn_id": turn["turn_id"], "reply": "host response"}) == reply
    assert len(app.agent.raw.agent.records()) == count
    done = app.call("activity_complete", {"activity_id": "a", "result": "Executed test artifact"})
    assert done["status"] == "completed"
    assert "Executed test artifact" in done["summary"]


def test_pending_turn_restart_and_cancel(tmp_path):
    app = service(tmp_path)
    turn = app.call("dialogue_begin", {"text": "pending"})
    reloaded = PluginService(PersistentLifeAgent(tmp_path, clock=app.agent.clock, provider=HarnessProvider()))
    reply = reloaded.call("dialogue_commit", {"turn_id": turn["turn_id"], "reply": "restored"})
    assert reply["agent"]["text"] == "restored"
    turn = reloaded.call("dialogue_begin", {"text": "cancel"})
    assert reloaded.call("dialogue_cancel", {"turn_id": turn["turn_id"]})["status"] == "cancelled"
    with pytest.raises(ValueError):
        reloaded.call("dialogue_commit", {"turn_id": turn["turn_id"], "reply": "late"})


def test_harness_ui_enqueues_instead_of_generating(tmp_path):
    app = service(tmp_path)
    events = list(app.stream("UI input", "ui"))
    assert [event.event for event in events] == ["start", "done"]
    assert events[-1].data["response"]["pending"]
    assert not [record for record in app.agent.raw.agent.records() if record.kind == "dialogue"]
    pending = app.call("state")["pending_turns"][0]
    app.call("dialogue_commit", {"turn_id": pending["turn_id"], "reply": "harness UI reply"})
    assert app.call("dialogue_history", {"conversation_id": "ui"})["records"][-1]["text"] == "harness UI reply"


def test_scoped_retrieval_and_tool_validation(tmp_path):
    app = service(tmp_path)
    app.agent.receive_user_message("scope_marker A", conversation_id="a")
    app.agent.receive_user_message("scope_marker B_SECRET", conversation_id="b")
    app.agent.record_agent("scope_marker WORK_SECRET", activity_id="work")
    result = app.call("memory_search", {"query": "scope_marker", "conversation_id": "a"})
    assert [r["text"] for r in result["raw_records"]] == ["scope_marker A"]
    context = app.call("context_get", {"kind": "user", "conversation_id": "a", "query": "scope_marker"})
    assert "B_SECRET" not in json.dumps(context["working"])
    assert "WORK_SECRET" not in json.dumps(context["working"])
    for name, args in [("timeline", {"limit": True}), ("health", {"bad": 1}),
                       ("dialogue_begin", {"text": "hello", "conversation_id": "../outside"})]:
        with pytest.raises(ValueError):
            app.call(name, args)


def test_writer_lease_excludes_another_owner(tmp_path):
    with RootLease(tmp_path):
        with pytest.raises(RuntimeError, match="already has a writer"):
            RootLease(tmp_path)
    with RootLease(tmp_path):
        pass


def test_same_tools_over_http_and_mcp_proxy(tmp_path):
    pytest.importorskip("mcp")
    app = service(tmp_path)
    server = create_server(app, WebUIConfig(port=0, auto_heartbeat=False))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = Request(url + "/api/tools/call", data=json.dumps({"name": "activity_create", "arguments": {
            "activity_id": "http", "name": "HTTP work"}}).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            assert json.loads(response.read())["activity_id"] == "http"
        request = Request(url + "/api/message/stream", data=json.dumps({"text": "UI pending", "conversation_id": "ui"}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            assert '"pending":true' in response.read().decode()
        asyncio.run(mcp_session(["--connect", url], expected_activity="http"))
        assert app.call("dialogue_history", {"conversation_id": "ui"})["records"][-1]["text"] == "MCP answered UI"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


async def mcp_session(extra_args, expected_activity=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "artificial_life.mcp_server", *extra_args])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == set(TOOLS)
            result = await session.call_tool("state", {})
            assert not result.isError
            if expected_activity:
                assert result.structuredContent["activities"][0]["activity_id"] == expected_activity
                for pending in result.structuredContent.get("pending_turns", []):
                    reply = await session.call_tool("dialogue_commit", {"turn_id": pending["turn_id"], "reply": "MCP answered UI"})
                    assert not reply.isError
            else:
                await session.call_tool("activity_create", {"activity_id": "mcp", "name": "MCP task"})
                turn = await session.call_tool("dialogue_begin", {"text": "mcp marker", "conversation_id": "mcp"})
                assert not turn.isError
                reply = await session.call_tool("dialogue_commit", {"turn_id": turn.structuredContent["turn_id"], "reply": "host mcp reply"})
                assert reply.structuredContent["agent"]["text"] == "host mcp reply"
            error = await session.call_tool("activity_start", {"activity_id": "missing"})
            assert error.isError


def test_real_stdio_mcp_restart(tmp_path):
    pytest.importorskip("mcp")
    asyncio.run(mcp_session(["--root", str(tmp_path)]))
    asyncio.run(mcp_session(["--root", str(tmp_path)], expected_activity="mcp"))


def test_isolated_mechanism_audit():
    from examples.audit_mechanisms import audit
    report = audit()
    failed = [check["check"] for check in report["checks"] if check["status"] == "FAIL"]
    # Empty roots intentionally wait for an explicit task from the harness.
    assert failed == ["empty_root_autonomous_activity"]
