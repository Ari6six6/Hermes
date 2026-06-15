"""Service scan: what's actually running on a target host, and which versions.

Hermes' clone engine reconstructs a web *app* over HTTP — it answers "how does
this site behave". This module answers the different question an operator usually
means by "scan it": which TCP services are listening (ssh, http, databases, mail,
...) and what software/version each one is. That's host-and-port level recon, not
page crawling.

It prefers nmap's service/version detection (`nmap -sV`) when nmap is on the
phone — that's the accurate path — and falls back to a dependency-free TCP
connect + banner scan otherwise. Like the rest of recon it runs on the phone
(where the net lives), is read-only (connect and read a banner, never write), and
its side-effecting pieces (`runner`, `probe`) are injected so the logic is fully
testable without a network.

Use it only against hosts you are authorized to test.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

# Curated common service ports for the no-nmap fallback: port -> default service
# name. nmap's own --top-ports list is far longer; this is the subset that
# actually answers "what runs here" for the services Hermes cares about.
COMMON_PORTS: dict[int, str] = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "domain",
    80: "http", 110: "pop3", 111: "rpcbind", 135: "msrpc", 139: "netbios-ssn",
    143: "imap", 161: "snmp", 389: "ldap", 443: "https", 445: "microsoft-ds",
    465: "smtps", 587: "submission", 631: "ipp", 636: "ldaps", 993: "imaps",
    995: "pop3s", 1025: "nfs-or-iis", 1433: "ms-sql", 1521: "oracle",
    1723: "pptp", 2049: "nfs", 2082: "cpanel", 2083: "cpanel-ssl",
    2375: "docker", 2376: "docker-ssl", 27017: "mongodb", 27018: "mongodb",
    3000: "http-dev", 3306: "mysql", 3389: "ms-wbt-server", 4444: "metasploit",
    5000: "http-dev", 5432: "postgresql", 5601: "kibana", 5672: "amqp",
    5900: "vnc", 5984: "couchdb", 6379: "redis", 7001: "weblogic",
    8000: "http-alt", 8008: "http-alt", 8080: "http-proxy", 8081: "http-alt",
    8086: "influxdb", 8088: "http-alt", 8443: "https-alt", 8888: "http-alt",
    9000: "http-alt", 9092: "kafka", 9200: "elasticsearch", 9300: "elasticsearch",
    9418: "git", 10000: "webmin", 11211: "memcached", 15672: "rabbitmq-mgmt",
    50070: "hadoop",
}

# Ports we should speak HTTP to when grabbing a banner.
_HTTP_PORTS = {80, 3000, 5000, 8000, 8008, 8080, 8081, 8088, 8888, 9000, 9200}
# Ports that expect TLS first.
_TLS_PORTS = {443, 465, 636, 993, 995, 2083, 8443}

_SSH_RE = re.compile(r"SSH-\d+\.\d+-(\S+)")
_SERVER_HDR_RE = re.compile(r"^Server:\s*(.+?)\s*$", re.I | re.M)
_FTP_RE = re.compile(r"220[ -].*?([A-Za-z][\w+.-]*?)[ /](\d[\w.]*)")
_SMTP_RE = re.compile(r"220[ -].*?ESMTP\s+([A-Za-z][\w.-]*)", re.I)


@dataclass
class Service:
    port: int
    proto: str = "tcp"
    state: str = "open"
    service: str = ""      # e.g. "ssh", "http"
    product: str = ""      # e.g. "OpenSSH", "nginx"
    version: str = ""      # e.g. "9.6p1", "1.27.0"
    extra: str = ""        # banner remnant / nmap extrainfo

    def label(self) -> str:
        sv = self.service or "?"
        soft = " ".join(x for x in (self.product, self.version) if x)
        tail = f"  {soft}" if soft else (f"  {self.extra[:48]}" if self.extra else "")
        return f"{self.port}/{self.proto}".ljust(11) + sv.ljust(14) + tail.strip()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    host: str
    engine: str = "builtin"        # "nmap" | "builtin"
    top_ports: int = 0
    services: list = field(default_factory=list)  # list[Service]
    error: str = ""

    def to_dict(self) -> dict:
        return {"host": self.host, "engine": self.engine, "top_ports": self.top_ports,
                "services": [s.to_dict() for s in self.services], "error": self.error}


def host_of(target: str) -> str:
    """The bare hostname to scan, from a URL or a host[:port] string."""
    t = target.strip()
    if "://" not in t:
        t = "//" + t
    return urlsplit(t).hostname or target.strip()


# -- nmap path -------------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _parse_nmap_xml(xml: str) -> list[Service]:
    out: list[Service] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for port in root.iter("port"):
        st = port.find("state")
        if st is None or st.get("state") != "open":
            continue
        svc = port.find("service")
        s = Service(
            port=int(port.get("portid", "0") or 0),
            proto=port.get("protocol", "tcp"),
            service=(svc.get("name", "") if svc is not None else ""),
            product=(svc.get("product", "") if svc is not None else ""),
            version=(svc.get("version", "") if svc is not None else ""),
            extra=(svc.get("extrainfo", "") if svc is not None else ""),
        )
        out.append(s)
    return sorted(out, key=lambda s: s.port)


def _nmap_scan(host: str, top_ports: int, runner, timeout: int) -> ScanResult:
    cmd = ["nmap", "-sV", "-Pn", "--top-ports", str(top_ports), "-oX", "-", host]
    try:
        rc, out, err = runner(cmd, timeout)
    except Exception as e:  # nmap missing/blew up — caller falls back
        return ScanResult(host, "nmap", top_ports, error=f"{type(e).__name__}: {e}")
    if rc != 0 and not out.strip():
        return ScanResult(host, "nmap", top_ports, error=(err or "nmap failed").strip()[:200])
    return ScanResult(host, "nmap", top_ports, services=_parse_nmap_xml(out))


# -- pure-Python fallback --------------------------------------------------

def _probe(host: str, port: int, timeout: float) -> str | None:
    """Connect to host:port. Returns a banner string (possibly empty) if open,
    or None if closed/unreachable. Read-only: connect, maybe one polite request,
    read what comes back."""
    import ssl

    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    try:
        sock = raw
        if port in _TLS_PORTS:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        sock.settimeout(timeout)
        if port in _HTTP_PORTS or port in _TLS_PORTS:
            try:
                sock.sendall(
                    f"GET / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: HermesScan/0.1\r\n\r\n".encode()
                )
            except OSError:
                pass
        try:
            return sock.recv(4096).decode("latin-1", "replace")
        except OSError:
            return ""
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _identify(port: int, banner: str) -> Service:
    """Best-effort service/product/version from a banner — light heuristics for
    the cases a connect scan can read; nmap does the heavy lifting when present."""
    service = COMMON_PORTS.get(port, "")
    s = Service(port=port, service=service, extra=(banner or "").strip()[:120])
    if not banner:
        return s
    m = _SSH_RE.search(banner)
    if m:
        s.service = "ssh"
        prod = m.group(1)
        if "_" in prod:
            name, _, ver = prod.partition("_")
            s.product, s.version = name, ver.split()[0] if ver else ""
        else:
            s.product = prod
        return s
    m = _SERVER_HDR_RE.search(banner)
    if m:
        s.service = "https" if port in _TLS_PORTS else "http"
        token = m.group(1).split()[0] if m.group(1) else ""
        s.product, _, s.version = token.partition("/")
        s.extra = ""
        return s
    m = _FTP_RE.search(banner)
    if m:
        s.service, s.product, s.version, s.extra = "ftp", m.group(1), m.group(2), ""
        return s
    m = _SMTP_RE.search(banner)
    if m:
        s.service, s.product, s.extra = "smtp", m.group(1), ""
        return s
    if banner.startswith("HTTP/"):
        s.service = "https" if port in _TLS_PORTS else "http"
    return s


def _builtin_scan(host: str, probe, timeout: float, workers: int,
                  ports: list[int] | None = None) -> ScanResult:
    ports = ports or sorted(COMMON_PORTS)
    found: list[Service] = []

    def one(port):
        banner = probe(host, port, timeout)
        return port if banner is None else _identify(port, banner)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for res in ex.map(one, ports):
            if isinstance(res, Service):
                found.append(res)
    return ScanResult(host, "builtin", len(ports),
                      services=sorted(found, key=lambda s: s.port))


# -- public entry ----------------------------------------------------------

def scan(target: str, *, top_ports: int = 1000, timeout: float = 2.0,
         workers: int = 100, nmap_timeout: int = 600, runner=_run, probe=_probe,
         nmap_path: str | None = "auto", ports: list[int] | None = None,
         on_event=None) -> ScanResult:
    """Scan `target` (a URL or host[:port]) for listening services and versions.

    Uses `nmap -sV` when nmap is available, else a built-in connect/banner scan.
    `nmap_path="auto"` resolves nmap from PATH; pass None to force the fallback,
    or a path string to force nmap. `runner`/`probe` are injected for testing.
    """
    def emit(text):
        if on_event:
            on_event(text)

    host = host_of(target)
    use_nmap = shutil.which("nmap") if nmap_path == "auto" else nmap_path
    if use_nmap:
        emit(f"service scan of {host} (nmap -sV, top {top_ports})...")
        result = _nmap_scan(host, top_ports, runner, nmap_timeout)
        if not result.error:
            emit(f"{len(result.services)} open service(s) found")
            return result
        emit(f"nmap unavailable ({result.error[:80]}) — falling back to connect scan")

    emit(f"service scan of {host} (built-in connect scan, {len(ports or COMMON_PORTS)} ports)...")
    result = _builtin_scan(host, probe, timeout, workers, ports)
    emit(f"{len(result.services)} open service(s) found")
    return result


def format_scan(result: ScanResult) -> str:
    """Human-readable open-ports → service → version listing."""
    engine = "nmap -sV" if result.engine == "nmap" else "built-in connect scan"
    head = f"services on {result.host} ({engine}):"
    if result.error and not result.services:
        return head + f"\n  scan error: {result.error}"
    if not result.services:
        return head + "\n  (no open services found on the scanned ports)"
    return head + "\n" + "\n".join("  " + s.label() for s in result.services)
