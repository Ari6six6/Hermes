"""Recon tools: the recon agent's eyes on a target, before any twin is sealed.

These run while the project's twin is still being built. They let the agent get to
know the target thoroughly — map its directories and endpoints, find its own
source and dependency files, see its full subdomain footprint — so the builder can
stand up an accurate copy. They make plain GET requests on the phone (where the
net lives) and only register while the twin is OPEN; once it's sealed they're gone.

Use them freely and to their full extent — the more you understand the target, the
better the rebuild.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from hermes.tools.base import obj_schema, tool
from hermes.twin import recon

UA = "Mozilla/5.0 (Linux; Android) HermesAgent/0.1"
MAX_DIRSCAN = 80


def _get(url, timeout=20):
    """One GET, on the phone. Returns (status, text). Stdlib-light."""
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
    "Map a domain's subdomains from public certificate-transparency logs (crt.sh) "
    "to see the target's full footprint before you reconstruct it.",
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
    "Look for the target's own source and dependency files (.git/config, .env, "
    "package.json, composer.json, ...). A hit is a shortcut straight to the real "
    "stack and code you'll rebuild from.",
    obj_schema({"url": {"type": "string"}}, ["url"]),
)
def recon_sources(args, ctx):
    base = _base(args["url"])
    findings = []
    for path in recon.SOURCE_FILE_PATHS:
        status, _ = _get(base + path)
        note = recon.interpret_source_hit(path, status) if isinstance(status, int) else None
        if note:
            findings.append("  " + note)
    if not findings:
        return f"no source/dependency files found on {base}"
    return f"source-file scan of {base}:\n" + "\n".join(findings)


@tool(
    "recon_dirscan",
    "Explore the target's structure: check a list of common paths and read its "
    "robots.txt and sitemap.xml to discover which directories and endpoints exist. "
    "Reports what's reachable, by status. Use it to map the surface you need to "
    "reproduce.",
    obj_schema({"url": {"type": "string"}}, ["url"]),
)
def recon_dirscan(args, ctx):
    base = _base(args["url"])
    paths = list(recon.COMMON_PATHS)

    # The site's own robots.txt / sitemap.xml are a ready-made map of paths.
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
