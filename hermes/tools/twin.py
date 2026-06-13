"""Twin tools: the agent's window onto the runtime twin of the target.

The twin is a faithful, SAFE, local copy of the target — not the live service. It
serves the target's real captured responses exactly, and declares a miss for
anything it hasn't seen rather than inventing an answer. When the agent needs a
case the twin lacks, `twin_expand` asks the (benign, read-only) clone layer to go
learn it and fold it in — the agent itself never touches the live target.

These tools only register when a sealed twin exists for the project.
"""

from __future__ import annotations

from hermes.tools.base import obj_schema, tool
from hermes.twin.model import TwinModel


def _twin(ctx) -> TwinModel:
    return TwinModel(ctx.project.twin_dir)


@tool(
    "twin_request",
    "Send a request to the runtime twin of the target — a faithful, SAFE local "
    "copy, not the live service. Returns the target's real captured response, or "
    "a MISS if the twin hasn't seen that request (it never fabricates one). The "
    "real response is ground truth: your reimplementation must match it.",
    obj_schema(
        {
            "path": {"type": "string", "description": "request path, e.g. /users/1"},
            "method": {"type": "string", "description": "GET (default), POST, ..."},
            "query": {"type": "string", "description": "query string (optional)"},
            "body": {"type": "string", "description": "request body (optional)"},
        },
        ["path"],
    ),
)
def twin_request(args, ctx):
    twin = _twin(ctx)
    if not twin.is_sealed():
        return "ERROR: no sealed twin for this project."
    method = (args.get("method") or "GET").upper()
    ex = twin.respond(method, args["path"], args.get("query", ""), args.get("body"))
    if ex is None:
        return (
            f"TWIN MISS for {method} {args['path']}. The twin has no real response "
            "for this request and does not invent one. If you need this case, call "
            "twin_expand to have the clone layer learn it from the target."
        )
    return (
        f"[twin: real captured response]\nHTTP {ex.status} {ex.content_type}\n"
        f"request: {ex.label()}\n\n{ex.response_body}"
    )


@tool(
    "twin_map",
    "Show the surface the twin covers: the route map (method + templated path + "
    "how many examples), whether an API spec was captured, plus the mission and "
    "winning condition for this build.",
    obj_schema({}, []),
)
def twin_map(args, ctx):
    twin = _twin(ctx)
    if not twin.is_sealed():
        return "ERROR: no sealed twin for this project."
    lines = [twin.summary(), "", "route map (method · template · examples):"]
    for method, template, n in twin.route_map():
        lines.append(f"  {method} {template}  ({n})")
    return "\n".join(lines)


@tool(
    "twin_stack",
    "Show what recon fingerprinted about the target's stack — server, runtime, "
    "framework/CMS and version, and whether it's a known open-source stack worth "
    "reconstructing as real software or an opaque service to mirror by behavior.",
    obj_schema({}, []),
)
def twin_stack(args, ctx):
    twin = _twin(ctx)
    if not twin.is_sealed():
        return "ERROR: no sealed twin for this project."
    stack = twin.stack
    if not stack:
        return "no stack fingerprint recorded."
    from hermes.twin.recon import StackReport

    report = StackReport(**stack)
    lines = [report.summary(), ""]
    if report.signals:
        lines.append("signals: " + ", ".join(report.signals))
    return "\n".join(lines)


@tool(
    "twin_expand",
    "Grow the twin to cover requests it's missing. Give the paths you need; the "
    "benign clone layer fetches them read-only from the target (on the phone, "
    "never from here) and folds them into the twin. Use this when twin_request "
    "returns a MISS for something you legitimately need.",
    obj_schema(
        {"paths": {"type": "array", "items": {"type": "string"},
                   "description": "paths to learn, e.g. ['/users/2', '/users/3']"}},
        ["paths"],
    ),
)
def twin_expand(args, ctx):
    from hermes.twin import clone as clone_mod

    twin = _twin(ctx)
    if not twin.exists():
        return "ERROR: no twin for this project."
    paths = [str(p) for p in (args.get("paths") or []) if str(p).strip()]
    if not paths:
        return "ERROR: give at least one path to learn."
    report = clone_mod.expand(twin, twin.source, paths)
    return (f"clone layer learned {report['added']} request(s) "
            f"({report['errors']} error(s)); twin now has {len(twin.exchanges())} "
            "exchange(s). Re-run twin_request for the paths you needed.")


TOOLS = [twin_request, twin_map, twin_stack, twin_expand]
