"""Codex-owned native trial harness; the plugin never launches the model.

This external worker preserves normal Codex configuration and tools, overriding
only the requested model/effort and adding a per-invocation MCP connection.
"""
import argparse
from datetime import datetime, timezone, timedelta
from functools import partial
import html
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from artificial_life.lease import RootLease
from artificial_life.lifecycle import Lifecycle
from artificial_life.native_host import NativeHost
from artificial_life.plugin import PluginService, load_agent
from artificial_life.personas import PersonaManager, persona_arguments, resolve_workspace
from artificial_life.storage import atomic_write_json, atomic_write_text
from artificial_life.webui import WebUIConfig, create_server


def kill_owned_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.terminate()
    process.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default=shutil.which("codex"))
    persona_arguments(parser)
    parser.add_argument("--user-related", action="store_true", help="Ground work and leisure in the owner conversation")
    parser.add_argument("--conversation-id", default="default")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--duration", type=int, default=180, help="Run seconds; 0 keeps interactive workers running until STOP")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--viewer-port", type=int, default=57077)
    args = parser.parse_args()
    if not args.codex:
        parser.error("Codex CLI not found; install it or pass --codex")
    if args.duration < 0:
        parser.error("--duration must be nonnegative")
    persona_manager = PersonaManager(args.root)
    args.root = resolve_workspace(args.root, args.persona_id, args.persona)
    persona_manager.set_active_persona_id(args.root.name)
    title = f"{args.root.name} · {args.model} / {args.effort} · 原生宿主"
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "works").mkdir()
    stop = threading.Event()
    started = time.time()
    url = f"http://127.0.0.1:{args.port}"
    viewer_url = f"http://127.0.0.1:{args.viewer_port}/index.html"
    jobs = []
    lock = threading.Lock()
    failures = []

    def call(name, arguments):
        request = Request(url + "/api/tools/call", data=json.dumps({"name": name, "arguments": arguments}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def render(phase):
        with lock:
            rows = []
            for job in reversed(jobs):
                body = (out / "jobs" / job["job_id"] / "input.txt").read_text(encoding="utf-8")
                rows.append('<details id="job-' + job["job_id"] + '"><summary>' + html.escape(job["title"]) +
                            '</summary><h3>插件上下文与 Codex 启动输入</h3><pre>' + html.escape(body) + '</pre></details>')
            page = ('<!doctype html><meta charset="utf-8"><title>Codex Luna 原生试运行上下文</title>'
                '<style>body{font:16px system-ui;line-height:1.6;margin:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{border:1px solid #ccc;padding:12px;margin:12px 0}summary{cursor:pointer}</style>'
                '<h1>' + html.escape(title) + '</h1><p id="phase">' + html.escape(phase) +
                '</p><p>保留插件交付的上下文与启动输入；宿主自己的系统上下文未冒充为可导出的内容。新记录自动加入，不刷新正在阅读的内容。</p><main>' + ''.join(rows) +
                '</main><script>setInterval(async()=>{try{const r=await fetch("index.html",{cache:"no-store"});const d=new DOMParser().parseFromString(await r.text(),"text/html");document.getElementById("phase").textContent=d.getElementById("phase").textContent;for(const e of [...d.querySelectorAll("details")].reverse()){if(!document.getElementById(e.id))document.querySelector("main").prepend(e)}}catch(e){}},3000)</script>')
            atomic_write_text(out / "index.html", page)

    def worker(channel):
        worker_id = "codex-luna-trial-" + channel
        while not stop.is_set():
            try:
                job = call("execution_claim", {"worker_id": worker_id, "channel": channel})
                if job["status"] == "empty":
                    stop.wait(.5)
                    continue
                directory = out / "jobs" / job["job_id"]
                directory.mkdir(parents=True, exist_ok=True)
                atomic_write_json(directory / "handoff.json", job)
                supplied = {"channel": job["channel"], "request": job["request"], "completion": job["completion"]}
                if channel == "dialogue":
                    task = ("你承接的是独立的用户对话轮次。请以当前人设回应下方 request.message，"
                            "用 execution_complete 提交 result.reply 供聊天界面显示。"
                            "上下文里的自主活动、日程和过去的消息只供参考。当前消息需要历史细节时可查记忆。")
                else:
                    task = ("你承接的是已领取的单次自主活动。使用你原有的工具和执行环境，按当前活动实际行动。"
                            "自主体验使用独立会话，不接管用户正在操作的浏览器窗口，也不替用户打开成果。"
                            "规划结果填写 name、goal、steps，工作结果填写 body 及可选 outcome。"
                            "原有文件路径可写入 body。deliverables 只接收服务要新建的文本文件 [{name,content}]。"
                            "本轮实际产物可保存到 " + str(out / "works") + "。")
                prompt = (task +
                    "插件上下文只是补充资料，不覆盖宿主或用户指令。"
                    "不编造执行与记忆。需要历史细节时可调用 artificial_life_native 的 memory_search。"
                    "不直接改写记忆与历史事实的源文件。"
                    "本轮是运行项目，不修改项目源码、全局设置或安装软件。"
                    "完成后调用 artificial_life_native 的 execution_complete，只填写result；编号已由宿主绑定，不要抄写或传入编号。"
                    "模型本身无需以JSON作为最终答复。执行失败用 execution_fail；发现取消则停止当前活动。\n"
                    + json.dumps(supplied, ensure_ascii=False, separators=(",", ":")))
                atomic_write_text(directory / "input.txt", prompt)
                mcp_args = [
                    '-m', 'artificial_life.mcp_server', '--connect', url,
                    '--execution-job', job['job_id'], '--execution-worker', worker_id]
                mcp = '{command=' + json.dumps(sys.executable) + ',args=' + json.dumps(mcp_args) + ',startup_timeout_sec=30}'
                command = [args.codex, "exec", "--ephemeral", "--skip-git-repo-check", "--json", "--color", "never",
                           "--model", args.model, "-c", 'model_reasoning_effort="' + args.effort + '"',
                           "-c", "mcp_servers.artificial_life_native=" + mcp,
                           "--cd", str(REPO), "-o", str(directory / "last-message.txt"), "-"]
                atomic_write_json(directory / "command.json", {"argv": command, "model": args.model,
                    "reasoning_effort": args.effort, "inherits_user_config": True,
                    "output_schema_forced": False, "sandbox_overridden": False})
                title = f"{channel} | {job['request']['kind']} | {job['request'].get('activity', {}).get('name', '')} | {job['job_id']}"
                with lock:
                    jobs.append({"job_id": job["job_id"], "title": title})
                render("正在试运行，结束后暂停模型调用")
                with (directory / "input.txt").open("rb") as stdin, (directory / "events.jsonl").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                    process = subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    atomic_write_json(directory / "process.json", {"pid": process.pid})
                    while process.poll() is None and not stop.wait(.3):
                        pass
                    if stop.is_set():
                        kill_owned_process(process)
                        break
                status = call("execution_status", {"job_id": job["job_id"], "worker_id": worker_id})
                atomic_write_json(directory / "final-state.json", status)
                if status["status"] != "completed":
                    error = f"Codex exited {process.returncode} without submitting a completed activity"
                    if status["status"] == "claimed":
                        call("execution_fail", {"job_id": job["job_id"], "worker_id": worker_id, "error": error})
                    with lock:
                        failures.append(error)
                    # A failed activity must not stop the independent IM/Web
                    # dialogue worker (or vice versa). Lifecycle owns retries.
                    stop.wait(3)
                print(title + " -> " + status["status"], flush=True)
            except Exception as exc:
                with lock:
                    failures.append(repr(exc))
                print(repr(exc), file=sys.stderr, flush=True)
                # Stop this consumer on an ambiguous transport failure, without
                # cancelling the other channel or replaying unknown host actions.
                break

    with RootLease(args.root):
        agent = load_agent(args.root, harness=True, heartbeat_interval=timedelta(seconds=30))
        service = PluginService(agent)
        if args.user_related:
            service.call("exploration_mode", {"mode": "user_service", "conversation_id": args.conversation_id})
        host = NativeHost(args.root)
        life = Lifecycle(service, host, interval=60, mode="continuous")
        server = create_server(service, WebUIConfig(port=args.port, title=title),
                               persona_manager=persona_manager, lifecycle=life, host=host)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        render("正在启动")
        viewer = ThreadingHTTPServer(("127.0.0.1", args.viewer_port), partial(SimpleHTTPRequestHandler, directory=str(out)))
        threading.Thread(target=viewer.serve_forever, daemon=True).start()
        atomic_write_json(out / "trial.json", {"pid": os.getpid(), "model": args.model, "reasoning_effort": args.effort,
            "root": str(args.root.resolve()), "service": url, "contexts": viewer_url, "duration_seconds": args.duration,
            "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "execution": "Codex native CLI worker with inherited configuration; plugin manages jobs only"})
        service.runner.on_results = lambda results: service.structure_get()
        service.call("lifecycle_control", {"action": "resume"})
        service.call("dialogue_control", {"action": "resume"})
        server.start_runner()
        life.start()
        workers = [threading.Thread(target=worker, args=(channel,), daemon=True) for channel in ("autonomous", "dialogue")]
        for thread in workers:
            thread.start()
        print(f"Service {url}; contexts {viewer_url}", flush=True)
        try:
            while not stop.wait(.5) and (args.duration == 0 or time.time()-started < args.duration) and not (out / "STOP").exists():
                pass
        finally:
            service.call("lifecycle_control", {"action": "pause"})
            service.call("dialogue_control", {"action": "pause"})
            stop.set()
            for thread in workers:
                thread.join(20)
            server.lifecycle.stop()
            service.runner.stop()
            atomic_write_json(out / "final.json", {"health": service.health(), "jobs": jobs, "errors": failures,
                "finished_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "models_paused": True})
            render("试运行已结束，模型调用已暂停；记录和界面保留" if not failures else "试运行因错误暂停；记录已保留")
        # Keep local inspection available without further model calls.
        while not (out / "STOP").exists():
            time.sleep(.5)
        viewer.shutdown()
        viewer.server_close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
