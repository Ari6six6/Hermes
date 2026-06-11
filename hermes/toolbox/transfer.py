"""Toolbox: move files between the phone project and the GPU box (binary-safe).

This is THE bridge for the no-internet-on-GPU rule: download on the phone,
push to the box; compute on the box, pull results back.
"""

import base64

TOOL = {
    "name": "transfer",
    "description": (
        "Copy a file between the phone project and the GPU box. "
        "direction 'push' = phone->box, 'pull' = box->phone. Binary-safe."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["push", "pull"]},
            "local_path": {"type": "string", "description": "path inside the project"},
            "remote_path": {"type": "string", "description": "path on the GPU box"},
        },
        "required": ["direction", "local_path", "remote_path"],
    },
}


def run(args, ctx):
    from hermes.paths import PathDenied, resolve_in

    if ctx.gpu is None:
        return "ERROR: no GPU box attached."
    try:
        local = resolve_in(ctx.project.root, args["local_path"])
    except PathDenied:
        return "DENIED: local_path must stay inside the project."
    remote = args["remote_path"]

    if args["direction"] == "push":
        if not local.is_file():
            return f"ERROR: no such local file: {args['local_path']}"
        data = base64.b64encode(local.read_bytes()).decode()
        rc, _, err = ctx.gpu.run(
            f"mkdir -p $(dirname {remote}) && base64 -d > {remote}",
            stdin=data,
            timeout=600,
        )
        if rc != 0:
            return f"ERROR: push failed: {err.strip()[-400:]}"
        return f"pushed {local.stat().st_size} bytes to {remote}"

    rc, out, err = ctx.gpu.run(f"base64 {remote}", timeout=600)
    if rc != 0:
        return f"ERROR: pull failed: {err.strip()[-400:]}"
    local.parent.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(out.encode())
    local.write_bytes(raw)
    return f"pulled {len(raw)} bytes to {args['local_path']}"
