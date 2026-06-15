from tests.conftest import FakeEndpoint

from hermes import sandbox
from hermes.sandbox import provision


def test_state_round_trip(home):
    sandbox.save_sandbox_state({"host": "vps.example", "port": 2222, "user": "root"})
    state = sandbox.load_sandbox_state()
    assert state["host"] == "vps.example" and state["port"] == 2222


def test_state_file_is_private(home):
    sandbox.save_sandbox_state({"host": "vps.example"})
    mode = sandbox.sandbox_state_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_endpoint_from_state_none_without_host():
    assert sandbox.endpoint_from_state({}) is None


def test_endpoint_from_state_uses_sandbox_workspace():
    ep = sandbox.endpoint_from_state({"host": "vps.example", "port": 2222})
    assert ep.host == "vps.example" and ep.port == 2222
    assert ep.remote_workspace == sandbox.SANDBOX_WORKSPACE


def test_probe_container_runtime_detects_docker():
    ep = FakeEndpoint(responses=[(0, "docker\n", "")])
    assert sandbox.probe_container_runtime(ep) == "docker"


def test_probe_container_runtime_detects_podman():
    ep = FakeEndpoint(responses=[(0, "podman\n", "")])
    assert sandbox.probe_container_runtime(ep) == "podman"


def test_probe_container_runtime_none():
    ep = FakeEndpoint(responses=[(0, "none\n", "")])
    assert sandbox.probe_container_runtime(ep) == ""


def test_probe_kvm_true_false():
    assert sandbox.probe_kvm(FakeEndpoint(responses=[(0, "KVM\n", "")])) is True
    assert sandbox.probe_kvm(FakeEndpoint(responses=[(0, "NOKVM\n", "")])) is False


def test_capabilities_bundles_probes():
    ep = FakeEndpoint(responses=[(0, "docker\n", ""), (0, "NOKVM\n", "")])
    caps = sandbox.capabilities(ep)
    assert caps == {"runtime": "docker", "kvm": False}


def test_ensure_runtime_returns_existing():
    ep = FakeEndpoint(responses=[(0, "docker\n", "")])
    assert provision.ensure_runtime(ep) == "docker"
    # only the probe ran — no install attempted
    assert not any("apt-get" in c for c in ep.calls)


def test_ensure_runtime_installs_when_missing():
    # probe(none), install(ok), re-probe(docker)
    ep = FakeEndpoint(responses=[(0, "none\n", ""), (0, "", ""), (0, "docker\n", "")])
    assert provision.ensure_runtime(ep) == "docker"
    assert any("apt-get install" in c and "docker.io" in c for c in ep.calls)


def test_ensure_runtime_raises_when_install_fails():
    ep = FakeEndpoint(responses=[(0, "none\n", ""), (1, "", "no space left")])
    try:
        provision.ensure_runtime(ep)
        assert False, "expected SandboxError"
    except provision.SandboxError as e:
        assert "no space left" in str(e)
