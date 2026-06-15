from hermes.twin import survey as survey_mod
from hermes.twin.survey import format_survey, survey


def _fetch_factory(pages):
    """pages: dict of path -> (status, text). Missing paths 404."""
    def fetch(method, url, headers=None, body=None, timeout=45):
        from urllib.parse import urlsplit
        if url.startswith("https://crt.sh"):
            return pages.get("__crt__", (404, {}, ""))[0], {}, pages.get("__crt__", (404, {}, ""))[1]
        path = urlsplit(url).path or "/"
        status, text = pages.get(path, (404, ""))
        return status, {"content-type": "text/html"}, text
    return fetch


def test_survey_maps_dirs_and_exposed():
    pages = {
        "/admin": (200, "admin"),
        "/login": (302, ""),
        "/.git/config": (200, "[core]"),
        "/.env": (403, ""),
        "/robots.txt": (404, ""),
        "/sitemap.xml": (404, ""),
    }
    res = survey("https://example.com", fetch=_fetch_factory(pages),
                 include_subdomains=False)
    dir_paths = {d["path"]: d["status"] for d in res.dirs}
    assert dir_paths.get("/admin") == 200
    assert dir_paths.get("/login") == 302   # non-404 counts as reachable
    exposed = {e["path"]: e for e in res.exposed}
    assert exposed["/.git/config"]["readable"] is True
    assert exposed["/.env"]["readable"] is False  # 403: present but needs auth
    assert "404" not in str(dir_paths.values())


def test_survey_mines_robots_paths():
    pages = {
        "/robots.txt": (200, "Disallow: /secret-area\nDisallow: /hidden"),
        "/secret-area": (200, "found it"),
        "/sitemap.xml": (404, ""),
    }
    res = survey("example.com", fetch=_fetch_factory(pages), include_subdomains=False)
    assert res.from_robots == 2
    assert any(d["path"] == "/secret-area" for d in res.dirs)


def test_survey_pulls_subdomains():
    crt = '[{"name_value":"api.example.com"},{"name_value":"www.example.com"}]'
    pages = {"/robots.txt": (404, ""), "/sitemap.xml": (404, ""), "__crt__": (200, crt)}
    res = survey("https://example.com", fetch=_fetch_factory(pages),
                 include_subdomains=True)
    assert "api.example.com" in res.subdomains
    assert "www.example.com" in res.subdomains


def test_survey_respects_max_paths():
    pages = {"/robots.txt": (404, ""), "/sitemap.xml": (404, "")}
    res = survey("example.com", fetch=_fetch_factory(pages), max_paths=5,
                 include_subdomains=False)
    assert res.checked == 5


def test_format_survey_renders():
    pages = {
        "/admin": (200, "x"),
        "/.git/config": (200, "[core]"),
        "/robots.txt": (404, ""), "/sitemap.xml": (404, ""),
    }
    res = survey("https://example.com", fetch=_fetch_factory(pages),
                 include_subdomains=False)
    out = format_survey(res)
    assert "example.com" in out
    assert "/admin" in out
    assert "/.git/config" in out
