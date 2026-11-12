"""Official MCP SDK stdio adapter; stdout is reserved for protocol messages."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from functools import partial
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from copy import deepcopy

from .lease import RootLease
from .plugin import PluginService, TOOLS, load_agent


HOST_INSTRUCTIONS = (
    "This plugin adds portable memory and lifecycle bookkeeping to the existing agent. "
    "Keep the host's model, tools, permissions, workspace, user instructions and native execution loop. "
    "Context is supplemental: read the concise tree context when useful, search memory only as needed, and cite sources. "
    "For managed activities, distinguish plans from executed facts and record actual results. "
    "Native lifecycle services expose execution_claim/execution_complete/execution_fail: claim due work, "
    "execute with your own native tools, then report the result through the tool; no special final-answer format is required. "
    "Dialogue and autonomous work have independent channels and cancellation; dialogue never controls autonomous scheduling. "
    "Unattended work requires a host-supported independent wakeup/worker; MCP alone cannot run a closed host."
)


class RemoteTools:
    """Proxy into the UI's tool API so there is only one writer/runner."""
    def __init__(self, url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("connect must be a loopback http URL")
        self.url = url.rstrip("/")

    def call(self, name, arguments):
        request = Request(self.url + "/api/tools/call", data=json.dumps({"name": name, "arguments": arguments}).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                body = json.loads(exc.read())
            except (ValueError, UnicodeError):
                raise ValueError(f"Service returned HTTP {exc.code}") from exc
            raise ValueError(body.get("message") or body.get("error") or f"HTTP {exc.code}") from exc


class BoundExecutionTools:
    """Transport binding keeps opaque management IDs out of model arguments."""
    def __init__(self, service, job_id, worker_id):
        import re
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not worker_id.strip():
            raise ValueError("invalid execution binding")
        self.service, self.job_id, self.worker_id = service, job_id, worker_id
        execution_tools = ("execution_complete", "execution_fail", "execution_status")
        self.tools = {name: deepcopy(TOOLS[name]) for name in (*execution_tools, "memory_search")}
        for name in execution_tools:
            definition = self.tools[name]["inputSchema"]
            for key in ("job_id", "worker_id"):
                definition["properties"].pop(key)
            definition["required"] = [k for k in definition["required"] if k not in {"job_id", "worker_id"}]

    def call(self, name, arguments):
        if name not in self.tools:
            raise ValueError("tool is not available in this bound execution session")
        arguments = dict(arguments)
        if name in {"execution_complete", "execution_fail", "execution_status"}:
            if "job_id" in arguments or "worker_id" in arguments:
                raise ValueError("execution IDs are bound by the host; omit job_id and worker_id")
            arguments.update(job_id=self.job_id, worker_id=self.worker_id)
        return self.service.call(name, arguments)


async def serve(service):
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import Tool, TextContent, CallToolResult
    except ImportError as exc:
        raise RuntimeError('Install with python -m pip install -e ".[mcp]"') from exc
    server = Server("artificial-life", instructions=HOST_INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return [Tool(name=name, **definition) for name, definition in getattr(service, "tools", TOOLS).items()]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            result = await asyncio.to_thread(service.call, name, arguments or {})
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], structuredContent=result)
        except (ValueError, KeyError, RuntimeError) as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    from .personas import persona_arguments, resolve_workspace
    parser = argparse.ArgumentParser(description="Artificial Life plugin MCP server")
    persona_arguments(parser)
    parser.add_argument("--root", type=Path, help="Explicit isolated tools-only writer, without an autonomous host")
    parser.add_argument("--connect", default=os.getenv("ARTIFICIAL_LIFE_CONNECT"))
    parser.add_argument("--standalone", action="store_true", help="Run a tools-only writer tied to this MCP process")
    parser.add_argument("--execution-job", help="Bind execution tools to one already-claimed native job")
    parser.add_argument("--execution-worker", help="Owner of the already-claimed job")
    args = parser.parse_args(argv)
    if bool(args.execution_job) != bool(args.execution_worker):
        parser.error("execution-job and execution-worker must be supplied together")
    if args.execution_job and not args.connect:
        parser.error("execution binding requires --connect")
    if args.persona_id and args.connect:
        parser.error("--connect selects the server's workspace; select persona when starting that server")
    if args.persona_id and args.root is None and not args.standalone:
        parser.error("persona selection requires --root or --standalone")
    if args.connect or (args.root is None and not args.standalone):
        service = RemoteTools(args.connect or "http://127.0.0.1:8771")
        if args.execution_job:
            service = BoundExecutionTools(service, args.execution_job, args.execution_worker)
        asyncio.run(serve(service))
        return
    args.root = args.root or Path(os.getenv("ARTIFICIAL_LIFE_ROOT", str(Path.home() / ".artificial-life" / "agent")))
    args.root = resolve_workspace(args.root, args.persona_id, args.persona)
    with RootLease(args.root):
        service = PluginService(load_agent(args.root, harness=True))
        service.runner.start()
        try:
            asyncio.run(serve(service))
        finally:
            service.runner.stop()


if __name__ == "__main__":
    main()
