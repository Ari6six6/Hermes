from tests.conftest import FakeEndpoint

from hermes.twin import deploy
from hermes.twin.model import Exchange


def _sealed_twin(project):
    twin = project.twin()
    twin.init(source="https://api.example.com")
    twin.add_exchange(Exchange(method="GET", path="/ping", status=200, response_body="pong"))
    twin.seal()
    return twin


def test_deploy_pushes_files_launches_and_confirms(project):
    twin = _sealed_twin(project)
    # pops in order: mkdir, push server.py, push exchanges.jsonl, lo-up, pkill,
    # launch, healthcheck. The last must report startup.
    responses = [(0, "", "")] * 6 + [(0, "twin up: http://127.0.0.1:8900 (1 exchanges)", "")]
    ep = FakeEndpoint(responses=responses)
    report = deploy.deploy(ep, twin, 8900)
    assert report["ok"]
    assert report["port"] == 8900
    assert any("server.py" in c for c in ep.calls)
    assert any("exchanges.jsonl" in c for c in ep.calls)
    assert any("nohup python3 server.py . 8900" in c for c in ep.calls)
    assert any("ip link set lo up" in c for c in ep.calls)
    # the model's exchanges actually streamed to the box
    assert b"pong" in ep.last_stdin


def test_deploy_reports_failure_when_no_startup(project):
    twin = _sealed_twin(project)
    ep = FakeEndpoint(responses=[(0, "", "")] * 7)  # healthcheck log empty
    report = deploy.deploy(ep, twin, 8900)
    assert not report["ok"]
    assert "error" in report


def test_deploy_fails_closed_on_push_error(project):
    twin = _sealed_twin(project)
    # mkdir ok, then first push (server.py) fails
    ep = FakeEndpoint(responses=[(0, "", ""), (1, "", "disk full")])
    report = deploy.deploy(ep, twin, 8900)
    assert not report["ok"]
    assert "server.py" in report["error"]
