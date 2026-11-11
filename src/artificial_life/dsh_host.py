"""DeepSeek Harness headless execution adapter; canonical commits stay in Lifecycle."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from .host import Interrupted, execution_prompt, result_schema, validate_result
from .storage import atomic_write_json, atomic_write_text, read_json


def resolve_command(config):
    command = config.get("command")
    if command is not None:
        if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
            raise ValueError("DSH command must be an argv list")
        return command
    # Electron can read bundled JavaScript inside app.asar; ordinary Node cannot.
    for folder in [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DeepSeek Harness", Path("C:/D/software/dsh")]:
        executable = folder / "DeepSeek Harness.exe"
        archive = folder / "resources/app.asar"
        if executable.is_file() and archive.is_file():
            return [str(executable), "--expose-internals", str(archive / "dsh/node_modules/@deepseek-ai/dsh/lib/bin.js")]
    executable = shutil.which("dsh")
    if executable:
        return [executable]
    raise ValueError("DeepSeek Harness not found; configure ROOT/config/dsh.json command")


def kill_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def execution_overlay(run):
    # A local overlay does not alter the user's installed profile. Tools are
    # executed by the shared activity dispatcher, not a nested DSH tool loop.
    disabled = ["session-title-llm", "session-telemetry-otel", "llm-retry", "agent-instructions", "skill-filesystem",
                "tool-working-directory", "tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search",
                "tool-skill", "tool-subagent-control", "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork",
                "tool-workflow", "tool-todo", "tool-goal", "tool-web", "goal-round-driver"]
    patches = [{"id": name, "disabled": True} for name in disabled]
    patches += [{"id": "system-prompt", "config": {"personaPrefix": "你是持续生活的agent，人设只以当前工作区上下文为准。只返回本轮合同要求的JSON，不调用工具。"}},
                {"id": "session-persistence-jsonl", "config": {"root": str(run / "sessions")}},
                {"id": "storage-json", "config": {"root": str(run / "storages")}}]
    return patches


def parse_events(path, request):
    events = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if any(e.get("type") in {"error", "tool_call"} for e in events):
        raise ValueError("DSH reported an error or unexpected direct tool call")
    terminals = [e for e in events if e.get("type") == "status" and e.get("phase") == "turn_end"]
    finals = [e for e in events if e.get("type") == "final"]
    steps = [e for e in events if e.get("type") == "status" and e.get("phase") == "step_end"]
    if len(finals) != 1 or not events or events[-1] != finals[0] or len(terminals) != 1 or terminals[0].get("reason", {}).get("kind") != "completed" or len(steps) != 1:
        raise ValueError("DSH did not finish exactly one bounded model step")
    text = finals[0].get("text", "").strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    return validate_result(json.loads(text), request)


class DSHHost:
    name = "deepseek-harness"

    def __init__(self, root: Path, config_path: Path | None = None, timeout=600):
        self.root = root.resolve()
        config = read_json(config_path or root / "config/dsh.json", {})
        if set(config) - {"command", "profile"} or config.get("profile", "headless") != "headless":
            raise ValueError("DSH config supports command and profile=headless only")
        self.command = resolve_command(config)
        if not 1 <= timeout <= 2400:
            raise ValueError("DSH timeout must be 1..2400 seconds")
        self.timeout = timeout

    def run(self, request, cancel: threading.Event):
        from .model_context import prepare_model_request
        request = prepare_model_request(request)
        if cancel.is_set():
            raise Interrupted("DSH cancelled before launch")
        run = self.root / "runtime/executions" / uuid4().hex
        run.mkdir(parents=True)
        atomic_write_json(run / "request.json", request)
        atomic_write_json(run / "schema.json", result_schema(request))
        # JSON is a YAML subset; the installed Cordis launcher accepts it.
        atomic_write_text(run / "execution.patch.yml", json.dumps(execution_overlay(run), ensure_ascii=False, indent=2))
        atomic_write_text(run / "prompt.txt", execution_prompt(request))
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1", DSH_PERMISSION_MODE="read-only")
        command = self.command + ["--profile", "headless", "--patch", str(run/"execution.patch.yml"), "--json", "-"]
        atomic_write_json(run / "host.json", {"host": self.name, "profile": "headless", "timeout": self.timeout})
        try:
            with (run/"prompt.txt").open("rb") as stdin, (run/"events.jsonl").open("wb") as stdout, (run/"stderr.log").open("wb") as stderr:
                process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=self.root,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        if cancel.wait(.1):
                            raise Interrupted("DSH interrupted by user input or service stop")
                        if time.monotonic()-started > self.timeout:
                            raise TimeoutError("DSH step exceeded timeout")
                    if cancel.is_set():
                        raise Interrupted("DSH cancelled before commit")
                    if process.returncode:
                        raise RuntimeError(f"DSH exited {process.returncode}; see {run / 'stderr.log'}")
                finally:
                    kill_tree(process)
            result = parse_events(run/"events.jsonl", request)
            atomic_write_json(run/"result.json", result)
            return result
        except Exception as exc:
            atomic_write_json(run/"error.json", {"type": type(exc).__name__, "error": str(exc)})
            raise
