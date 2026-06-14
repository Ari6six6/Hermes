from tests.conftest import FakeEndpoint

from hermes.tools import build_registry
from hermes.tools.base import ToolContext
from hermes.tools.builder import build_recipe, build_run


def _open_twin(project):
    twin = project.twin()
    twin.init(source="https://api.example.com")
    return twin


def _ctx(project, cfg, gpu):
    # build_run dispatches remote_shell, so it needs a live registry + box.
    registry = build_registry(project, cfg, lambda *a, **k: True)
    ctx = ToolContext(project=project, cfg=cfg, gpu=gpu)
    ctx.registry = registry
    return ctx


def test_model_recipe_roundtrip(project):
    twin = _open_twin(project)
    twin.add_step("apt-get install -y nginx", note="web server")
    twin.add_step("systemctl start nginx")
    steps = twin.recipe()
    assert [s["cmd"] for s in steps] == ["apt-get install -y nginx", "systemctl start nginx"]
    assert steps[0]["note"] == "web server"


def test_build_run_records_successful_step(project, cfg):
    _open_twin(project)
    gpu = FakeEndpoint(responses=[(0, "Reading package lists... done", "")])
    out = build_run.fn({"command": "tar x,zf wp.tgz", "note": "unpack"}, _ctx(project, cfg, gpu))
    assert "recorded to recipe" in out
    assert project.twin().recipe()[0]["cmd"] == "tar x,zf wp.tgz"


def test_build_run_does_not_record_failure(project, cfg):
    _open_twin(project)
    gpu = FakeEndpoint(responses=[(1, "", "No such file")])
    out = build_run.fn({"command": "make"}, _ctx(project, cfg, gpu))
    assert "not recorded" in out
    assert project.twin().recipe() == []


def test_build_run_records_provisioning_install(project, cfg):
    # Installing software on the box is allowed now, so a successful apt install
    # runs and is captured into the recipe.
    _open_twin(project)
    gpu = FakeEndpoint(responses=[(0, "nginx is the newest version", "")])
    out = build_run.fn({"command": "apt-get install -y nginx", "note": "web server"},
                       _ctx(project, cfg, gpu))
    assert "recorded to recipe" in out
    assert project.twin().recipe()[0]["cmd"] == "apt-get install -y nginx"


def test_build_run_does_not_record_bounced_egress(project, cfg):
    # A raw download is bounced to the phone, never run on the box — not captured.
    _open_twin(project)
    gpu = FakeEndpoint(responses=[(0, "should not be reached", "")])
    out = build_run.fn({"command": "curl -O https://x/y.tgz"}, _ctx(project, cfg, gpu))
    assert "not recorded" in out
    assert project.twin().recipe() == []


def test_build_recipe_lists_steps(project, cfg):
    twin = _open_twin(project)
    twin.add_step("./configure", note="setup")
    twin.add_step("make install")
    out = build_recipe.fn({}, ToolContext(project=project, cfg=cfg))
    assert "2 step(s)" in out
    assert "./configure" in out and "# setup" in out
    assert "make install" in out


def test_build_recipe_empty(project, cfg):
    _open_twin(project)
    out = build_recipe.fn({}, ToolContext(project=project, cfg=cfg))
    assert "recipe empty" in out


def test_recipe_tools_register_while_open(project, cfg):
    yes = lambda *a, **k: True
    _open_twin(project)
    names = build_registry(project, cfg, yes).names()
    assert {"build_run", "build_recipe"} <= set(names)
