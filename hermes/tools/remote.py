"""Tools that execute on the rented GPU box over SSH.

The GPU box is the agent's compute sandbox — shell and file ops there run
without confirmation (commands are echoed to the screen). Hard rule: NO
internet from the GPU box.

That rule is enforced by the kernel, not by a word-list. When the box can
drop a command's network at the kernel level (`unshare -n`, verified at
attach time) every command runs inside that empty namespace and physically
has no route out. When it can't, remote_shell fails closed: the NETWORK_RE
deny-list below only recognizes the *names* of networking programs, so a
one-line python/node/perl script — or bash's `/dev/tcp` — opens a socket
without ever tripping it. We refuse rather than run arbitrary code under a
guarantee we can't keep. The operator can still opt the whole box onto the
network explicitly with `config set allow_gpu_network true`.
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
    "DENIED: internet access from the GPU box is not allowed. All network "
    "operations must go through the phone — use http_request / web_search. To "
    "get a file onto the box: download it on the phone (download_file "
    "toolbox), then push it with the `transfer` toolbox tool (equip both via "
    "equip_tool). remote_write works for small text files only."
)

NET_UNISOLATED_DENIED = (
    "DENIED: this GPU box cannot isolate a command's network at the kernel "
    "level (`unshare -n` is unavailable in its container), so remote_shell is "
    "disabled here — the no-internet guarantee can't be enforced and the "
    "deny-list alone is trivially bypassable from any interpreter. Options for "
    "the operator: attach a box that supports network namespaces, or, to "
    "deliberately put this box online, `config set allow_gpu_network true`. "
    "Until then, run code on the phone or move files there with the `transfer` "
    "toolbox tool."
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
        # Fast, helpful redirect for the obvious networking programs.
        if NETWORK_RE.search(command):
            return NETWORK_DENIED
        if ctx.gpu.net_isolation:
            # Kernel-level isolation: the command runs in an empty network
            # namespace and physically has no route out, whatever it tries.
            inner = f"unshare -n -- sh -c {shlex.quote(command)}"
        else:
            # No kernel isolation: the deny-list above is the only thing left,
            # and it can't see a socket opened from python/node/perl/`/dev/tcp`.
            # Fail closed rather than honor the guarantee in name only.
            return NET_UNISOLATED_DENIED
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
