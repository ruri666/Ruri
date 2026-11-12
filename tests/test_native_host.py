from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
import time
from urllib.request import Request, urlopen

import pytest

from artificial_life.adaptive_execution import run_adaptive
from artificial_life.host import Interrupted
from artificial_life.native_host import NativeHost
from artificial_life.lifecycle import Lifecycle
from artificial_life.plugin import HarnessProvider
from artificial_life.storage import read_json
from artificial_life.webui import create_server, WebUIConfig
from test_lifecycle import fixture


@contextmanager
def running(function, *args):
    cancel = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(function, *args, cancel)
    try:
        yield future, cancel
    finally:
        cancel.set()
        pool.shutdown(wait=True)


def claim(host, worker, channel="autonomous"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = host.claim(worker, channel)
        if result["status"] != "empty":
            return result
        host.changed.wait(.01)
    pytest.fail("Native host did not publish its request")


def test_native_agent_keeps_own_tools_and_no_nested_model(tmp_path, monkeypatch):
    service, _, _ = fixture(tmp_path / "memory")
    host = NativeHost(service.agent.root)
    def forbidden(*args, **kwargs):
        pytest.fail("Native mode must not launch a subprocess or assemble a replacement model prompt")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("artificial_life.host.execution_prompt", forbidden)
    monkeypatch.setattr("artificial_life.lifecycle.ActivityPlugins", forbidden)
    native_life = Lifecycle(service, host)
    assert native_life.plugins is None
    request = {"kind": "work", "activity": {"id": "a", "name": "使用宿主已有能力"}}
    with running(lambda req, cancel: run_adaptive(host, service, req, cancel), request) as (future, _):
        job = claim(host, "test-agent")
        assert "tool_contract" not in job["request"]
        assert "schema" not in job["request"]
        assert "tools_remaining" not in job["request"]
        assert job["request"]["execution_environment"]["tools_owner"] == "host"
        # The existing host can use tools/workspaces beyond this memory root.
        # Eight actual file operations are not constrained by the plugin's
        # optional registered-tool loop or its six-round call limit.
        project = tmp_path / "host-project"
        project.mkdir()
        for i in range(8):
            (project / f"native-{i}.txt").write_text(str(i), encoding="utf-8")
        result = {"body": "实际创建了八份文件。", "host_receipts": [str(p) for p in project.glob("*.txt")]}
        host.complete(job["job_id"], "test-agent", result)
        assert future.result(timeout=3) == result
        assert len(list(project.glob("*.txt"))) == 8


def test_claim_ownership_and_replay_safe_completion(tmp_path):
    host = NativeHost(tmp_path)
    with running(host.run, {"kind": "plan"}) as (future, _):
        job = claim(host, "one")
        assert host.claim("two")["status"] == "empty"
        assert host.claim("one")["job_id"] == job["job_id"]
        with pytest.raises(ValueError, match="owned"):
            host.complete(job["job_id"], "two", {"name": "wrong"})
        result = {"name": "自己的计划", "steps": []}
        done = host.complete(job["job_id"], "one", result)
        assert host.complete(job["job_id"], "one", result) == done
        with pytest.raises(ValueError, match="different"):
            host.complete(job["job_id"], "one", {"name": "altered"})
        assert future.result(timeout=3) == result


def test_channels_and_cancellation_are_independent(tmp_path):
    host = NativeHost(tmp_path)
    with running(host.run, {"kind": "work"}) as (autonomous, auto_cancel):
        work = claim(host, "worker", "autonomous")
        with running(host.run, {"kind": "reply", "message": "你好"}) as (dialogue, dialogue_cancel):
            reply = claim(host, "worker", "dialogue")
            assert reply["completion"]["result_fields"] == ["reply"]
            assert "timing" not in reply["completion"]
            assert "files" not in reply["completion"]
            with pytest.raises(ValueError, match="nonempty reply"):
                host.complete(reply["job_id"], "worker", {"body": "自主活动结果"})
            with pytest.raises(ValueError, match="cannot submit a dialogue reply"):
                host.complete(work["job_id"], "worker", {"reply": "你好"})
            assert host.inspect(reply["job_id"], "worker")["status"] == "claimed"
            assert host.inspect(work["job_id"], "worker")["status"] == "claimed"
            dialogue_cancel.set()
            with pytest.raises(Interrupted):
                dialogue.result(timeout=3)
            assert host.inspect(reply["job_id"], "worker")["status"] == "cancelled"
            with pytest.raises(ValueError, match="claimed"):
                host.complete(reply["job_id"], "worker", {"reply": "late"})
            assert not auto_cancel.is_set() and not autonomous.done()
            host.complete(work["job_id"], "worker", {"body": "继续自主工作"})
            assert autonomous.result(timeout=3)["body"] == "继续自主工作"


def test_failure_and_restart_do_not_fake_completion(tmp_path):
    host = NativeHost(tmp_path)
    with running(host.run, {"kind": "work"}) as (future, _):
        job = claim(host, "worker")
        host.fail(job["job_id"], "worker", "宿主实际执行失败")
        with pytest.raises(RuntimeError, match="实际执行失败"):
            future.result(timeout=3)
    stale = tmp_path / "runtime/host-jobs" / ("a" * 32)
    stale.mkdir()
    (stale / "state.json").write_text(json.dumps({"status": "claimed", "channel": "autonomous", "worker_id": "old"}), encoding="utf-8")
    restarted = NativeHost(tmp_path)
    assert read_json(stale / "state.json", {})["status"] == "cancelled"
    assert restarted.claim("new")["status"] == "empty"


def test_native_lifecycle_full_plan_and_work_using_shared_tools(tmp_path):
    service, _, _ = fixture(tmp_path)
    host = NativeHost(tmp_path)
    life = Lifecycle(service, host, interval=30, mode="rhythm")
    def tick(cancel):
        life.cancel = cancel
        life.tick()
    with running(tick) as (future, _):
        job = claim(host, "host")
        claimed = service.call("execution_claim", {"worker_id": "host"})
        assert claimed["job_id"] == job["job_id"]
        service.call("execution_complete", {"job_id": job["job_id"], "worker_id": "host",
                     "result": {"name": "宿主自主选择的工作", "goal": "使用原有文件工具", "steps": ["生成实际文件"],
                                "plugin_actions": [{"step": 1, "capability": "host.native-files", "arguments_json": "{}", "input_from_step": 0}]}})
        future.result(timeout=3)
    with running(tick) as (future, _):
        job = claim(host, "host")
        assert job["request"]["kind"] == "work"
        assert "plugin_capabilities" not in job["request"]
        assert "plugin_action" not in job["request"]
        artifact = tmp_path / "native-output.txt"
        artifact.write_text("由宿主自己的执行环境生成", encoding="utf-8")
        service.call("execution_complete", {"job_id": job["job_id"], "worker_id": "host",
                     "result": {"body": artifact.read_text(encoding="utf-8")}})
        future.result(timeout=3)
    events = service.agent.runtime.timeline.events()
    assert any(e.event_type == "lifecycle.artifact_committed" for e in events)
    assert not service.state()["pending_turns"]
    assert host.status()["model_owner"] == "host"


def test_native_tools_require_an_attached_native_executor(tmp_path):
    service, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="engine native"):
        service.call("execution_claim", {"worker_id": "host"})


