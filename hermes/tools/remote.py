"""Tools that execute on the rented GPU box over SSH.

The GPU box is the agent's compute sandbox — shell and file ops there run
without confirmation (commands are echoed to the screen). Internet from the
box is a deny-list speed bump, not a cage: a root agent can route around it,
so the deny-list is paired with an honest ask in the prompt rather than a
false claim of impossibility. Obvious network commands are redirected to the
phone-side web tools.
"""

from __future__ import annotations

import re
import shlex

from hermes.ssh import anchored_path, shell_path
from hermes.tools.base import obj_schema, tool
from hermes.ui import dim

NETWORK_RE = re.compile(
    r"(?:^|[;&|]\s*|\bsudo\s+)"
    r"(curl|wget|aria2c|axel|ssh|scp|sftp|rsync|nc|ncat|netcat|socat|ping|"
    r"git\s+(?:clone|pull|fetch|push|remote)|"
    r"pip3?\s+(?:install|download)|uv\s+pip\s+install|"
    r"apt(?:-get)?\s+(?:install|update|upgrade)|apk\s+add|yum\s+install|"
    r"conda\s+(?:install|create)|npm\s+(?:install|i)\b|"
    r"huggingface-cli|hf\s+download|gdown)\b"
)

NETWORK_DENIED = (
    "Not blocking you — asking you: please keep this off the GPU box and run "
    "it from the phone instead. The box can technically reach the network, but "
    "we want every byte of egress to go through the phone where the operator "
    "can see it (it's rented from a stranger). Use http_request / web_search "
    "for the network part. To get a file onto the box: download it on the "
    "phone (download_file toolbox), then push it with the `transfer` toolbox "
    "tool (equip both via equip_tool). remote_write works for small text "
    "files only."
)


def _need_gpu(ctx):
    if ctx.gpu is None:
        return "ERROR: no GPU box attached. Tell the operator to run `gpu attach`."
    return None


@tool(
    "remote_shell",
    "Run a shell command on the GPU box (Linux, root) — your compute sandbox "
    "for running code, builds, and heavy work. Default cwd is the remote "
    "workspace. Keep internet off the box: run network steps from the phone "
    "instead (the operator wants all egress visible there).",
    obj_schema(
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds, default 120"},
            "cwd": {"type": "string", "description": "remote working dir (optional)"},
        },
        ["command"],
    ),
)
def remote_shell(args, ctx):
    err = _need_gpu(ctx)
    if err:
        return err
    command = args["command"]
    inner = f"({command})"
    if not ctx.cfg.get("allow_gpu_network", False):
        if NETWORK_RE.search(command):
            return NETWORK_DENIED
        # The regex above is a fast, helpful first line; when the box supports
        # network namespaces the command also physically loses the network.
        if ctx.gpu.net_isolation:
            inner = f"unshare -n -- sh -c {shlex.quote(command)}"
    timeout = min(int(args.get("timeout", 120)), 1800)
    cwd = shell_path(anchored_path(args.get("cwd") or "", ctx.gpu.remote_workspace))
    print(dim(f"  [gpu] $ {command}"))
    rc, out, errout = ctx.gpu.run(f"cd {cwd} && {inner}", timeout=timeout)
    body = (out or "") + (("\n[stderr]\n" + errout) if errout else "")
    return f"exit code {rc}\n{body.strip() or '(no output)'}"


@tool(
    "remote_read",
    "Read a text file from the GPU box. Relative paths resolve inside the "
    "remote workspace. Text only — to move binary files or anything large, "
    "equip the `transfer` toolbox tool.",
    obj_schema({"path": {"type": "string"}}, ["path"]),
)
def remote_read(args, ctx):
    err = _need_gpu(ctx)
    if err:
        return err
    path = anchored_path(args["path"], ctx.gpu.remote_workspace)
    rc, out, errout = ctx.gpu.run(f"cat {shell_path(path)}", timeout=60)
    if rc != 0:
        return f"ERROR: {errout.strip() or 'read failed'}"
    return out


@tool(
    "remote_write",
    "Write a text file on the GPU box. Relative paths resolve inside the "
    "remote workspace. Text only — to move binary files or anything large, "
    "equip the `transfer` toolbox tool.",
    obj_schema(
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
)
def remote_write(args, ctx):
    err = _need_gpu(ctx)
    if err:
        return err
    path = anchored_path(args["path"], ctx.gpu.remote_workspace)
    rc, _, errout = ctx.gpu.write_file(path, args["content"])
    if rc != 0:
        return f"ERROR: {errout.strip() or 'write failed'}"
    return f"wrote {len(args['content'])} chars to {path} on the GPU box"


TOOLS = [remote_shell, remote_read, remote_write]
