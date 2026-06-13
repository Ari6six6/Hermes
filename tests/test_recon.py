from hermes.twin.model import Exchange
from hermes.twin.recon import StackReport, fingerprint


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
