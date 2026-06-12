"""GPU box state: ~/.hermes/gpu.json plus helpers to rebuild an endpoint."""

from __future__ import annotations

import json
import os
import shlex

from hermes.config import hermes_home


def gpu_state_path():
    return hermes_home() / "gpu.json"


def load_gpu_state() -> dict:
    path = gpu_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_gpu_state(state: dict) -> None:
    hermes_home().mkdir(parents=True, exist_ok=True)
    gpu_state_path().write_text(json.dumps(state, indent=2) + "\n")
    os.chmod(gpu_state_path(), 0o600)


# Run inside the namespace: a real connection attempt to a public host that
# MUST fail. unshare merely executing proves nothing — this proves egress is
# actually gone. ENETUNREACH in an empty netns returns immediately, so the
# timeout only bites on a box that genuinely has (leaking) connectivity.
_NET_PROBE_SCRIPT = (
    "import socket\n"
    "try:\n"
    "    socket.setdefaulttimeout(4)\n"
    "    socket.create_connection(('1.1.1.1', 53)).close()\n"
    "    print('NETLEAK')\n"
    "except OSError:\n"
    "    print('NETBLOCKED')\n"
)


def probe_net_isolation(ep) -> bool:
    """Does the box really drop a command's network at the kernel level?

    Mirrors the exact wrap remote_shell uses (`unshare -n -- sh -c ...`) and
    then *proves* it: from inside the namespace it tries to reach a public
    host and demands the attempt fail. A box that can't isolate — `unshare`
    not permitted, no python3, or (the dangerous case) a namespace that still
    has a route out — flunks rather than getting the benefit of the doubt.
    """
    inner = "python3 -c " + shlex.quote(_NET_PROBE_SCRIPT)
    rc, out, _ = ep.run("unshare -n -- sh -c " + shlex.quote(inner), timeout=30)
    return rc == 0 and "NETBLOCKED" in out and "NETLEAK" not in out


def endpoint_from_state(state: dict):
    from hermes.ssh import SSHEndpoint

    if not state.get("host"):
        return None
    return SSHEndpoint(
        host=state["host"],
        port=int(state.get("port", 22)),
        user=state.get("user", "root"),
        remote_workspace=state.get("remote_workspace", "~/hermes-workspace"),
        net_isolation=bool(state.get("net_isolation", False)),
    )
