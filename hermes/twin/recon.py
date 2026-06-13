"""Recon: fingerprint a target's stack so the builder knows what to reconstruct.

Two kinds of target, two kinds of twin:

  - **opaque / bespoke** — hidden logic we can only mirror by its observable
    behavior (the behavioral twin: recorded responses).
  - **known open-source stack** (WordPress, Drupal, Django, Rails, ...) — we can
    identify it from the outside and reconstruct the REAL software in the sandbox,
    because that software is public and downloadable. Far higher fidelity than a
    mock: the twin literally *is* the software.

This module is deterministic black-box detection over responses the (read-only,
benign) recon pass already fetched — server, runtime, framework/CMS and versions,
read from headers, generator tags, cookies, and well-known paths. It decides which
kind of twin the builder should stand up.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

# Read-only probes a capable recon agent runs to find the real source — exposed
# VCS metadata, dependency manifests, build files. A 200 here is gold: it can
# reveal the exact stack, dependencies, even the whole source history.
EXPOSED_SOURCE_PATHS = (
    "/.git/config", "/.git/HEAD", "/.svn/entries", "/.hg/requires",
    "/.env", "/package.json", "/composer.json", "/composer.lock",
    "/requirements.txt", "/Gemfile", "/go.mod", "/Dockerfile",
    "/docker-compose.yml", "/.gitlab-ci.yml", "/wp-config.php.bak",
)


# Content-discovery wordlist: common dirs/paths a site exposes. Read-only GETs —
# this is visibility of what's publicly reachable, not fuzzing or bypass.
COMMON_PATHS = (
    "/admin", "/login", "/logout", "/dashboard", "/account", "/user/login",
    "/api", "/api/v1", "/api/v2", "/graphql", "/swagger", "/swagger-ui",
    "/openapi.json", "/docs", "/redoc",
    "/wp-admin", "/wp-login.php", "/wp-json", "/administrator", "/user",
    "/config", "/settings", "/backup", "/backups", "/uploads", "/files",
    "/static", "/assets", "/images", "/js", "/css", "/media",
    "/robots.txt", "/sitemap.xml", "/.well-known/security.txt",
    "/health", "/healthz", "/status", "/metrics", "/server-status",
    "/phpinfo.php", "/info.php", "/test", "/dev", "/staging", "/old",
    "/readme.html", "/README.md", "/CHANGELOG.md", "/license.txt",
)


def parse_robots_paths(text: str) -> list[str]:
    """Paths the owner names in robots.txt (Disallow/Allow/Sitemap) — often the
    most interesting dirs, since they're the ones they'd rather hide."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"(?:dis)?allow:\s*(\S+)", line, re.I)
        if m and m.group(1) not in ("/", "*"):
            out.append(m.group(1))
        m = re.match(r"sitemap:\s*(\S+)", line, re.I)
        if m:
            out.append(m.group(1))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def parse_sitemap_paths(text: str) -> list[str]:
    """URLs/paths listed in a sitemap.xml."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text or "", re.I)


def parse_crtsh(text: str) -> list[str]:
    """Subdomains from a crt.sh JSON response (public certificate transparency)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    names: set[str] = set()
    for row in data if isinstance(data, list) else []:
        for n in str(row.get("name_value", "")).splitlines():
            n = n.strip().lstrip("*.").lower()
            if n and " " not in n:
                names.add(n)
    return sorted(names)


def interpret_exposure(path: str, status: int) -> str | None:
    """A human-readable finding for an exposed-source probe, or None if nothing."""
    if status == 200:
        return f"EXPOSED ({status}) {path} — pull it; it likely reveals the real stack/source"
    if status in (401, 403):
        return f"present but protected ({status}) {path}"
    return None

# (label, header-name, regex over the value, optional version group)
_HEADER_RULES = [
    ("server", "server", re.compile(r"^([A-Za-z\-]+)(?:/([\d.]+))?"), True),
    ("runtime", "x-powered-by", re.compile(r"(PHP|ASP\.NET|Express|Servlet)[/ ]?([\d.]*)", re.I), True),
    ("runtime", "x-aspnet-version", re.compile(r"([\d.]+)"), False),
]

# Recognizable applications/frameworks we could actually stand up. Each: markers
# in headers (cookie/header substrings) and body, plus a version extractor.
@dataclass
class _Product:
    name: str
    cookie_markers: tuple = ()
    body_markers: tuple = ()
    path_markers: tuple = ()
    version_re: "re.Pattern | None" = None


