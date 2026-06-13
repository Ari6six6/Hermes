"""Recon tools: the recon agent's eyes on a target, before any twin is sealed.

This is the recon/builder phase — the ONLY phase allowed to touch the live
target, and only ever READ-ONLY. The aim is to inspect the site as intimately as
is *legal*: enumerate reachable directories and endpoints, find exposed source and
dependency manifests, map subdomains. No fuzzing of inputs, no auth bypass, no
mutation — just visibility of what the target already exposes to the public, so
the builder can reconstruct the real stack faithfully.

These tools run ON THE PHONE (where the net lives) and only register while the
project's twin is still OPEN. Once the twin is sealed, the build phase begins and
live access is gone.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from hermes.tools.base import obj_schema, tool
from hermes.twin import recon

UA = "Mozilla/5.0 (Linux; Android) HermesAgent/0.1 (read-only recon)"
MAX_DIRSCAN = 80


def _get(url, timeout=20):
    """One read-only GET, on the phone. Returns (status, text). Stdlib-light."""
    import httpx

    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=timeout,
                      follow_redirects=False)
        return r.status_code, r.text
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"


def _base(url: str) -> str:
    s = urlsplit(url if url.startswith(("http://", "https://")) else "https://" + url)
    return f"{s.scheme}://{s.netloc}"


@tool(
    "recon_subdomains",
    "Recon (read-only): enumerate a domain's subdomains from public certificate "
    "transparency logs (crt.sh). Widens the attack surface you can legally map "
    "before building the twin.",
    obj_schema({"domain": {"type": "string", "description": "e.g. example.com"}},
               ["domain"]),
)
def recon_subdomains(args, ctx):
    domain = str(args["domain"]).strip().lstrip("*.").lower()
    if "." not in domain or "/" in domain:
        return "ERROR: give a bare domain like example.com"
    status, text = _get(f"https://crt.sh/?q=%25.{domain}&output=json", timeout=30)
    if status != 200:
        return f"ERROR: crt.sh returned {status}: {str(text)[:200]}"
    subs = recon.parse_crtsh(text)
    if not subs:
        return f"no subdomains found for {domain}"
    return f"{len(subs)} subdomain(s) for {domain}:\n" + "\n".join("  " + s for s in subs[:200])


@tool(
    "recon_sources",
    "Recon (read-only): probe the target for exposed source and dependency "
    "manifests (.git/config, .env, package.json, composer.json, ...). A hit is "
    "gold — it reveals the real stack, dependencies, sometimes the whole source "
    "to reconstruct from.",
    obj_schema({"url": {"type": "string"}}, ["url"]),
)
def recon_sources(args, ctx):
    base = _base(args["url"])
    findings = []
    for path in recon.EXPOSED_SOURCE_PATHS:
        status, _ = _get(base + path)
        note = recon.interpret_exposure(path, status) if isinstance(status, int) else None
        if note:
            findings.append("  " + note)
    if not findings:
        return f"no exposed source/manifests found on {base}"
    return f"exposed-source scan of {base}:\n" + "\n".join(findings)


@tool(
    "recon_dirscan",
    "Recon (read-only): inspect the site as intimately as is legal — enumerate "
    "reachable directories and endpoints from a common-paths list, and mine "
    "robots.txt and sitemap.xml for paths the owner publishes. Reports what "
    "exists (by status). No fuzzing or bypass — visibility only.",
    obj_schema({"url": {"type": "string"}}, ["url"]),
)
def recon_dirscan(args, ctx):
    base = _base(args["url"])
    paths = list(recon.COMMON_PATHS)

    # Mine robots/sitemap first — the owner's own map of interesting paths.
    rs, rtext = _get(base + "/robots.txt")
    extra = recon.parse_robots_paths(rtext) if rs == 200 else []
    ss, stext = _get(base + "/sitemap.xml")
    if ss == 200:
        extra += [urlsplit(u).path for u in recon.parse_sitemap_paths(stext)]
    for p in extra:
        if p and p not in paths:
            paths.append(p)

    hits = []
    for path in paths[:MAX_DIRSCAN]:
        if not path.startswith("/"):
            continue
        status, _ = _get(base + path)
        if isinstance(status, int) and status not in (404, 410):
            hits.append(f"  {status}  {path}")
    head = f"dirscan of {base} ({len(paths[:MAX_DIRSCAN])} paths checked):\n"
    if extra:
        head = f"dirscan of {base} (+{len(extra)} path(s) from robots/sitemap):\n"
    return head + ("\n".join(hits) if hits else "  (nothing reachable found)")


TOOLS = [recon_subdomains, recon_sources, recon_dirscan]
