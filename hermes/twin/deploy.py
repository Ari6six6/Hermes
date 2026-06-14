"""Run the twin on the box: deploy the standalone server + model and launch it.

`build serve` puts a live twin at http://127.0.0.1:<port> inside the sandbox, so
the solution the agent writes — and its tests — can hit it exactly like the real
API, while staying offline and safe. The server is pure stdlib, so deploying is
just: push two files, bring loopback up, launch with nohup, confirm it answered.
"""

from __future__ import annotations

from pathlib import Path

from hermes.ssh import anchored_path, shell_path
from hermes.twin.model import TwinModel

SERVER_SRC = Path(__file__).parent / "server.py"
REMOTE_SUBDIR = "twin-runtime"


def _stop_cmd(port: int) -> str:
    """pkill -f matches an unanchored regex against the whole command line, so a
    bare `server.py . {port}` for port 890 would also kill twins on 8900-8909
    (and the dots would match any char). Escape the dots and anchor the port at
    the end of the argv (the server launches as `python3 server.py . <port>`)."""
    return rf"pkill -f 'server\.py \. {int(port)}$' 2>/dev/null || true"


def deploy(ep, model: TwinModel, port: int, on_event=None) -> dict:
    """Push the twin to the box and start it on localhost:<port>. Returns
    {"ok", "port", "remote_dir", "log"} (or {"ok": False, "error"})."""
    def emit(text):
        if on_event:
            on_event(text)

    remote_dir = anchored_path(REMOTE_SUBDIR, ep.remote_workspace)
    rq = shell_path(remote_dir)
    ep.run(f"mkdir -p {rq}")

    for name, path in (("server.py", SERVER_SRC),
                       ("exchanges.jsonl", model.exchanges_path)):
        if not Path(path).exists():
            return {"ok": False, "error": f"missing local file: {name}"}
        rc, err = ep.run_in_from_file(f"cat > {rq}/{name}", Path(path))
        if rc != 0:
            return {"ok": False, "error": f"failed to push {name}: {err.strip()[-300:]}"}
        emit(f"pushed {name}")

    # loopback up (a fresh net namespace leaves lo down), restart any old twin.
    ep.run("ip link set lo up 2>/dev/null || true")
    ep.run(_stop_cmd(port))
    ep.run(f"cd {rq} && nohup python3 server.py . {port} > twin.log 2>&1 < /dev/null & echo $!")
    emit(f"launched on :{port}")

    rc, out, _ = ep.run(f"sleep 1; cat {rq}/twin.log 2>/dev/null")
    log = (out or "").strip()
    if "twin up" not in log:
        return {"ok": False, "error": log or "twin did not report startup",
                "remote_dir": remote_dir, "log": log}
    return {"ok": True, "port": port, "remote_dir": remote_dir, "log": log}


def stop(ep, port: int) -> None:
    ep.run(_stop_cmd(port))
