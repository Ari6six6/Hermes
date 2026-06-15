from hermes.twin import scan as scan_mod
from hermes.twin.scan import Service, format_scan, host_of, scan

NMAP_XML = """<?xml version="1.0"?>
<nmaprun>
  <host>
    <ports>
      <port protocol="tcp" portid="22">
        <state state="open"/>
        <service name="ssh" product="OpenSSH" version="9.6p1" extrainfo="Ubuntu"/>
      </port>
      <port protocol="tcp" portid="80">
        <state state="open"/>
        <service name="http" product="nginx" version="1.27.0"/>
      </port>
      <port protocol="tcp" portid="81">
        <state state="closed"/>
      </port>
    </ports>
  </host>
</nmaprun>"""


def test_host_of_strips_scheme_and_port():
    assert host_of("https://example.com/path?q=1") == "example.com"
    assert host_of("example.com:8443") == "example.com"
    assert host_of("example.com") == "example.com"


def test_scan_uses_nmap_when_present():
    def runner(cmd, timeout):
        assert cmd[:2] == ["nmap", "-sV"]
        assert "--top-ports" in cmd
        return 0, NMAP_XML, ""

    res = scan("https://example.com", runner=runner, nmap_path="/usr/bin/nmap")
    assert res.engine == "nmap"
    ports = {s.port: s for s in res.services}
    assert set(ports) == {22, 80}  # closed port dropped
    assert ports[22].product == "OpenSSH" and ports[22].version == "9.6p1"
    assert ports[80].service == "http" and ports[80].product == "nginx"


def test_scan_falls_back_when_no_nmap():
    banners = {
        22: "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3\r\n",
        80: "HTTP/1.1 200 OK\r\nServer: nginx/1.27.0\r\nContent-Type: text/html\r\n\r\n",
        443: None,  # closed
    }

    def probe(host, port, timeout):
        return banners.get(port)

    res = scan("example.com", nmap_path=None, probe=probe, ports=[22, 80, 443])
    assert res.engine == "builtin"
    ports = {s.port: s for s in res.services}
    assert set(ports) == {22, 80}
    assert ports[22].service == "ssh" and ports[22].product == "OpenSSH"
    assert ports[22].version == "9.6p1"
    assert ports[80].service == "http" and ports[80].product == "nginx"
    assert ports[80].version == "1.27.0"


def test_scan_falls_back_when_nmap_errors():
    def runner(cmd, timeout):
        return 1, "", "nmap: command not found"

    def probe(host, port, timeout):
        return "SSH-2.0-OpenSSH_9.6p1\r\n" if port == 22 else None

    res = scan("example.com", runner=runner, probe=probe,
               nmap_path="/usr/bin/nmap", ports=[22, 80])
    assert res.engine == "builtin"  # nmap failed -> fell back
    assert [s.port for s in res.services] == [22]


def test_format_scan_lists_services():
    res = scan_mod.ScanResult(
        host="example.com", engine="nmap", top_ports=1000,
        services=[Service(port=22, service="ssh", product="OpenSSH", version="9.6p1"),
                  Service(port=443, service="https", product="nginx", version="1.27.0")])
    out = format_scan(res)
    assert "example.com" in out and "nmap -sV" in out
    assert "22/tcp" in out and "OpenSSH 9.6p1" in out
    assert "443/tcp" in out and "nginx 1.27.0" in out


def test_format_scan_handles_empty():
    res = scan_mod.ScanResult(host="example.com", engine="builtin")
    assert "no open services" in format_scan(res)
