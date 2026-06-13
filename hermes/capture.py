"""The clone engine: build a sealed replica of a target service, benignly.

This is the ONE component that touches the live target, and it is deliberately
operator-driven (a CLI action on the phone), not an agent tool — the agent never
decides to go poke a live service. Capture is intentionally gentle: read-only
methods only (GET/HEAD), a hard cap on how many requests it makes, and a polite
delay between them. It records what the service openly returns, then seals the
bundle. From that point on the agent only ever sees the frozen recording.

`fetch` is injected so the recording logic is testable without a network.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

from hermes.oracle import OracleBundle, Probe

# Endpoints worth a benign read on most services — they describe the surface
# without poking at behavior. All plain GETs.
DISCOVERY_PATHS = (
    "/openapi.json", "/swagger.json", "/.well-known/openapi.json",
    "/robots.txt", "/health", "/healthz", "/version",
)

SAFE_METHODS = ("GET", "HEAD")
UA = "Mozilla/5.0 (Linux; Android) HermesAgent/0.1 (benign capture)"


def _httpx_fetch(method: str, url: str, headers: dict | None = None,
                 body: str | None = None, timeout: int = 45):
    """Real network read, on the phone. Returns (status, headers, text)."""
    import httpx

    hdrs = {"User-Agent": UA}
    if isinstance(headers, dict):
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    resp = httpx.request(method, url, headers=hdrs, content=body,
                         timeout=timeout, follow_redirects=True)
    return resp.status_code, dict(resp.headers), resp.text


def _normalize_specs(base: str, specs) -> list[dict]:
    """Turn loose request specs (strings or dicts) into a uniform list."""
    out: list[dict] = []
    for spec in specs:
        if isinstance(spec, str):
            out.append({"method": "GET", "path": spec})
        elif isinstance(spec, dict):
            out.append({
                "method": (spec.get("method") or "GET"),
                "path": spec.get("path", "/"),
                "query": spec.get("query", ""),
                "headers": spec.get("headers") or {},
            })
    return out


def capture(bundle: OracleBundle, base_url: str, specs=None, *,
            include_discovery: bool = True, max_probes: int = 200,
            delay: float = 0.5, fetch=_httpx_fetch, on_event=None) -> dict:
    """Record the target's observable behavior into `bundle`, then seal it.

    `specs` are request specs (a path string, or a dict with method/path/query).
    Non-read methods are skipped — capture stays benign. Returns a small report
    dict. `on_event(kind, text)` is an optional progress hook for the CLI.
    """
    def emit(kind: str, text: str):
        if on_event:
            on_event(kind, text)

    bundle.init(source=base_url, mode="url", win_condition=bundle.win_condition)

    queue = _normalize_specs(base_url, specs or ["/"])
    if include_discovery:
        seen = {(s["method"].upper(), s["path"]) for s in queue}
        for p in DISCOVERY_PATHS:
            if ("GET", p) not in seen:
                queue.append({"method": "GET", "path": p})

    recorded = 0
    skipped = 0
    errors = 0
    for i, spec in enumerate(queue):
        if recorded >= max_probes:
            emit("info", f"reached cap of {max_probes} probes — stopping")
            break
        method = spec["method"].upper()
        if method not in SAFE_METHODS:
            skipped += 1
            emit("skip", f"{method} {spec['path']} (non-read method — capture stays benign)")
            continue
        url = urljoin(base_url if base_url.endswith("/") else base_url + "/",
                      spec["path"].lstrip("/"))
        query = spec.get("query", "")
        if query:
            url = f"{url}?{query}"
        if i > 0 and delay:
            time.sleep(delay)
        try:
            status, resp_headers, text = fetch(method, url, spec.get("headers"))
        except Exception as e:  # network is best-effort; one bad path isn't fatal
            errors += 1
            emit("error", f"{method} {spec['path']}: {type(e).__name__}: {e}")
            continue
        probe = Probe(
            method=method,
            path=spec["path"] if spec["path"].startswith("/") else "/" + spec["path"],
            query=query,
            status=status,
            content_type=str(resp_headers.get("content-type", "")),
            response_headers={k: v for k, v in resp_headers.items()
                              if k.lower() in ("content-type", "content-length", "etag")},
            response_body=text,
        )
        bundle.add_probe(probe)
        recorded += 1
        emit("probe", f"{method} {spec['path']} -> {status} ({len(text)}B)")

    bundle.seal()
    report = {"recorded": recorded, "skipped": skipped, "errors": errors}
    emit("done", f"sealed: {recorded} probe(s) recorded, {skipped} skipped, {errors} error(s)")
    return report
