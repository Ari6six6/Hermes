# Hermes

A package-per-prompt agent shell for Termux. It rents nothing and hides
nothing: you rent a GPU on [Vast.ai](https://vast.ai), Hermes provisions
[Hermes-4.3-36B](https://huggingface.co/NousResearch/Hermes-4.3-36B) on it via
vLLM, and gives you an agent that lives across two machines:

- **your phone** (Termux) — where the operator is, where every project lives,
  and the **only place with internet access**;
- **the GPU box** — the model's home and the agent's disposable compute
  sandbox. Network access from there is blocked by design.

## The stateful machine

Every prompt you send starts a **fresh chat**. The agent has no rolling
conversation — instead it receives a *package* assembled from project state:

```
# MISSION            <- mission.md, yours to edit
# PROMPT HISTORY     <- your prompts (never the model's old replies)
# RUN SUMMARIES      <- short summaries the agent wrote about its own runs
# NOTES              <- facts the agent chose to remember
# WORKSPACE          <- listing of its file area
# CURRENT REQUEST    <- what you just typed
```

Every section has a hard budget that scales with the served context window, so
the prompt can never creep. Within a run the agent loops freely over native
tool calls (vLLM's `--tool-call-parser hermes` — the format the model was
trained on) until it answers and files its `finish_run` summary.

## Install (Termux)

```sh
pkg install python openssh git
git clone <this repo> && cd Hermes
pip install -e .
hermes
```

## Workflow

```text
hermes
> config set vast_api_key <your key>
> project new myproject
> mission edit                  # tell the agent what this project is
# rent a GPU in the Vast.ai console, then:
> gpu attach                    # auto-discovers via the API
                                # (or: gpu attach ssh -p PORT root@HOST)
> gpu serve                     # detects GPUs, picks a tier, launches vLLM,
                                # tunnels port 8000, waits until ready
> run fix the parser in workspace/scraper.py and test it on the box
> gpu down                      # stop vLLM + optionally stop the instance
```

`config set backend mock` lets you exercise the whole loop with no GPU.

## GPU tiers

`gpu serve` reads `nvidia-smi` and adapts — quantization is FP8 everywhere,
context length scales with total VRAM:

| total VRAM | context | example boxes |
|---|---|---|
| < 44 GB | refused | weights alone need ~37 GB |
| 44–56 GB | 16k (tight) | 1× 48GB card, 2× 24GB |
| 56–96 GB | 32–64k | A100 80GB, RTX 6000 Pro 96GB |
| 96–168 GB | 128–192k | H200 140GB |
| 168+ GB | 256k | 2× RTX 6000 Pro |

Override with `config set max_model_len <n>`. Architecture notes: Hopper, Ada
and Blackwell run FP8 natively; Ampere (A100/A40/3090) falls back to
weight-only FP8 (Marlin) — works, somewhat slower. Pre-Ampere is not
supported. The agent is told its context size and the package budgets shrink
automatically on small tiers.

## What the agent can do

| tool | runs on | gate |
|---|---|---|
| read/write/edit/list files | phone, project dir | free inside the project |
| `local_shell` | phone | **always asks you y/n** |
| `remote_shell`, `remote_read/write` | GPU box | free — it's the sandbox; network commands blocked |
| `http_request`, `web_search` | **phone** | GET free; POST etc. ask you |
| `write_note`, `finish_run` | phone | free |
| `list_toolbox` / `equip_tool` | — | library tools load on demand |
| `forge_tool` | phone | you review the source before it loads |

The toolbox ships ready-made tools (`download_file`, `transfer`, `todo`,
`json_query`) whose schemas don't bloat the prompt until equipped. Forged
tools are plain Python files in `<project>/tools/`, loaded only after you
approve the exact source (re-approval on any change).

**The hard rule:** anything internet happens on the phone. The GPU box gets
files pushed to it (`transfer`), never a network connection out.

## Layout

```
~/.hermes/            config.json · persona.md · gpu.json · logs
~/hermes-projects/<name>/
  mission.md          your standing orders (edit anytime)
  notes.md            the agent's memory notes
  history.jsonl       your prompts
  workspace/          the agent's file area
  tools/              forged tools + approval manifest
  runs/NNNN/          transcript.jsonl + summary.md per run
```

Everything is plain text — `nano mission.md` works fine. `persona.md` in
`~/.hermes/` is appended to the (deliberately lean) system prompt; keep it
short, the philosophy lives there now.

## Development

```sh
pip install -e ".[dev]"
python -m pytest tests/
```

Tests cover package assembly and budgets, path-escape defenses, the tool
registry (including forging/approval), the full agent loop against a scripted
mock backend, and the GPU tier planner.
