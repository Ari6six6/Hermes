from hermes.tools import build_registry
from hermes.tools import recon as recon_tools
from hermes.tools.base import ToolContext
from hermes.twin.model import Exchange


def _ctx(project, cfg):
    return ToolContext(project=project, cfg=cfg)


def test_recon_subdomains(project, cfg, monkeypatch):
    crt = '[{"name_value":"api.example.com"},{"name_value":"www.example.com"}]'
    monkeypatch.setattr(recon_tools, "_get", lambda url, timeout=20: (200, crt))
    out = recon_tools.recon_subdomains.fn({"domain": "example.com"}, _ctx(project, cfg))
    assert "api.example.com" in out and "www.example.com" in out


def test_recon_subdomains_rejects_url():
    from hermes.tools.base import ToolContext
    out = recon_tools.recon_subdomains.fn({"domain": "http://x/y"}, ToolContext(None, None))
    assert out.startswith("ERROR")


def test_recon_sources_reports_exposed(project, cfg, monkeypatch):
    def fake_get(url, timeout=20):
        return (200, "ref: x") if url.endswith("/.git/config") else (404, "")
    monkeypatch.setattr(recon_tools, "_get", fake_get)
    out = recon_tools.recon_sources.fn({"url": "https://example.com"}, _ctx(project, cfg))
    assert "EXPOSED" in out and "/.git/config" in out


def test_recon_dirscan_finds_paths_and_mines_robots(project, cfg, monkeypatch):
    def fake_get(url, timeout=20):
        if url.endswith("/robots.txt"):
            return 200, "Disallow: /secret-area"
        if url.endswith("/sitemap.xml"):
            return 404, ""
        if url.endswith(("/admin", "/secret-area")):
            return 200, "ok"
        return 404, ""
    monkeypatch.setattr(recon_tools, "_get", fake_get)
    out = recon_tools.recon_dirscan.fn({"url": "https://example.com"}, _ctx(project, cfg))
    assert "/admin" in out
    assert "/secret-area" in out          # discovered via robots.txt
    assert "robots/sitemap" in out


def test_recon_tools_register_while_twin_open_not_after_seal(project, cfg):
    yes = lambda *a, **k: True
    # no twin yet -> no recon tools
    assert "recon_dirscan" not in build_registry(project, cfg, yes).names()
    # open twin -> recon phase: recon tools present, twin tools absent
    twin = project.twin()
    twin.init(source="https://example.com")
    names = build_registry(project, cfg, yes).names()
    assert {"recon_subdomains", "recon_sources", "recon_dirscan"} <= set(names)
    assert "twin_request" not in names
    # sealed twin -> build phase: recon tools gone, twin tools present
    twin.add_exchange(Exchange(method="GET", path="/", status=200, response_body="ok"))
    twin.seal()
    names = build_registry(project, cfg, yes).names()
    assert "recon_dirscan" not in names
    assert "twin_request" in names
