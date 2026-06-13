"""Oracle tools: query the sealed recording of the target — the parity oracle.

These are the agent's window onto the target, and they are READ-ONLY REPLAY of a
frozen recording. There is no live service behind them: the operator captured the
target once, on the phone, and sealed it. Querying the oracle never reaches the
network. Its recorded responses are the ground truth a reimplementation must
match — and the antithesis uses them to break work that doesn't.

These tools only register when a sealed bundle exists for the project.
"""

from __future__ import annotations

from hermes.oracle import OracleBundle
from hermes.tools.base import obj_schema, tool

_PREFACE = (
    "RECORDED REPLICA (not live). This response was captured from the target "
    "and frozen — you are reading a fixture, not hitting a server.\n"
)


def _bundle(ctx) -> OracleBundle:
    return OracleBundle(ctx.project.oracle_dir)


@tool(
    "oracle_query",
    "Query the parity oracle: the sealed RECORDING of the target service. This "
    "is a frozen replica, NOT the live service — it never touches the network. "
    "Give a request (method/path/query/body) and get back the target's recorded "
    "response. That recorded response is ground truth: your reimplementation must "
    "produce the same output for the same input.",
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
def oracle_query(args, ctx):
    bundle = _bundle(ctx)
    if not bundle.is_sealed():
        return "ERROR: no sealed oracle for this project."
    method = (args.get("method") or "GET").upper()
    probe = bundle.replay(method, args["path"], args.get("query", ""), args.get("body"))
    if probe is None:
        avail = ", ".join(sorted({p.label() for p in bundle.probes()})[:20]) or "(none)"
        return (
            "NO MATCH in the recorded replica for "
            f"{method} {args['path']}. The replica is a fixed recording — it has "
            "no data for this input, and there is no live service to fall back to. "
            "Stay within what was recorded.\nRecorded requests include: " + avail
        )
    return (
        f"{_PREFACE}HTTP {probe.status} {probe.content_type}\n"
        f"request: {probe.label()}\n\n{probe.response_body}"
    )


@tool(
    "oracle_list",
    "List what the parity oracle recorded: every captured request and its status, "
    "plus the operator's plain-English winning condition for this build. Use it to "
    "see the surface you must reproduce.",
    obj_schema({}, []),
)
def oracle_list(args, ctx):
    bundle = _bundle(ctx)
    if not bundle.is_sealed():
        return "ERROR: no sealed oracle for this project."
    lines = [bundle.summary(), "", "recorded requests:"]
    for probe in bundle.probes():
        lines.append(f"  {probe.label()} -> {probe.status} ({len(probe.response_body)}B)")
    return "\n".join(lines)


TOOLS = [oracle_query, oracle_list]
