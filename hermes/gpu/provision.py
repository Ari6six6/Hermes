"""Provision vLLM on whatever box was rented: detect the GPUs, pick a tier
(quantization is FP8 everywhere; context length scales with VRAM), launch,
tunnel, and poll until the OpenAI endpoint answers.

Architecture notes:
- Hopper/Ada/Blackwell (H100/H200, L40S, RTX 4090/6000 Ada, RTX 6000 Pro):
  native FP8 — full speed.
- Ampere (A100, A40, RTX 3090...): vLLM falls back to weight-only FP8
  (Marlin kernels) — works, modest throughput cost.
- Pre-Ampere: not supported.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

MODEL_MAX_LEN = 524288  # Hermes 4.3 supports up to 512K
MIN_TOTAL_GB = 44  # FP8 36B weights ~37GB + runtime overhead

# vLLM gets its own venv so pip never fights the box's apt-managed packages.
# Installing into the system Python fails with "Cannot uninstall <pkg>, RECORD
# file not found. Hint: The package was installed by debian." — apt packages
# carry no RECORD for pip to remove, so any dependency vLLM wants to upgrade
# (e.g. PyJWT) aborts the whole install. --system-site-packages keeps the box's
# preinstalled CUDA/torch visible (no multi-GB re-download); vLLM's own
# dependency upgrades land inside the venv, shadowing the system copies without
# touching them.
VENV_DIR = "~/.hermes-venv"
VLLM_BIN = f"{VENV_DIR}/bin/vllm"

# (total VRAM GB threshold, max_model_len) — first row whose threshold fits.
CONTEXT_TIERS = [
    (56, 16384),
    (72, 32768),
    (96, 65536),
    (120, 131072),
    (168, 196608),
]
CONTEXT_BEYOND = 262144


class ProvisionError(Exception):
    pass


@dataclass
class ServePlan:
    tensor_parallel: int
    max_model_len: int
    gpu_memory_utilization: float
    total_vram_gb: int
    gpu_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def plan_serve(gpus: list[tuple[str, int]], cfg) -> ServePlan:
    """gpus: [(name, vram_mb), ...] from nvidia-smi."""
    if not gpus:
        raise ProvisionError("no GPUs detected on the box (nvidia-smi empty)")
    total_gb = sum(mb for _, mb in gpus) // 1024
    if total_gb < MIN_TOTAL_GB:
        raise ProvisionError(
            f"only {total_gb}GB total VRAM — Hermes-4.3-36B in FP8 needs "
            f"~{MIN_TOTAL_GB}GB+. Rent a bigger box (48GB is the floor)."
        )
    override = cfg.get("max_model_len", 0)
    if override:
        max_len = min(int(override), MODEL_MAX_LEN)
    else:
        max_len = CONTEXT_BEYOND
        for threshold, length in CONTEXT_TIERS:
            if total_gb < threshold:
                max_len = length
                break
    notes = []
    if total_gb < 56:
        notes.append(
            "tight fit: small context tier — the agent's package budget "
            "shrinks automatically to keep loops healthy."
        )
    names = [name for name, _ in gpus]
    if any("A100" in n or "A40" in n or "3090" in n or "A6000" in n for n in names):
        notes.append("Ampere GPU: FP8 runs as weight-only (Marlin) — works, a bit slower.")
    return ServePlan(
        tensor_parallel=len(gpus),
        max_model_len=max_len,
        gpu_memory_utilization=0.95 if total_gb < 72 else 0.92,
        total_vram_gb=total_gb,
        gpu_names=names,
        notes=notes,
    )


def detect_gpus(endpoint) -> list[tuple[str, int]]:
    rc, out, err = endpoint.run(
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits",
        timeout=30,
    )
    if rc != 0:
        raise ProvisionError(f"nvidia-smi failed: {err.strip() or out.strip()}")
    gpus = []
    for line in out.strip().splitlines():
        try:
            name, mem = line.rsplit(",", 1)
            gpus.append((name.strip(), int(float(mem.strip()))))
        except ValueError:
            continue
    return gpus


def vllm_command(cfg, plan: ServePlan) -> str:
    parts = [
        VLLM_BIN, "serve", cfg.get("model"),
        f"--quantization {cfg.get('quantization', 'fp8')}",
        f"--tensor-parallel-size {plan.tensor_parallel}",
        f"--max-model-len {plan.max_model_len}",
        f"--gpu-memory-utilization {plan.gpu_memory_utilization}",
        "--enable-auto-tool-choice --tool-call-parser hermes",
        f"--port {cfg.get('gpu_port', 8000)}",
    ]
    parts += [str(a) for a in cfg.get("extra_vllm_args", [])]
    return " ".join(parts)


def launch(endpoint, cfg, plan: ServePlan) -> None:
    rc, out, _ = endpoint.run("cat ~/vllm.pid 2>/dev/null && kill -0 $(cat ~/vllm.pid) 2>/dev/null && echo RUNNING")
    if "RUNNING" in out:
        print("vLLM already running on the box (kill it first with `gpu down` to relaunch).")
        return
    print("ensuring vLLM is installed (first time can take a few minutes)...")
    install = (
        f"test -x {VLLM_BIN} && exit 0; "
        # python3-venv is missing on some base images — install it on demand.
        f"python3 -m venv --system-site-packages {VENV_DIR} 2>/dev/null || "
        f"{{ apt-get update -qq && apt-get install -y -qq python3-venv && "
        f"python3 -m venv --system-site-packages {VENV_DIR}; }} && "
        f"{VENV_DIR}/bin/pip install -q -U pip vllm hf_transfer"
    )
    rc, _, err = endpoint.run(install, timeout=1800)
    if rc != 0:
        raise ProvisionError(f"vLLM install failed: {err.strip()[-800:]}")
    endpoint.run(f"mkdir -p {endpoint.remote_workspace}")
    cmd = vllm_command(cfg, plan)
    print(f"launching: {cmd}")
    rc, _, err = endpoint.run(
        "HF_HUB_ENABLE_HF_TRANSFER=1 nohup " + cmd + " > ~/vllm.log 2>&1 & echo $! > ~/vllm.pid"
    )
    if rc != 0:
        raise ProvisionError(f"launch failed: {err.strip()[-800:]}")


def wait_ready(endpoint, cfg, deadline_s: int = 1800) -> bool:
    """Poll the tunneled endpoint; stream fresh vllm.log lines meanwhile."""
    url = f"http://127.0.0.1:{cfg.get('local_port', 8000)}/v1/models"
    start = time.time()
    seen_bytes = 0
    while time.time() - start < deadline_s:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        rc, out, _ = endpoint.run(
            f"tail -c +{seen_bytes + 1} ~/vllm.log 2>/dev/null", timeout=20
        )
        if rc == 0 and out:
            seen_bytes += len(out.encode())
            for line in out.splitlines()[-30:]:
                print("  | " + line[:160])
        time.sleep(5)
    return False
