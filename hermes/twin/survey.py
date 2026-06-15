"""Webserver survey: the full topography of an authorized target.

`scan.py` answers "what host services run" (ports → service → version) and the
recon fingerprint answers "what web stack runs" (CMS/framework/server). This maps
the *shape* of the webserver itself: which directories and endpoints exist, and
which of them leak readable info — source, config, VCS metadata, backups. For a
legacy CMS sitting on a box that's the difference between "there's a webserver"
and "here's its layout and what's exposed."

It probes known paths read-only (GET) and records that a path EXISTS and its
status — not a mirror of page content. Like the rest of recon it runs on the
phone, and its network call (`fetch`) is injected so the logic is testable
without a network.

Use only against hosts you are authorized to test.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from hermes.twin import recon

# A fuller path wordlist than the agent tool's quick list — admin/auth, APIs,
# config/backups, infra and the legacy-CMS furniture worth mapping. Combined with
# recon.COMMON_PATHS and any paths the target's own robots.txt/sitemap.xml reveal.
EXTRA_PATHS = (
    # admin / auth surfaces
    "/admin.php", "/admin/login", "/admin/index.php", "/cms", "/manager",
    "/manager/html", "/panel", "/cpanel", "/webmail", "/phpmyadmin", "/pma",
    "/adminer.php", "/wp-admin/admin-ajax.php", "/xmlrpc.php",
    # api surfaces
    "/api/v3", "/api/docs", "/api-docs", "/graphiql", "/rest", "/soap",
    "/.well-known/openapi.json", "/v2/api-docs", "/actuator", "/actuator/health",
    "/actuator/env", "/jsonrpc", "/rpc",
    # source / vcs / config exposure (dirs; specific files live in SOURCE_FILE_PATHS)
    "/.git/", "/.svn/", "/.hg/", "/.bzr/", "/.well-known/",
    "/config.php", "/configuration.php", "/settings.php", "/wp-config.php",
    "/web.config", "/app.config", "/config.json", "/config.yml", "/config.yaml",
    "/.htaccess", "/.htpasswd", "/.dockerenv", "/.aws/credentials",
    # backups / dumps / archives
    "/backup.zip", "/backup.tar.gz", "/backup.sql", "/db.sql", "/dump.sql",
    "/database.sql", "/site.zip", "/www.zip", "/public_html.zip", "/.bak",
    "/index.php.bak", "/config.php.bak", "/.env.bak", "/.env.local",
    "/.env.production",
    # logs / data leaks
    "/logs", "/log", "/error.log", "/access.log", "/debug.log", "/storage/logs",
    "/error_log", "/install.log",
    # installers / setup left behind
    "/install", "/install.php", "/setup", "/setup.php", "/update.php",
    "/upgrade.php", "/wizard", "/web-console",
    # legacy CMS furniture
    "/wp-content/debug.log", "/wp-content/uploads/", "/sites/default/files/",
    "/typo3", "/typo3conf/", "/bitrix/", "/umbraco", "/ghost", "/magento_version",
    "/app/etc/local.xml", "/администратор",
    # infra / status
    "/server-info", "/nginx_status", "/status.php", "/.well-known/security.txt",
    "/elmah.axd", "/trace.axd", "/__debug__", "/_profiler",
)

DEFAULT_MAX_PATHS = 400


@dataclass
class SurveyResult:
    host: str
    dirs: list = field(default_factory=list)       # [{path, status}]
    exposed: list = field(default_factory=list)    # [{path, status, note, readable}]
    subdomains: list = field(default_factory=list)  # ["api.example.com", ...]
    checked: int = 0
    from_robots: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _base(url: str) -> str:
    s = urlsplit(url if url.startswith(("http://", "https://")) else "https://" + url)
    return f"{s.scheme}://{s.netloc}"


def survey(base_url: str, *, fetch, max_paths: int = DEFAULT_MAX_PATHS,
           workers: int = 40, include_subdomains: bool = True,
           on_event=None) -> SurveyResult:
    """Map the webserver at `base_url`. `fetch(method, url, headers)` returns
    (status, headers, text), matching clone._httpx_fetch."""
    def emit(text):
        if on_event:
            on_event(text)

    base = _base(base_url)
    result = SurveyResult(host=urlsplit(base).netloc)

    # The target's own robots.txt / sitemap.xml are a ready-made map of paths.
    paths = list(dict.fromkeys(recon.COMMON_PATHS + EXTRA_PATHS))
    try:
        rs, _, rtext = fetch("GET", base + "/robots.txt", None)
        if rs == 200:
            extra = recon.parse_robots_paths(rtext)
            result.from_robots = len(extra)
            for p in extra:
                if p.startswith("/") and p not in paths:
                    paths.append(p)
    except Exception:
        pass
    try:
        ss, _, stext = fetch("GET", base + "/sitemap.xml", None)
        if ss == 200:
            for u in recon.parse_sitemap_paths(stext):
                p = urlsplit(u).path
                if p.startswith("/") and p not in paths:
                    paths.append(p)
    except Exception:
        pass

    paths = paths[:max_paths]
    result.checked = len(paths)
    emit(f"mapping {len(paths)} path(s) on {base}"
         + (f" (+{result.from_robots} from robots)" if result.from_robots else ""))

    def probe(path):
        try:
            status, _, _ = fetch("GET", base + path, None)
        except Exception:
            return None
        if isinstance(status, int) and status not in (404, 410):
            return {"path": path, "status": status}
        return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for hit in ex.map(probe, paths):
            if hit:
                result.dirs.append(hit)
    result.dirs.sort(key=lambda d: d["path"])
    emit(f"{len(result.dirs)} reachable dir(s)/endpoint(s)")

    # Readable source/config/VCS/backups — the high-value leaks.
    def probe_source(path):
        try:
            status, _, _ = fetch("GET", base + path, None)
        except Exception:
            return None
        if not isinstance(status, int):
            return None
        note = recon.interpret_source_hit(path, status)
        if note:
            return {"path": path, "status": status, "note": note,
                    "readable": status == 200}
        return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for hit in ex.map(probe_source, recon.SOURCE_FILE_PATHS):
            if hit:
                result.exposed.append(hit)
    result.exposed.sort(key=lambda d: (not d["readable"], d["path"]))
    if result.exposed:
        readable = sum(1 for e in result.exposed if e["readable"])
        emit(f"{len(result.exposed)} exposed source/config file(s) "
             f"({readable} readable)")

    if include_subdomains:
        domain = urlsplit(base).hostname or ""
        # bare registrable-ish domain; crt.sh wildcard query handles the rest
        parts = domain.split(".")
        q = ".".join(parts[-2:]) if len(parts) >= 2 else domain
        try:
            st, _, text = fetch("GET", f"https://crt.sh/?q=%25.{q}&output=json", None)
            if st == 200:
                result.subdomains = recon.parse_crtsh(text)[:200]
                emit(f"{len(result.subdomains)} subdomain(s) in CT logs for {q}")
        except Exception:
            pass

    return result


def format_survey(result: SurveyResult) -> str:
    lines = [f"webserver survey of {result.host} ({result.checked} path(s) checked):"]
    if result.dirs:
        lines.append("  dirs/endpoints:")
        for d in result.dirs[:60]:
            lines.append(f"    {d['status']}  {d['path']}")
        if len(result.dirs) > 60:
            lines.append(f"    ... (+{len(result.dirs) - 60} more)")
    else:
        lines.append("  dirs/endpoints: (none of the probed paths were reachable)")
    if result.exposed:
        lines.append("  exposed source/config:")
        for e in result.exposed:
            lines.append(f"    {e['note']}")
    if result.subdomains:
        shown = ", ".join(result.subdomains[:12])
        more = f" (+{len(result.subdomains) - 12} more)" if len(result.subdomains) > 12 else ""
        lines.append(f"  subdomains: {shown}{more}")
    return "\n".join(lines)
