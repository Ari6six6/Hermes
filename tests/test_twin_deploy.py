from tests.conftest import FakeEndpoint

from hermes.twin import deploy
from hermes.twin.model import Exchange


def _sealed_twin(project):
    twin = project.twin()
    twin.init(source="https://api.example.com")
    twin.add_exchange(Exchange(method="GET", path="/ping", status=200, response_body="pong"))
    twin.seal()
    return twin


def _twin_with_recipe(project):
    twin = project.twin()
    twin.init(source="https://api.example.com")
    twin.add_step("apt-get install -y nginx", "install nginx")
    twin.add_step("nohup nginx -g 'daemon off;' &", "run on $TWIN_PORT")
    twin.add_exchange(Exchange(method="GET", path="/", status=200, response_body="hi"))
    twin.seal()
    return twin


def test_deploy_from_blueprint_replays_recipe(project):
    twin = _twin_with_recipe(project)
    # order: mkdir, lo-up, healthcheck-pre(down), step1, step2, sleep, healthcheck-post(up)
    ep = FakeEndpoint(responses=[
        (0, "", ""), (0, "", ""), (1, "", ""),         # pre-check: not up yet
        (0, "", ""), (0, "", ""), (0, "", ""),         # 2 steps + sleep
        (0, "", ""),                                   # post-check: listening
    ])
    report = deploy.deploy(ep, twin, 8900)
    assert report["ok"] and report["source"] == "blueprint"
    assert any("export TWIN_PORT=8900" in c for c in ep.calls)
    assert any("nginx" in c for c in ep.calls)


def test_deploy_from_blueprint_short_circuits_when_already_up(project):
    twin = _twin_with_recipe(project)
    # mkdir, lo-up, healthcheck-pre(up) -> returns without replaying
    ep = FakeEndpoint(responses=[(0, "", ""), (0, "", ""), (0, "", "")])
    report = deploy.deploy(ep, twin, 8900)
    assert report["ok"] and "already up" in report["log"]
    assert not any("export TWIN_PORT" in c for c in ep.calls)  # no replay


def test_deploy_from_blueprint_fails_on_bad_step(project):
    twin = _twin_with_recipe(project)
    # mkdir, lo-up, pre-check(down), step1 fails
    ep = FakeEndpoint(responses=[(0, "", ""), (0, "", ""), (1, "", ""),
                                 (1, "", "package not found")])
    report = deploy.deploy(ep, twin, 8900)
    assert not report["ok"]
    assert "recipe step 1" in report["error"]
    # the transcript is written to the phone for debugging
    assert deploy.serve_log_path(twin).exists()
    log = deploy.serve_log_path(twin).read_text()
    assert "package not found" in log and "apt-get install -y nginx" in log


def test_deploy_clean_wipes_and_frees_port_then_replays(project):
    twin = _twin_with_recipe(project)
    # clean path skips the pre-check: fuser, rm, mkdir, lo-up, step1, step2, sleep, post(up)
    ep = FakeEndpoint(responses=[(0, "", "")] * 7 + [(0, "", "")])
    report = deploy.deploy(ep, twin, 8900, clean=True)
    assert report["ok"] and report["source"] == "blueprint"
    assert any("rm -rf" in c for c in ep.calls)
    assert any("fuser -k 8900/tcp" in c for c in ep.calls)
    assert any("export TWIN_PORT=8900" in c for c in ep.calls)


def test_deploy_writes_serve_log_on_success(project):
    twin = _twin_with_recipe(project)
    ep = FakeEndpoint(responses=[(0, "", ""), (0, "", ""), (1, "", ""),
                                 (0, "", ""), (0, "", ""), (0, "", ""), (0, "", "")])
    report = deploy.deploy(ep, twin, 8900)
    assert report["ok"]
    log = deploy.serve_log_path(twin).read_text()
    assert "health check :8900 -> listening" in log


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


def test_deploy_replay_failure_points_at_serve_log(project):
    # The replay (no-recipe) path must, like the blueprint path, write a serve
    # log and hand back log_path so the CLI can point the operator at it.
    twin = _sealed_twin(project)        # no recipe -> replay responder
    ep = FakeEndpoint(responses=[(0, "", "")] * 7)  # twin.log empty -> no startup
    report = deploy.deploy(ep, twin, 8900)
    assert not report["ok"] and report["source"] == "replay"
    assert report.get("log_path")       # cli.py keys off this to print the path
    assert deploy.serve_log_path(twin).exists()
    assert "launch :8900" in deploy.serve_log_path(twin).read_text()


def test_deploy_replay_success_reports_log_path(project):
    twin = _sealed_twin(project)
    responses = [(0, "", "")] * 6 + [(0, "twin up: http://127.0.0.1:8900 (1 exchanges)", "")]
    ep = FakeEndpoint(responses=responses)
    report = deploy.deploy(ep, twin, 8900)
    assert report["ok"] and report["source"] == "replay"
    assert report.get("log_path")
    assert deploy.serve_log_path(twin).exists()


def test_stop_anchors_exact_port_no_prefix_collision():
    # pkill -f matches an unanchored regex; stopping :890 must not also match the
    # argv of a twin on :8900-:8909. The pattern escapes the dots and anchors
    # the port at end-of-line.
    ep = FakeEndpoint()
    deploy.stop(ep, 890)
    cmd = ep.calls[-1]
    assert r"server\.py \. 890$" in cmd          # escaped + anchored
    assert "'server.py . 890'" not in cmd        # not the old loose pattern
