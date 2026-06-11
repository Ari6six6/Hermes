"""App configuration: ~/.hermes/config.json with sane defaults.

HERMES_HOME env var overrides the home dir (used by tests).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "backend": "openai",  # "openai" (vLLM endpoint) or "mock"
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key": "hermes",  # vLLM doesn't check it, but the client wants one
    "model": "NousResearch/Hermes-4.3-36B",
    "quantization": "fp8",  # on-the-fly FP8; weight-only fallback on Ampere
    "vast_api_key": "",
    "projects_dir": str(Path.home() / "hermes-projects"),
    "current_project": "",
    "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
    "max_turns": 20,
    "max_tool_result_chars": 8000,
    "package_budget_tokens": 10000,  # scaled down automatically on small contexts
    "history_max_prompts": 30,
    "summaries_max": 8,
    "allow_gpu_network": False,  # hard rule: internet goes through the phone
    "max_model_len": 0,  # 0 = pick automatically from detected VRAM
    "gpu_port": 8000,
    "local_port": 8000,
    "max_completion_tokens": 8192,
    "extra_vllm_args": [],
}


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def config_path() -> Path:
    return hermes_home() / "config.json"


def persona_path() -> Path:
    return hermes_home() / "persona.md"


DEFAULT_PERSONA = """\
You are Hermes: sharp, direct, loyal. You think hard before you act, you keep
your operator informed in plain language, and you finish what you start.
"""


class Config:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        path = config_path()
        if path.exists():
            try:
                stored = json.loads(path.read_text())
                _deep_update(data, stored)
            except (json.JSONDecodeError, OSError) as e:
                print(f"warning: could not read {path}: {e} — using defaults")
        return cls(data)

    def save(self) -> None:
        home = hermes_home()
        home.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(self.data, indent=2) + "\n")
        os.chmod(config_path(), 0o600)  # holds vast_api_key
        if not persona_path().exists():
            persona_path().write_text(DEFAULT_PERSONA)

    def get(self, key: str, default=None):
        """Dotted-key get: cfg.get("sampling.temperature")."""
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value) -> None:
        """Dotted-key set with naive type coercion from strings."""
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = _coerce(value)

    def __getitem__(self, key: str):
        return self.data[key]


def _deep_update(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def _coerce(value):
    if not isinstance(value, str):
        return value
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def read_persona(max_chars: int = 2000) -> str:
    path = persona_path()
    if not path.exists():
        return DEFAULT_PERSONA
    text = path.read_text()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[persona truncated]"
    return text
