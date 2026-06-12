import pytest

from hermes.gpu.provision import (
    MODEL_MAX_LEN,
    VENV_DIR,
    VLLM_BIN,
    ProvisionError,
    launch,
    plan_serve,
    vllm_command,
)
from hermes.gpu.ssh import SSHEndpoint, SSHError, parse_ssh_string


def test_tier_h200(cfg):
    plan = plan_serve([("NVIDIA H200", 143771)], cfg)
    assert plan.tensor_parallel == 1
    assert plan.max_model_len == 196608
    assert plan.gpu_memory_utilization == 0.92


def test_tier_two_rtx6000pro(cfg):
    plan = plan_serve([("RTX 6000 Pro", 97887), ("RTX 6000 Pro", 97887)], cfg)
    assert plan.tensor_parallel == 2
    assert plan.max_model_len == 262144


def test_tier_single_48gb_is_tight(cfg):
    plan = plan_serve([("RTX 6000 Ada", 49140)], cfg)
    assert plan.max_model_len == 16384
    assert plan.gpu_memory_utilization == 0.95
    assert any("tight" in n for n in plan.notes)


def test_tier_96gb(cfg):
    plan = plan_serve([("RTX 6000 Pro", 97887)], cfg)
    assert plan.max_model_len == 65536


def test_too_small_rejected(cfg):
    with pytest.raises(ProvisionError):
        plan_serve([("RTX 4090", 24564)], cfg)


def test_ampere_note(cfg):
    plan = plan_serve([("NVIDIA A100-SXM4-80GB", 81920)], cfg)
    assert any("Ampere" in n for n in plan.notes)


def test_override_capped_at_model_max(cfg):
    cfg.set("max_model_len", 999999)
    plan = plan_serve([("NVIDIA H200", 143771)], cfg)
    assert plan.max_model_len == MODEL_MAX_LEN


def test_vllm_command(cfg):
    plan = plan_serve([("NVIDIA H200", 143771)], cfg)
    cmd = vllm_command(cfg, plan)
    assert "--tool-call-parser hermes" in cmd
    assert "--enable-auto-tool-choice" in cmd
    assert "--quantization fp8" in cmd
    assert "NousResearch/Hermes-4.3-36B" in cmd
    assert "--tensor-parallel-size 1" in cmd


def test_vllm_command_uses_venv_binary(cfg):
    plan = plan_serve([("NVIDIA H200", 143771)], cfg)
    # vLLM must be invoked from its isolated venv, not the system PATH.
    assert vllm_command(cfg, plan).startswith(f"{VLLM_BIN} serve ")


def test_launch_installs_into_isolated_venv(cfg):
    from conftest import FakeEndpoint

    ep = FakeEndpoint([
        (0, "", ""),  # running-check: not running
        (0, "", ""),  # install
        (0, "", ""),  # mkdir workspace
        (0, "", ""),  # launch
    ])
    launch(ep, cfg, plan_serve([("NVIDIA H200", 143771)], cfg))

    install = ep.calls[1]
    # Never install into the system Python — that's what hits the apt/RECORD
    # uninstall failure. Everything goes through the venv.
    assert f"python3 -m venv --system-site-packages {VENV_DIR}" in install
    assert f"{VENV_DIR}/bin/pip install" in install
    assert "pip install -q -U vllm" not in install  # no bare system install


def test_launch_skips_when_already_running(cfg):
    from conftest import FakeEndpoint

    ep = FakeEndpoint([(0, "RUNNING", "")])
    launch(ep, cfg, plan_serve([("NVIDIA H200", 143771)], cfg))
    assert len(ep.calls) == 1  # bailed before installing


def test_launch_raises_on_install_failure(cfg):
    from conftest import FakeEndpoint

    ep = FakeEndpoint([
        (0, "", ""),  # not running
        (1, "", "Cannot uninstall PyJWT 2.7.0, RECORD file not found."),
    ])
    with pytest.raises(ProvisionError, match="vLLM install failed"):
        launch(ep, cfg, plan_serve([("NVIDIA H200", 143771)], cfg))


def test_parse_ssh_strings():
    assert parse_ssh_string("ssh -p 12345 root@ssh4.vast.ai -L 8080:localhost:8080") == \
        ("root", "ssh4.vast.ai", 12345)
    assert parse_ssh_string("ssh://root@1.2.3.4:2222") == ("root", "1.2.3.4", 2222)
    assert parse_ssh_string("ssh root@host.example") == ("root", "host.example", 22)
    with pytest.raises(SSHError):
        parse_ssh_string("not an ssh string")


def test_tunnel_args_pure(home):
    ep = SSHEndpoint(host="h", port=2222)
    args = ep.tunnel_args(8000, 8000)
    assert "-N" in args
    assert "8000:127.0.0.1:8000" in args
    assert "ExitOnForwardFailure=yes" in args
    assert "ControlMaster=no" in args  # a tunnel must not ride the multiplexed master


def test_probe_net_isolation():
    from conftest import FakeEndpoint

    from hermes.gpu import probe_net_isolation

    assert probe_net_isolation(FakeEndpoint([(0, "NETOK", "")])) is True
    assert probe_net_isolation(FakeEndpoint([(1, "", "unshare: not permitted")])) is False


def test_endpoint_state_carries_net_isolation(home):
    from hermes.gpu import endpoint_from_state

    state = {"host": "h", "port": 22, "user": "root", "net_isolation": True}
    assert endpoint_from_state(state).net_isolation is True
    assert endpoint_from_state({"host": "h"}).net_isolation is False  # old gpu.json


def test_shell_path_quoting():
    from hermes.ssh import shell_path

    assert shell_path("~") == '"$HOME"'
    assert shell_path("~/work space") == '"$HOME"/\'work space\''
    assert shell_path("/plain/path") == "/plain/path"
    assert shell_path("/tmp/$(rm -rf /)") == "'/tmp/$(rm -rf /)'"
