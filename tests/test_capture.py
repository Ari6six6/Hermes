from hermes import capture as capture_mod
from hermes.capture import capture


class FakeFetch:
    """Records calls; returns a canned (status, headers, text) per URL."""

    def __init__(self, responses=None, default=(200, {"content-type": "application/json"}, "{}")):
        self.responses = responses or {}
        self.default = default
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=45):
        self.calls.append((method, url))
        return self.responses.get(url, self.default)


def test_capture_records_specs_and_seals(project, monkeypatch):
    monkeypatch.setattr(capture_mod.time, "sleep", lambda *_: None)
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")
    fetch = FakeFetch(responses={
        "https://api.example.com/users/1": (200, {"content-type": "application/json"}, '{"id":1}'),
    })
    report = capture(bundle, "https://api.example.com", ["/users/1"],
                     include_discovery=False, fetch=fetch, delay=0)
    assert report["recorded"] == 1
    assert bundle.is_sealed()
    assert bundle.replay("GET", "/users/1").response_body == '{"id":1}'


def test_capture_skips_non_read_methods(project, monkeypatch):
    monkeypatch.setattr(capture_mod.time, "sleep", lambda *_: None)
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")
    fetch = FakeFetch()
    report = capture(
        bundle, "https://api.example.com",
        [{"method": "POST", "path": "/users"}, {"method": "GET", "path": "/users"}],
        include_discovery=False, fetch=fetch, delay=0,
    )
    assert report["skipped"] == 1
    assert report["recorded"] == 1
    # the POST never went out — capture stays benign
    assert all(m != "POST" for m, _ in fetch.calls)


def test_capture_honors_max_probes(project, monkeypatch):
    monkeypatch.setattr(capture_mod.time, "sleep", lambda *_: None)
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")
    fetch = FakeFetch()
    report = capture(bundle, "https://api.example.com",
                     [f"/p{i}" for i in range(10)],
                     include_discovery=False, max_probes=3, fetch=fetch, delay=0)
    assert report["recorded"] == 3
    assert len(bundle.probes()) == 3


def test_capture_tries_discovery_paths(project, monkeypatch):
    monkeypatch.setattr(capture_mod.time, "sleep", lambda *_: None)
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")
    fetch = FakeFetch()
    capture(bundle, "https://api.example.com", ["/"], include_discovery=True,
            fetch=fetch, delay=0)
    urls = {u for _, u in fetch.calls}
    assert "https://api.example.com/openapi.json" in urls
    assert "https://api.example.com/robots.txt" in urls


def test_capture_survives_fetch_errors(project, monkeypatch):
    monkeypatch.setattr(capture_mod.time, "sleep", lambda *_: None)
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")

    def flaky(method, url, headers=None, body=None, timeout=45):
        if "boom" in url:
            raise RuntimeError("connection reset")
        return 200, {"content-type": "text/plain"}, "ok"

    report = capture(bundle, "https://api.example.com", ["/boom", "/fine"],
                     include_discovery=False, fetch=flaky, delay=0)
    assert report["errors"] == 1
    assert report["recorded"] == 1
    assert bundle.is_sealed()
