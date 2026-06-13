from hermes.oracle import Probe
from hermes.tools import build_registry
from hermes.tools.base import ToolContext
from hermes.tools.oracle import oracle_list, oracle_query


def _seal(project, probes):
    bundle = project.oracle()
    bundle.init(source="https://api.example.com", win_condition="parity on /users")
    for p in probes:
        bundle.add_probe(p)
    bundle.seal()
    return bundle


def _ctx(project, cfg):
    return ToolContext(project=project, cfg=cfg)


def test_oracle_query_returns_recorded_response(project, cfg):
    _seal(project, [Probe(method="GET", path="/users/1", status=200,
                          response_body='{"id":1}', content_type="application/json")])
    out = oracle_query.fn({"path": "/users/1"}, _ctx(project, cfg))
    assert "RECORDED REPLICA" in out
    assert '{"id":1}' in out
    assert "HTTP 200" in out


def test_oracle_query_miss_refuses_to_go_live(project, cfg):
    _seal(project, [Probe(method="GET", path="/users/1", status=200, response_body="x")])
    out = oracle_query.fn({"path": "/users/999"}, _ctx(project, cfg))
    assert "NO MATCH" in out
    assert "no live service" in out
    assert "/users/1" in out  # shows what is available


def test_oracle_list_shows_surface_and_win_condition(project, cfg):
    _seal(project, [
        Probe(method="GET", path="/users/1", status=200, response_body="a"),
        Probe(method="GET", path="/users/2", status=404, response_body="b"),
    ])
    out = oracle_list.fn({}, _ctx(project, cfg))
    assert "parity on /users" in out
    assert "/users/1 -> 200" in out
    assert "/users/2 -> 404" in out


def test_oracle_query_errors_without_sealed_bundle(project, cfg):
    out = oracle_query.fn({"path": "/x"}, _ctx(project, cfg))
    assert out.startswith("ERROR")


def test_oracle_tools_register_only_when_sealed(project, cfg):
    yes = lambda *a, **k: True
    # no bundle -> not registered
    reg = build_registry(project, cfg, yes)
    assert "oracle_query" not in reg.names()
    # unsealed bundle -> still not registered
    project.oracle().init(source="https://api.example.com")
    assert "oracle_query" not in build_registry(project, cfg, yes).names()
    # sealed -> registered
    _seal(project, [Probe(method="GET", path="/", status=200, response_body="ok")])
    reg = build_registry(project, cfg, yes)
    assert "oracle_query" in reg.names()
    assert "oracle_list" in reg.names()
