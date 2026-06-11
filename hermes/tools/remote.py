"""Tools that execute on the rented GPU box over SSH.

The GPU box is the agent's compute sandbox — shell and file ops there run
without confirmation (commands are echoed to the screen). Hard rule: NO
internet from the GPU box; obvious network commands are blocked and the
agent is redirected to the phone-side web tools.
"""

from __future__ import annotations

import re
import shlex

from hermes.ssh import shell_path
from hermes.tools.base import obj_schema, tool

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
    "DENIED: internet access from the GPU box is not allowed. All network "
    "operations must go through the phone — use http_request / web_search, or "
    "download on the phone and push the file with remote_write."
)


def _need_gpu(ctx):
    if ctx.gpu is None:
        return "ERROR: no GPU box attached. Tell the operator to run `gpu attach`."
    return None


@tool(
    "remote_shell",
    "Run a shell command on the GPU box (Linux, root) — your compute sandbox "
    "for running code, builds, and heavy work. Default cwd is the remote "
    "workspace. NO internet from there: network commands are blocked.",
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
    cwd = shell_path(args.get("cwd") or ctx.gpu.remote_workspace)
    print(f"  [gpu] $ {command}")
    rc, out, errout = ctx.gpu.run(f"cd {cwd} && {inner}", timeout=timeout)
    body = (out or "") + (("\n[stderr]\n" + errout) if errout else "")
    return f"exit code {rc}\n{body.strip() or '(no output)'}"


@tool(
    "remote_read",
    "Read a text file from the GPU box.",
    obj_schema({"path": {"type": "string"}}, ["path"]),
)
def remote_read(args, ctx):
    err = _need_gpu(ctx)
    if err:
        return err
    rc, out, errout = ctx.gpu.run(f"cat {shell_path(args['path'])}", timeout=60)
    if rc != 0:
        return f"ERROR: {errout.strip() or 'read failed'}"
    return out


@tool(
    "remote_write",
    "Write a text file on the GPU box.",
    obj_schema(
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
)
def remote_write(args, ctx):
    err = _need_gpu(ctx)
    if err:
        return err
    rc, _, errout = ctx.gpu.write_file(args["path"], args["content"])
    if rc != 0:
        return f"ERROR: {errout.strip() or 'write failed'}"
    return f"wrote {len(args['content'])} chars to {args['path']} on the GPU box"


TOOLS = [remote_shell, remote_read, remote_write]
