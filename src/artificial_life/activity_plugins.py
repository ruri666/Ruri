"""Explicitly configured, bounded JSON subprocess capabilities for life activities.

The model selects registered capabilities; it never supplies executable commands.
Version 1 supports read-only plugins only, so interrupted calls can safely retry.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .host import Interrupted
from .storage import atomic_write_json, read_json


class ActivityPlugins:
    def __init__(self, root: Path):
        self.root = root
        document = read_json(root / "config/activity-plugins.json", {"version": 1, "plugins": []})
        if document.get("version") != 1 or not isinstance(document.get("plugins"), list):
            raise ValueError("Invalid activity plugin configuration")
        self.entries = {}
        for item in document["plugins"]:
            key = item.get("id", "")
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", key) or key in self.entries:
                raise ValueError("Invalid or duplicate plugin id")
            if item.get("read_only") is not True or not isinstance(item.get("description"), str):
                raise ValueError("Plugins require a description and read_only=true")
            if not isinstance(item.get("command"), list) or not item["command"] or not all(isinstance(s, str) and s for s in item["command"]):
                raise ValueError("Plugin command must be an explicit argv list")
            if item.get("timeout", 10) <= 0:
                raise ValueError("Plugin timeout must be positive")
            fields = item.get("arguments", {})
            if not isinstance(fields, dict) or not all(isinstance(k, str) and v in ("string", "integer", "boolean") for k, v in fields.items()):
                raise ValueError("Plugin arguments must declare primitive types")
            self.entries[key] = item
        from .learned_capabilities import load_learned
        load_learned(root, self.entries)

    def catalog(self):
        return [{"id": key, "description": item["description"], "arguments": item.get("arguments", {}),
                 "accepts_previous_result": item.get("accepts_input", False)} for key, item in self.entries.items()]

    def validate_actions(self, actions, step_count):
        if not isinstance(actions, list) or len(actions) > step_count:
            raise ValueError("Invalid plugin actions")
        found = {}
        types = {"string": str, "integer": int, "boolean": bool}
        for action in actions:
            if not isinstance(action, dict) or set(action) != {"step", "capability", "arguments_json", "input_from_step"}:
                raise ValueError("Invalid plugin action fields")
            step, source = action["step"], action["input_from_step"]
            if type(step) is not int or not 1 <= step <= step_count or step in found or type(source) is not int:
                raise ValueError("Invalid or duplicate plugin step")
            item = self.entries.get(action["capability"])
            if item is None:
                raise ValueError("Unknown plugin capability")
            if not isinstance(action["arguments_json"], str):
                raise ValueError("Plugin arguments must be JSON text")
            args = json.loads(action["arguments_json"])
            fields = item.get("arguments", {})
            if not isinstance(args, dict) or set(args) != set(fields) or any(type(args[k]) is not types[t] for k, t in fields.items()):
                raise ValueError("Plugin argument schema mismatch")
            if source and (not item.get("accepts_input") or not 1 <= source < step):
                raise ValueError("Plugin dependency must reference an earlier plugin step")
            found[step] = action
        if any(a["input_from_step"] and a["input_from_step"] not in found for a in found.values()):
            raise ValueError("Plugin dependency is not a plugin step")
        return found

    def execute(self, activity_id, action, previous, cancel):
        # IDs become directory names, including dynamically selected tool calls.
        if not isinstance(activity_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", activity_id) or activity_id in {".", ".."}:
            raise ValueError("Invalid plugin execution id")
        item = self.entries[action["capability"]]
        payload = {"arguments": json.loads(action["arguments_json"]), "input": previous}
        fingerprint = hashlib.sha256(json.dumps({"action": action, "payload": payload, "plugin": item}, sort_keys=True).encode()).hexdigest()
        folder = self.root / "works/plugin-activities" / activity_id
        receipt_path = folder / "调用回执.json"
        saved = read_json(receipt_path, {})
        if saved:
            if saved.get("fingerprint") != fingerprint:
                raise ValueError("Existing receipt does not match planned call")
            raw_path = folder / "插件原始结果.json"
            if not raw_path.exists() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != saved.get("result_sha256") or read_json(raw_path, {}) != saved.get("result"):
                raise ValueError("Existing plugin result has been modified")
            if "workflow" in item:
                from .learned_capabilities import verify_receipt
                for child in saved["result"]["step_receipts"]:
                    verify_receipt(self.root, child["path"], child["sha256"])
            return saved, receipt_path
        if cancel.is_set():
            raise Interrupted("Plugin cancelled before launch")
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(folder / "调用请求.json", payload)
        if "workflow" in item:
            from .learned_capabilities import execute_workflow
            result, _ = execute_workflow(self, activity_id, item["workflow"], payload["arguments"], cancel)
            atomic_write_json(folder / "插件原始结果.json", result)
            receipt = {"version": 1, "activity_id": activity_id, "capability": action["capability"],
                       "fingerprint": fingerprint, "action": action, "request": payload, "result": result,
                       "result_sha256": hashlib.sha256((folder / "插件原始结果.json").read_bytes()).hexdigest()}
            atomic_write_json(receipt_path, receipt)
            return receipt, receipt_path
        # Temporary files avoid pipe deadlocks. Output is bounded before parsing.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
            stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8")); stdin.seek(0)
            process = subprocess.Popen(item["command"], stdin=stdin, stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            start = time.monotonic()
            try:
                while process.poll() is None:
                    if cancel.wait(.05):
                        raise Interrupted("Plugin interrupted")
                    if time.monotonic()-start > item.get("timeout", 10):
                        raise TimeoutError("Plugin exceeded timeout")
                    if stdout.tell() > 32768 or stderr.tell() > 32768:
                        raise ValueError("Plugin output exceeded 32 KiB")
                if cancel.is_set():
                    raise Interrupted("Plugin cancelled before receipt")
                stdout.seek(0); raw = stdout.read(32769)
                stderr.seek(0); error = stderr.read(32769)
                if process.returncode or len(raw) > 32768 or len(error) > 32768:
                    raise ValueError(f"Plugin failed or output exceeded limit (exit={process.returncode})")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict) or not result:
                    raise ValueError("Plugin must return a non-empty JSON object")
            except Exception as exc:
                stderr.seek(0)
                detail = stderr.read(32768).decode("utf-8", errors="replace")
                atomic_write_json(folder / "调用失败.json", {"type": type(exc).__name__, "error": str(exc),
                                  "exit_code": process.poll(), "stderr_tail": detail[-2000:]})
                raise
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
        receipt = {"version": 1, "activity_id": activity_id, "capability": action["capability"],
                   "fingerprint": fingerprint, "action": action, "request": payload, "result": result,
                   "result_sha256": hashlib.sha256(raw).hexdigest()}
        atomic_write_json(folder / "插件原始结果.json", result)
        receipt["result_sha256"] = hashlib.sha256((folder / "插件原始结果.json").read_bytes()).hexdigest()
        atomic_write_json(receipt_path, receipt)
        return receipt, receipt_path
