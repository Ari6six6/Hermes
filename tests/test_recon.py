from hermes.twin.model import Exchange
from hermes.twin.recon import (
    StackReport,
    fingerprint,
    interpret_exposure,
    parse_crtsh,
    parse_robots_paths,
    parse_sitemap_paths,
)


def test_parse_crtsh_dedupes_and_strips_wildcards():
    text = ('[{"name_value":"a.example.com\\n*.example.com"},'
            '{"name_value":"b.example.com"},{"name_value":"a.example.com"}]')
    assert parse_crtsh(text) == ["a.example.com", "b.example.com", "example.com"]


def test_parse_crtsh_bad_json():
    assert parse_crtsh("not json") == []


def test_parse_robots_paths_pulls_disallow_and_sitemap():
    robots = "User-agent: *\nDisallow: /admin\nDisallow: /\nAllow: /public\nSitemap: https://x/s.xml"
    paths = parse_robots_paths(robots)
    assert "/admin" in paths
    assert "/public" in paths
    assert "/" not in paths  # uninteresting catch-all dropped
    assert any("s.xml" in p for p in paths)


def test_parse_sitemap_paths():
    xml = "<urlset><url><loc>https://x/a</loc></url><url><loc>https://x/b</loc></url></urlset>"
    assert parse_sitemap_paths(xml) == ["https://x/a", "https://x/b"]


def test_interpret_exposure():
    assert "EXPOSED" in interpret_exposure("/.git/config", 200)
    assert "protected" in interpret_exposure("/.env", 403)
    assert interpret_exposure("/.env", 404) is None


def _ex(path="/", body="", headers=None):
    return Exchange(method="GET", path=path, status=200, response_body=body,
                    response_headers=headers or {})


def test_detects_wordpress_known_stack_with_version():
    exchanges = [
        _ex("/", '<meta name="generator" content="WordPress 6.4.2" />\n<link href="/wp-content/themes/x/style.css">',
            {"server": "Apache/2.4.52", "x-powered-by": "PHP/8.1.2"}),
        _ex("/wp-json", '{"name":"site"}'),
    ]
    r = fingerprint(exchanges)
    assert r.kind == "known_stack"
    assert r.product == "WordPress"
    assert r.product_version == "6.4.2"
    assert r.server == "Apache/2.4.52"
    assert r.runtime == "PHP/8.1.2"
    assert r.confidence == "high"
    assert r.reconstructable()


def test_detects_django_from_cookie():
    r = fingerprint([_ex("/admin/login/", "<input name='csrfmiddlewaretoken'>",
                         {"set-cookie": "csrftoken=abc; Path=/"})])
    assert r.kind == "known_stack"
    assert r.product == "Django"


def test_detects_express_from_cookie_header():
    r = fingerprint([_ex("/", "hello", {"set-cookie": "connect.sid=s%3Aabc",
                                        "x-powered-by": "Express"})])
    assert r.product == "Express"
    assert r.runtime == "Express"


def test_opaque_when_no_markers():
    r = fingerprint([_ex("/api/things", '{"things":[]}',
                         {"server": "nginx", "content-type": "application/json"})])
    assert r.kind == "opaque"
    assert r.server == "nginx"
    assert not r.reconstructable()
    assert "opaque" in r.summary()


def test_summary_known_stack_mentions_reconstruction():
    r = StackReport(kind="known_stack", product="WordPress", product_version="6.4",
                    server="Apache/2.4", runtime="PHP/8.1", confidence="high")
    assert "WordPress 6.4" in r.summary()
    assert "reconstruct" in r.summary()


def test_strongest_match_wins_when_multiple_signals():
    # WordPress has more hits (generator + path + body) than a stray cookie.
    exchanges = [
        _ex("/", '<meta name="generator" content="WordPress 6.5"/> /wp-includes/ wp-json',
            {"set-cookie": "wordpress_test_cookie=1"}),
        _ex("/wp-login.php", "log in"),
    ]
    r = fingerprint(exchanges)
    assert r.product == "WordPress"
    assert r.confidence == "high"
