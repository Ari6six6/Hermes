from hermes import package
from hermes.tools import build_registry
from hermes.tools.base import ToolContext
from hermes.tools.twin import twin_expand, twin_map, twin_request
from hermes.twin.model import Exchange


def _seal(project, exchanges, **manifest):
    twin = project.twin()
    twin.init(source="https://api.example.com", **manifest)
    for ex in exchanges:
        twin.add_exchange(ex)
    twin.seal()
    return twin


def _ctx(project, cfg):
    return ToolContext(project=project, cfg=cfg)


def test_twin_request_returns_real_response(project, cfg):
    _seal(project, [Exchange(method="GET", path="/users/1", status=200,
                             response_body='{"id":1}', content_type="application/json")])
    out = twin_request.fn({"path": "/users/1"}, _ctx(project, cfg))
    assert "real captured response" in out
    assert '{"id":1}' in out and "HTTP 200" in out


def test_twin_request_miss_points_to_expand(project, cfg):
    _seal(project, [Exchange(method="GET", path="/users/1", status=200, response_body="x")])
    out = twin_request.fn({"path": "/users/2"}, _ctx(project, cfg))
    assert "TWIN MISS" in out
    assert "twin_expand" in out  # never fabricates; points to growth


def test_twin_map_shows_routes_and_goal(project, cfg):
    _seal(project,
          [Exchange(method="GET", path="/users/1", status=200, response_body="a"),
           Exchange(method="GET", path="/users/2", status=200, response_body="b")],
          win_condition="parity on /users")
    out = twin_map.fn({}, _ctx(project, cfg))
    assert "parity on /users" in out
    assert "/users/{id}" in out


def test_twin_expand_grows_via_clone_layer(project, cfg, monkeypatch):
    _seal(project, [Exchange(method="GET", path="/users/1", status=200, response_body="a")])

    def fake_expand(twin, base_url, paths, **kw):
        from hermes.twin.model import Exchange as Ex
        twin.unseal()
        for p in paths:
            twin.add_exchange(Ex(method="GET", path=p, status=200,
                                 response_body="grown", source="expand"))
        twin.seal()
        return {"added": len(paths), "errors": 0}

    monkeypatch.setattr("hermes.twin.clone.expand", fake_expand)
    out = twin_expand.fn({"paths": ["/users/2"]}, _ctx(project, cfg))
    assert "learned 1" in out
    assert project.twin().respond("GET", "/users/2").response_body == "grown"


def test_twin_tools_register_only_when_sealed(project, cfg):
    yes = lambda *a, **k: True
    assert "twin_request" not in build_registry(project, cfg, yes).names()
    project.twin().init(source="https://api.example.com")  # open, not sealed
    assert "twin_request" not in build_registry(project, cfg, yes).names()
    _seal(project, [Exchange(method="GET", path="/", status=200, response_body="ok")])
    names = build_registry(project, cfg, yes).names()
    assert {"twin_request", "twin_map", "twin_expand"} <= set(names)


def test_build_mode_block_injected_when_sealed(project):
    assert package.build_mode_block(project) == ""  # no twin yet
    _seal(project, [Exchange(method="GET", path="/", status=200, response_body="ok")],
          mission="reimplement /users", win_condition="byte-match")
    block = package.build_mode_block(project)
    assert "SAFE TWIN" in block
    assert "reimplement /users" in block
    assert "byte-match" in block


def test_build_mode_block_absent_until_sealed(project):
    project.twin().init(source="https://api.example.com")
    assert package.build_mode_block(project) == ""


def test_build_mode_reaches_system_prompt(project, cfg):
    _seal(project, [Exchange(method="GET", path="/", status=200, response_body="ok")],
          mission="reimplement /users", win_condition="byte-match")
    system = package.assemble(project, "go", {}, cfg)[0]["content"]
    assert "SAFE TWIN" in system
    assert "reimplement /users" in system
