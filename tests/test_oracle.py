from hermes.oracle import OracleBundle, Probe, request_key


def _probe(method="GET", path="/users/1", query="", status=200,
           body='{"id":1}', request_body=None):
    return Probe(method=method, path=path, query=query, status=status,
                 response_body=body, request_body=request_body)


def test_request_key_is_order_insensitive_on_query():
    a = request_key("get", "/search", "b=2&a=1")
    b = request_key("GET", "/search", "a=1&b=2")
    assert a == b


def test_request_key_separates_method_and_body():
    assert request_key("GET", "/x") != request_key("POST", "/x")
    assert request_key("POST", "/x", body="a") != request_key("POST", "/x", body="b")


def test_request_key_pulls_query_out_of_path():
    assert request_key("GET", "/s?a=1&b=2") == request_key("GET", "/s", "b=2&a=1")


def test_init_creates_unsealed_bundle(project):
    bundle = project.oracle()
    bundle.init(source="https://api.example.com", win_condition="match /users")
    assert bundle.exists()
    assert not bundle.is_sealed()
    assert bundle.source == "https://api.example.com"
    assert bundle.win_condition == "match /users"


def test_add_probe_then_replay_exact_match(project):
    bundle = project.oracle()
    bundle.init(source="https://api.example.com")
    bundle.add_probe(_probe(path="/users/1", body='{"id":1}'))
    hit = bundle.replay("GET", "/users/1")
    assert hit is not None
    assert hit.response_body == '{"id":1}'
    assert bundle.replay("GET", "/users/2") is None


def test_replay_matches_query_regardless_of_order(project):
    bundle = project.oracle()
    bundle.init(source="x")
    bundle.add_probe(_probe(path="/search", query="a=1&b=2", body="ok"))
    assert bundle.replay("GET", "/search", "b=2&a=1").response_body == "ok"


def test_seal_freezes_and_blocks_further_capture(project):
    bundle = project.oracle()
    bundle.init(source="x")
    bundle.add_probe(_probe())
    bundle.seal()
    assert bundle.is_sealed()
    assert bundle.read_manifest()["probe_count"] == 1
    try:
        bundle.add_probe(_probe(path="/other"))
        assert False, "sealed bundle must refuse new probes"
    except ValueError:
        pass


def test_probes_survive_reload(project):
    bundle = project.oracle()
    bundle.init(source="x")
    bundle.add_probe(_probe(path="/a", body="A"))
    bundle.add_probe(_probe(path="/b", body="B"))
    bundle.seal()
    fresh = OracleBundle(project.oracle_dir)
    assert {p.path for p in fresh.probes()} == {"/a", "/b"}
    assert fresh.replay("GET", "/b").response_body == "B"


def test_summary_reports_state(project):
    bundle = project.oracle()
    bundle.init(source="https://api.example.com", win_condition="parity on /users")
    bundle.add_probe(_probe())
    assert "OPEN" in bundle.summary()
    bundle.seal()
    out = bundle.summary()
    assert "sealed" in out
    assert "parity on /users" in out