def test_dialogue_worker_does_not_replay_failed_or_orphaned_native_turn(tmp_path):
    service, _, _ = fixture(tmp_path)
    host = NativeHost(tmp_path)
    worker = Lifecycle(service, host).dialogue
    orphaned = service.dialogue_begin("Interrupted before reply", execution_owner="service")
    worker.save(phase="replying", active_turn=orphaned["turn_id"])
    worker.start()
    try:
        assert service._read_turn(orphaned["turn_id"])[1]["status"] == "cancelled"
        failed = service.dialogue_begin("Open the game", execution_owner="service")
        job = claim(host, "host", "dialogue")
        assert job["request"]["message"] == "Open the game"
        host.fail(job["job_id"], "host", "Execution outcome unknown")
        for _ in range(100):
            if service._read_turn(failed["turn_id"])[1]["status"] == "cancelled":
                break
            time.sleep(.01)
        assert service._read_turn(failed["turn_id"])[1]["status"] == "cancelled"
        following = service.dialogue_begin("Next message", execution_owner="service")
        job = claim(host, "host", "dialogue")
        assert job["request"]["message"] == "Next message"
        host.complete(job["job_id"], "host", {"reply": "Received"})
        for _ in range(100):
            if service._read_turn(following["turn_id"])[1]["status"] == "committed":
                break
            time.sleep(.01)
        assert service._read_turn(following["turn_id"])[1]["status"] == "committed"
    finally:
        worker.stop()


def test_direct_native_dialogue_is_not_queued_a_second_time(tmp_path):
    service, _, _ = fixture(tmp_path)
    service.agent.provider = HarnessProvider()
    host = NativeHost(tmp_path)
    life = Lifecycle(service, host)
    turn = service.call("dialogue_begin", {"text": "原宿主里的对话"})
    assert service.state()["pending_turns"][0]["execution_owner"] == "host"
    life.dialogue.tick()
    assert host.status()["pending_jobs"] == 0
    service.call("dialogue_commit", {"turn_id": turn["turn_id"], "reply": "宿主原生回复"})
    list(service.stream("另一个界面的交流", "ui"))
    assert service.state()["pending_turns"][0]["execution_owner"] == "service"
    def tick(cancel):
        life.dialogue.cancel = cancel
        life.dialogue.tick()
    with running(tick) as (future, _):
        job = claim(host, "host", "dialogue")
        assert job["request"]["kind"] == "reply"
        host.complete(job["job_id"], "host", {"reply": "原生宿主处理界面消息"})
        future.result(timeout=3)
    assert service.dialogue_history("ui")["records"][-1]["text"] == "原生宿主处理界面消息"


def test_http_client_uses_same_native_handoff_without_model_execution(tmp_path):
    service, _, _ = fixture(tmp_path)
    host = NativeHost(tmp_path)
    life = Lifecycle(service, host)
    server = create_server(service, WebUIConfig(port=0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(name, arguments):
        request = Request(f"http://127.0.0.1:{server.server_address[1]}/api/tools/call",
                          data=json.dumps({"name": name, "arguments": arguments}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return json.load(response)
    def tick(cancel):
        life.cancel = cancel
        life.tick()
    try:
        with running(tick) as (future, _):
            claim(host, "http-agent")
            job = call("execution_claim", {"worker_id": "http-agent"})
            assert call("execution_status", {"job_id": job["job_id"], "worker_id": "http-agent"})["status"] == "claimed"
            call("execution_complete", {"job_id": job["job_id"], "worker_id": "http-agent",
                 "result": {"name": "HTTP宿主自己选择的活动", "steps": ["执行宿主选择的具体步骤"]}})
            future.result(timeout=3)
        assert any(a.name == "HTTP宿主自己选择的活动" for a in service.agent.activities.all())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
