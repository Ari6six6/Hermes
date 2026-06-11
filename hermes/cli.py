"""The Hermes REPL — short commands for a phone keyboard.

  run <text>        talk to the agent (alias: r)
  project ...       new/use/list (alias: p)
  gpu ...           attach/serve/status/tunnel/down (alias: g)
  mission/notes/history/summaries/tools/config/persona/help/quit
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx

from hermes import __version__, agent
from hermes.config import Config, hermes_home, persona_path
from hermes.gpu import endpoint_from_state, load_gpu_state, save_gpu_state
from hermes.gpu.ssh import SSHEndpoint, SSHError, kill_pid, parse_ssh_string, pid_alive
from hermes.llm import make_backend
from hermes.project import Project, ProjectError

BANNER = f"hermes v{__version__} — type `help`"


# ---------------------------------------------------------------- helpers
def _projects_dir(cfg) -> Path:
    return Path(cfg.get("projects_dir")).expanduser()


def _current_project(cfg) -> Project | None:
    name = cfg.get("current_project")
    if not name:
        return None
    try:
        return Project.load(_projects_dir(cfg), name)
    except ProjectError:
        return None


def _probe_vllm(cfg) -> bool:
    try:
        url = f"http://127.0.0.1:{cfg.get('local_port', 8000)}/v1/models"
        return httpx.get(url, timeout=4).status_code == 200
    except httpx.HTTPError:
        return False


def _ensure_tunnel(cfg, state) -> None:
    """Best effort: restart the tunnel if the pid died."""
    ep = endpoint_from_state(state)
    if ep is None:
        return
    if pid_alive(state.get("tunnel_pid", 0)) and _probe_vllm(cfg):
        return
    if state.get("tunnel_pid"):
        kill_pid(state["tunnel_pid"])
    pid = ep.start_tunnel(cfg.get("local_port", 8000), cfg.get("gpu_port", 8000))
    state["tunnel_pid"] = pid
    save_gpu_state(state)


def _gpu_status_line(cfg, state) -> str:
    if not state.get("host"):
        return "not attached"
    up = "vllm:up" if _probe_vllm(cfg) else "vllm:DOWN"
    ctx = state.get("served_ctx")
    return f"{state['host']}:{state['port']} ({up}{f', ctx {ctx}' if ctx else ''})"


def _edit_file(path: Path) -> None:
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(path)])


# ---------------------------------------------------------------- commands
def cmd_run(cfg, args: str) -> None:
    if not args.strip():
        print("usage: run <prompt>")
        return
    project = _current_project(cfg)
    if project is None:
        print("no current project — `project new <name>` or `project use <name>`")
        return
    state = load_gpu_state()
    gpu = endpoint_from_state(state)
    if cfg.get("backend") != "mock":
        if state.get("host"):
            _ensure_tunnel(cfg, state)
        if not _probe_vllm(cfg):
            print("vLLM endpoint not reachable — `gpu attach` + `gpu serve` first "
                  "(or `config set backend mock` for a dry run).")
            return
    env = {
        "gpu_status": _gpu_status_line(cfg, state),
        "remote_workspace": state.get("remote_workspace", "~/hermes-workspace"),
        "context_window": state.get("served_ctx", 0),
    }
    backend = make_backend(cfg)
    agent.run(project, args.strip(), cfg, backend, gpu=gpu, env=env)


def cmd_project(cfg, args: str) -> None:
    parts = args.split()
    sub = parts[0] if parts else "list"
    pdir = _projects_dir(cfg)
    if sub == "new" and len(parts) > 1:
        try:
            Project.create(pdir, parts[1])
        except ProjectError as e:
            print(e)
            return
        cfg.set("current_project", parts[1])
        cfg.save()
        print(f"project '{parts[1]}' created and selected. Edit its mission: `mission edit`")
    elif sub == "use" and len(parts) > 1:
        try:
            Project.load(pdir, parts[1])
        except ProjectError as e:
            print(e)
            return
        cfg.set("current_project", parts[1])
        cfg.save()
        print(f"switched to '{parts[1]}'")
    else:
        current = cfg.get("current_project")
        names = Project.list_names(pdir)
        if not names:
            print("(no projects yet — `project new <name>`)")
        for n in names:
            print(("* " if n == current else "  ") + n)


def cmd_gpu(cfg, args: str) -> None:
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else "status"
    state = load_gpu_state()

    if sub == "attach":
        if len(parts) > 1:
            try:
                user, host, port = parse_ssh_string(parts[1])
            except SSHError as e:
                print(e)
                return
            instance_id = None
        else:
            from hermes.gpu.vast import VastError, running_instances
            try:
                instances = running_instances(cfg.get("vast_api_key", ""))
            except VastError as e:
                print(f"{e}\n(fallback: paste it — `gpu attach ssh -p PORT root@HOST`)")
                return
            if not instances:
                print("no running Vast.ai instances found.")
                return
            if len(instances) > 1:
                for i, inst in enumerate(instances):
                    print(f"  [{i}] id={inst['id']} {inst['num_gpus']}x{inst['gpu_name']} ${inst['dph']:.2f}/hr")
                try:
                    pick = int(input("which? "))
                    inst = instances[pick]
                except (ValueError, IndexError, EOFError):
                    print("cancelled")
                    return
            else:
                inst = instances[0]
            user, host, port = "root", inst["ssh_host"], int(inst["ssh_port"])
            instance_id = inst["id"]
        ep = SSHEndpoint(host=host, port=port, user=user)
        print(f"checking ssh {user}@{host}:{port} ...")
        if not ep.check():
            print("ssh check failed — is your key registered with Vast.ai?")
            return
        ep.run(f"mkdir -p {ep.remote_workspace}")
        state = {
            "instance_id": instance_id,
            "host": host, "port": port, "user": user,
            "remote_workspace": ep.remote_workspace,
            "tunnel_pid": 0, "served_ctx": 0,
        }
        save_gpu_state(state)
        print("attached. Next: `gpu serve`")

    elif sub == "serve":
        from hermes.gpu import provision
        ep = endpoint_from_state(state)
        if ep is None:
            print("not attached — `gpu attach` first")
            return
        try:
            gpus = provision.detect_gpus(ep)
            plan = provision.plan_serve(gpus, cfg)
        except provision.ProvisionError as e:
            print(f"cannot serve: {e}")
            return
        print(f"GPUs: {', '.join(plan.gpu_names)} — {plan.total_vram_gb}GB total")
        print(f"plan: tp={plan.tensor_parallel}, context={plan.max_model_len}, "
              f"util={plan.gpu_memory_utilization}")
        for note in plan.notes:
            print(f"note: {note}")
        try:
            provision.launch(ep, cfg, plan)
        except provision.ProvisionError as e:
            print(f"launch failed: {e}")
            return
        _ensure_tunnel(cfg, state)
        print("waiting for the model to come up (first run downloads ~37GB)...")
        if provision.wait_ready(ep, cfg):
            state["served_ctx"] = plan.max_model_len
            save_gpu_state(state)
            print(f"ready — Hermes is listening (context {plan.max_model_len}). Try: run hello")
        else:
            print("timed out. Inspect with: gpu status / `remote tail -n 50 ~/vllm.log`")

    elif sub == "status":
        if not state.get("host"):
            print("not attached")
            return
        print(f"box: {state['user']}@{state['host']}:{state['port']}"
              + (f" (vast id {state['instance_id']})" if state.get("instance_id") else ""))
        print(f"tunnel: pid {state.get('tunnel_pid')} "
              f"{'alive' if pid_alive(state.get('tunnel_pid', 0)) else 'dead'}")
        print(f"vllm endpoint: {'UP' if _probe_vllm(cfg) else 'down'}")
        ep = endpoint_from_state(state)
        rc, out, _ = ep.run(
            "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader",
            timeout=20,
        )
        if rc == 0:
            print(out.strip())

    elif sub == "tunnel":
        _ensure_tunnel(cfg, state)
        print("tunnel " + ("up" if _probe_vllm(cfg) else "started (endpoint not answering yet)"))

    elif sub == "down":
        ep = endpoint_from_state(state)
        if ep:
            ep.run("kill $(cat ~/vllm.pid) 2>/dev/null; rm -f ~/vllm.pid")
            print("vLLM stopped.")
        if state.get("tunnel_pid"):
            kill_pid(state["tunnel_pid"])
            state["tunnel_pid"] = 0
        if state.get("instance_id") and cfg.get("vast_api_key"):
            answer = input(f"stop Vast instance {state['instance_id']} (stops billing)? [y/N] ")
            if answer.strip().lower() == "y":
                from hermes.gpu.vast import VastError, stop_instance
                try:
                    stop_instance(cfg.get("vast_api_key"), state["instance_id"])
                    print("instance stopped.")
                except VastError as e:
                    print(e)
        state["served_ctx"] = 0
        save_gpu_state(state)
    else:
        print("usage: gpu attach [sshstr] | serve | status | tunnel | down")


def cmd_config(cfg, args: str) -> None:
    args = args.strip()
    # accept both `config key value` and `config set key value` / `config get key`
    first, _, rest = args.partition(" ")
    if first in ("set", "get"):
        args = rest.strip()
    parts = args.split(maxsplit=1)
    if len(parts) == 2:
        cfg.set(parts[0], parts[1])
        cfg.save()
        print(f"{parts[0]} = {cfg.get(parts[0])}")
    elif len(parts) == 1 and parts[0]:
        print(json.dumps(cfg.get(parts[0]), indent=2))
    else:
        redacted = dict(cfg.data)
        if redacted.get("vast_api_key"):
            redacted["vast_api_key"] = "***"
        print(json.dumps(redacted, indent=2))


def cmd_info(cfg, what: str, args: str) -> None:
    project = _current_project(cfg)
    if project is None:
        print("no current project")
        return
    if what == "mission":
        if args.strip() == "edit":
            _edit_file(project.mission_path)
        else:
            print(project.read_mission())
    elif what == "notes":
        print(project.read_notes() or "(no notes)")
    elif what == "history":
        n = int(args) if args.strip().isdigit() else 20
        for e in project.recent_prompts(n):
            print(f"[{e.get('run', '?'):>4}] {e.get('ts', '')}  {e.get('text', '')[:120]}")
    elif what == "summaries":
        n = int(args) if args.strip().isdigit() else 3
        for run_id, text in project.recent_summaries(n):
            print(f"--- run {run_id:04d} ---\n{text}\n")


def cmd_tools(cfg) -> None:
    from hermes.confirm import confirm
    from hermes.tools import build_registry
    project = _current_project(cfg)
    if project is None:
        print("no current project")
        return
    registry = build_registry(project, cfg, confirm)
    for name in registry.names():
        t = registry._tools[name]
        print(f"  {name} [{t.origin}]")
    print("\nlibrary (equip via the agent's list_toolbox/equip_tool):")
    for name, t in registry.library_tools().items():
        print(f"  {name}: {t.description[:90]}")


HELP = """\
run <text>            start an agent run (alias: r)
project new|use|list  manage projects (alias: p)
mission [edit]        show/edit the project mission
notes / history [n] / summaries [n]
tools                 list the agent's tools
gpu attach [sshstr] | serve | status | tunnel | down   (alias: g)
persona edit          edit the persona appended to the system prompt
config [key [value]]  view/set configuration
quit                  exit
"""


def dispatch(cfg, line: str) -> bool:
    """Returns False to exit the REPL."""
    line = line.strip()
    if not line:
        return True
    cmd, _, rest = line.partition(" ")
    cmd = {"r": "run", "p": "project", "g": "gpu", "exit": "quit", "q": "quit"}.get(cmd, cmd)
    if cmd == "quit":
        return False
    elif cmd == "help":
        print(HELP)
    elif cmd == "run":
        cmd_run(cfg, rest)
    elif cmd == "project":
        cmd_project(cfg, rest)
    elif cmd == "gpu":
        cmd_gpu(cfg, rest)
    elif cmd == "config":
        cmd_config(cfg, rest)
    elif cmd in ("mission", "notes", "history", "summaries"):
        cmd_info(cfg, cmd, rest)
    elif cmd == "tools":
        cmd_tools(cfg)
    elif cmd == "persona":
        _edit_file(persona_path())
    else:
        print(f"unknown command: {cmd} (try `help`)")
    return True


def main() -> None:
    cfg = Config.load()
    cfg.save()  # materialize defaults + persona on first start
    hermes_home().mkdir(parents=True, exist_ok=True)
    print(BANNER)
    project = cfg.get("current_project") or "-"
    print(f"project: {project} · backend: {cfg.get('backend')}")

    session = None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        session = PromptSession(history=FileHistory(str(hermes_home() / "repl_history")))
    except Exception:
        pass

    while True:
        prompt_text = f"hermes({cfg.get('current_project') or '-'})> "
        try:
            line = session.prompt(prompt_text) if session else input(prompt_text)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            if not dispatch(cfg, line):
                break
        except Exception as e:  # the REPL must survive anything
            print(f"error: {type(e).__name__}: {e}")
    print("bye.")


if __name__ == "__main__":
    main()
