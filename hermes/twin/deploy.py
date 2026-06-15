"""Run the twin on the box: spin the reconstructed server up from the blueprint.

`build serve` stands the twin up at http://127.0.0.1:<port> inside the sandbox so
the solution the agent writes — and its tests — can hit it like the real target,
while staying offline and safe.

The twin is the **real software**, and the blueprint that rebuilds it lives on the
phone: the project's `twin/recipe.jsonl` (the ordered, captured reconstruction
steps) plus the recon manifest. So when you change GPUs or reset the box, `build
serve` replays that blueprint onto the fresh box and the runtime Linux server
comes back up — no agent turns, no reinventing the wheel. The serving step binds
`$TWIN_PORT`, which we export to `port`.

When there is no recipe yet (an opaque/bespoke target captured only as recorded
responses), we fall back to the self-contained stdlib replay server
(`server.py`) as a reference responder for diffing — it is not the twin.
"""

from __future__ import annotations

from pathlib import Path

from hermes.ssh import anchored_path, shell_path
from hermes.twin.model import TwinModel

SERVER_SRC = Path(__file__).parent / "server.py"
REMOTE_SUBDIR = "twin-runtime"

# A network-free liveness probe: a TCP connect to the port, in stdlib python
# (always on the box — curl is bounced to the phone by the net policy). Exit 0
# means something is listening.
_HEALTHCHECK = (
    "python3 - <<'PY'\n"
    "import socket,sys\n"
    "s=socket.socket(); s.settimeout(2)\n"
    "sys.exit(0 if s.connect_ex(('127.0.0.1',{port}))==0 else 1)\n"
    "PY"
)


def _alive(ep, port: int) -> bool:
    rc, _, _ = ep.run(_HEALTHCHECK.format(port=int(port)))
    return rc == 0


def _stop_cmd(port: int) -> str:
    """pkill -f matches an unanchored regex against the whole command line, so a
    bare `server.py . {port}` for port 890 would also kill twins on 8900-8909
    (and the dots would match any char). Escape the dots and anchor the port at
    the end of the argv (the server launches as `python3 server.py . <port>`)."""
    return rf"pkill -f 'server\.py \. {int(port)}$' 2>/dev/null || true"


def deploy(ep, model: TwinModel, port: int, on_event=None,
           step_timeout: int = 1800) -> dict:
    """Bring the twin up on localhost:<port>. Replays the blueprint recipe to
    stand up the real reconstructed server; falls back to the stdlib replay
    responder when there's no recipe. Returns {"ok", "port", "remote_dir",
    "log", "source"} (or {"ok": False, "error", ...})."""
    def emit(text):
        if on_event:
            on_event(text)

    recipe = model.recipe()
    if recipe:
        return _serve_from_blueprint(ep, model, port, recipe, emit, step_timeout)
    emit("no recipe yet — serving the recorded-response reference responder")
    return _serve_replay(ep, model, port, emit)


def _serve_from_blueprint(ep, model, port, recipe, emit, step_timeout) -> dict:
    """Replay the captured reconstruction steps to stand the real stack up."""
    remote_dir = anchored_path(REMOTE_SUBDIR, ep.remote_workspace)
    rq = shell_path(remote_dir)
    ep.run(f"mkdir -p {rq}")
    ep.run("ip link set lo up 2>/dev/null || true")  # fresh net ns leaves lo down

    if _alive(ep, port):
        emit(f"already serving on :{port}")
        return {"ok": True, "port": port, "remote_dir": remote_dir,
                "log": "already up", "source": "blueprint"}

    emit(f"spinning up from blueprint — replaying {len(recipe)} recipe step(s)")
    for i, step in enumerate(recipe, 1):
        cmd = step.get("cmd", "")
        note = step.get("note") or cmd
        full = f"cd {rq} && export TWIN_PORT={int(port)} && {cmd}"
        rc, out, err = ep.run(full, timeout=step_timeout)
        if rc != 0:
            return {"ok": False, "remote_dir": remote_dir, "source": "blueprint",
                    "error": f"recipe step {i}/{len(recipe)} failed "
                             f"({note[:60]}): " + (err or out).strip()[-300:]}
        emit(f"step {i}/{len(recipe)} ok: {note[:60]}")

    ep.run("sleep 1")  # let a just-launched daemon bind the port
    if _alive(ep, port):
        emit(f"runtime server up on :{port}")
        return {"ok": True, "port": port, "remote_dir": remote_dir,
                "log": f"blueprint replayed, listening on :{port}",
                "source": "blueprint"}
    return {"ok": False, "remote_dir": remote_dir, "source": "blueprint",
            "error": f"recipe replayed but nothing is listening on :{port} — the "
                     "serving step must bind $TWIN_PORT and run in the background "
                     "(e.g. nohup ... &)"}


def _serve_replay(ep, model: TwinModel, port: int, emit) -> dict:
    """Fallback: push the stdlib replay server + recorded exchanges and launch it.
    This is the reference responder for opaque targets, not the reconstructed twin."""
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
                "remote_dir": remote_dir, "log": log, "source": "replay"}
    return {"ok": True, "port": port, "remote_dir": remote_dir, "log": log,
            "source": "replay"}


def stop(ep, port: int) -> None:
    ep.run(_stop_cmd(port))
