"""Persistent process owner. UI and MCP are clients, never the lifetime owner."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from .api_host import APIConfig, APIHost
from .host import CodexHost
from .native_host import NativeHost
from .dsh_host import DSHHost
from .lease import RootLease
from .lifecycle import Lifecycle
from .plugin import PluginService, load_agent
from .personas import PersonaManager, persona_arguments
from .webui import WebUIConfig, create_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the autonomous Artificial Life service")
    persona_arguments(parser)
    parser.add_argument("--engine", choices=["native", "codex", "api", "dsh"], default="codex")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--mode", choices=["rhythm", "continuous"])
    parser.add_argument("--codex")
    parser.add_argument("--model", help="Explicit Codex host model")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    parser.add_argument("--timeout", type=int, help="Override host timeout in seconds")
    parser.add_argument("--api-config", type=Path, help="Non-secret API JSON config; default ROOT/config/api.json")
    parser.add_argument("--dsh-config", type=Path, help="DSH command JSON; default ROOT/config/dsh.json")
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args(argv)
    defaults = {"native": (Path.home() / ".artificial-life" / "native", 8771), "codex": ("D:/memory", 8768), "api": ("D:/memory/standalone", 8769), "dsh": ("D:/memory/dsh", 8770)}
    args.root = (args.root or Path(defaults[args.engine][0])).resolve()
    args.port = args.port or defaults[args.engine][1]
    
    if args.interval < 1 or args.timeout is not None and args.timeout < 1:
        parser.error("interval/timeout must be positive")
    if args.supervise:
        command = [sys.executable, "-u", "-m", "artificial_life.daemon", "--root", str(args.root.resolve()),
                   "--port", str(args.port), "--interval", str(args.interval), "--engine", args.engine]
        if args.timeout is not None:
            command += ["--timeout", str(args.timeout)]
        if args.mode:
            command += ["--mode", args.mode]
        if args.persona_id:
            command += ["--persona-id", args.persona_id]
        if args.persona is not None:
            command += ["--persona", args.persona]
        if args.api_config:
            command += ["--api-config", str(args.api_config.resolve())]
        if args.codex:
            command += ["--codex", args.codex]
        if args.model:
            command += ["--model", args.model]
        if args.reasoning_effort:
            command += ["--reasoning-effort", args.reasoning_effort]
        if args.dsh_config:
            command += ["--dsh-config", str(args.dsh_config.resolve())]
        log = args.root / "runtime" / "supervisor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        while True:
            with log.open("ab") as output:
                process = subprocess.Popen(command, stdout=output, stderr=output,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                code = process.wait()
                output.write(f"\nworker exited {code}; restart in 10 seconds\n".encode())
            # A second owner is rejected rather than starting a crash loop.
            if code == 73:
                return
            time.sleep(10)
    try:
        lease = RootLease(args.root)
    except RuntimeError:
        raise SystemExit(73)
    with lease:
        persona_mgr = PersonaManager(args.root)
        persona_mgr.ensure_layout()
        if args.persona_id and args.persona:
            try:
                persona_mgr.create_persona(args.persona_id, args.persona_id, args.persona)
            except ValueError:
                pass
            persona_mgr.set_active_persona_id(args.persona_id)
        elif args.persona_id:
            persona_mgr.set_active_persona_id(args.persona_id)

        active_id = persona_mgr.get_active_persona_id()
        workspace = persona_mgr.get_workspace(active_id)

        if args.engine == "native":
            host = NativeHost(workspace, auto_fallback_delay=1.0)
        elif args.engine == "api":
            from dataclasses import replace
            config = APIConfig.load(args.api_config or workspace / "config/api.json")
            if args.timeout is not None:
                config = replace(config, timeout=args.timeout)
            host = APIHost(workspace, config)
        elif args.engine == "dsh":
            host = DSHHost(workspace, args.dsh_config, args.timeout or 600)
        else:
            host = CodexHost(workspace, args.codex, args.timeout or 600,
                             model=args.model, reasoning_effort=args.reasoning_effort)
        agent = load_agent(workspace, harness=True)
        service = PluginService(agent)
        worker = Lifecycle(service, host, interval=args.interval, mode=args.mode)
        service.runner.on_results = lambda results: service.call("structure_get")
        server = create_server(service, WebUIConfig(port=args.port, title={"native": "宿主原生执行", "api": "API 独立版", "codex": "Codex 版", "dsh": "DeepSeek Harness 版"}[args.engine]),
                               persona_manager=persona_mgr, lifecycle=worker, host=host)
        service.structure_get()
        server.start_runner()
        worker.start()
        print(f"Artificial Life autonomous service: http://127.0.0.1:{args.port}/ root={args.root.resolve()} (active persona={active_id})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            worker.stop()
            service.runner.stop()
            server.server_close()


if __name__ == "__main__":
    main()
