"""The Hermes REPL — short commands for a phone keyboard.

  run <text>        talk to the agent (alias: r)
  project ...       new/use/list (alias: p)
  gpu ...           attach/serve/status/tunnel/up/down (alias: g)
  mission/notes/history/summaries/tools/config/persona/help/quit
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from hermes import __version__, agent
from hermes import hosts as hosts_mod
from hermes.config import Config, hermes_home, persona_path
from hermes.gpu import (
    endpoint_from_state,
    load_gpu_state,
    probe_net_isolation,
    save_gpu_state,
)
from hermes.llm import make_backend
from hermes.project import Project, ProjectError
from hermes.sandbox import (
    capabilities as sandbox_capabilities,
    endpoint_from_state as sandbox_endpoint_from_state,
    load_sandbox_state,
    save_sandbox_state,
)
from hermes.ssh import SSHEndpoint, SSHError, kill_pid, parse_ssh_string, pid_alive
from hermes.ui import bold, cyan, dim, green, magenta, red, yellow

BANNER = f"{bold(magenta('hermes'))} {dim('v' + __version__)} — type {cyan('help')}"


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


def _sandbox_status_line() -> str:
    state = load_sandbox_state()
    if not state.get("host"):
        return "not attached"
    caps = []
    if state.get("runtime"):
        caps.append(state["runtime"])
    if state.get("kvm"):
        caps.append("kvm")
    suffix = f" ({', '.join(caps)})" if caps else ""
    return f"{state['user']}@{state['host']}:{state['port']}{suffix}"


def _edit_file(path: Path) -> None:
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(path)])


def _pick_model(cfg):
    """Let the operator choose which model to serve, defaulting to the one the
    config already points at. Persists the choice so `run` serves the same
    identity. Returns the chosen ModelSpec, or None if cancelled."""
    from hermes.models import model_list, resolve

    specs = model_list()
    current = resolve(cfg)
    default_idx = next((i for i, s in enumerate(specs) if s.key == current.key), 0)
    print(dim("which model?"))
    for i, s in enumerate(specs):
        tag = green("ready") if s.ready else yellow("experimental")
        here = cyan(" ← current") if s.key == current.key else ""
        print(f"  {cyan(f'[{i + 1}]')} {s.label} [{tag}]{here}")
    try:
        raw = input(f"model [{default_idx + 1}]? ").strip()
    except EOFError:
        raw = ""
    if not raw:
        spec = specs[default_idx]
    else:
        try:
            spec = specs[int(raw) - 1]
            if int(raw) < 1:
                raise IndexError
        except (ValueError, IndexError):
            print(yellow("not a listed choice — cancelled"))
            return None
    # The served name is what the OpenAI client (llm.py) sends; keep it in sync.
    cfg.set("model_id", spec.key)
    cfg.set("model", spec.served_name)
    cfg.set("quantization", spec.quantization)
    # Apply this model's tuned build — sampling, completion budget, stall
    # tolerance — so the agent loop and client serve its optimized profile, not
    # the previous model's. (The Hermes profile equals the app defaults.)
    for key, value in spec.runtime_config().items():
        cfg.set(key, value)
    cfg.save()
    return spec


# ---------------------------------------------------------------- commands
def cmd_run(cfg, args: str) -> None:
    if not args.strip():
        print(dim("usage: run <prompt>"))
        return
    project = _current_project(cfg)
    if project is None:
        print(yellow("no current project") + dim(" — `project new <name>` or `project use <name>`"))
        return
    state = load_gpu_state()
    gpu = endpoint_from_state(state)
    sandbox = sandbox_endpoint_from_state(load_sandbox_state())
    if cfg.get("backend") != "mock":
        if state.get("host"):
            _ensure_tunnel(cfg, state)
        if not _probe_vllm(cfg):
            print(red("vLLM endpoint not reachable") + dim(" — `gpu attach` + `gpu serve` first "
                  "(or `config set backend mock` for a dry run)."))
            return
    from hermes.models import resolve
    spec = resolve(cfg)
    env = {
        "gpu_status": _gpu_status_line(cfg, state),
        "sandbox_status": _sandbox_status_line(),
        "remote_workspace": state.get("remote_workspace", "~/hermes-workspace"),
        "context_window": state.get("served_ctx", 0),
        "model_identity": spec.identity,
        "model_tool_guidance": spec.tool_guidance,
    }
    prompt = args.strip()
    # `run build` is the refinement loop: reopen the twin and run a recon/build
    # pass that diffs the reconstruction against the live target and closes the
    # gap. Each invocation is another pass.
    if prompt.lower() == "build":
        twin = project.twin()
        if not twin.exists():
            print(yellow("not a build project") + dim(" — `project build <name> <url>` first"))
            return
        if twin.is_sealed():
            twin.unseal()
            print(dim("reopened the twin for a refinement pass."))
        prompt = (
            "Refinement pass. Use twin_diff to compare the live target against the "
            "twin as it stands, then close every divergence — reconstruct/build what "
            "the target really runs, and record/correct samples — until twin_diff "
            "reports all-match. Then twin_seal."
        )

    backend = make_backend(cfg)
    agent.run(project, prompt, cfg, backend, gpu=gpu, env=env, sandbox=sandbox)


def cmd_project(cfg, args: str) -> None:
    parts = args.split()
    sub = parts[0] if parts else "list"
    pdir = _projects_dir(cfg)
    if sub == "new" and len(parts) > 1:
        try:
            Project.create(pdir, parts[1])
        except ProjectError as e:
            print(red(e))
            return
        cfg.set("current_project", parts[1])
        cfg.save()
        print(green(f"project '{parts[1]}' created and selected.") + dim(" Edit its mission: `mission edit`"))
    elif sub == "build" and len(parts) >= 3:
        name, url = parts[1], parts[2]
        if not url.startswith(("http://", "https://")):
            print(red("usage: project build <name> <http(s)-url>"))
            return
        try:
            project = Project.create(pdir, name)
        except ProjectError as e:
            print(red(e))
            return
        cfg.set("current_project", name)
        cfg.save()
        twin = project.twin()
        twin.init(source=url, mode="url")
        # The builder needs to move files phone<->box and pull FOSS on the phone;
        # equip those up front so it isn't stuck fumbling for them.
        for t in ("download_file", "transfer"):
            project.equip_tool(t)
        report = _clone_target(cfg, twin, url, seal=False)
        print(green(f"build project '{name}' created — recon done: "
                    f"{report.get('services', 0)} service(s), "
                    f"{report.get('dirs', 0)} dir(s)/endpoint(s), "
                    f"{report.get('exposed', 0)} exposed file(s), stack fingerprinted (open)."))
        print(dim("Set the task with `mission edit`, then `run build` — the agent "
                  "stands up a runtime clone of the real webserver from this recon "
                  "(each `run build` is another reconstruction pass)."))
    elif sub == "use" and len(parts) > 1:
        try:
            Project.load(pdir, parts[1])
        except ProjectError as e:
            print(red(e))
            return
        cfg.set("current_project", parts[1])
        cfg.save()
        print(green(f"switched to '{parts[1]}'"))
    else:
        current = cfg.get("current_project")
        names = Project.list_names(pdir)
        if not names:
            print(dim("(no projects yet — `project new <name>`)"))
        for n in names:
            print(green("* ") + bold(n) if n == current else "  " + n)


def cmd_gpu(cfg, args: str) -> None:
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else "status"
    state = load_gpu_state()

    if sub == "attach":
        if len(parts) > 1:
            try:
                user, host, port = parse_ssh_string(parts[1])
            except SSHError as e:
                print(red(e))
                return
            instance_id = None
        else:
            from hermes.gpu.vast import VastError, running_instances
            try:
                instances = running_instances(cfg.get("vast_api_key", ""))
            except VastError as e:
                print(red(e) + dim("\n(fallback: paste it — `gpu attach ssh -p PORT root@HOST`)"))
                return
            if not instances:
                print(yellow("no running Vast.ai instances found."))
                return
            if len(instances) > 1:
                for i, inst in enumerate(instances):
                    print(f"  {cyan(f'[{i}]')} id={inst['id']} {inst['num_gpus']}x{inst['gpu_name']} ${inst['dph']:.2f}/hr")
                try:
                    pick = int(input("which? "))
                    inst = instances[pick]
                except (ValueError, IndexError, EOFError):
                    print(yellow("cancelled"))
                    return
            else:
                inst = instances[0]
            user, host, port = "root", inst["ssh_host"], int(inst["ssh_port"])
            instance_id = inst["id"]
        ep = SSHEndpoint(host=host, port=port, user=user)
        print(dim(f"checking ssh {user}@{host}:{port} ..."))
        if not ep.check():
            print(red("ssh check failed — is your key registered with Vast.ai?"))
            return
        ep.run(f"mkdir -p {ep.remote_workspace}")
        isolated = probe_net_isolation(ep)
        print("network isolation: " + (
            green("kernel-level (unshare)") if isolated
            else yellow("regex deny-list only (unshare unavailable in this container)")
        ))
        if state.get("tunnel_pid"):  # don't orphan a tunnel to the old box
            kill_pid(state["tunnel_pid"])
        state = {
            "instance_id": instance_id,
            "host": host, "port": port, "user": user,
            "remote_workspace": ep.remote_workspace,
            "net_isolation": isolated,
            "tunnel_pid": 0, "served_ctx": 0,
        }
        save_gpu_state(state)
        print(green("attached.") + dim(" Next: `gpu serve`"))

    elif sub == "serve":
        from hermes.gpu import provision
        ep = endpoint_from_state(state)
        if ep is None:
            print(yellow("not attached — `gpu attach` first"))
            return
        if "net_isolation" not in state:  # attached with an older version
            state["net_isolation"] = probe_net_isolation(ep)
            save_gpu_state(state)
            ep = endpoint_from_state(state)
        spec = _pick_model(cfg)
        if spec is None:
            print(yellow("cancelled"))
            return
        try:
            gpus = provision.detect_gpus(ep)
            plan = provision.plan_serve(gpus, cfg, spec)
        except provision.ProvisionError as e:
            print(red(f"cannot serve: {e}"))
            return
        print(f"model: {cyan(spec.label)}")
        print(f"GPUs: {cyan(', '.join(plan.gpu_names))} — {plan.total_vram_gb}GB total")
        if spec.server == "vllm":
            detail = f"vLLM · tp={plan.tensor_parallel}, util={plan.gpu_memory_utilization}"
        else:
            detail = f"llama.cpp · {plan.tensor_parallel} GPU(s)"
        print(f"plan: {detail}, context={plan.max_model_len}")
        for note in plan.notes:
            print(yellow(f"note: {note}"))
        try:
            provision.launch(ep, cfg, plan, spec)
        except provision.ProvisionError as e:
            print(red(f"launch failed: {e}"))
            return
        _ensure_tunnel(cfg, state)
        print(dim(f"waiting for the model to come up ({spec.weights_note})..."))
        if provision.wait_ready(ep, cfg):
            state["served_ctx"] = plan.max_model_len
            save_gpu_state(state)
            print(green(f"ready — {spec.label} is listening (context {plan.max_model_len}).")
                  + dim(" Try: run hello"))
        else:
            print(red("timed out.") + dim(" Inspect with: gpu status / `remote tail -n 50 ~/vllm.log`"))

    elif sub == "status":
        if not state.get("host"):
            print(yellow("not attached"))
            return
        box = f"{state['user']}@{state['host']}:{state['port']}"
        print(f"box: {cyan(box)}"
              + (dim(f" (vast id {state['instance_id']})") if state.get("instance_id") else ""))
        print(f"tunnel: pid {state.get('tunnel_pid')} "
              + (green("alive") if pid_alive(state.get("tunnel_pid", 0)) else red("dead")))
        print("vllm endpoint: " + (green("UP") if _probe_vllm(cfg) else red("down")))
        ep = endpoint_from_state(state)
        rc, out, _ = ep.run(
            "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader",
            timeout=20,
        )
        if rc == 0:
            print(out.strip())

    elif sub == "tunnel":
        _ensure_tunnel(cfg, state)
        print("tunnel " + (green("up") if _probe_vllm(cfg)
                           else yellow("started (endpoint not answering yet)")))

    elif sub in ("up", "resume"):
        iid = state.get("instance_id")
        if not iid or not cfg.get("vast_api_key"):
            print(yellow("no paused Vast instance to resume")
                  + dim(" — `gpu attach` to a running box instead"))
            return
        from hermes.gpu.vast import VastError, get_instance, start_instance
        try:
            start_instance(cfg.get("vast_api_key"), iid)
        except VastError as e:
            print(red(e))
            return
        print(dim(f"resuming Vast instance {iid} — waiting for it to boot..."))
        inst = None
        for _ in range(40):  # ~2 minutes
            try:
                inst = get_instance(cfg.get("vast_api_key"), iid)
            except VastError:
                inst = None
            if inst and inst.get("status") == "running" and inst.get("ssh_host"):
                break
            time.sleep(3)
        else:
            print(red("instance didn't come back up in time")
                  + dim(" — try `gpu up` again, or check the Vast console"))
            return
        # SSH host/port can change across a stop/start — always re-read them.
        user, host, port = "root", inst["ssh_host"], int(inst["ssh_port"])
        ep = SSHEndpoint(host=host, port=port, user=user)
        print(dim(f"checking ssh {user}@{host}:{port} ..."))
        if not ep.check():
            print(red("ssh check failed after resume")
                  + dim(" — the box may still be booting; try `gpu up` again shortly"))
            return
        ep.run(f"mkdir -p {ep.remote_workspace}")
        isolated = probe_net_isolation(ep)
        if state.get("tunnel_pid"):  # the old tunnel points at the pre-pause host
            kill_pid(state["tunnel_pid"])
        state.update({
            "host": host, "port": port, "user": user,
            "remote_workspace": ep.remote_workspace,
            "net_isolation": isolated, "tunnel_pid": 0, "served_ctx": 0,
        })
        save_gpu_state(state)
        print(green("resumed.") + dim(" The disk persisted, so `gpu serve` skips the "
              "weight download / llama.cpp rebuild. Next: `gpu serve`"))

    elif sub == "down":
        ep = endpoint_from_state(state)
        if ep:
            ep.run("kill $(cat ~/vllm.pid) 2>/dev/null; rm -f ~/vllm.pid")
            print(green("vLLM stopped."))
        if state.get("tunnel_pid"):
            kill_pid(state["tunnel_pid"])
            state["tunnel_pid"] = 0
        if ep:
            ep.close_master()  # don't leave the multiplexed ssh around
        if state.get("instance_id") and cfg.get("vast_api_key"):
            answer = input(
                f"pause Vast instance {state['instance_id']}? stops billing but keeps "
                "the disk, so `gpu up` resumes fast (weights + build intact) [y/N] "
            )
            if answer.strip().lower() == "y":
                from hermes.gpu.vast import VastError, stop_instance
                try:
                    stop_instance(cfg.get("vast_api_key"), state["instance_id"])
                    print(green("instance paused.")
                          + dim(" Resume later with `gpu up`. (To stop paying for the "
                                "disk too, destroy it in the Vast console.)"))
                except VastError as e:
                    print(red(e))
        state["served_ctx"] = 0
        save_gpu_state(state)
    else:
        print(dim("usage: gpu attach [sshstr] | serve | status | tunnel | up | down"))


def cmd_sandbox(cfg, args: str) -> None:
    """The persistent VPS where the runtime twin lives. Separate from the GPU box
    (rented on demand for compute): the sandbox host stays up so a contained clone
    of the target keeps running between runs."""
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else "status"
    state = load_sandbox_state()

    if sub == "add":
        if len(parts) < 2:
            print(red("usage: sandbox add ssh://user@host[:port]  (or a pasted `ssh -p PORT user@host`)"))
            return
        try:
            user, host, port = parse_ssh_string(parts[1])
        except SSHError as e:
            print(red(e))
            return
        from hermes.sandbox import SANDBOX_WORKSPACE
        ep = SSHEndpoint(host=host, port=port, user=user,
                         remote_workspace=SANDBOX_WORKSPACE)
        print(dim(f"checking ssh {user}@{host}:{port} ..."))
        if not ep.check():
            print(red("ssh check failed — is the VPS reachable and your key installed?"))
            return
        ep.run(f"mkdir -p {ep.remote_workspace}")
        print(dim("probing what the box can isolate with..."))
        caps = sandbox_capabilities(ep)
        runtime = caps["runtime"] or yellow("none yet (install on first `build serve`)")
        print(f"container runtime: {cyan(str(runtime))}")
        print("kvm (microVM-capable): " + (
            green("yes") if caps["kvm"]
            else dim("no — running plain containers (expected on a cheap VPS)")
        ))
        if state.get("tunnel_pid"):  # don't orphan a tunnel to the old box
            kill_pid(state["tunnel_pid"])
        save_sandbox_state({
            "host": host, "port": port, "user": user,
            "remote_workspace": ep.remote_workspace,
            "runtime": caps["runtime"], "kvm": caps["kvm"],
            "tunnel_pid": 0,
        })
        print(green("sandbox host registered.")
              + dim(" The runtime twin will be built and run here. Next: `build serve`."))

    elif sub == "status":
        if not state.get("host"):
            print(yellow("no sandbox host — `sandbox add ssh://user@host[:port]`"))
            return
        box = f"{state['user']}@{state['host']}:{state['port']}"
        print(f"box: {cyan(box)}")
        ep = sandbox_endpoint_from_state(state)
        caps = sandbox_capabilities(ep) if ep else {"runtime": state.get("runtime", ""), "kvm": state.get("kvm", False)}
        print(f"container runtime: {cyan(caps['runtime'] or 'none yet')}")
        print("kvm: " + (green("yes") if caps["kvm"] else dim("no")))
        print(f"tunnel: pid {state.get('tunnel_pid')} "
              + (green("alive") if pid_alive(state.get("tunnel_pid", 0)) else dim("none")))

    elif sub == "down":
        if state.get("tunnel_pid"):
            kill_pid(state["tunnel_pid"])
            state["tunnel_pid"] = 0
            save_sandbox_state(state)
        ep = sandbox_endpoint_from_state(state)
        if ep:
            ep.close_master()
        print(green("sandbox tunnel torn down.")
              + dim(" The twin container keeps running on the VPS until `build serve` "
                    "respins it or you stop it there."))

    elif sub == "rm":
        save_sandbox_state({})
        print(green("sandbox host forgotten."))

    else:
        print(dim("usage: sandbox add <ssh-string> | status | down | rm"))


def cmd_host(cfg, args: str) -> None:
    parts = args.split()
    sub = parts[0] if parts else "list"
    hosts = hosts_mod.load_hosts()

    if sub == "add" and len(parts) >= 3:
        name = parts[1]
        if not hosts_mod.HOST_NAME_RE.match(name):
            print(red("host name must match [A-Za-z0-9_-]{1,32}"))
            return
        # ssh:// form leaves room for a trailing note; a pasted `ssh -p ...`
        # command consumes the whole rest of the line.
        if parts[2].startswith("ssh://"):
            sshstr, note = parts[2], " ".join(parts[3:])
        else:
            sshstr, note = " ".join(parts[2:]), ""
        try:
            user, host, port = parse_ssh_string(sshstr)
        except SSHError as e:
            print(red(e))
            return
        ep = SSHEndpoint(host=host, port=port, user=user)
        print(dim(f"checking ssh {user}@{host}:{port} ..."))
        if not ep.check():
            print(yellow("warning: ssh check failed — saving anyway (server may be down)"))
        hosts[name] = {"host": host, "port": port, "user": user, "note": note}
        hosts_mod.save_hosts(hosts)
        print(green(f"host '{name}' registered.") + dim(" The agent reaches it with "
              "host_shell/host_read/host_write (reads free, writes ask you)."))

    elif sub == "rm" and len(parts) == 2:
        if hosts.pop(parts[1], None) is None:
            print(red(f"no such host: {parts[1]}"))
            return
        hosts_mod.save_hosts(hosts)
        print(green(f"host '{parts[1]}' removed."))

    elif sub == "list" or not parts:
        if not hosts:
            print(dim("(no managed hosts — `host add <name> ssh://user@host[:port]`)"))
        for name, rec in sorted(hosts.items()):
            note = dim(f"  {rec['note']}") if rec.get("note") else ""
            print(f"  {cyan(name)}  {rec.get('user', 'root')}@{rec['host']}:{rec.get('port', 22)}{note}")
    else:
        print(dim("usage: host add <name> <ssh-string> [note] | list | rm <name>"))


def _clone_target(cfg, twin, url: str, seal: bool = False) -> dict:
    """Recon a target into a twin blueprint, with live progress. This is not a
    page mirror — the twin is meant to be a runtime clone of the real webserver,
    so we fingerprint *what runs there* (the web stack and the listening
    services/versions), not what its pages look like. seal=False leaves it open
    for the recon/builder agent to reconstruct and seal."""
    from hermes.twin import clone as clone_mod
    from hermes.twin import scan as scan_mod
    from hermes.twin import survey as survey_mod
    colors = {"exchange": green, "spec": cyan, "error": red, "done": cyan, "stack": cyan}

    def on_event(kind, text):
        print(colors.get(kind, dim)(f"  {text}"))

    # Web-stack fingerprint: a light read of the root (+ discovery/well-known
    # endpoints) to identify the app/framework/server — no crawl of the site's
    # pages. max_depth=0 means we never follow links into the page graph.
    print(dim(f"fingerprinting the web stack at {url} (read-only, on the phone)..."))
    report = clone_mod.clone(
        twin, url,
        fetch=clone_mod._httpx_fetch,
        max_exchanges=cfg.get("twin_clone_max", 200),
        delay=cfg.get("twin_clone_delay", 0.5),
        max_depth=cfg.get("twin_clone_depth", 0),
        seal=seal,
        on_event=on_event,
    )

    # Service scan: the other half of "what runs here" — which TCP services are
    # listening and their versions (nmap -sV when present, else a connect scan).
    if cfg.get("scan_on_build", True):
        result = scan_mod.scan(
            url,
            top_ports=cfg.get("scan_top_ports", 1000),
            timeout=cfg.get("scan_timeout", 2.0),
            workers=cfg.get("scan_workers", 100),
            on_event=lambda t: print(cyan(f"  {t}")),
        )
        twin.store_services(result.to_dict())
        print(scan_mod.format_scan(result))
        report["services"] = len(result.services)

    # Webserver topography: which dirs/endpoints exist and what's readable
    # (source/config/VCS/backups) — the shape of the target, not its content.
    if cfg.get("survey_on_build", True):
        print(dim(f"surveying the webserver topography at {url}..."))
        sv = survey_mod.survey(
            url,
            fetch=clone_mod._httpx_fetch,
            max_paths=cfg.get("survey_max_paths", 400),
            workers=cfg.get("survey_workers", 40),
            include_subdomains=cfg.get("survey_subdomains", True),
            on_event=lambda t: print(cyan(f"  {t}")),
        )
        twin.store_topography(sv.to_dict())
        print(survey_mod.format_survey(sv))
        report["dirs"] = len(sv.dirs)
        report["exposed"] = len(sv.exposed)
    return report


def cmd_build(cfg, args: str) -> None:
    """The runtime twin: clone a target into a faithful, safe local copy the
    agent builds against — never the live service."""
    project = _current_project(cfg)
    if project is None:
        print(yellow("no current project") + dim(" — `project build <name> <url>` to start one"))
        return
    parts = args.split(maxsplit=1)
    sub = parts[0] if parts else "show"
    rest = parts[1].strip() if len(parts) > 1 else ""
    twin = project.twin()

    if sub == "win":
        if not twin.exists():
            print(yellow("no target yet — `project build <name> <url>` first"))
            return
        if not rest:
            print(twin.win_condition or dim("(no winning condition set)"))
            return
        twin.set_win_condition(rest)
        print(green("winning condition recorded."))

    elif sub == "clone":  # re-seed (e.g. after changing depth/cap), leaves it open
        if not twin.exists():
            print(yellow("no target yet — `project build <name> <url>` first"))
            return
        mission, win = twin.mission, twin.win_condition
        twin.init(source=twin.source, mode="url", mission=mission, win_condition=win)
        report = _clone_target(cfg, twin, twin.source, seal=False)
        print(green(f"twin re-seeded (open) — {report['exchanges']} sample(s)."))

    elif sub == "seal":  # freeze a seeded twin without the agent (quick path)
        if not twin.exists():
            print(yellow("no target yet — `project build <name> <url>` first"))
            return
        if twin.is_sealed():
            print(dim("already sealed."))
        elif not twin.exchanges():
            print(red("nothing to seal — twin has no samples."))
        else:
            twin.seal()
            print(green(f"twin sealed — {len(twin.exchanges())} sample(s). Build phase open."))

    elif sub == "serve":  # run the twin on the box for the solution to hit
        if not twin.is_sealed():
            print(yellow("twin isn't sealed yet — seal it first "
                         "(the agent's twin_seal, or `build seal`)."))
            return
        state = load_gpu_state()
        ep = endpoint_from_state(state)
        if ep is None:
            print(yellow("no GPU box attached — `gpu attach` first."))
            return
        from hermes.twin import deploy as twin_deploy
        port = cfg.get("twin_port", 8900)
        clean = rest.strip() == "clean"
        note = " (clean respin)" if clean else ""
        print(dim(f"spinning the twin up on the box (localhost:{port}) from the blueprint{note} ..."))
        report = twin_deploy.deploy(
            ep, twin, port, clean=clean,
            step_timeout=cfg.get("twin_serve_step_timeout", 1800),
            on_event=lambda t: print(dim("  " + t)),
        )
        if report["ok"]:
            via = "reconstructed from recipe" if report.get("source") == "blueprint" \
                else "recorded-response responder (no recipe yet)"
            print(green(f"twin live in the sandbox: http://127.0.0.1:{port}")
                  + dim(f"  [{via}]"))
        else:
            print(red(f"twin failed to start: {report.get('error')}"))
            if report.get("log_path"):
                print(dim(f"  serve log: {report['log_path']}  (or `build logs`)"))

    elif sub == "logs":  # the last build-serve transcript, for debugging a respin
        from hermes.twin import deploy as twin_deploy
        path = twin_deploy.serve_log_path(twin)
        if path.exists():
            print(path.read_text().rstrip())
        else:
            print(dim("no serve log yet — run `build serve` first."))

    elif sub == "blueprint":  # show the portable blueprint that respins the twin
        if not twin.exists():
            print(yellow("no target yet — `project build <name> <url>` first"))
            return
        recipe = twin.recipe()
        print(bold(f"blueprint for '{project.name}'") + dim(f"  ({project.twin_dir})"))
        print(twin.summary())
        if recipe:
            print(cyan(f"\nrecipe ({len(recipe)} step(s)) — `build serve` replays this "
                       "on any box to respin the runtime server:"))
            for i, s in enumerate(recipe, 1):
                note = dim(f"   # {s['note']}") if s.get("note") else ""
                print(f"  {i}. {s['cmd']}{note}")
        else:
            print(dim("\n(no recipe yet — reconstruct the stack with `run build`; "
                      "build_run captures each working step into the blueprint)"))

    elif sub == "clear":
        import shutil
        if project.twin_dir.exists():
            shutil.rmtree(project.twin_dir)
        print(green("twin cleared."))

    else:  # show
        print(twin.summary())


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
        print(yellow("no current project"))
        return
    if what == "mission":
        if args.strip() == "edit":
            _edit_file(project.mission_path)
        else:
            print(project.read_mission())
    elif what == "notes":
        print(project.read_notes() or dim("(no notes)"))
    elif what == "history":
        n = int(args) if args.strip().isdigit() else 20
        for e in project.recent_prompts(n):
            head = f"[{e.get('run', '?'):>4}] {e.get('ts', '')}"
            print(f"{dim(head)}  {e.get('text', '')[:120]}")
    elif what == "summaries":
        n = int(args) if args.strip().isdigit() else 3
        for run_id, text in project.recent_summaries(n):
            print(f"{cyan(f'--- run {run_id:04d} ---')}\n{text}\n")


def cmd_tools(cfg) -> None:
    from hermes.confirm import confirm
    from hermes.tools import build_registry
    project = _current_project(cfg)
    if project is None:
        print(yellow("no current project"))
        return
    registry = build_registry(project, cfg, confirm)
    for name in registry.names():
        t = registry._tools[name]
        print(f"  {cyan(name)} {dim(f'[{t.origin}]')}")
    print("\nlibrary (equip via the agent's list_toolbox/equip_tool):")
    for name, t in registry.library_tools().items():
        print(f"  {cyan(name)}: {t.description[:90]}")


HELP = f"""\
{cyan('run')} <text>            start an agent run {dim('(alias: r)')}
{cyan('project')} new|use|list  manage projects {dim('(alias: p)')}
{cyan('mission')} [edit]        show/edit the project mission
{cyan('notes')} / {cyan('history')} [n] / {cyan('summaries')} [n]
{cyan('tools')}                 list the agent's tools
{cyan('gpu')} attach [sshstr] | serve | status | tunnel | down   {dim('(alias: g)')}
{cyan('host')} add <name> <sshstr> [note] | list | rm <name>     your real servers
{cyan('sandbox')} add <sshstr> | status | down | rm              the VPS where the runtime twin lives
{cyan('project')} build <name> <url>   reconstruct a target into a twin to work against
{cyan('build')} win <text> | clone | seal | serve [clean] | blueprint | logs | show | clear   the twin for this project
{cyan('persona')} edit          edit the persona appended to the system prompt
{cyan('config')} [key [value]]  view/set configuration
{cyan('quit')}                  exit
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
    elif cmd == "host":
        cmd_host(cfg, rest)
    elif cmd == "sandbox":
        cmd_sandbox(cfg, rest)
    elif cmd == "build":
        cmd_build(cfg, rest)
    elif cmd == "config":
        cmd_config(cfg, rest)
    elif cmd in ("mission", "notes", "history", "summaries"):
        cmd_info(cfg, cmd, rest)
    elif cmd == "tools":
        cmd_tools(cfg)
    elif cmd == "persona":
        _edit_file(persona_path())
    else:
        print(red(f"unknown command: {cmd}") + dim(" (try `help`)"))
    return True


def main() -> None:
    cfg = Config.load()
    cfg.save()  # materialize defaults + persona on first start
    hermes_home().mkdir(parents=True, exist_ok=True)
    print(BANNER)
    project = cfg.get("current_project") or "-"
    print(f"project: {cyan(project)} {dim('·')} backend: {cyan(cfg.get('backend'))}")

    session = None
    ansi = None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import ANSI as ansi
        from prompt_toolkit.history import FileHistory
        session = PromptSession(history=FileHistory(str(hermes_home() / "repl_history")))
    except Exception:
        pass

    while True:
        proj = cfg.get("current_project") or "-"
        prompt_text = f"{magenta('hermes')}({cyan(proj)})> "
        try:
            line = session.prompt(ansi(prompt_text)) if session else input(prompt_text)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            if not dispatch(cfg, line):
                break
        except Exception as e:  # the REPL must survive anything
            print(red(f"error: {type(e).__name__}: {e}"))
    print(dim("bye."))


if __name__ == "__main__":
    main()
