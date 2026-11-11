"""Immutable, tested compositions of registered capabilities, scoped to one root."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .storage import atomic_write_json, read_json

TYPES = {"string": str, "integer": int, "boolean": bool}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_definition(value, entries):
    if not isinstance(value, dict) or set(value) != {"id", "description", "arguments", "steps"}:
        raise ValueError("Capability requires id, description, arguments, steps")
    key = value["id"]
    if not isinstance(key, str) or not re.fullmatch(r"learned\.[a-z][a-z0-9_.-]{0,60}", key) or key in entries:
        raise ValueError("New capability requires a unique learned.* id")
    if not isinstance(value["description"], str) or not value["description"].strip():
        raise ValueError("Capability description is required")
    fields = value["arguments"]
    if not isinstance(fields, dict) or any(not isinstance(k, str) or t not in TYPES for k, t in fields.items()):
        raise ValueError("Capability arguments must declare primitive types")
    if not isinstance(value["steps"], list) or not 1 <= len(value["steps"]) <= 8:
        raise ValueError("Capability needs 1..8 executable steps")
    for index, step in enumerate(value["steps"], 1):
        if not isinstance(step, dict) or set(step) != {"capability", "arguments", "input_from_step"}:
            raise ValueError("Invalid workflow step")
        target = entries.get(step["capability"])
        # Keep execution bounds explicit: compositions call adapters, not other workflows.
        if target is None or "workflow" in target:
            raise ValueError("Workflow steps require installed primitive capabilities")
        params = step["arguments"]
        if not isinstance(params, dict) or set(params) != set(target.get("arguments", {})):
            raise ValueError("Workflow argument schema mismatch")
        for name, kind in target.get("arguments", {}).items():
            val = params[name]
            if isinstance(val, dict):
                if set(val) != {"parameter"} or not isinstance(val["parameter"], str) or fields.get(val["parameter"]) != kind:
                    raise ValueError("Invalid workflow parameter reference")
            elif type(val) is not TYPES[kind]:
                raise ValueError("Invalid workflow literal type")
        source = step["input_from_step"]
        if type(source) is not int or source < 0 or source >= index or (source and not target.get("accepts_input")):
            raise ValueError("Workflow dependencies must reference earlier results")
    return value


def actions_for(definition, arguments):
    fields = definition["arguments"]
    if not isinstance(arguments, dict) or set(arguments) != set(fields) or any(type(arguments[k]) is not TYPES[t] for k, t in fields.items()):
        raise ValueError("Learned capability argument schema mismatch")
    return [{"step": i, "capability": s["capability"],
             "arguments_json": json.dumps({k: arguments[v["parameter"]] if isinstance(v, dict) else v
                                            for k, v in s["arguments"].items()}, ensure_ascii=False),
             "input_from_step": s["input_from_step"]} for i, s in enumerate(definition["steps"], 1)]


def verify_receipt(root, relative, expected):
    folder = root.resolve() / "works/plugin-activities"
    path = (root / relative).resolve()
    if not path.is_relative_to(folder) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError("Learned capability test receipt is missing or modified")
    receipt = read_json(path, {})
    raw = path.parent / "插件原始结果.json"
    if (not receipt.get("result") or not raw.is_file()
            or hashlib.sha256(raw.read_bytes()).hexdigest() != receipt.get("result_sha256")
            or read_json(raw, {}) != receipt["result"]):
        raise ValueError("Learned capability test result is missing or modified")


def load_learned(root, entries):
    for path in sorted((root / "capabilities/learned").glob("*.json")):
        document = read_json(path, {})
        definition = validate_definition(document.get("definition"), entries)
        if document.get("version") != 1 or document.get("definition_sha256") != digest(definition):
            raise ValueError("Learned capability definition integrity failure")
        receipts = document.get("test_receipts", [])
        if len(receipts) != len(definition["steps"]):
            raise ValueError("Learned capability must have a tested receipt for every step")
        for receipt in receipts:
            verify_receipt(root, receipt["path"], receipt["sha256"])
        entries[definition["id"]] = {**definition, "workflow": definition, "read_only": True}


def execute_workflow(plugins, call_id, definition, arguments, cancel):
    actions = actions_for(definition, arguments)
    plugins.validate_actions(actions, len(actions))
    receipts, results = [], {}
    for action in actions:
        receipt, path = plugins.execute(f"{call_id}-s{action['step']}", action,
                                       results.get(action["input_from_step"]), cancel)
        results[action["step"]] = receipt["result"]
        receipts.append({"path": str(path.relative_to(plugins.root)),
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"output": results[len(actions)], "step_receipts": receipts}, receipts


def learn(plugins, call_id, proposal, cancel):
    if not isinstance(proposal, dict) or set(proposal) != {"definition", "test_arguments"}:
        raise ValueError("Learning requires definition and test_arguments")
    definition = validate_definition(proposal["definition"], plugins.entries)
    result, receipts = execute_workflow(plugins, call_id, definition, proposal["test_arguments"], cancel)
    if cancel.is_set():
        from .host import Interrupted
        raise Interrupted("Learning cancelled before registration")
    path = plugins.root / "capabilities/learned" / (definition["id"] + ".json")
    if path.exists():
        raise ValueError("Learned capability versions are immutable; choose a new id")
    atomic_write_json(path, {"version": 1, "definition": definition, "definition_sha256": digest(definition),
                             "test_arguments": proposal["test_arguments"], "test_receipts": receipts})
    plugins.entries[definition["id"]] = {**definition, "workflow": definition, "read_only": True}
    return {"registered": definition["id"], "definition_sha256": digest(definition),
            "path": str(path.relative_to(plugins.root)), "test_result": result}
