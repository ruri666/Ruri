"""Runner integration: no real model or platform credentials are used."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_autonomous_cli_failure_keeps_independent_dialogue_and_persona_ui(tmp_path):
    repo = Path(__file__).parents[1]
    out = tmp_path / "run"
    port, viewer_port = free_port(), free_port()
    # Python is deliberately NOT a Codex executable: the autonomous child exits
    # with an error. This must not stop the other channel or the whole service.
    command = [sys.executable, str(repo / "scripts/run_codex_native_trial.py"),
               "--root", str(tmp_path / "data"), "--output", str(out),
               "--persona-id", "test-persona", "--persona", "An isolated transport test persona.",
               "--codex", sys.executable, "--duration", "0", "--port", str(port),
               "--viewer-port", str(viewer_port)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if list(out.glob("jobs/*/final-state.json")):
                break
            assert process.poll() is None
            time.sleep(.1)
        states = list(out.glob("jobs/*/final-state.json"))
        assert states, "Expected a real child process exit receipt"
        assert process.poll() is None
        assert not (out / "final.json").exists()
        with urlopen(f"http://127.0.0.1:{port}/api/personas", timeout=3) as response:
            personas = json.load(response)
        assert personas["active"] == "test-persona"
        assert any(p["id"] == "test-persona" for p in personas["personas"])
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
            health = json.load(response)
        assert health["lifecycle"]["dialogue"]["worker_running"]
        assert health["lifecycle"]["dialogue"]["enabled"]
    finally:
        out.mkdir(exist_ok=True)
        (out / "STOP").touch()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
