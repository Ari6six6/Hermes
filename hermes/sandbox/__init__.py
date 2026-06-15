"""The sandbox host: a persistent VPS where the runtime twin actually lives.

This is the box the vision always wanted — a real, always-on Linux machine,
separate from the (ephemeral, rented-on-demand) GPU box, where a *contained*
clone of the target service runs and stays alive between runs. The phone is the
operator; the GPU box is disposable compute; the sandbox host is the lab bench.

State lives in ~/.hermes/sandbox.json (0600), mirroring hermes.gpu. The endpoint
reuses hermes.ssh.SSHEndpoint verbatim — same run/tunnel/file plumbing as the GPU
box and managed hosts. Unlike a managed host (fail-closed, every write gated), the
sandbox runs *free*: it is a disposable workshop like the GPU box, so its polarity
is a deny-list speed bump, not a cage.
"""

from __future__ import annotations

import json
import os

from hermes.config import hermes_home

# Where the twin's reconstructed software is built and run on the VPS.
SANDBOX_WORKSPACE = "~/hermes-sandbox"


def sandbox_state_path():
    return hermes_home() / "sandbox.json"


def load_sandbox_state() -> dict:
    path = sandbox_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_sandbox_state(state: dict) -> None:
    hermes_home().mkdir(parents=True, exist_ok=True)
    sandbox_state_path().write_text(json.dumps(state, indent=2) + "\n")
    os.chmod(sandbox_state_path(), 0o600)


def endpoint_from_state(state: dict):
    from hermes.ssh import SSHEndpoint

    if not state.get("host"):
        return None
    return SSHEndpoint(
        host=state["host"],
        port=int(state.get("port", 22)),
        user=state.get("user", "root"),
        remote_workspace=state.get("remote_workspace", SANDBOX_WORKSPACE),
    )


def probe_container_runtime(ep) -> str:
    """Which container runtime the VPS has, '' if none. Docker preferred (the
    common case on a plain Ubuntu box); podman accepted where it's the default."""
    rc, out, _ = ep.run(
        "command -v docker >/dev/null 2>&1 && echo docker || "
        "{ command -v podman >/dev/null 2>&1 && echo podman || echo none; }",
        timeout=30,
    )
    found = (out or "").strip().splitlines()[-1].strip() if rc == 0 and out.strip() else "none"
    return found if found in ("docker", "podman") else ""


def probe_kvm(ep) -> bool:
    """Does the VPS expose /dev/kvm? Gate for the future Firecracker microVM path
    — most cheap VPSes are themselves KVM guests with nested virt OFF, so this is
    usually False and we run plain containers instead."""
    rc, out, _ = ep.run("test -e /dev/kvm && echo KVM || echo NOKVM", timeout=20)
    return rc == 0 and "KVM" in out and "NOKVM" not in out


def capabilities(ep) -> dict:
    """Probe what isolation the VPS can offer, for sandbox.json + status."""
    return {
        "runtime": probe_container_runtime(ep),
        "kvm": probe_kvm(ep),
    }
