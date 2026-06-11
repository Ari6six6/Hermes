"""Package assembly: turn project state + a new prompt into [system, user].

This is a pure function of inputs, with a hard per-section budget so the
prompt can never creep — the failure mode of the previous app. Budgets scale
down automatically when the served context window is small.
"""

from __future__ import annotations

import time
from pathlib import Path

from hermes.config import Config, read_persona
from hermes.project import Project

APPROX_CHARS_PER_TOKEN = 4
PROMPTS_DIR = Path(__file__).parent / "prompts"

# Fraction of the total package budget given to each section.
SECTION_SHARES = {
    "mission": 0.20,
    "history": 0.15,
    "summaries": 0.40,
    "notes": 0.15,
    "workspace": 0.10,
}


def render(template: str, variables: dict) -> str:
    out = template
    for key, value in variables.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def truncate_keep_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "[...truncated...]\n" + text[-max_chars:]


def truncate_keep_head(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...truncated...]"


def package_budget_chars(cfg: Config, context_window: int) -> int:
    budget_tokens = cfg.get("package_budget_tokens", 10000)
    if context_window:
        # Leave room for tool schemas, the system prompt, the in-run tool
        # loop, and the model's output.
        budget_tokens = min(budget_tokens, int(context_window * 0.30))
    return max(budget_tokens, 1500) * APPROX_CHARS_PER_TOKEN


def build_system_prompt(project: Project, env: dict) -> str:
    template = (PROMPTS_DIR / "system.md").read_text()
    ctx = env.get("context_window") or 0
    variables = {
        "project_name": project.name,
        "project_dir": str(project.root),
        "remote_workspace": env.get("remote_workspace", "~/hermes-workspace"),
        "gpu_status": env.get("gpu_status", "not attached"),
        "context_window": f"~{ctx} tokens" if ctx else "unknown (assume modest)",
        "date": time.strftime("%Y-%m-%d"),
    }
    system = render(template, variables)
    persona = read_persona().strip()
    if persona:
        system += "\n\n## Persona\n\n" + persona
    return system


def assemble(project: Project, prompt: str, env: dict, cfg: Config) -> list[dict]:
    """Build the two-message package. `env` carries gpu_status,
    remote_workspace and context_window (0 if unknown)."""
    total_chars = package_budget_chars(cfg, env.get("context_window") or 0)
    budget = {k: int(total_chars * share) for k, share in SECTION_SHARES.items()}

    mission = truncate_keep_head(project.read_mission().strip(), budget["mission"])

    history_entries = project.recent_prompts(cfg.get("history_max_prompts", 30))
    history_lines = [
        f"[{e.get('run', '?'):>4}] {e.get('text', '')}" for e in history_entries
    ]
    history = truncate_keep_tail("\n".join(history_lines), budget["history"])

    summary_entries = project.recent_summaries(cfg.get("summaries_max", 8))
    summary_blocks = [
        f"## Run {run_id:04d}\n{text}" for run_id, text in summary_entries
    ]
    summaries = truncate_keep_tail("\n\n".join(summary_blocks), budget["summaries"])

    notes = truncate_keep_tail(project.read_notes().strip(), budget["notes"])
    workspace = truncate_keep_head(project.workspace_listing(), budget["workspace"])

    user = "\n\n".join(
        [
            "# MISSION\n" + (mission or "(empty)"),
            "# PROMPT HISTORY (operator, oldest first)\n" + (history or "(none yet)"),
            "# RUN SUMMARIES (your own past runs)\n" + (summaries or "(none yet)"),
            "# NOTES (your own)\n" + (notes or "(none)"),
            "# WORKSPACE\n" + workspace,
            "# CURRENT REQUEST\n" + prompt.strip(),
        ]
    )

    return [
        {"role": "system", "content": build_system_prompt(project, env)},
        {"role": "user", "content": user},
    ]


def summary_nudge() -> str:
    return (PROMPTS_DIR / "summary.md").read_text().strip()
