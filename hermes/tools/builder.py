"""Builder tools: stand up the twin during the recon/build phase.

These are the recon/builder agent's hands for assembling the twin — record
ground-truth samples of how the target responds, pull a batch at once, and seal
the twin when it's proven accurate. They register only while the twin is still
OPEN; sealing ends the phase and hands off to the build agents.
"""

from __future__ import annotations

from hermes.tools.base import obj_schema, tool
from hermes.twin.model import Exchange, TwinModel


def _twin(ctx) -> TwinModel:
    return TwinModel(ctx.project.twin_dir)


@tool(
    "twin_record",
    "Record one ground-truth sample into the twin: a real request and the real "
    "response the target gave for it (fetch it with http_request first). These "
    "samples are what later runs prove their solution against.",
    obj_schema(
        {
            "path": {"type": "string", "description": "request path, e.g. /users/1"},
            "status": {"type": "integer", "description": "real HTTP status, e.g. 200"},
            "response_body": {"type": "string", "description": "the real response body"},
            "method": {"type": "string", "description": "GET (default), POST, ..."},
            "query": {"type": "string", "description": "query string (optional)"},
            "request_body": {"type": "string", "description": "request body (optional)"},
            "content_type": {"type": "string", "description": "e.g. application/json"},
        },
        ["path", "status", "response_body"],
    ),
)
def twin_record(args, ctx):
    twin = _twin(ctx)
    if not twin.exists():
        return "ERROR: no twin for this project."
    if twin.is_sealed():
        return "ERROR: the twin is sealed. Recon/build is over for this twin."
    twin.add_exchange(Exchange(
        method=(args.get("method") or "GET").upper(),
        path=args["path"], query=args.get("query", ""),
        status=int(args["status"]), response_body=str(args["response_body"]),
        request_body=args.get("request_body"),
        content_type=str(args.get("content_type", "")), source="builder",
    ))
    return f"recorded {(args.get('method') or 'GET').upper()} {args['path']} -> {args['status']}. " \
           f"twin now has {len(twin.exchanges())} sample(s)."


@tool(
    "twin_clone",
    "Pull a batch of ground-truth samples from the target at once: crawl it from "
    "the source URL (and any seed paths you give) and record what it returns. Read "
    "requests only. Use it to bootstrap or widen the twin quickly; refine with "
    "twin_record / http_request.",
    obj_schema(
        {"seeds": {"type": "array", "items": {"type": "string"},
                   "description": "extra paths to start from, e.g. ['/api', '/users']"}},
        [],
    ),
)
def twin_clone(args, ctx):
    from hermes.twin import clone as clone_mod

    twin = _twin(ctx)
    if not twin.exists():
        return "ERROR: no twin for this project."
    if twin.is_sealed():
        return "ERROR: the twin is sealed."
    seeds = [str(s) for s in (args.get("seeds") or []) if str(s).strip()]
    report = clone_mod.clone(
        twin, twin.source, seeds=seeds or None, seal=False,
        max_exchanges=ctx.cfg.get("twin_clone_max", 200),
        delay=ctx.cfg.get("twin_clone_delay", 0.5),
        max_depth=ctx.cfg.get("twin_clone_depth", 2),
    )
    return (f"cloned {report['recorded']} request(s) ({report['errors']} error(s)); "
            f"twin now has {report['exchanges']} sample(s). Stack: "
            f"{report['stack'].get('product') or report['stack'].get('kind', 'unknown')}.")


@tool(
    "twin_seal",
    "Seal the twin: freeze it and open the build phase. Do this ONLY once you've "
    "verified the twin behaves like the target — an inaccurate twin poisons "
    "everything built on it. Needs at least one recorded sample.",
    obj_schema({}, []),
)
def twin_seal(args, ctx):
    twin = _twin(ctx)
    if not twin.exists():
        return "ERROR: no twin for this project."
    if twin.is_sealed():
        return "the twin is already sealed."
    if not twin.exchanges():
        return ("ERROR: the twin has no samples yet — record real responses with "
                "twin_record or twin_clone before sealing.")
    twin.seal()
    return (f"twin sealed with {len(twin.exchanges())} sample(s). The build phase "
            "is open — the next run builds the solution against this frozen twin.")


TOOLS = [twin_record, twin_clone, twin_seal]