_PRODUCTS = [
    _Product("WordPress",
             cookie_markers=("wordpress_", "wp-settings"),
             body_markers=("/wp-content/", "/wp-includes/", "wp-json"),
             path_markers=("/wp-login.php", "/wp-json", "/wp-admin"),
             version_re=re.compile(r'name="generator" content="WordPress ([\d.]+)"', re.I)),
    _Product("Drupal",
             cookie_markers=("SESS",),
             body_markers=("Drupal.settings", "/sites/default/", "drupal.js"),
             path_markers=("/user/login", "/core/misc/drupal.js"),
             version_re=re.compile(r'name="generator" content="Drupal ([\d.]+)', re.I)),
    _Product("Joomla", body_markers=("/media/jui/", "com_content"),
             path_markers=("/administrator",),
             version_re=re.compile(r'name="generator" content="Joomla! ([\d.]+)', re.I)),
    _Product("Django", cookie_markers=("csrftoken", "django"),
             body_markers=("csrfmiddlewaretoken",), path_markers=("/admin/login/",)),
    _Product("Ruby on Rails", cookie_markers=("_rails", "_session_id"),
             body_markers=('name="csrf-param"',)),
    _Product("Laravel", cookie_markers=("laravel_session", "XSRF-TOKEN")),
    _Product("Express", cookie_markers=("connect.sid",)),
    _Product("Next.js", body_markers=("__NEXT_DATA__", "/_next/static/")),
]


@dataclass
class StackReport:
    kind: str = "opaque"           # "known_stack" | "opaque"
    product: str = ""              # e.g. "WordPress"
    product_version: str = ""
    server: str = ""               # e.g. "Apache/2.4.52"
    runtime: str = ""              # e.g. "PHP/8.1"
    confidence: str = "low"        # low | medium | high
    signals: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def reconstructable(self) -> bool:
        return self.kind == "known_stack"

    def summary(self) -> str:
        if self.kind == "known_stack":
            ver = f" {self.product_version}" if self.product_version else ""
            stack = ", ".join(x for x in (self.server, self.runtime) if x)
            return (f"known stack: {self.product}{ver}"
                    + (f" on {stack}" if stack else "")
                    + f"  ({self.confidence} confidence) — reconstruct the real software")
        hints = ", ".join(x for x in (self.server, self.runtime) if x)
        return ("opaque service" + (f" ({hints})" if hints else "")
                + " — mirror observable behavior")


def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return str(v)
    return ""


def fingerprint(exchanges) -> StackReport:
    """Detect the stack from recorded exchanges (each with .response_headers,
    .response_body, .path). Deterministic, read-only over what we already have."""
    report = StackReport()
    bodies = []
    paths = set()
    all_headers: dict[str, str] = {}
    cookie_blob = ""
    for ex in exchanges:
        headers = getattr(ex, "response_headers", {}) or {}
        for k, v in headers.items():
            all_headers.setdefault(k.lower(), str(v))
            if k.lower() == "set-cookie":
                cookie_blob += " " + str(v)
        bodies.append(getattr(ex, "response_body", "") or "")
        paths.add(getattr(ex, "path", "") or "")
    body_blob = "\n".join(bodies)

    # server / runtime from headers
    server_val = _header(all_headers, "server")
    if server_val:
        report.server = server_val.split(";")[0].strip()
        report.signals.append(f"Server: {report.server}")
    powered = _header(all_headers, "x-powered-by")
    if powered:
        report.runtime = powered.split(",")[0].strip()
        report.signals.append(f"X-Powered-By: {report.runtime}")

    # application / framework
    best = None
    best_hits = 0
    for prod in _PRODUCTS:
        hits = []
        for m in prod.cookie_markers:
            if m.lower() in cookie_blob.lower():
                hits.append(f"cookie~{m}")
        for m in prod.body_markers:
            if m in body_blob:
                hits.append(f"body~{m}")
        for m in prod.path_markers:
            if any(p == m or p.startswith(m) for p in paths):
                hits.append(f"path~{m}")
        if len(hits) > best_hits:
            best, best_hits = prod, len(hits)
            best_hits_list = hits
    if best is not None:
        report.kind = "known_stack"
        report.product = best.name
        report.signals.extend(best_hits_list)
        if best.version_re:
            m = best.version_re.search(body_blob)
            if m:
                report.product_version = m.group(1)
                report.signals.append(f"version {report.product_version}")
        report.confidence = (
            "high" if best_hits >= 2 or report.product_version else "medium"
        )
    return report
